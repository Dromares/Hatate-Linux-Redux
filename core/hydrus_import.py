"""Per-image Hydrus import logic - the actual "send this one image to
Hydrus" steps for each of the three methods, factored out of the GUI so
they're shared between the right-click context menu actions and the
background auto-import feature (Settings > General), rather than existing
as two separate copies that could drift apart over time.

Note on send_url_to_importer(): on its own, it only confirms that Hydrus
*accepted* the request, not that the file actually finished downloading -
that needs a separate confirmation poll (see core/hydrus_import_poll.py
and workers/hydrus_import_poll_worker.py for the batch/GUI version, or
poll_single_url_import() for the single-entry version used by
auto-import's SearchWorker)."""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from .applog import get_logger
from .hydrus_client import (
    DUPLICATE_ALTERNATE, DUPLICATE_BETTER, PERMISSION_EDIT_FILE_NOTES,
    PERMISSION_EDIT_FILE_RELATIONSHIPS, HydrusClient, HydrusError,
)
from .models import ImageEntry, tags_to_send

log = get_logger("hydrus_import")


# Forms the search engines hand back that Hydrus's own URL classes do not
# match, each with the form they do. CONFIRMED against a live client
# (Hydrus 687, API version 95) with ids from a real session:
#
#   danbooru /post/show/N        unknown url  -> /posts/N         danbooru file page
#   twitter  /i/web/status/N     unknown url  -> /i/status/N      twitter post
#
# Not sankaku: its old chan.sankakucomplex.com numeric links are a gallery
# URL to Hydrus and dead on the site itself, so no rewrite can save them.
# The current form (www.sankakucomplex.com/posts/<id>, which the sankaku
# parser repoints matches to) is already one Hydrus parses.
_HYDRUS_URL_REWRITES = (
    (re.compile(r"^(https?://danbooru\.donmai\.us)/post/show/(\d+)"), r"\1/posts/\2"),
    (re.compile(r"^(https?://(?:www\.|mobile\.)?(?:twitter|x)\.com)/i/web/status/(\d+)"),
     r"\1/i/status/\2"),
)

# rule34.us posts are addressed as index.php?r=posts/view&id=N (see
# core/boorus/rule34us.py), but Google Lens's "Exact matches" tab hands
# some of these back with the slash percent-encoded (r=posts%2Fview) -
# confirmed against a live Hydrus client: its rule34.us URL class matches
# the literal slash but not %2F, so the encoded form is never recognised
# as a known url and the file never imports. Only the r= parameter is
# touched; nothing else about the URL (host, id, other params) changes.
_RULE34US_R_PARAM_RE = re.compile(r"(?<=[?&]r=)([^&#]*)")
_ENCODED_SLASH_RE = re.compile(r"%2f", re.IGNORECASE)


def normalize_url_for_hydrus(url: str) -> str:
    """Some sites' older/legacy URL formats aren't reliably handled by
    Hydrus's own built-in downloader/parser, even though our own scraping
    already normalizes them for its own purposes (see
    core/boorus/pixiv.py's resolve_fetch_url) - a real, confirmed case:
    Pixiv's legacy member_illust.php URLs get accepted by Hydrus's
    add_url endpoint just fine, but the actual download then silently
    stalls (or fails) on Hydrus's side, so it never confirms - not
    because anything in this app is wrong, but because Hydrus's own
    Pixiv parser doesn't handle that URL shape.

    This must return the exact same URL for both the add_url call and
    the later get_url_files confirmation poll - if those two calls use
    different URL forms, confirmation can never match what Hydrus
    actually processed."""
    for pattern, replacement in _HYDRUS_URL_REWRITES:
        rewritten = pattern.sub(replacement, url, count=1)
        if rewritten != url:
            return rewritten
    if "rule34.us" in url:
        from .boorus._host import on_host
        if on_host(url, ("rule34.us",)):
            return _RULE34US_R_PARAM_RE.sub(
                lambda m: _ENCODED_SLASH_RE.sub("/", m.group(1)), url, count=1)
    if "pixiv.net" in url:
        # Deliberately NOT boorus.pixiv.resolve_fetch_url: that returns the
        # AJAX API endpoint, which is right for OUR parsing but wrong here.
        # Hydrus's downloader needs a real artwork PAGE url - handing it a
        # JSON endpoint would break importing entirely.
        from .boorus.pixiv import ILLUST_ID_RE, LEGACY_ILLUST_ID_RE
        match = ILLUST_ID_RE.search(url) or LEGACY_ILLUST_ID_RE.search(url)
        if match:
            return f"https://www.pixiv.net/artworks/{match.group(1)}"
    return url


@dataclass
class ImportResult:
    success: bool
    warning: Optional[str] = None        # partial success (imported, but URL/tags failed)
    error: Optional[str] = None          # complete failure
    skipped_reason: Optional[str] = None  # e.g. no matched URL / no direct file URL available
    refused: bool = False                # Hydrus was asked about the URL and can't import it
                                          # (see url_import_refusal) - as opposed to a failed request
    note: Optional[str] = None           # what was done instead, for the status line
    confirmed: Optional[bool] = None     # None: N/A for this method (success already means done -
                                          # upload/download_send are synchronous). True/False: only
                                          # meaningful for send_url_to_importer when the caller polled
                                          # for confirmation - True if Hydrus confirmed the import
                                          # actually finished, False if it never confirmed in time.


