"""Core logic (no Qt dependency) for auto-importing tags Hydrus already has
for a file, keyed by matching the file's own SHA256 hash - the same hash
Hydrus uses to identify files. Kept separate from workers/hydrus_lookup_worker.py
so it's plain and testable without a Qt event loop.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Dict, List, NamedTuple, Optional

from .applog import get_logger
from .config import Settings
from .hydrus_client import HydrusClient, HydrusError
from .models import ImageEntry, Tag, TagSource
from .tag_rules import apply_namespace_remap

log = get_logger("hydrus_lookup")


class TagLookupResult(NamedTuple):
    tagged_count: int
    error: Optional[str] = None    # set when the lookup itself failed - a
                                    # 0 here means "Hydrus had nothing for
                                    # these hashes", which is not a failure
                                    # and must stay distinguishable from one


HYDRUS_FILENAME_RE = re.compile(r"^([0-9a-f]{64})(\.[A-Za-z0-9]+)?$")


def hash_from_filename(path: str) -> str:
    """The SHA256 taken from the FILENAME, for files sitting in Hydrus's
    own file store.

    Hydrus names every file in client_files after its SHA256 - the exact
    hash this app otherwise spends time computing - and sorts them into
    shard directories (fa1/, f5b/, ...). So for a batch added straight
    out of that store, the hash is already sitting in the path and
    reading the file contributes nothing.

    Returns "" when the name isn't a 64-hex-character SHA256, which is
    the overwhelmingly common case for ordinary files.

    NOTE this trusts the name to match the contents. That holds inside
    Hydrus's store, which the app never writes to, but not for an
    arbitrary file someone renamed. Callers must treat it as an
    optimisation to be verified, not a fact - see
    verify_filename_hashes().
    """
    match = HYDRUS_FILENAME_RE.match(os.path.basename(path))
    return match.group(1) if match else ""


def verify_filename_hashes(paths: List[str], sample_size: int = 3) -> bool:
    """Actually reads a few of the given files and checks their real
    hash against the one in the filename.

    The filename shortcut is only sound while the names really are
    content hashes. Rather than take that on trust for a whole batch,
    this spot-checks a small sample: cheap (a handful of files against
    tens of thousands) and enough to catch the case that matters -
    someone pointing the shortcut at files that merely look
    hash-shaped, where every hash would silently be wrong and every
    duplicate check and Hydrus lookup along with it.
    """
    candidates = [p for p in paths if hash_from_filename(p)]
    if not candidates:
        return False

    step = max(1, len(candidates) // sample_size)
    sample = candidates[::step][:sample_size]
    for path in sample:
        claimed = hash_from_filename(path)
        actual = hash_file(path)
        if not actual:
            log.warning("Could not verify %s - unreadable; not trusting filename hashes", path)
            return False
        if actual != claimed:
            log.warning(
                "%s is named like a SHA256 but its contents hash to %s… - "
                "filenames are NOT content hashes here, hashing everything properly",
                os.path.basename(path), actual[:12],
            )
            return False
    log.info(
        "Verified %d sampled file(s): filenames match their contents, so the rest of the "
        "batch can take its hashes from the names without reading them", len(sample),
    )
    return True


def hash_file(path: str) -> str:
    """Returns the lowercase hex SHA256 of a file's raw bytes - this is
    exactly how Hydrus identifies files, so it matches what Hydrus will
    assign on upload without needing to actually upload first."""
    try:
        with open(path, "rb") as fh:
            # file_digest (3.11+) reads straight into a reusable buffer
            # instead of allocating a new bytes object per chunk. On 3.10
            # the manual loop is the same hash, just a little slower.
            if hasattr(hashlib, "file_digest"):
                return hashlib.file_digest(fh, "sha256").hexdigest()
            digest = hashlib.sha256()
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
            return digest.hexdigest()
    except OSError as exc:
        log.warning("Could not hash %s: %s", path, exc)
        return ""


def parse_tag(raw: str, source: TagSource = TagSource.HYDRUS) -> Tag:
    if ":" in raw:
        namespace, name = raw.split(":", 1)
    else:
        namespace, name = None, raw
    return Tag(name=name, source=source, namespace=namespace)


def _size_matches(path: str, hydrus_size) -> bool:
    """Whether the local file is the size Hydrus recorded for the hash we
    looked it up by. Unknown sizes pass - older Hydrus versions may not
    report one, and refusing to import tags over a missing field would be
    worse than the risk it guards against."""
    if not hydrus_size:
        return True
    try:
        local_size = os.path.getsize(path)
    except OSError:
        return True  # can't check; don't punish the file for that
    if local_size == hydrus_size:
        return True
    log.warning(
        "%s is %d bytes but Hydrus says that hash is %d bytes - the hash doesn't match this "
        "file, so its Hydrus tags were NOT imported. If this file came from a batch using "
        "filename-derived hashes, turn that off in Settings > General and re-add it.",
        os.path.basename(path), local_size, hydrus_size,
    )
    return False


def apply_existing_hydrus_tags(entries: List[ImageEntry], settings: Settings) -> TagLookupResult:
    """For each entry, hashes its local file and checks whether Hydrus
    already has tags for that exact file - if so, imports them as
    TagSource.HYDRUS tags, with the configured namespace remap rules
    applied (Settings > Tag Namespaces) same as freshly-searched tags.
    Mutates the entries in place. Returns how many entries received tags,
    plus an error string when the lookup itself failed (as opposed to
    Hydrus simply not recognising any of the hashes) so callers can tell
    the two apart instead of showing both as "0 tags found".
    No-ops (tagged_count=0, no error) if Hydrus isn't configured."""
    if not settings.hydrus.access_key:
        log.debug("No Hydrus access key configured, skipping tag lookup for %d file(s)", len(entries))
        return TagLookupResult(0)

    hash_to_entries: Dict[str, List[ImageEntry]] = {}
    for entry in entries:
        file_hash = entry.hydrus_hash or hash_file(entry.path)
        if not file_hash:
            continue
        entry.hydrus_hash = file_hash
        hash_to_entries.setdefault(file_hash, []).append(entry)

    log.debug("Hashed %d/%d file(s) into %d unique hash(es)",
              sum(len(v) for v in hash_to_entries.values()), len(entries), len(hash_to_entries))

    if not hash_to_entries:
        return TagLookupResult(0)

    client = HydrusClient(settings.hydrus)
    try:
        metadata_by_hash = client.get_tags_and_sizes_for_hashes(list(hash_to_entries.keys()))
    except HydrusError as exc:
        log.warning("Hydrus tag lookup failed: %s", exc)
        return TagLookupResult(0, error=str(exc))

    log.debug("Hydrus recognized %d/%d of those hashes", len(metadata_by_hash), len(hash_to_entries))

    tagged_count = 0
    for file_hash, (tag_names, hydrus_size) in metadata_by_hash.items():
        if not tag_names:
            continue
        tag_objs = apply_namespace_remap([parse_tag(name) for name in tag_names], settings)
        for entry in hash_to_entries.get(file_hash, []):
            # Cross-check the size Hydrus has for this hash against the
            # actual local file. For a hash computed from the file's
            # contents these always agree, so a mismatch means the hash
            # doesn't identify this file - which can only happen if it
            # came from somewhere other than reading the bytes, i.e. the
            # filename shortcut on a file whose name lies. Importing here
            # would put another file's tags on this one, silently and
            # permanently, so it's skipped and reported instead.
            if not _size_matches(entry.path, hydrus_size):
                continue
            entry.add_tags(tag_objs, replace_source=TagSource.HYDRUS)
            tagged_count += 1

    log.info("Auto-imported existing Hydrus tags for %d/%d file(s)", tagged_count, len(entries))
    return TagLookupResult(tagged_count)
