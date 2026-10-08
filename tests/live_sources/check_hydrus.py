"""DAN-193 live-source check: does the Hydrus Client API still answer
the endpoints this app parses with the exact JSON field shapes
core/hydrus_client.py depends on?

Not part of `run_tests.sh` or CI - see README.md in this directory. Run
through the shared entry point:

    python3 -m tests.live_sources.run_checks hydrus

**Read-only.** Every belief below only GETs. Nothing here imports a
file, writes a tag, or touches file relationships - see
core/hydrus_client.py for which of its methods write, and
tests/test_hydrus_client_write_path.py (mocked) for the only coverage
those get. If a belief about a write endpoint's response shape mattered,
it would have to be reported as an uncovered belief instead of verified
here - see the module's ticket, DAN-193.

Unlike every other check in this directory, this one has no escaped
defect behind it yet. It earns its place anyway because every import and
every tag write this app ever does goes through this one contract, and
a renamed or retyped JSON field on any endpoint it reads would surface
only as whatever the downstream code happens to do with a missing key -
silently, the same failure shape as DAN-79, just one layer lower, and
one nobody would notice until a write went wrong against the wrong
file or the wrong tag service.

The connection and auth halves (Hydrus not running, a rejected access
key, a missing permission) are already loud in core/hydrus_client.py
itself - HydrusError on 401/403, missing_permission()'s named, actionable
message - and are not re-litigated here; the first belief below only
confirms the connection cheaply, as a gate the later beliefs can assume,
exactly as the ticket asked. Every belief past that is about JSON shape
under a 200, which nothing before this check ever asserted against a
real client.

Reads the real access key the same way the app does: Settings.load()
(see core/paths.py for where that file lives) rather than this module
reading or constructing one itself. The key is never logged, printed, or
included in any assertion message below - only field names and value
TYPES, which is also why the field-shape beliefs assert key presence and
type rather than dumping response bodies into their failure text.
"""
from __future__ import annotations

import re
import sys

from .. import _path  # noqa: F401

from core.config import Settings
from core.hydrus_client import HydrusClient, HydrusError
from .harness import Check, SourceUnavailable, exit_code, memoize_once

CHECK = Check("hydrus")

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


@memoize_once
def _client() -> HydrusClient:
    settings = Settings.load().hydrus
    if not settings.access_key:
        raise SourceUnavailable(
            "no Hydrus access key is configured (Settings > Hydrus in the app) - "
            "there is nothing to check this runner's own client against.",
        )
    return HydrusClient(settings)


def _unavailable(label: str, exc: Exception) -> SourceUnavailable:
    # HydrusError already carries a clean, key-free message (see
    # core/hydrus_client.py's _handle) - connection failures, 401s, and
    # 403s are exactly the "already loud" cases this check does not
    # re-litigate, it just stops here instead of treating any of them as
    # a shape belief breaking.
    return SourceUnavailable(f"{label}: {exc}", evidence=str(exc))


@CHECK.belief("the access key connects with the permissions this app assumes")
def _connects() -> str:
    """Cheap and deliberately shallow - core/hydrus_client.py already
    reports a bad key or a down client loudly on its own (HydrusError
    401/403, missing_permission()'s named message). This belief exists
    only so every belief after it can assume a working connection rather
    than re-diagnosing one on every failure."""
    client = _client()
    try:
        ok = client.test_connection()
    except HydrusError as exc:
        raise _unavailable("/verify_access_key", exc) from exc
    assert ok, "/verify_access_key answered with an empty/falsy body"
    missing = client.missing_permission(3)  # "Search for and Fetch Files"
    assert missing is None, (
        f"this access key cannot search or fetch files: {missing} - every belief "
        "below needs this permission, so there is nothing further to check."
    )
    return "connected, has Search for and Fetch Files"