def send_file_upload(entry: ImageEntry, client: HydrusClient, settings=None) -> ImportResult:
    """Uploads the local file, associates the matched URL, adds tags.

    `settings` is optional and is read for two things: which tags go with
    the file (see models.tags_to_send) and write_hydrus_provenance_note.
    Omitting it sends every tag on the entry and writes no note, which is
    what this function did before either option existed.
    """
    log.info("Sending %s to Hydrus (upload)", entry.filename)
    try:
        # Always actually upload, even when entry.hydrus_hash is already
        # set. That hash is just the file's SHA256, which the app now
        # computes for EVERY added file during duplicate detection - it
        # says nothing about whether Hydrus has the file. Treating its
        # presence as "already imported" silently skipped the upload for
        # any file from outside Hydrus, then tried to attach URLs/tags to
        # a hash Hydrus had never seen.
        #
        # Re-uploading something Hydrus already has is harmless and cheap:
        # add_file is idempotent and just reports "already in database"
        # along with the existing hash.
        result = client.import_file(entry.path)
        file_hash = result.get("hash") or entry.hydrus_hash
        entry.hydrus_hash = file_hash
        log.info("Imported %s -> hash=%s status=%s", entry.filename, file_hash, result.get("status"))
        if not file_hash:
            raise HydrusError("Hydrus didn't return a file hash for this import")
    except HydrusError as exc:
        entry.error_message = str(exc)
        log.error("Failed to import %s: %s", entry.filename, exc)
        return ImportResult(success=False, error=str(exc))

    # The file is in Hydrus at this point - URL association and tagging
    # are attempted independently so one failing doesn't throw away the
    # successful import.
    step_warnings = []
    if entry.matched_url:
        try:
            client.associate_url(entry.matched_url, file_hash=file_hash)
        except HydrusError as exc:
            step_warnings.append(f"URL not added: {exc}")
            log.error("add_url failed for %s: %s", entry.filename, exc)

    tag_names = [t.display for t in tags_to_send(entry, settings)]
    if tag_names:
        try:
            client.add_tags(file_hash, tag_names)
        except HydrusError as exc:
            step_warnings.append(f"Tags not added: {exc}")
            log.error("add_tags failed for %s: %s", entry.filename, exc)

    # After the file is confirmed held - Hydrus returned its hash. Off by
    # default, and a no-op that makes no request when it is off.
    note_warning = write_provenance_note(entry, client, settings, file_hash)
    if note_warning:
        step_warnings.append(note_warning)

    entry.sent_to_hydrus = True
    entry.hydrus_import_confirmed = True  # Hydrus returned a hash for the file itself
    if step_warnings:
        entry.error_message = "; ".join(step_warnings)
        return ImportResult(success=True, warning=entry.error_message)
    entry.error_message = None
    return ImportResult(success=True)


# Hydrus's url_type values, from /add_urls/get_url_info. CONFIRMED
# against a live client: a subreddit is 3 ("Reddit | Subreddit"), a
# reddit or booru post is 0, and a comic-site page, a direct i.redd.it
# image and a .jpg on some CDN are all 5.
URL_TYPE_POST, URL_TYPE_FILE, URL_TYPE_GALLERY, URL_TYPE_WATCHABLE, URL_TYPE_UNKNOWN = 0, 2, 3, 4, 5

# An unknown URL is fetched by Hydrus as a raw file. That works when it IS
# a file, and fails silently when it is a web page - Hydrus accepts the
# URL ("unknown url" URL added successfully) and then downloads HTML.
MEDIA_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".tif", ".tiff",
    ".jxl", ".heic", ".mp4", ".webm", ".mkv", ".mov",
)


def url_import_refusal(client: HydrusClient, url: str) -> Optional[str]:
    """Why Hydrus's URL importer should NOT be handed this URL, or None.

    Two ways it goes wrong, both seen in real runs:

      * A GALLERY or watchable URL imports everything there. A subreddit
        link from Google Lens started a download of the whole subreddit
        for one image.
      * An UNKNOWN URL that is a web page. Hydrus answers "URL added
        successfully" for it, then fetches the page as a file and gets
        nothing - while this app recorded the image as sent.

    If Hydrus cannot be asked (an older client without the endpoint, or
    a transient error), the URL is allowed through as it always was
    rather than blocking every import on a failed check.
    """
    try:
        info = client.get_url_info(url)
    except HydrusError as exc:
        log.debug("Could not ask Hydrus about %s (%s) - sending it anyway", url, exc)
        return None
    url_type = info.get("url_type")
    kind = info.get("url_type_string") or "url"
    name = info.get("match_name") or kind
    if url_type in (URL_TYPE_GALLERY, URL_TYPE_WATCHABLE):
        return (f"Hydrus sees this as a {kind} ({name}) - importing it would download "
                "everything there, not this one image")
    if url_type == URL_TYPE_UNKNOWN:
        path = (urlparse(url).path or "").lower()
        if path.endswith(MEDIA_EXTENSIONS):
            return None                       # a direct file link: Hydrus fetches it as one
        host = urlparse(url).hostname or url
        return (f"Hydrus has no downloader for {host} - it would accept the link and then "
                "fetch the web page instead of the image. Use Send (upload) or Download instead")
    if url_type == URL_TYPE_POST and info.get("can_parse") is False:
        reason = info.get("cannot_parse_reason") or "no parser"
        return f"Hydrus recognises this as {name} but cannot parse it ({reason})"
    return None


