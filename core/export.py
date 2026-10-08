"""Writes the working list out as CSV or JSON.

Nothing left this app before except the matched-URL text log, which is
one line per found image and drops everything else. So a run over tens
of thousands of images produced results that could only be looked at,
never counted, scripted against, or compared with a later run - a
question as ordinary as "which sites did the not-founds come from" had
no answer short of reading the table by eye.

One schema, two renderers. FIELDS below is the single definition of what
an exported row contains, and both writers walk it, so the CSV and the
JSON can never come to disagree about what a column means or which ones
exist. Adding a field is one entry here.

CSV is for spreadsheets and JSON is for scripts, and the difference that
matters is tags. CSV joins them into one cell with ", " - the convention
every booru uses, readable in a spreadsheet, and safe for naive
line-based readers. That is lossy if a tag itself contains a comma,
which nothing here prevents for a tag someone typed by hand. JSON
carries tags as a real array, with each one's namespace and source kept
separate, and is therefore the lossless form. Anything that must not
lose a tag should read the JSON.

One caveat on "lossless", added with DAN-72: an export carries the same
tags a Hydrus send would, which means the `filter_sent_tags_by_source`
setting drops tag sources from both. That setting is off by default, so
the default export is still everything; when it is on, the point of it
is that one list decides what leaves this app, and an export quietly
disagreeing with the send would defeat that. `tag_count` counts the
same filtered list the `tags` field holds, so the two can't disagree.
"""
from __future__ import annotations

import csv
import io
import json
import time
from typing import Callable, Iterable, List, NamedTuple, Optional

from .models import ImageEntry, Tag, tags_to_send

# The format's own version, written into the JSON. A later build that
# changes what a field means can be told apart from this one by a script
# that kept an older export to diff against.
SCHEMA_VERSION = 1

CSV_TAG_SEPARATOR = ", "


def _sent_state(entry: ImageEntry) -> str:
    """The same three states the Sent column shows."""
    if not entry.sent_to_hydrus:
        return ""
    return "sent" if entry.hydrus_import_confirmed else "queued"


def _cache_state(entry: ImageEntry) -> str:
    """Which of the three the Cache column shows. Provisional wins: it
    says the result is deliberately unfinished, which matters more to
    anyone reading this than where it came from."""
    if entry.searched_without_saucenao:
        return "provisional"
    return entry.result_source or ""


# The two tag fields below are the only ones whose value depends on the
# settings, so FIELDS keeps its one-argument extractors and _row_values
# overrides just these two by key. TAG_FIELD_KEYS names them in one place
# so an extractor and its override can't drift apart unnoticed.
TAG_FIELD_KEYS = ("tags", "tag_count")


def _tag_strings(entry: ImageEntry) -> List[str]:
    return [t.display for t in entry.tags]


class Field(NamedTuple):
    key: str
    extract: Callable[[ImageEntry], object]


# Ordered as a reader wants them: what the file is, then what was found,
# then how good it is, then the tags, then the housekeeping.
FIELDS: tuple = (
    Field("filename", lambda e: e.filename),
    Field("path", lambda e: e.path),
    Field("status", lambda e: e.status.label),
    Field("matched_url", lambda e: e.matched_url or ""),
    Field("site", lambda e: e.booru_name or ""),
    Field("engine", lambda e: (e.selected_candidate.engine
                               if e.selected_candidate else "")),
    Field("similarity", lambda e: e.similarity if e.similarity is not None else ""),
    # Next to the number it qualifies: without it the column mixes real
    # comparisons with ascii2d/Google ordinal positions indistinguishably,
    # and a spreadsheet cannot filter what it cannot see.
    Field("similarity_measured",
          lambda e: e.similarity_measured if e.similarity is not None else ""),
    Field("local_width", lambda e: e.local_width or ""),
    Field("local_height", lambda e: e.local_height or ""),
    Field("match_width", lambda e: e.match_width or ""),
    Field("match_height", lambda e: e.match_height or ""),
    Field("tag_count", lambda e: len(e.tags)),
    Field("tags", _tag_strings),
    Field("candidate_count", lambda e: len(e.candidates)),
    Field("sent", _sent_state),
    Field("reviewed", lambda e: e.reviewed),
    Field("upscale_verdict", lambda e: e.upscale_verdict or ""),
    Field("cache", _cache_state),
    Field("error", lambda e: e.error_message or ""),
    Field("file_missing", lambda e: e.file_missing),
    Field("hydrus_hash", lambda e: e.hydrus_hash or ""),
)