@CHECK.belief("get_services still names at least one tag service")
def _services_shape() -> str:
    """core/hydrus_client.py's list_tag_services() reads each service
    entry's 'type_pretty' (to recognise a tag service at all) and 'name'
    (the display string put in front of the user in the service
    picklist) out of /get_services's 'services' dict. A rename of either
    key would make every service silently stop looking like a tag
    service - list_tag_services() has no other way to notice, it would
    just return an empty list, the same shape as a client with no tag
    services configured at all."""
    client = _client()
    try:
        services = client.list_tag_services()
    except HydrusError as exc:
        raise _unavailable("/get_services", exc) from exc
    assert services, (
        "/get_services returned no entry whose 'type_pretty' contains 'tag' - "
        "either this Hydrus genuinely has no tag services (unusual - even the "
        "default local 'my tags' service should show up), or 'type_pretty'/the "
        "service dict's shape changed under list_tag_services()."
    )
    key, name = services[0]
    assert key and name, f"a tag service entry had an empty key or name: {(key, name)!r}"
    return f"{len(services)} tag service(s), e.g. {name!r}"


@memoize_once
def _sample_file():
    """One arbitrary, ordinary (non-trashed) file this Hydrus actually
    holds, fetched once and shared by every file_metadata-shaped belief
    below so they don't each repeat the search."""
    client = _client()
    try:
        file_ids = client.search_files(["system:limit=1"])
    except HydrusError as exc:
        raise _unavailable("/get_files/search_files", exc) from exc
    if not file_ids:
        raise SourceUnavailable(
            "this Hydrus holds zero files (system:limit=1 returned none) - there "
            "is nothing to check the file_metadata shape against.",
        )
    try:
        files = client.get_file_metadata(file_ids)
    except HydrusError as exc:
        raise _unavailable("/get_files/file_metadata", exc) from exc
    assert files, (
        f"search_files returned file_id {file_ids[0]} but file_metadata returned "
        "nothing for it - the two endpoints disagree about a file that was just "
        "found."
    )
    return files[0]


@CHECK.belief("search_files + file_metadata still carry file identity and dimensions")
def _file_identity_shape() -> str:
    """get_file_metadata() reads 'file_id', 'hash', 'mime', 'width', and
    'height' out of each /get_files/file_metadata entry (see
    core/hydrus_client.py:HydrusFile). A rename of any of these comes
    back as a silently None/missing field on the HydrusFile this app then
    uses to label and size the file everywhere else - never an exception,
    just a wrong or blank value downstream."""
    f = _sample_file()
    assert isinstance(f.file_id, int), f"file_id was {f.file_id!r} ({type(f.file_id).__name__}), not an int"
    assert f.hash and _HASH_RE.match(f.hash), (
        f"hash was {f.hash!r} - expected a 64-character lowercase hex SHA256, the "
        "shape get_file_metadata assumes and callers key off of."
    )
    assert f.mime, f"mime was {f.mime!r} - expected a non-empty MIME string"
    for dim in (f.width, f.height):
        assert dim is None or isinstance(dim, int), f"a dimension was {dim!r}, neither an int nor None"
    return f"file_id={f.file_id}, mime={f.mime!r}, {f.width}x{f.height}"


@CHECK.belief("file_metadata's is_trashed/is_deleted/is_local still mean what deletion_states() assumes")
def _deletion_state_shape() -> str:
    """deletion_states() (core/hydrus_client.py) derives "present" only
    when is_trashed is falsy AND (is_deleted is falsy AND is_local is
    truthy) - a rename of any one of those three booleans makes an
    ordinary, present file misreport as "deleted" or "unknown" rather
    than raising, which is exactly the kind of silent drift this suite
    exists to catch. The sampled file came back from an unfiltered
    system:limit search, so it is expected to be an ordinary present
    file - not trashed, not deleted."""
    f = _sample_file()
    client = _client()
    try:
        states = client.deletion_states([f.hash])
    except HydrusError as exc:
        raise _unavailable("/get_files/file_metadata (deletion_states)", exc) from exc
    state = states.get(f.hash)
    assert state == "present", (
        f"deletion_states() reported {state!r} for a file system:limit=1 just "
        "found with no trash/deleted filter - expected 'present'. Either this "
        "file is genuinely trashed/deleted (unlikely for an unfiltered search) "
        "or is_trashed/is_deleted/is_local changed shape under deletion_states()."
    )
    return f"deletion_states()['{f.file_id}'...] = {state!r}"