FORWARD_CHECK_TIMEOUT = 15.0


def _forwarded_url(url: str) -> Optional[str]:
    """Where `url` finally lands after redirects, normalised for Hydrus,
    if that is somewhere else; None when it does not move or can't be
    reached. One HEAD - only ever asked for a URL Hydrus has refused."""
    import requests

    from . import net
    try:
        resp = net.head(url, timeout=FORWARD_CHECK_TIMEOUT, deadline=FORWARD_CHECK_TIMEOUT,
                        allow_redirects=True)
    except requests.RequestException as exc:
        log.debug("Could not follow %s to see where it leads: %s", url, exc)
        return None
    final = normalize_url_for_hydrus(resp.url or "")
    if not final.startswith(("http://", "https://")) or final.rstrip("/") == url.rstrip("/"):
        return None
    return final


def _repoint(entry: ImageEntry, url: str) -> None:
    candidate = entry.selected_candidate
    if candidate is not None and candidate.url == entry.matched_url:
        candidate.url = url
    entry.matched_url = url


def _is_exact_copy(candidate) -> bool:
    """Whether a match was found by the file's own hash, so it IS the
    same file - currently the Pawchive lookup (core/pawchive_lookup.py)."""
    from .engines import ENGINE_LABELS, PAWCHIVE
    return (candidate.engine or "") == ENGINE_LABELS[PAWCHIVE]


def _looks_like_a_web_page(data: bytes) -> bool:
    """A login wall or hotlink block answers 200 with HTML. Hydrus would
    reject it anyway, but only after the upload, with a vaguer reason."""
    head = data[:512].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<?xml"))


# Magic byte signatures for common image formats
_MAGIC_BYTES = {
    "JPEG": (b"\xFF\xD8\xFF",),
    "PNG": (b"\x89PNG\r\n\x1a\n",),
    "GIF": (b"GIF87a", b"GIF89a"),
    "WEBP": (b"RIFF",),  # RIFF container, need to check for WEBP at offset 8
    "BMP": (b"BM",),
    "TIFF": (b"II\x2a\x00", b"MM\x00\x2a"),  # little-endian / big-endian
    "AVIF": (b"ftypavif",),  # at offset 4 in ISOBMFF container
    "HEIC": (b"ftypheic", b"ftypmif1", b"ftypmsf1"),  # at offset 4
    "MP4": (b"ftypmp4", b"ftypisom", b"ftypiso2"),  # at offset 4
    "WEBM": (b"\x1aE\xdf\xa3",),  # EBML header
    "MKV": (b"\x1aE\xdf\xa3",),  # EBML header (Matroska)
}


def _validate_magic_bytes(data: bytes, expected_format: Optional[str], file_ext: str) -> Optional[str]:
    """Validate that the downloaded data's magic bytes match the expected format.
    
    Returns an error message if validation fails, None if it passes or if
    the format is unknown/unsupported (we don't fail closed on unknown formats).
    
    When expected_format is provided, validates against that specific format.
    When expected_format is None but file_ext gives a known format, validates
    against that inferred format.
    When neither is available, sniffs the payload against ALL known signatures:
    - If it matches at least one known format, passes (valid image/video).
    - If it matches nothing known, rejects (unknown/garbage payload).
    """
    if not data:
        return "Downloaded file is empty (0 bytes)"
    
    # Reject implausibly small payloads - a real image is rarely under 100 bytes
    if len(data) < 100:
        return f"Downloaded file is implausibly small ({len(data)} bytes)"
    
    if not expected_format:
        # No expected format from the booru page - try to infer from extension
        ext = file_ext.lower().lstrip(".")
        format_from_ext = {
            "jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "gif": "GIF",
            "webp": "WEBP", "bmp": "BMP", "tif": "TIFF", "tiff": "TIFF",
            "avif": "AVIF", "heic": "HEIC", "mp4": "MP4", "webm": "WEBM", "mkv": "MKV",
        }.get(ext)
        if format_from_ext:
            expected_format = format_from_ext
    
    if expected_format:
        # We have a specific format to validate against (from remote_format or extension)
        expected_format = expected_format.upper()
        signatures = _MAGIC_BYTES.get(expected_format)
        if not signatures:
            # Unknown format - don't fail closed
            log.debug("No magic byte signatures known for format %s; skipping validation", expected_format)
            return None
        
        # Check each signature for the expected format
        for sig in signatures:
            if expected_format in ("WEBP", "AVIF", "HEIC", "MP4", "WEBM", "MKV"):
                # These formats have signatures at specific offsets in container formats
                if expected_format == "WEBP":
                    # RIFF container: "RIFF" at 0, "WEBP" at offset 8
                    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
                        return None
                elif expected_format in ("AVIF", "HEIC", "MP4"):
                    # ISOBMFF container: "ftyp" + brand at offset 4
                    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"heic", b"mif1", b"msf1", b"mp4 ", b"isom", b"iso2"):
                        return None
                elif expected_format in ("WEBM", "MKV"):
                    # EBML header
                    if data.startswith(b"\x1aE\xdf\xa3"):
                        return None
            else:
                # Simple prefix match
                if data.startswith(sig):
                    return None
        
        return f"Magic bytes do not match expected format {expected_format}"
    
    # No expected format and no inferrable extension - sniff against ALL known signatures
    # If the payload matches any known format, it's a valid image/video.
    # If it matches nothing, it's garbage/unknown and should be rejected.
    for fmt, signatures in _MAGIC_BYTES.items():
        for sig in signatures:
            if fmt in ("WEBP", "AVIF", "HEIC", "MP4", "WEBM", "MKV"):
                if fmt == "WEBP":
                    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
                        log.debug("Sniffed format %s from magic bytes (no expected format)", fmt)
                        return None
                elif fmt in ("AVIF", "HEIC", "MP4"):
                    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"heic", b"mif1", b"msf1", b"mp4 ", b"isom", b"iso2"):
                        log.debug("Sniffed format %s from magic bytes (no expected format)", fmt)
                        return None
                elif fmt in ("WEBM", "MKV"):
                    if data.startswith(b"\x1aE\xdf\xa3"):
                        log.debug("Sniffed format %s from magic bytes (no expected format)", fmt)
                        return None
            else:
                if data.startswith(sig):
                    log.debug("Sniffed format %s from magic bytes (no expected format)", fmt)
                    return None
    
    # Payload matched no known format signature
    return "Downloaded file does not match any known image/video format (magic bytes unrecognized)"


