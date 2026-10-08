"""Thin wrapper around the Hydrus Client API.

Docs: https://hydrusnetwork.github.io/hydrus/client_api.html
Requires the "client api" service to be running and access permitted for:
Import/Delete Files, Add Tags, Add URLs, Search Files.
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

from .applog import get_logger
from .config import HydrusSettings

log = get_logger("hydrus")


def _looks_like_service_key(value: str) -> bool:
    """Best-effort guess at whether this is a hex service key rather than a
    human-readable service name. Not authoritative - Hydrus's own default
    local tag service key is a short hex string (the hex-encoding of the
    literal text "local tags"), not a fixed 64-char length, so this is only
    used to choose which JSON field to try first; add_tags() falls back to
    the other field automatically if the first guess is wrong."""
    value = value.strip()
    return bool(value) and len(value) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in value)


def _extract_current_tags(metadata_entry: dict) -> List[str]:
    """Pulls the file's current (status "0") tags out of a /get_files/file_metadata
    entry, combined across every tag service Hydrus has for it. Used both to
    show existing tags for a Query Hydrus import and to auto-import tags
    when adding a local file Hydrus already knows about."""
    tags_by_service = metadata_entry.get("tags") or {}
    all_tags: set[str] = set()
    for info in tags_by_service.values():
        if not isinstance(info, dict):
            continue
        status_map = info.get("display_tags") or info.get("storage_tags") or {}
        current = status_map.get("0") or []
        all_tags.update(current)
    return sorted(all_tags)


class HydrusError(Exception):
    """Anything that stopped a Hydrus call from doing what it was asked.

    `status_code` is the HTTP status Hydrus answered with, where there was
    one (None for a connection failure or a locally-raised error). Callers
    that need to tell a missing permission from a missing endpoint have to
    branch on that, and matching on the message text would break the
    moment somebody rewords it.
    """

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


# Hydrus's numeric permission ids, verbatim from /request_new_permissions
# in its Client API docs - these are the names on the checkboxes the user
# actually ticks, which is what an error message has to say to be
# actionable.
HYDRUS_PERMISSIONS = {
    0: "Import and Edit URLs",
    1: "Import and Delete Files",
    2: "Edit File Tags",
    3: "Search for and Fetch Files",
    4: "Manage Pages",
    5: "Manage Cookies and Headers",
    6: "Manage Database",
    7: "Edit File Notes",
    8: "Edit File Relationships",
    9: "Edit File Ratings",
    10: "Manage Popups",
    11: "Edit File Times",
    12: "Commit Pending",
    13: "See Local Paths",
}
PERMISSION_EDIT_FILE_RELATIONSHIPS = 8
PERMISSION_EDIT_FILE_NOTES = 7

# Where a permission is granted, in Hydrus's own menu wording. A 403 that
# only says "forbidden" leaves the user with nothing to do about it.
WHERE_PERMISSIONS_LIVE = (
    "Hydrus: services → review services → local → client api, "
    "then edit your access key and tick it"
)

# The "relationship" enum of /manage_file_relationships/set_file_relationships,
# from the Client API docs. Only the two this app can justify are named.
#
#   4 "set A as better" - A and B are the same picture and A is the copy
#     to keep; Hydrus merges B's duplicate group into A's.
#   3 "set as alternates" - one picture in two versions (a different edit
#     or crop), kept side by side rather than one superseding the other.
#
# Deliberately NOT used: 0 (potential duplicates) would just queue the pair
# for the user to decide in the duplicate filter, which is work this app
# already did; 1 (false positives) and 2 (same quality) say things this
# app has no evidence for.
DUPLICATE_BETTER = 4
DUPLICATE_ALTERNATE = 3


@dataclass
class HydrusFile:
    file_id: int
    hash: str
    mime: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    tags: List[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.tags is None:
            self.tags = []


class HydrusClient:
    def __init__(self, settings: HydrusSettings):
        self.settings = settings

    def _headers(self) -> Dict[str, str]:
        h = {}
        if self.settings.access_key:
            h["Hydrus-Client-API-Access-Key"] = self.settings.access_key
        return h

    def _get(self, path: str, params: Optional[dict] = None):
        log.debug("GET %s params=%s", path, params)
        try:
            resp = requests.get(
                f"{self.settings.api_url}{path}",
                params=params or {},
                headers=self._headers(),
                timeout=self.settings.timeout,
                verify=self.settings.verify_ssl,
            )
        except requests.RequestException as exc:
            log.error("GET %s failed: %s", path, exc)
            raise HydrusError(f"Could not reach Hydrus at {self.settings.api_url}: {exc}") from exc
        return self._handle(path, resp)

    def _post(self, path: str, json_body: Optional[dict] = None,
              raw_data: Optional[bytes] = None, extra_headers: Optional[dict] = None):
        headers = {**self._headers(), **(extra_headers or {})}
        try:
            if raw_data is not None:
                log.debug("POST %s (%d raw bytes)", path, len(raw_data))
                resp = requests.post(
                    f"{self.settings.api_url}{path}",
                    data=raw_data,
                    headers=headers,
                    timeout=self.settings.timeout,
                    verify=self.settings.verify_ssl,
                )
            else:
                log.debug("POST %s body=%s", path, _json.dumps(json_body or {})[:500])
                resp = requests.post(
                    f"{self.settings.api_url}{path}",
                    json=json_body or {},
                    headers=headers,
                    timeout=self.settings.timeout,
                    verify=self.settings.verify_ssl,
                )
        except requests.RequestException as exc:
            log.error("POST %s failed: %s", path, exc)
            raise HydrusError(f"Could not reach Hydrus at {self.settings.api_url}: {exc}") from exc
        return self._handle(path, resp)

    @staticmethod
    def _handle(path: str, resp: requests.Response):
        log.debug("%s -> HTTP %d", path, resp.status_code)
        if resp.status_code == 401:
            log.error("%s -> 401 Unauthorized: access key rejected", path)
            raise HydrusError("Hydrus rejected the access key (401) - check Settings > Hydrus",
                              401)
        if resp.status_code == 403:
            log.error("%s -> 403 Forbidden: %s", path, resp.text[:300])
            raise HydrusError(
                f"Access key lacks permission for this action (403): {resp.text[:200]}", 403
            )
        if resp.status_code >= 400:
            log.error("%s -> HTTP %d: %s", path, resp.status_code, resp.text[:500])
            raise HydrusError(f"Hydrus returned HTTP {resp.status_code}: {resp.text[:300]}",
                              resp.status_code)
        try:
            return resp.json()
        except ValueError:
            return {}

    def test_connection(self) -> bool:
        data = self._get("/verify_access_key")
        return bool(data)

    def get_services(self) -> Dict[str, dict]:
        """Raw response from /get_services: {service_key: {name, type_pretty, ...}}."""
        data = self._get("/get_services")
        return data.get("services", {})

    def list_tag_services(self) -> List[tuple]:
        """Returns [(service_key, display_name), ...] for every service that
        can hold tags (local tag services and tag repositories), for a
        settings-dialog picklist so the user doesn't have to hand-type a
        service name or hunt down its hex key."""
        services = self.get_services()
        result = []
        for key, info in services.items():
            type_pretty = (info.get("type_pretty") or "").lower()
            if "tag" in type_pretty:
                result.append((key, info.get("name", key)))
        return result

    def search_files(self, tags: List[str]) -> List[int]:
        data = self._get("/get_files/search_files", {
            "tags": _json.dumps(tags),
            "file_sort_type": 0,
        })
        return data.get("file_ids", [])

    def get_file_metadata(self, file_ids: List[int]) -> List[HydrusFile]:
        data = self._get("/get_files/file_metadata", {
            "file_ids": _json.dumps(file_ids),
        })
        result = []
        for m in data.get("metadata", []):
            result.append(HydrusFile(
                file_id=m.get("file_id"),
                hash=m.get("hash", ""),
                mime=m.get("mime"),
                width=m.get("width"),
                height=m.get("height"),
                tags=_extract_current_tags(m),
            ))
        return result

    def get_tags_for_hashes(self, hashes: List[str]) -> Dict[str, List[str]]:
        """Looks up existing Hydrus tags for local files identified by their
        SHA256 hash - used to auto-import tags when adding a file that
        Hydrus already knows about, without having to upload it first.
        Returns {hash: [tag, ...]}, only for hashes Hydrus recognises;
        hashes it has never seen simply won't be in the result."""
        return {h: tags for h, (tags, _size) in self.get_tags_and_sizes_for_hashes(hashes).items()}

    def get_tags_and_sizes_for_hashes(self, hashes: List[str]) -> Dict[str, tuple]:
        """As get_tags_for_hashes, but also returns the file size Hydrus
        has recorded for each hash: {hash: ([tag, ...], size_or_None)}.

        The size comes back in the same response at no extra cost, and it
        gives callers a way to sanity-check that a hash really does
        identify the local file they think it does - see
        apply_existing_hydrus_tags."""
        if not hashes:
            return {}
        data = self._get("/get_files/file_metadata", {"hashes": _json.dumps(hashes)})
        result: Dict[str, tuple] = {}
        for m in data.get("metadata", []):
            h = m.get("hash")
            if h:
                result[h] = (_extract_current_tags(m), m.get("size"))
        return result

    def filter_known_hashes(self, hashes: List[str]) -> set:
        """Which of these hashes Hydrus actually has, as a set.

        Needed before asking for thumbnails: /get_files/thumbnail is
        documented never to 404 - an unknown file gets Hydrus's generic
        fallback icon instead. Fetching blind would therefore quietly
        replace every non-Hydrus file's thumbnail with a Hydrus logo,
        which looks like a rendering bug rather than a missing file."""
        if not hashes:
            return set()
        try:
            data = self._get("/get_files/file_metadata", {
                "hashes": _json.dumps(hashes),
                # Identifiers only - we want existence, not tags. Skips
                # building (and transferring) a full metadata payload for
                # what can be tens of thousands of files.
                "only_return_identifiers": "true",
            })
        except HydrusError as exc:
            log.warning("filter_known_hashes failed: %s", exc)
            return set()
        # A hash Hydrus has never seen still comes back here, with a null
        # file_id - so the presence of a row says nothing on its own.
        return {
            m.get("hash") for m in data.get("metadata", [])
            if m.get("hash") and m.get("file_id") is not None
        }

    # Hydrus's page-type enum. 6 is a file search page, the only kind
    # that can be handed a set of hashes to display.
    _PAGE_TYPE_FILE_SEARCH = 6

    def show_files_in_client(self, hashes: List[str], page_name: str) -> str:
        """Opens a new page in the Hydrus GUI showing these files.

        One call: /manage_pages/new_page takes the hashes, names the page
        and focuses it. The alternative - find an existing page with
        get_pages, append with add_files, then focus_page - works on
        older clients but has to pick somebody else's page to dump files
        into, and appending to whatever the user happened to be looking
        at is not a thing to do on their behalf.

        `system_hash_locked` is deliberately NOT set. It would pin the
        page to exactly these hashes, which sounds tidier but takes away
        the user's ability to then search or navigate from what they were
        shown - and being shown the file is the entire point.

        Needs the **Manage Pages** permission, which is not among the
        ones this app needs for anything else, so a key set up for
        importing alone will 403 here. Raises HydrusError; the caller
        turns that into something the user can act on.
        """
        if not hashes:
            raise HydrusError("No files to show.")
        data = self._post("/manage_pages/new_page", {
            "page_type": self._PAGE_TYPE_FILE_SEARCH,
            "page_name": page_name,
            "hashes": list(hashes),
            "focus_page": True,
        })
        return (data or {}).get("page_key", "")

    def missing_permission(self, permission_id: int) -> Optional[str]:
        """A named, actionable message when the access key definitely does
        NOT hold `permission_id` - None when it does, and None again when
        Hydrus cannot be asked.

        /verify_access_key answers with the key's own `basic_permissions`
        list, so this is a real check rather than firing the call and
        reading the wreckage. "Inconclusive is not missing" is deliberate,
        and the same rule hydrus_import.url_import_refusal() follows: an
        unreachable or unusually old client must not silently disable a
        feature the user turned on. A key that genuinely lacks the
        permission still fails at the call itself, where the wrappers
        below name it just as precisely - the probe only buys the chance
        to say so BEFORE anything is written.
        """
        try:
            info = self._get("/verify_access_key")
        except HydrusError as exc:
            log.debug("Could not read the access key's permissions: %s", exc)
            return None
        if info.get("permits_everything"):
            # The catch-all key: covers this permission and any added later.
            return None
        permissions = info.get("basic_permissions")
        if not isinstance(permissions, list):
            return None
        if permission_id in permissions:
            return None
        name = HYDRUS_PERMISSIONS.get(permission_id, f"permission {permission_id}")
        return (f'your Hydrus access key does not have the "{name}" permission. '
                f"Add it in {WHERE_PERMISSIONS_LIVE}")

    @staticmethod
    def _relationship_failure(exc: HydrusError) -> HydrusError:
        """Turns a failed /manage_file_relationships call into something
        the user can act on.

        The two failures worth naming are the two the user can fix, and a
        bare "403" or "HTTP 404" tells them neither: a key without the
        permission needs one checkbox ticked, and a Hydrus predating the
        endpoint needs updating. Anything else is passed through unchanged
        rather than dressed up as a diagnosis.
        """
        name = HYDRUS_PERMISSIONS[PERMISSION_EDIT_FILE_RELATIONSHIPS]
        if exc.status_code == 403:
            return HydrusError(
                f'your Hydrus access key does not have the "{name}" permission, which is '
                f"needed to record duplicate relationships. Add it in {WHERE_PERMISSIONS_LIVE}",
                403)
        if exc.status_code == 404:
            return HydrusError(
                "this Hydrus is too old to record duplicate relationships over the Client API "
                "(it has no /manage_file_relationships endpoint). Update Hydrus, or turn the "
                "setting off in Settings > Hydrus", 404)
        return exc

    def set_file_relationship(self, hash_a: str, hash_b: str, relationship: int,
                              do_default_content_merge: bool = True) -> None:
        """Records how two files Hydrus already holds relate to each other.

        `relationship` is Hydrus's own enum - see DUPLICATE_BETTER and
        DUPLICATE_ALTERNATE above. Both hashes must be files Hydrus is
        actually holding; it is the caller's job to have established that
        (deletion_states() is how), because Hydrus answers 200 either way
        and a pairing against a file it has never seen is not a thing the
        user can find or undo.

        `delete_a`/`delete_b` are deliberately never sent. Hydrus offers
        them on this same call, and being told which of two files is
        better is not consent to delete the other one.
        """
        if not hash_a or not hash_b:
            raise HydrusError("Both file hashes are needed to set a duplicate relationship")
        if hash_a.lower() == hash_b.lower():
            # Hydrus would reject this, but the useful place to say so is
            # here: it means the caller paired a file with itself.
            raise HydrusError("A file cannot be set as a duplicate of itself")
        try:
            self._post("/manage_file_relationships/set_file_relationships", {
                "relationships": [{
                    "hash_a": hash_a,
                    "hash_b": hash_b,
                    "relationship": relationship,
                    "do_default_content_merge": do_default_content_merge,
                }],
            })
        except HydrusError as exc:
            raise self._relationship_failure(exc) from exc

    def set_kings(self, hashes: List[str]) -> None:
        """Promotes each file to the king - the best representative - of
        its own duplicate group.

        Needed alongside DUPLICATE_BETTER rather than implied by it.
        Hydrus's documented king merge rules only guarantee A ends up king
        when A was ALREADY the king of its own group; "King A > Non-King
        B", for instance, leaves King B where it was. Asking outright is
        what makes "the copy the user chose is the one Hydrus shows" true
        in every case. Hydrus documents it as idempotent, including for a
        file with no duplicates at all.
        """
        if not hashes:
            return
        try:
            self._post("/manage_file_relationships/set_kings", {"hashes": list(hashes)})
        except HydrusError as exc:
            raise self._relationship_failure(exc) from exc

    @staticmethod
    def _notes_failure(exc: HydrusError) -> HydrusError:
        """Turns a failed /add_notes call into something the user can act
        on, the same two named cases as _relationship_failure: a key
        missing one checkbox, and a Hydrus predating the endpoint. Notes
        need their own version of this because they need their own
        permission - "Edit File Notes" is not implied by anything else
        this app asks for."""
        name = HYDRUS_PERMISSIONS[PERMISSION_EDIT_FILE_NOTES]
        if exc.status_code == 403:
            return HydrusError(
                f'your Hydrus access key does not have the "{name}" permission, which is '
                f"needed to write a note onto the file. Add it in {WHERE_PERMISSIONS_LIVE}",
                403)
        if exc.status_code == 404:
            return HydrusError(
                "this Hydrus is too old to write file notes over the Client API (it has no "
                "/add_notes/set_notes endpoint). Update Hydrus, or turn the setting off in "
                "Settings > Hydrus", 404)
        return exc

    def set_note(self, file_hash: str, name: str, text: str) -> None:
        """Writes one named note onto a file Hydrus is holding.

        `merge_cleverly` is deliberately NOT sent, so Hydrus REPLACES any
        existing note of this name rather than appending to it. That is
        what makes re-sending the same file idempotent: this app owns the
        note it names, and a note that grew a fresh copy of itself on
        every send would be worse than no note. Notes under any other
        name - including ones the user typed themselves - are untouched
        either way; set_notes only writes the names it is given.

        The hash must be one Hydrus actually holds. Hydrus answers 200 for
        a note written against a hash it has never seen, exactly as it
        does for a relationship, so a note written blind would look like
        it worked and leave the user nothing to find.
        """
        if not file_hash:
            raise HydrusError("A file hash is needed to write a note")
        if not name:
            raise HydrusError("A note name is needed to write a note")
        try:
            self._post("/add_notes/set_notes", {
                "hash": file_hash,
                "notes": {name: text},
            })
        except HydrusError as exc:
            raise self._notes_failure(exc) from exc

    def delete_files(self, hashes: List[str], reason: Optional[str] = None) -> List[str]:
        """Asks Hydrus to delete these files from its own database.

        This is the ONLY correct way to get rid of a file that lives in
        Hydrus's store. Deleting it off disk leaves Hydrus's record intact,
        so it goes on believing it holds a file that is no longer there -
        which is a much worse state than either having the file or having
        deleted it.

        Hydrus moves a file out of "my files" and into its trash, so this
        is recoverable from Hydrus's own UI (and via undelete_files) until
        the trash is emptied. It does NOT physically remove the file here.

        `reason` is recorded by Hydrus alongside the deletion and shows up
        in its own interface, which is worth filling in - a file that
        vanished with no explanation is hard to account for later.

        Returns the hashes it actually asked Hydrus to delete.

        Hashes Hydrus doesn't hold are filtered out FIRST, and that is not
        an optimisation. Deleting a hash Hydrus has never seen does not
        no-op: it writes a deletion record for it, which then makes Hydrus
        refuse that file if it is ever imported later. Confirmed against a
        live client while building this - a made-up hash came back
        is_deleted=True afterwards, and the record could not be cleared
        again. So the filter is what stops a delete of files Hydrus never
        had from quietly poisoning the user's database against them.
        """
        if not hashes:
            return []
        # Strictly "Hydrus is holding this file right now". Neither a hash
        # echoing back from file_metadata nor the presence of a file_id is
        # enough: Hydrus answers for hashes it has never seen, and a file
        # it has already deleted keeps its file_id. Both were verified
        # against a live client - and getting this wrong is what writes
        # the bogus deletion record described above.
        states = self.deletion_states(list(hashes))
        deletable = [h for h in hashes if states.get(h) == "present"]
        skipped = len(hashes) - len(deletable)
        if skipped:
            log.info("Not deleting %d hash(es) Hydrus isn't holding - deleting one of those "
                     "would only record it as deleted", skipped)
        if not deletable:
            return []
        body: Dict[str, object] = {"hashes": deletable}
        if reason:
            body["reason"] = reason
        self._post("/add_files/delete_files", body)
        log.info("Asked Hydrus to delete %d file(s)", len(deletable))
        return deletable

    def undelete_files(self, hashes: List[str]) -> None:
        """Pulls files back out of Hydrus's trash."""
        if not hashes:
            return
        self._post("/add_files/undelete_files", {"hashes": list(hashes)})

    def deletion_states(self, hashes: List[str]) -> Dict[str, str]:
        """What Hydrus currently thinks of each hash.

        "trashed"  - deleted from my files, sitting in the trash
        "deleted"  - gone from Hydrus's local storage entirely
        "present"  - still held normally
        "unknown"  - Hydrus has no record of this hash

        Used to CONFIRM a delete rather than assume it: the delete
        endpoint answers with an empty body whatever happens, including
        for hashes it has never heard of.
        """
        if not hashes:
            return {}
        states: Dict[str, str] = {h: "unknown" for h in hashes}
        try:
            data = self._get("/get_files/file_metadata", {"hashes": _json.dumps(list(hashes))})
        except HydrusError as exc:
            log.warning("Could not read deletion states: %s", exc)
            return states
        for m in data.get("metadata", []):
            h = m.get("hash")
            if not h:
                continue
            if m.get("file_id") is None:
                states[h] = "unknown"
            elif m.get("is_trashed"):
                states[h] = "trashed"
            elif m.get("is_deleted") or not m.get("is_local"):
                states[h] = "deleted"
            else:
                states[h] = "present"
        return states

    def get_thumbnail(self, file_hash: str) -> Optional[bytes]:
        """Raw bytes of Hydrus's own stored thumbnail for a file.

        Hydrus has already generated these, and they're a few KB against
        the original's megabytes - so for files it knows, this avoids
        reading the full file just to draw a small preview. Only call it
        for hashes confirmed present (see filter_known_hashes), or you'll
        get a fallback icon rather than an error.
        """
        url = f"{self.settings.api_url}/get_files/thumbnail"
        try:
            resp = requests.get(
                url, params={"hash": file_hash}, headers=self._headers(),
                timeout=self.settings.timeout, verify=self.settings.verify_ssl,
            )
        except requests.RequestException as exc:
            log.debug("Hydrus thumbnail request failed for %s: %s", file_hash[:12], exc)
            return None
        if resp.status_code != 200:
            log.debug("Hydrus thumbnail for %s returned HTTP %d", file_hash[:12], resp.status_code)
            return None
        return resp.content

    def file_id_for_hash(self, file_hash: str) -> Optional[int]:
        """Hydrus's own numeric id for a file it holds, or None.

        Needed because /get_files/file is addressed by file_id, not by
        hash (see get_file_path_url) - so anything that has only a hash
        and wants the actual bytes back out of Hydrus has to look the id
        up first. Identifiers only: the caller wants an id, not a full
        metadata payload.

        Returns None rather than raising for every "no id" case, since
        every one of them means the same thing to a caller: this file
        cannot be fetched. A hash Hydrus has never seen still comes back
        in the response with a null file_id (the same detail
        filter_known_hashes turns on), so a row's presence is not the
        answer.
        """
        if not file_hash:
            return None
        try:
            data = self._get("/get_files/file_metadata", {
                "hashes": _json.dumps([file_hash]),
                "only_return_identifiers": "true",
            })
        except HydrusError as exc:
            log.warning("file_id_for_hash failed for %s: %s", file_hash[:12], exc)
            return None
        for m in data.get("metadata", []):
            if (m.get("hash") or "").lower() != file_hash.lower():
                continue
            file_id = m.get("file_id")
            return int(file_id) if file_id is not None else None
        return None

    def get_file_path_url(self, file_id: int) -> str:
        """URL to fetch the raw file bytes/thumbnail from Hydrus for local use."""
        return f"{self.settings.api_url}/get_files/file?file_id={file_id}"

    def download_file(self, file_id: int, dest_path: str):
        try:
            resp = requests.get(
                self.get_file_path_url(file_id),
                headers=self._headers(),
                timeout=self.settings.timeout,
                verify=self.settings.verify_ssl,
                stream=True,
            )
        except requests.RequestException as exc:
            log.error("download_file %d failed: %s", file_id, exc)
            raise HydrusError(f"Could not download file {file_id}: {exc}") from exc
        if resp.status_code != 200:
            log.error("download_file %d -> HTTP %d", file_id, resp.status_code)
            raise HydrusError(f"Hydrus returned HTTP {resp.status_code} for file {file_id}")
        with open(dest_path, "wb") as fh:
            for chunk in resp.iter_content(65536):
                fh.write(chunk)

    def import_file(self, path: str) -> dict:
        """Uploads a file's raw bytes as the POST body. The Hydrus Client
        API's /add_files/add_file does NOT accept a multipart/form-data
        upload (an earlier version of this wrapper incorrectly sent one,
        which Hydrus rejects) - it wants either the file's bytes as the
        entire request body, or a JSON {"path": "..."} if Hydrus is running
        on this same machine and can read the path itself. We use the raw
        bytes upload since it works regardless of where Hydrus is running.
        """
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            log.error("import_file could not read %s: %s", path, exc)
            raise HydrusError(f"Could not read local file {path}: {exc}") from exc

        result = self._post(
            "/add_files/add_file",
            raw_data=data,
            extra_headers={"Content-Type": "application/octet-stream"},
        )
        # Hydrus returns HTTP 200 even for an unsuccessful import - the
        # actual result is in "status": 1/2=ok (imported/already had it),
        # 3=previously deleted, 4=failed, 7=vetoed by an import option.
        status = result.get("status")
        if status in (4, 7):
            note = result.get("note", "no details given")
            log.error("add_file rejected %s: status=%s note=%s", path, status, note)
            raise HydrusError(f"Hydrus rejected the file: {note}")
        return result

    def associate_url(self, url: str, file_hash: Optional[str] = None):
        """Tags a file we've already uploaded ourselves with its source URL.
        Does NOT download anything - see import_url() for that."""
        # Hydrus's Client API expects "url_to_add" here, not "url" - sending
        # the wrong key name is silently ignored by Hydrus's JSON parsing
        # and it reports "Did not find any URLs to add or delete!".
        body: Dict[str, Any] = {"url_to_add": url}
        if file_hash:
            body["hashes"] = [file_hash]
        return self._post("/add_urls/associate_url", body)

    def import_url(
        self, url: str, tags: Optional[List[str]] = None, service_key: Optional[str] = None,
        show_destination_page: bool = False, destination_page_name: Optional[str] = None,
    ) -> dict:
        """Hands the URL to Hydrus's own downloader system, exactly like
        pasting it into Hydrus's 'urls' import box - Hydrus fetches and
        imports the file itself using its own site parsers, rather than us
        uploading the file we already have. Useful for sites Hydrus already
        has a downloader for, and for grabbing a fresh copy from source.
        """
        body: dict = {"url": url, "show_destination_page": show_destination_page}
        if destination_page_name:
            body["destination_page_name"] = destination_page_name

        if tags:
            service_key = service_key or self.settings.tag_service_key or "my tags"
            resolved_key = self._resolve_to_service_key(service_key)
            if resolved_key:
                body["service_keys_to_additional_tags"] = {resolved_key: tags}
            else:
                # add_url's tag argument has no name-based fallback like
                # add_tags does - if we can't resolve a real key, import
                # without extra tags rather than fail the whole request.
                log.warning(
                    "import_url: could not resolve tag service %r to a real key, "
                    "importing without extra tags", service_key,
                )

        return self._post("/add_urls/add_url", body)

    def get_url_info(self, url: str) -> dict:
        """How Hydrus classifies a URL before anything is imported: its
        url_type (0 post, 2 file, 3 gallery, 4 watchable, 5 unknown), the
        url class that matched, and whether Hydrus has a parser for it."""
        return self._get("/add_urls/get_url_info", {"url": url})

    def get_url_files(self, url: str) -> dict:
        """Asks Hydrus what it actually knows about a URL - specifically
        whether a file has been imported for it yet. Unlike import_url()'s
        response (which only confirms the request was *accepted*, not that
        the download+import actually finished - Hydrus's downloader works
        asynchronously), this reflects real database state. The response's
        "url_file_statuses" list is empty while nothing has been imported
        yet, and gains an entry (with a real file hash) once it has."""
        return self._get("/add_urls/get_url_files", {"url": url})

    def _resolve_to_service_key(self, value: str) -> Optional[str]:
        """import_url's service_keys_to_additional_tags strictly requires a
        real hex service key (unlike add_tags, which accepts names too) -
        if the user configured a plain name, look it up via get_services()."""
        if _looks_like_service_key(value):
            return value
        try:
            for key, name in self.list_tag_services():
                if name.strip().lower() == value.strip().lower():
                    return key
        except HydrusError:
            pass
        return None

    def add_tags(self, file_hash: str, tags: List[str], service_key: Optional[str] = None):
        service_key = service_key or self.settings.tag_service_key
        if not service_key:
            service_key = "my tags"  # Hydrus's default local tag service name

        key_body = {"hash": file_hash, "service_keys_to_tags": {service_key: tags}}
        name_body = {"hash": file_hash, "service_names_to_tags": {service_key: tags}}

        # Try whichever field the value looks more likely to be first, then
        # automatically fall back to the other on a "not found"/"couldn't
        # parse" error. We can't tell key from name with certainty - even
        # Hydrus's own built-in local tag service has a short, unusual hex
        # key - so correctness comes from this fallback, not the guess.
        primary, fallback = (
            (key_body, name_body) if _looks_like_service_key(service_key) else (name_body, key_body)
        )

        try:
            return self._post("/add_tags/add_tags", primary)
        except HydrusError as first_exc:
            log.warning(
                "add_tags: first attempt for service %r failed (%s), retrying with the other field",
                service_key, first_exc,
            )
            try:
                return self._post("/add_tags/add_tags", fallback)
            except HydrusError as second_exc:
                log.error("add_tags: both attempts failed for service %r: %s", service_key, second_exc)
                raise second_exc from first_exc