@CHECK.belief("a file with tags still exposes them through the tags-by-service shape")
def _tags_shape() -> str:
    """_extract_current_tags() (core/hydrus_client.py) reads
    tags[service_key]['display_tags' or 'storage_tags']['0'] out of each
    metadata entry - three nested keys, any one of which silently
    renaming collapses this file's tags to an empty list rather than
    raising. Deliberately searched FOR a tagged file (system:number of
    tags > 0) rather than reusing the single arbitrary sample above,
    because an untagged file passing this belief would prove nothing -
    an empty result is also what a real rename produces."""
    client = _client()
    try:
        file_ids = client.search_files(["system:number of tags > 0", "system:limit=1"])
    except HydrusError as exc:
        raise _unavailable("/get_files/search_files", exc) from exc
    if not file_ids:
        raise SourceUnavailable(
            "this Hydrus has no file with system:number of tags > 0 - there is no "
            "tagged file to check the tags-by-service shape against. This says "
            "nothing about whether that shape still parses.",
        )
    try:
        files = client.get_file_metadata(file_ids)
    except HydrusError as exc:
        raise _unavailable("/get_files/file_metadata", exc) from exc
    assert files, f"file_metadata returned nothing for {file_ids[0]}, which search_files just found"
    tags = files[0].tags
    assert tags, (
        f"file_id {files[0].file_id} matched system:number of tags > 0 but "
        "get_file_metadata()'s _extract_current_tags() produced an empty list - "
        "the 'tags' / 'display_tags'/'storage_tags' / status-key '0' shape it "
        "reads has changed."
    )
    return f"file_id={files[0].file_id}: {len(tags)} tag(s), e.g. {tags[0]!r}"


@CHECK.belief("get_url_info still reports a URL's type as the documented field")
def _url_info_shape() -> str:
    """get_url_info() (core/hydrus_client.py) passes /add_urls/get_url_info's
    whole response straight back to its caller, which reads 'url_type'
    (an int) to decide how to treat a URL before anything is imported -
    see hydrus_import.py. Read-only: this endpoint classifies a URL, it
    does not fetch or import it, so any URL is safe to ask about."""
    client = _client()
    probe_url = "https://example.invalid/this-url-is-never-imported"
    try:
        info = client.get_url_info(probe_url)
    except HydrusError as exc:
        raise _unavailable("/add_urls/get_url_info", exc) from exc
    assert isinstance(info, dict) and "url_type" in info, (
        f"/add_urls/get_url_info's response had no 'url_type' key - got keys "
        f"{sorted(info.keys()) if isinstance(info, dict) else type(info).__name__!r}"
    )
    assert isinstance(info["url_type"], int), (
        f"'url_type' was {info['url_type']!r} ({type(info['url_type']).__name__}), not an int"
    )
    return f"url_type={info['url_type']!r} for an unrecognised URL"


@CHECK.belief("get_url_files still reports import status as the documented list")
def _url_files_shape() -> str:
    """get_url_files() (core/hydrus_client.py) passes /add_urls/get_url_files's
    response straight back; callers (the DAN-49 import-confirmation poll)
    read 'url_file_statuses' as a list that gains an entry once a file is
    actually imported for the URL. Read-only lookup against real database
    state, not an import - a never-imported URL is expected to answer
    with an empty list, which is itself part of the documented shape."""
    client = _client()
    probe_url = "https://example.invalid/this-url-is-never-imported"
    try:
        info = client.get_url_files(probe_url)
    except HydrusError as exc:
        raise _unavailable("/add_urls/get_url_files", exc) from exc
    assert isinstance(info, dict) and "url_file_statuses" in info, (
        f"/add_urls/get_url_files's response had no 'url_file_statuses' key - got "
        f"keys {sorted(info.keys()) if isinstance(info, dict) else type(info).__name__!r}"
    )
    assert isinstance(info["url_file_statuses"], list), (
        f"'url_file_statuses' was a {type(info['url_file_statuses']).__name__}, not a list"
    )
    return f"url_file_statuses: {len(info['url_file_statuses'])} entr{'y' if len(info['url_file_statuses']) == 1 else 'ies'}"


if __name__ == "__main__":
    sys.exit(exit_code(CHECK.run()))