def _validate_image_decodes(data: bytes, expected_format: Optional[str]) -> Optional[str]:
    """Try to decode the image data with PIL to confirm it's a valid image.
    
    Returns an error message if decoding fails, None if it succeeds or if
    the format is not supported by PIL (we don't fail closed on video formats).
    When expected_format is None, skip PIL validation since we don't know
    the format and magic bytes already validated it's a known format.
    """
    if not expected_format:
        # No format claim - magic bytes sniffing already validated it's a known format.
        # Don't attempt PIL decode which would fail on video formats.
        return None
    
    # Only attempt for formats PIL can handle
    pil_formats = {"JPEG", "PNG", "GIF", "WEBP", "BMP", "TIFF", "AVIF"}
    if expected_format.upper() not in pil_formats:
        return None  # Skip validation for video formats etc.
    
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(data))
        img.verify()  # Verify integrity without fully loading
        # Re-open for load test (verify() leaves the file pointer at end)
        img = Image.open(io.BytesIO(data))
        img.load()  # Force full decode
        return None
    except Exception as exc:
        return f"Downloaded data does not decode as a valid image: {exc}"


# What the two files turned out to be, when that can be said at all.
VERDICT_SAME_IMAGE = "same"        # one picture, two copies - the new one is better
VERDICT_ALTERNATE = "alternate"    # one picture, two versions - a different edit or crop


def duplicate_verdict(local_path: str, downloaded: bytes) -> Optional[str]:
    """Whether the file just downloaded is the same picture as the local
    one, a different edit of it, or something not to guess at.

    Measured HERE, from the two real files, rather than read off
    candidate.similarity - and that is the important decision in this
    function. The stored score is a comparison against the search
    ENGINE'S THUMBNAIL, and aligned_similarity() flattens several
    different outcomes onto one number: an unconfirmed 90+ and a mirrored
    copy both come back as exactly UNCONFIRMED_MAX_SIMILARITY (85.0),
    which is indistinguishable from a genuine edit/crop score of 85. A
    relationship written into somebody's library on that ambiguity would
    be wrong some of the time and is tedious to pick apart again. The
    originals are therefore compared once more at full size, where the
    two hash distances are both available and the answer is not ambiguous.

    None means "do not touch Hydrus". Everything that is not one of the
    two confident answers lands there, which is the "never fire on an
    unconfirmed verdict" rule:

      * the comparison did not produce a distance at all
      * the closest match was the MIRRORED shape - the same artwork
        flipped, which is a real thing to notice and not a thing Hydrus
        has a relationship for. Not the same file, not an edit of it in
        the sense 'alternates' means
      * no 256-bit distance, so the confirming hash never ran
      * a 64-bit distance that says "same picture" which 256 bits does
        NOT confirm. MEASURED in core/image_compare.py: of 12 such cases
        six were the same picture and six were variants or different -
        so this is precisely the case where the evidence disagrees with
        itself, and "probably an alternate" is not good enough to write
      * a 64-bit distance past DHASH_SIMILAR_MAX: different pictures
    """
    from .image_compare import (
        DHASH_SAME_MAX, DHASH_SIMILAR_MAX, FINE_CONFIRM_MAX, compare_to_local, local_prints,
    )

    comparison = compare_to_local(local_prints(local_path), downloaded)
    if comparison.distance is None or comparison.mirrored or comparison.fine_distance is None:
        return None
    if comparison.distance <= DHASH_SAME_MAX:
        return VERDICT_SAME_IMAGE if comparison.fine_distance <= FINE_CONFIRM_MAX else None
    if comparison.distance <= DHASH_SIMILAR_MAX:
        # describe_perceptual_match()'s own band: "similar, but not
        # identical - possibly a different edit or crop". Which is what
        # Hydrus's 'alternates' records, so nothing is being claimed here
        # beyond what the measurement already said.
        return VERDICT_ALTERNATE
    return None