HEADERS: tuple = tuple(f.key for f in FIELDS)


def _csv_value(value: object) -> str:
    """One field, rendered for a spreadsheet.

    Booleans become TRUE/FALSE rather than Python's True/False, which is
    what a spreadsheet recognises as a boolean rather than as text.
    """
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, list):
        return CSV_TAG_SEPARATOR.join(str(v) for v in value)
    return "" if value is None else str(value)


def _row_values(entry: ImageEntry, settings: Optional[object]) -> dict:
    """Every field of one row, keyed by field name, with the tag fields
    resolved against the settings. Both renderers go through here so the
    CSV and the JSON always agree on what a row contains."""
    values = {f.key: f.extract(entry) for f in FIELDS if f.key not in TAG_FIELD_KEYS}
    sent = _exported_tags(entry, settings)
    values["tags"] = [t.display for t in sent]
    values["tag_count"] = len(sent)
    return values


def _exported_tags(entry: ImageEntry, settings: Optional[object]) -> List[Tag]:
    """The tags an export carries - deliberately the same set a Hydrus
    send would carry. See the module docstring."""
    return tags_to_send(entry, settings)


def to_csv(entries: Iterable[ImageEntry], settings: Optional[object] = None) -> str:
    """The list as CSV, header row included.

    Written through the csv module rather than by joining strings, so a
    tag or a filename containing a comma or a quote is quoted properly
    instead of silently shifting every later column along by one.

    Line terminator is fixed to \\r\\n, which is what RFC 4180 asks for
    and what Excel expects; leaving it to the platform would produce a
    file that opens correctly on the machine that wrote it and nowhere
    else.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(HEADERS)
    for entry in entries:
        values = _row_values(entry, settings)
        writer.writerow([_csv_value(values[key]) for key in HEADERS])
    return buffer.getvalue()


def _json_record(entry: ImageEntry, settings: Optional[object] = None) -> dict:
    record = _row_values(entry, settings)
    # The lossless form: namespace and source stay separate fields rather
    # than being flattened into one "namespace:name" string that nothing
    # can reliably take apart again (a tag may itself contain a colon).
    record["tags"] = [
        {"name": t.name, "namespace": t.namespace, "source": t.source.value}
        for t in _exported_tags(entry, settings)
    ]
    return record


def to_json(entries: Iterable[ImageEntry], generated_at: Optional[float] = None,
            settings: Optional[object] = None) -> str:
    """The list as JSON: an object with the images under "images".

    An object rather than a bare array so the file can say when it was
    written and how many rows it holds - which is what makes two exports
    of the same library comparable, and what tells a reader a truncated
    file is truncated.
    """
    rows = [_json_record(e, settings) for e in entries]
    moment = time.time() if generated_at is None else generated_at
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(moment)),
        "count": len(rows),
        "images": rows,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def format_for_path(path: str) -> str:
    """"csv" or "json", chosen from the filename's extension.

    Defaults to CSV for anything unrecognised rather than refusing: the
    file dialog already puts a real extension on, and someone who typed
    "results.txt" meant a table, not an error.
    """
    return "json" if path.lower().endswith(".json") else "csv"


def render(entries: Iterable[ImageEntry], fmt: str,
           settings: Optional[object] = None) -> str:
    return (to_json(entries, settings=settings) if fmt == "json"
            else to_csv(entries, settings))