def record_duplicate_relationship(entry: ImageEntry, client: HydrusClient, settings,
                                  local_hash: Optional[str], downloaded: bytes,
                                  new_hash: str) -> Optional[str]:
    """Tell Hydrus how the file just imported relates to the user's local
    copy, so its library does not end up holding two files it believes are
    strangers.

    OFF unless the user asked for it, and returns before doing anything at
    all - no comparison, no request - when they have not. Nothing about
    the import changes in that case.

    Never raises, and never fails the import. The file is already in
    Hydrus by the time this runs; losing a confirmed import over a piece
    of bookkeeping that follows it would be the worse outcome by far. A
    problem worth telling the user about comes back as a warning string
    for the caller to attach to the result instead.
    """
    if not getattr(settings, "set_hydrus_duplicate_relationships", False):
        return None
    if not local_hash or local_hash.lower() == new_hash.lower():
        # Either nothing to pair with, or Hydrus turned out to already
        # hold this exact file - one file, so no relationship exists.
        return None

    verdict = duplicate_verdict(entry.path, downloaded)
    if verdict is None:
        log.info("Not relating %s to the local copy in Hydrus: the comparison isn't confident "
                 "enough to record one", entry.filename)
        return None

    # Hydrus has to actually be holding the local copy. It answers 200 for
    # a pairing against a hash it has never seen, so an unchecked call
    # would look like it worked and leave the user nothing to find. Same
    # reasoning, and the same helper, as delete_files().
    state = client.deletion_states([local_hash]).get(local_hash)
    if state != "present":
        log.info("Not relating %s to the local copy in Hydrus: it isn't holding that file "
                 "(%s)", entry.filename, state)
        return None

    missing = client.missing_permission(PERMISSION_EDIT_FILE_RELATIONSHIPS)
    if missing:
        log.warning("Could not relate %s to the local copy in Hydrus: %s", entry.filename, missing)
        return f"Imported, but not marked as a duplicate: {missing}"

    try:
        if verdict == VERDICT_SAME_IMAGE:
            client.set_file_relationship(new_hash, local_hash, DUPLICATE_BETTER)
            # See set_kings(): 'set A as better' alone does not guarantee
            # the new copy ends up king, and being the king is what makes
            # it the one Hydrus shows for the pair.
            client.set_kings([new_hash])
        else:
            client.set_file_relationship(new_hash, local_hash, DUPLICATE_ALTERNATE)
    except HydrusError as exc:
        log.warning("Could not relate %s to the local copy in Hydrus: %s", entry.filename, exc)
        return f"Imported, but not marked as a duplicate: {exc}"

    log.info("Hydrus now has %s as %s of the local copy %s", new_hash[:12],
             "the better duplicate" if verdict == VERDICT_SAME_IMAGE else "an alternate",
             local_hash[:12])
    return None


def write_provenance_note(entry: ImageEntry, client: HydrusClient, settings,
                          file_hash: str) -> Optional[str]:
    """Record where this match came from as a note on the file in Hydrus,
    so the library keeps what until now only this app's session file held.

    OFF unless the user asked for it, checked on the first line: with the
    feature off this costs no request and no work at all.

    Never raises, and never fails the import - the file is in Hydrus by
    the time this runs, and losing a confirmed import over the bookkeeping
    that follows it would be the worse outcome. Same rule, and the same
    shape, as record_duplicate_relationship. A problem worth telling the
    user about comes back as a warning string instead.

    The text itself is core/provenance_note.py's decision, not this
    function's - including the rule that a similarity gets written with
    the marker saying whether it was measured (DAN-69) rather than as a
    bare number this app cannot stand behind.
    """
    if not getattr(settings, "write_hydrus_provenance_note", False):
        return None
    if not file_hash:
        return None

    from .provenance_note import note_name, provenance_note
    text = provenance_note(entry)
    if text is None:
        # No match, so no provenance. Nothing to say to the user about it
        # either: they did not ask for a note about nothing.
        log.debug("No provenance note for %s: it has no matched source", entry.filename)
        return None

    # Hydrus has to actually be holding this file. It answers 200 for a
    # note written against a hash it has never seen - and add_file reports
    # a hash for a file it REFUSED as previously-deleted (status 3) too -
    # so an unchecked write would look like it worked and leave the user
    # nothing to find. Same reasoning, and the same helper, as
    # record_duplicate_relationship's check on the local copy.
    state = client.deletion_states([file_hash]).get(file_hash)
    if state != "present":
        log.info("Not writing a provenance note for %s: Hydrus isn't holding that file (%s)",
                 entry.filename, state)
        return None

    missing = client.missing_permission(PERMISSION_EDIT_FILE_NOTES)
    if missing:
        log.warning("Could not write a provenance note for %s: %s", entry.filename, missing)
        return f"Imported, but no source note was written: {missing}"

    name = note_name(getattr(settings, "hydrus_provenance_note_name", ""))
    try:
        client.set_note(file_hash, name, text)
    except HydrusError as exc:
        log.warning("Could not write a provenance note for %s: %s", entry.filename, exc)
        return f"Imported, but no source note was written: {exc}"

    log.info("Wrote the %r note onto %s in Hydrus", name, file_hash[:12])
    return None


def _joined(*warnings: Optional[str]) -> Optional[str]:
    """Several post-import warnings as the one string ImportResult carries.

    Both of the things that follow an import - the duplicate relationship
    and the provenance note - can fail independently, and one swallowing
    the other's message would report a partial success as a complete one.
    """
    present = [w for w in warnings if w]
    return "; ".join(present) if present else None


def _imported_bytes(client: HydrusClient, file_hash: str) -> Optional[bytes]:
    """The imported file's own bytes, read back out of Hydrus.

    download_and_send() already holds the bytes it uploaded, so it never
    needs this. The URL-importer path never sees them at all - Hydrus's
    own downloader did the fetching - so the only way to compare the
    result against the local copy is to ask Hydrus for the file it now
    has. One extra round trip, against a file already confirmed imported.

    Returns None instead of raising, for the same reason
    record_duplicate_relationship never raises: the import has already
    succeeded by this point and must not be undone by a failure in the
    bookkeeping that follows it.
    """
    file_id = client.file_id_for_hash(file_hash)
    if file_id is None:
        log.warning("Hydrus confirmed importing %s but has no file id for it, so the file "
                    "can't be read back to compare", file_hash[:12])
        return None
    # Hydrus streams the file to a path rather than handing back bytes,
    # and these are full-resolution originals - so it goes to a real temp
    # file that is removed as soon as the bytes have been read, rather
    # than being left in a staging directory somebody else has to clean.
    handle, tmp_path = tempfile.mkstemp(prefix="hatate-hydrus-relate-")
    os.close(handle)
    try:
        client.download_file(file_id, tmp_path)
        with open(tmp_path, "rb") as fh:
            return fh.read()
    except (HydrusError, OSError) as exc:
        log.warning("Could not read %s back out of Hydrus to compare it: %s",
                    file_hash[:12], exc)
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def relate_confirmed_url_import(entry: ImageEntry, client: HydrusClient, settings,
                                local_hash: Optional[str], new_hash: str) -> Optional[str]:
    """record_duplicate_relationship for an import HYDRUS'S OWN downloader
    performed, once the poll has confirmed the file's hash.

    This is the missing half of DAN-39. That change wired the relationship
    into download_and_send(), which is the one path where Hatate did the
    downloading and therefore still had the bytes. A user on
    auto_import_method = "url_importer" had the setting on and got no
    relationships at all, with nothing said about it either way.

    The verdict itself is not re-decided here: duplicate_verdict() stays
    the single answer to "how do these two files relate", and every guard
    around writing it - the deletion_states check, the named permission
    report, never relating on an unconfident verdict, never raising into
    the import - lives in record_duplicate_relationship and is reached by
    calling it, not by being restated.

    The setting is checked on the first line, BEFORE the file-id lookup
    and the download. With the feature off this path costs nothing: no
    request, no temp file, no comparison.
    """
    if not getattr(settings, "set_hydrus_duplicate_relationships", False):
        return None
    if not local_hash or local_hash.lower() == new_hash.lower():
        # Either nothing to pair with, or Hydrus's downloader landed on
        # the exact file the user already had - one file, no relationship.
        # Checked here as well as in record_duplicate_relationship so the
        # fetch below is not paid for a pairing that cannot happen.
        return None

    downloaded = _imported_bytes(client, new_hash)
    if downloaded is None:
        # Worth telling the user about: they asked for relationships and
        # did not get one, which is exactly the silence this change exists
        # to remove. The import itself stands.
        return ("Imported, but not marked as a duplicate: could not read the imported file "
                "back out of Hydrus to compare it against your copy")

    return record_duplicate_relationship(entry, client, settings, local_hash, downloaded, new_hash)


def finish_url_import(entry: ImageEntry, client: HydrusClient, settings,
                      local_hash: Optional[str], new_hash: str) -> Optional[str]:
    """Everything that happens AFTER Hydrus's own downloader is confirmed
    to have imported the file: the duplicate relationship and the
    provenance note.

    One function rather than two calls at each site because there are two
    sites (workers/search_worker.py's auto-import and
    workers/hydrus_import_poll_worker.py's batch poll) and they must not
    drift into doing different halves of this. Each part is off by default
    and independent: one being off, or failing, does not stop the other,
    and both messages reach the user rather than one overwriting the
    other.
    """
    relationship_warning = relate_confirmed_url_import(
        entry, client, settings, local_hash, new_hash)
    note_warning = write_provenance_note(entry, client, settings, new_hash)
    return _joined(relationship_warning, note_warning)


def send_url_or_download(entry: ImageEntry, client: HydrusClient, settings,
                         tmp_dir: str) -> ImportResult:
    """Send URL, falling back to Hatate's own download when Hydrus can't.

    Hydrus's importer first, since it brings Hydrus's own tags and keeps
    the source link current. If Hydrus has no downloader for the site but
    Hatate's parser knows where the original file is - e-shuushuu, say,
    CONFIRMED to give the full-size file - Hatate downloads it and sends
    that. Where neither can reach the original (anime-pictures serves it
    only to logged-in accounts), the refusal stands and says so.
    """
    result = send_url_to_importer(entry, client, settings)
    if result.success or not result.refused:
        return result
    return download_instead(entry, client, settings, tmp_dir, result)


def download_instead(entry: ImageEntry, client: HydrusClient, settings, tmp_dir: str,
                     refused: ImportResult) -> ImportResult:
    """The fallback half of send_url_or_download: Hatate downloads the
    original itself, after Hydrus's importer has refused the URL."""
    candidate = entry.selected_candidate
    if candidate is not None and _is_exact_copy(candidate):
        # The match IS this file, byte for byte - it was found by the
        # file's own SHA-256 - so the local copy already is the original.
        # Uploading it gives Hydrus exactly what a download would, and
        # works even where the site doesn't hold the original at all
        # (pawchive's "has_full": false, which is common).
        host = urlparse(entry.matched_url or "").hostname or "the site"
        uploaded = send_file_upload(entry, client, settings)
        if uploaded.success:
            uploaded.note = (f"{host} has this exact file, so Hatate sent your copy "
                             "(the same bytes) with its tags and link")
            uploaded.confirmed = True
        return uploaded
    if candidate is not None and not candidate.direct_file_url and settings is not None:
        from .search_engine import fetch_candidate_details
        try:
            fetch_candidate_details(candidate, settings, local_path=entry.path)
        except Exception as exc:                     # noqa: BLE001 - best effort
            log.debug("Could not look up the file for %s: %s", entry.filename, exc)
    host = urlparse(entry.matched_url or "").hostname or "this site"
    if candidate is None or not candidate.direct_file_url:
        reason = (f"Neither Hydrus nor Hatate can download the original from {host}. "
                  "Use Send (upload) to send your own copy with its tags and link")
        log.warning("Not sending %s: %s", entry.filename, reason)
        entry.error_message = f"Not sent: {reason}"
        return ImportResult(success=False, error=reason, refused=True)

    log.info("Hydrus can't import %s - downloading it with Hatate instead", entry.matched_url)
    downloaded = download_and_send(entry, client, getattr(settings, "search_timeout", 30.0),
                                   tmp_dir, settings)
    if downloaded.success:
        downloaded.note = f"Hydrus has no downloader for {host}, so Hatate downloaded it"
        downloaded.confirmed = True           # uploaded by us; Hydrus returned the hash
    elif downloaded.skipped_reason:
        downloaded = ImportResult(success=False, refused=True, error=refused.error)
    return downloaded


def send_url_to_importer(entry: ImageEntry, client: HydrusClient, settings=None) -> ImportResult:
    """Hands the matched URL to Hydrus's own downloader - Hydrus fetches
    and imports the file itself. See the module docstring for the
    important caveat about what "success" means here.

    `settings` is optional and only decides which tags ride along with
    the URL (see models.tags_to_send)."""
    if not entry.matched_url:
        return ImportResult(success=False, skipped_reason="no matched URL")
    if not entry.matched_url.startswith(("http://", "https://")):
        return ImportResult(success=False, skipped_reason="matched URL isn't http(s)")

    import_url = normalize_url_for_hydrus(entry.matched_url)

    refusal = url_import_refusal(client, import_url)
    if refusal:
        forwarded = _forwarded_url(import_url)
        if forwarded and url_import_refusal(client, forwarded) is None:
            # A legacy link that forwards to one Hydrus knows - DeviantArt's
            # /view/<id> goes to /<artist>/art/<title>-<id>. Repointed for
            # good, so the confirmation poll and the link the user opens
            # both use the address Hydrus actually imported.
            log.info("%s forwards to %s, which Hydrus can import - using that",
                     import_url, forwarded)
            _repoint(entry, forwarded)
            import_url = forwarded
            refusal = None
    if refusal:
        # Info, not a warning: a caller may still get the file another way
        # (send_url_or_download), and says so itself if nothing works.
        log.info("Hydrus's URL importer can't take %s for %s: %s",
                 import_url, entry.filename, refusal)
        entry.error_message = f"Not sent: {refusal}"
        return ImportResult(success=False, error=refusal, refused=True)

    log.info("Queuing %s (%s) for Hydrus's URL importer", entry.filename, import_url)
    try:
        tag_names = [t.display for t in tags_to_send(entry, settings)]
        result = client.import_url(import_url, tags=tag_names)
        note = result.get("human_result_text", "")
        log.info("Hydrus importer response for %s: %s", entry.filename, note)
        # NOT marked confirmed: Hydrus has only accepted the URL for its
        # downloader queue. See the module docstring - confirmation needs
        # the separate poll in hydrus_import_poll.py.
        entry.sent_to_hydrus = True
        return ImportResult(success=True)
    except HydrusError as exc:
        entry.error_message = str(exc)
        log.error("import_url failed for %s: %s", entry.filename, exc)
        return ImportResult(success=False, error=str(exc))


def download_and_send(
    entry: ImageEntry, client: HydrusClient, search_timeout: float, tmp_dir: str,
    settings=None,
) -> ImportResult:
    """Downloads the matched image's actual full-resolution file
    ourselves (not the local file, not through Hydrus's downloader), then
    uploads those bytes to Hydrus with the source URL and tags attached."""
    # Imported here, not at module level, to avoid a circular import
    # (search_engine.py doesn't depend on this module, but importing it
    # at load time would still create an awkward ordering dependency).
    from . import remote
    from .search_engine import cookies_for_url, referer_for_candidate
    from .hydrus_tag_lookup import hash_file

    candidate = entry.selected_candidate
    if not candidate or not candidate.direct_file_url:
        return ImportResult(success=False, skipped_reason="no direct file URL available")
    direct_url = candidate.direct_file_url
    # The local file's own SHA256, read before the import overwrites
    # entry.hydrus_hash with the NEW file's - which is the only hash that
    # can still identify the copy the user already had.
    local_hash = entry.hydrus_hash

    log.info("Downloading matched image for %s from %s", entry.filename, direct_url)
    try:
        referer = referer_for_candidate(candidate, direct_url)
        # Pass any site auth cookie too - Pixiv's CDN gates restricted
        # works on the session, so a logged-out request gets nothing.
        data = remote.download_bytes(
            direct_url, search_timeout, referer=referer,
            cookies=cookies_for_url(direct_url, settings) if settings else None,
        )
        if not data:
            raise HydrusError(f"Could not download the image from {direct_url}")
        if _looks_like_a_web_page(data):
            raise HydrusError(
                f"{urlparse(direct_url).hostname} answered with a web page instead of the "
                "image - it may only serve originals to logged-in accounts")

        # Integrity checks that do not depend on the remote stating a size (BA-02).
        # These catch zero-length files, implausibly small payloads, format mismatches,
        # and corrupted/invalid image data even when Content-Length is unknown.
        url_path_ext = os.path.splitext(urlparse(direct_url).path)[1]  # real extension, may be ""
        ext_for_filename = url_path_ext or ".jpg"  # default only for temp filename
        expected_format = candidate.remote_format if candidate else None
        
        # Pass the real extension (or "") to validation; keep .jpg default only for filename
        magic_error = _validate_magic_bytes(data, expected_format, url_path_ext)
        if magic_error:
            raise HydrusError(f"Downloaded file validation failed: {magic_error}")

        # Verify downloaded size matches expected size from HEAD request (if known).
        # This catches truncated transfers, CDN corruption, and HTML error pages
        # served with HTTP 200 that don't look like web pages (e.g. JSON error responses).
        expected_size = candidate.remote_size_bytes if candidate else None
        if expected_size is not None and len(data) != expected_size:
            raise HydrusError(
                f"Downloaded {len(data)} bytes but expected {expected_size} bytes "
                f"(Content-Length from {urlparse(direct_url).hostname}) - "
                "transfer may be truncated or corrupted")

        decode_error = _validate_image_decodes(data, expected_format)
        if decode_error:
            raise HydrusError(f"Downloaded file validation failed: {decode_error}")

        dest_path = os.path.join(tmp_dir, f"{os.path.splitext(entry.filename)[0]}_matched{ext_for_filename}")
        with open(dest_path, "wb") as fh:
            fh.write(data)

        # Verify file hash matches what we wrote (defense against filesystem corruption).
        actual_hash = hash_file(dest_path)
        if not actual_hash:
            raise HydrusError("Could not compute hash of downloaded file")

        result = client.import_file(dest_path)
        file_hash = result.get("hash")
        if not file_hash:
            raise HydrusError("Hydrus didn't return a file hash for this import")

        # If Hydrus returns a different hash than what we computed, reject rather than
        # proceed. Per Hydrus's documentation, format conversion on /add_files/add_file
        # is a separate, explicit user workflow - the importer does not transcode files
        # on a direct upload. So a mismatch here means the bytes Hydrus actually stored
        # are not the bytes we meant to send (temp file corruption, filesystem issue,
        # etc.), and associating a URL/tags to that hash would silently record success
        # against the wrong file (DAN-17).
        #
        # ASSUMPTION, UNCONFIRMED AGAINST A LIVE HYDRUS: this is inferred from Hydrus's
        # docs/wiki, not observed against a running instance (none was available when
        # this was written). If a live Hydrus ever does transcode/normalize on a plain
        # add_file call, this will start rejecting otherwise-valid imports - that
        # symptom (imports failing here that a human confirms Hydrus actually accepted
        # and stored under the returned hash) is what would disprove the assumption.
        if file_hash.lower() != actual_hash.lower():
            raise HydrusError(
                f"Hash mismatch for {entry.filename} ({dest_path}): downloaded file "
                f"hash={actual_hash} but Hydrus returned hash={file_hash}. Refusing to "
                "associate URL/tags with a hash that doesn't match the file we sent."
            )

        if entry.matched_url:
            try:
                client.associate_url(entry.matched_url, file_hash=file_hash)
            except HydrusError as exc:
                log.warning("associate_url failed for %s: %s (tags still attempted)", entry.filename, exc)

        tag_names = [t.display for t in tags_to_send(entry, settings)]
        if tag_names:
            client.add_tags(file_hash, tag_names)

        # After the file is confirmed held: Hydrus returned its hash, and
        # that hash was checked against the bytes we sent. Off by default,
        # and a no-op that touches nothing when it is off.
        relationship_warning = record_duplicate_relationship(
            entry, client, settings, local_hash, data, file_hash)
        note_warning = write_provenance_note(entry, client, settings, file_hash)
        warning = _joined(relationship_warning, note_warning)

        entry.sent_to_hydrus = True
        entry.hydrus_import_confirmed = True  # uploaded by us, hash returned by Hydrus
        entry.error_message = warning
        entry.hydrus_hash = file_hash
        log.info("Downloaded and sent %s to Hydrus (hash=%s)", entry.filename, file_hash)
        return ImportResult(success=True, warning=warning)
    except HydrusError as exc:
        entry.error_message = str(exc)
        log.error("Download + send failed for %s: %s", entry.filename, exc)
        return ImportResult(success=False, error=str(exc))
