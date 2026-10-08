"""The image table's right-click menu.

Two things about the shape of this, both of which it used to get wrong by
being one 207-line method on MainWindow.

The menu was built as a pile of local action variables and then dispatched
by a seventeen-branch `elif chosen == some_action` chain at the bottom.
Adding an item meant remembering to add a branch, and forgetting produced
a menu entry that appeared, enabled, and did nothing at all when clicked -
with no error anywhere. Here each item carries its handler, so an item
that exists is an item that works, and there is a test asserting exactly
that over the whole menu.

The labels are the other half. Nearly every entry reads differently for
one row than for several ("Reset result" against "Reset results on 12
selected images"), which is pure text logic that needed a window, a table
and a selection to reach. It is a plain function here.
"""
from __future__ import annotations

from typing import Callable, Dict, List, NamedTuple, Optional

from PyQt6.QtWidgets import QMenu

from core.models import MatchStatus


class MenuItem(NamedTuple):
    """One entry: what it says, what it does, and whether it can be used."""
    label: str
    handler: Callable[..., object]
    enabled: bool = True
    tooltip: str = ""


def count_label(count: int, singular: str, plural: str) -> str:
    """"Reset result" for one row, "Reset results on 12 selected images"
    for more. `plural` takes {n}.

    A menu that says "Delete from Hydrus…" while twelve rows are selected
    is how someone deletes twelve files meaning to delete one, so the
    count is in the label wherever an action is destructive or expensive.
    """
    return singular if count == 1 else plural.format(n=count)


# The engine sub-menu. Each is a fresh search through that engine alone,
# ignoring the configured primary/fallback order, bypassing the cache.
ENGINE_ITEMS = (
    ("iqdb", "IQDB only",
     "Searches IQDB and nothing else. IQDB has no daily quota, so this is the one to "
     "use when SauceNAO's allowance is spent."),
    ("saucenao", "SauceNAO only", "Searches SauceNAO and nothing else."),
    None,   # separator
    ("ascii2d", "ascii2d only",
     "Colour and feature search. Often finds Pixiv and Twitter posts IQDB misses. "
     "Does not report a real similarity percentage."),
    ("tracemoe", "trace.moe only",
     "Anime-screenshot search: which series, episode, and timestamp. "
     "Anonymous use has a monthly quota - leave this for actual screenshots."),
    ("iqdb3d", "IQDB 3D only",
     "3d.iqdb.org - the 3D/CG index, same protocol as IQDB."),
    ("googleimages", "Google Images only",
     "Reverse-searches the whole web rather than a booru index. Worth trying on an "
     "image the others cannot place: it finds the page a picture was posted on, "
     "though it brings back no tags. Needs the Cloud Vision API key from "
     "Settings > Engine."),
    ("pawchive", "Pawchive only",
     "Looks this exact file up on pawchive.pw by its SHA-256 - Patreon and Fanbox "
     "posts that SauceNAO and IQDB don't index. Only finds identical copies, and "
     "sends the file's hash, not the picture."),
    ("pawchiveindex", "Pawchive index only",
     "Searches the local index of pawchive artists (Files > Pawchive Index) for this "
     "picture, including resized or re-saved copies. Local - nothing is sent."),
    ("googlelens", "Google Lens only",
     "The reverse image search from images.google.com, read out of a real browser. "
     "Usually the best of these on art nothing else can place - and the slowest, "
     "since every image is a page load. Needs the optional PyQt6-WebEngine package."),
    ("yandex", "Yandex only",
     "yandex.com's reverse image search. Finds rule34.us, Danbooru and Xbooru posts the "
     "others miss, and keeps only posts on sites the app can read tags from. Uploads "
     "the picture to Yandex."),
)

# "Not sent yet" is the selection you actually want before a batch send,
# and it can't be expressed as a MatchStatus - an image is Found (good)
# whether or not it has been sent.
SENT_STATES = (
    ("Not sent to Hydrus", lambda e: not e.sent_to_hydrus),
    ("Queued (unconfirmed)", lambda e: e.sent_to_hydrus and not e.hydrus_import_confirmed),
    ("Sent to Hydrus", lambda e: e.sent_to_hydrus and e.hydrus_import_confirmed),
    ("Missing from disk", lambda e: e.file_missing),
)


# Two decisions and one absence. "Needs review" is the selection that
# makes a long pass resumable: it is what "Not sent to Hydrus" cannot
# express, since that collects rows already judged and rows never opened
# into one indistinguishable pile.
REVIEW_STATES = (
    ("Needs review", lambda e: e.needs_review),
    ("Reviewed (kept the local file)", lambda e: e.reviewed),
    ("Decided (reviewed or sent)", lambda e: not e.needs_review),
)


def review_state_items(window) -> List[MenuItem]:
    items = []
    for label, predicate in REVIEW_STATES:
        matching = sum(1 for e in window.entries if predicate(e))
        items.append(MenuItem(
            label=f"{label} ({matching})",
            handler=lambda desc=label, p=predicate: window._select_rows_by_predicate(desc, p),
            enabled=matching > 0,
        ))
    return items


def selection_items(window) -> List[MenuItem]:
    """The "Select by …" entries, which are offered whether or not
    anything is selected - they are how you make a selection."""
    items = []
    for status in sorted(MatchStatus, key=lambda s: s.sort_rank):
        matching = sum(1 for e in window.entries if e.status == status)
        items.append(MenuItem(
            label=f"{status.label} ({matching})",
            handler=lambda s=status: window._select_rows_by_status(s),
            enabled=matching > 0,
        ))
    return items


# Selecting by what the Cache column says. Provisional is the one that
# matters: those results are unfinished by design and need running again
# once SauceNAO's allowance resets - a result nobody can find twice is no
# better than a cached one.
CACHE_STATES = (
    ("Provisional (searched without SauceNAO)",
     lambda e: getattr(e, "searched_without_saucenao", False)),
    ("From the cache", lambda e: e.result_source == "cached"
     and not getattr(e, "searched_without_saucenao", False)),
    ("Freshly searched", lambda e: e.result_source == "fresh"
     and not getattr(e, "searched_without_saucenao", False)),
)


def cache_state_items(window) -> List[MenuItem]:
    items = []
    for label, predicate in CACHE_STATES:
        matching = sum(1 for e in window.entries if predicate(e))
        items.append(MenuItem(
            label=f"{label} ({matching})",
            handler=lambda desc=label, p=predicate: window._select_rows_by_predicate(desc, p),
            enabled=matching > 0,
        ))
    return items


def sent_state_items(window) -> List[MenuItem]:
    items = []
    for label, predicate in SENT_STATES:
        matching = sum(1 for e in window.entries if predicate(e))
        items.append(MenuItem(
            label=f"{label} ({matching})",
            handler=lambda desc=label, p=predicate: window._select_rows_by_predicate(desc, p),
            enabled=matching > 0,
        ))
    return items


def _all_reviewed(entries) -> bool:
    """Whether the whole selection is already marked.

    Decides which way the one menu entry acts. A mixed selection counts
    as not-all-reviewed, so the entry marks rather than unmarks - marking
    is the common intent, and it is the reversible direction of the two.
    """
    return bool(entries) and all(e.reviewed for e in entries)


def _review_mark_label(entries, count: int) -> str:
    if _all_reviewed(entries):
        return count_label(count, "Unmark as reviewed",
                           "Unmark {n} selected images as reviewed")
    return count_label(count, "Mark as reviewed",
                       "Mark {n} selected images as reviewed")


def row_items(window, rows) -> List[Optional[MenuItem]]:
    """Everything that acts on the selected rows. None is a separator.

    `window` is passed explicitly rather than these living on it: the
    seventeen methods below are genuinely MainWindow's, and naming them
    here in one list makes that coupling visible instead of spread through
    a 200-line method.
    """
    count = len(rows)
    # Resolved once, right here, rather than left as row numbers for the
    # handlers to re-resolve later: exec() below runs a nested Qt event
    # loop that keeps pumping queued cross-thread signals while the menu
    # is open, and a background removal (settings.remove_after_import)
    # can shift every row below it up by one before the click lands. A
    # handler re-resolving a row number after that acts on whatever slid
    # into it - not on what the user actually selected. Entry objects
    # don't shift.
    entries = list(window.table_model.entries_at(r.row() for r in rows))
    resettable = sum(1 for e in entries if e.has_result())
    first_entry = window.table_model.entry_at(rows[0].row()) if rows else None

    items: List[Optional[MenuItem]] = [
        MenuItem(
            count_label(count, "Re-search selected image", "Re-search {n} selected images"),
            lambda: window._research_rows(entries),
        ),
        MenuItem(
            count_label(count, "Reset result", "Reset results on {n} selected images"),
            lambda: window._reset_rows(entries),
            enabled=resettable > 0,
            tooltip=(
                "Puts these back to Not searched, discarding the match, its tags and any "
                "error - as though they had just been added.\n\n"
                "Tags you typed yourself and tags Hydrus already holds are kept; only the "
                "ones this search brought in are dropped. Whether a file has been sent to "
                "Hydrus is left alone too, since that stays true either way.\n\n"
                "The saved cache entry is dropped as well, so the next search really does "
                "search rather than handing back the result being discarded here.\n\n"
                "Nothing on disk, and nothing in Hydrus, is touched."
            ),
        ),
        None,
        MenuItem(
            "Send File + URL + Tags to Hydrus (upload)",
            window.action_send_to_hydrus,
            tooltip="Uploads the local file, associates the matched URL with it, "
                    "and adds the tag list.",
        ),
        MenuItem(
            "Send URL to Hydrus's URL Importer…",
            window.action_import_url_to_hydrus,
            tooltip="Hands the matched URL to Hydrus's own downloader, which fetches "
                    "and imports the file itself - like pasting it into Hydrus's URL box.",
        ),
        MenuItem(
            "Download Matched Image + Send to Hydrus with Tags",
            window.action_download_and_send_to_hydrus,
            tooltip="Downloads the actual full-resolution matched image ourselves (not your "
                    "local file, not through Hydrus's downloader) and uploads that to Hydrus "
                    "with the URL and tags already attached.",
        ),
        None,
        MenuItem(
            _review_mark_label(entries, count),
            lambda: window._set_rows_reviewed(entries, not _all_reviewed(entries)),
            tooltip=(
                "Records that you have looked at these and decided against sending "
                "them - the local copy is better, or the match is wrong.\n\n"
                "Nothing is sent, changed on disk, or touched in Hydrus. It only "
                "takes the row out of \"Needs review\", so a pass over a large "
                "library can be put down and picked up without hunting for your "
                "place: press N, or use Select by Review State.\n\n"
                "Resetting a result clears the mark, since the decision was about "
                "the match being discarded."
            ),
        ),
        None,
        MenuItem(
            "Compare with Match…",
            lambda: window._open_compare_dialog(first_entry),
            enabled=count == 1,
            tooltip="Opens the local file and its match side by side with a wipe slider, "
                    "fetching the match at full resolution rather than the downscaled "
                    "preview, and reports whether they're actually the same image.",
        ),
        None,
        MenuItem(
            count_label(count, "Check for Upscaling", "Check {n} images for Upscaling"),
            lambda: window._check_rows_for_upscaling(entries),
            tooltip="Checks image metadata for known AI upscaler tool names (reliable when "
                    "present), compares against the matched source's size, and tests whether "
                    "a downscale+re-upscale round-trip closely reconstructs the image (a "
                    "heuristic - can't reliably detect AI-based upscaling on its own).",
        ),
        None,
        MenuItem(
            count_label(count, "Show in Hydrus", "Show {n} selected images in Hydrus"),
            lambda: window._show_rows_in_hydrus(entries),
            enabled=any(e.hydrus_hash for e in entries),
            tooltip="Opens a new page in the Hydrus client showing these files, and "
                    "switches Hydrus to it.\n\n"
                    "The opposite direction to everything else here: instead of sending "
                    "a result to Hydrus, it jumps to the copy Hydrus already holds, so "
                    "you can check it against its real tags, duplicates and ratings.\n\n"
                    "Only works for files Hydrus actually has, and needs the \"manage "
                    "pages\" permission on your access key - which nothing else here "
                    "uses, so it may need adding.",
        ),
        None,
        MenuItem(
            "Check Match Availability (remove dead links)",
            lambda: window._check_match_availability(first_entry),
            enabled=count == 1,
            tooltip="Checks whether each matched site still has the content, and offers to "
                    "drop any that are definitely gone (deleted/removed posts) from this "
                    "image's match list. Only removes matches returning a definite 404/410 - "
                    "anything unreachable or merely blocked stays, since that doesn't mean "
                    "the post is gone. Works on one image at a time.",
        ),
        None,
        MenuItem(
            count_label(count, "Export this image…",
                        "Export {n} selected images…"),
            lambda: window.action_export_results(entries),
            tooltip="Writes just these rows out as CSV or JSON. Files > Export Results "
                    "does the whole list.",
        ),
        MenuItem(
            count_label(count, "Write Tag File beside this image…",
                        "Write Tag Files beside {n} selected images…"),
            lambda: window.action_write_tag_files(entries),
            tooltip="Writes each image's tags to a text file next to the image itself, "
                    "one tag per line - what other taggers and Hydrus's own sidecar "
                    "importer read.\n\n"
                    "The only thing in this menu that writes into your picture folders. "
                    "It shows you what it is about to write and never replaces a text "
                    "file already there without asking. Files > Write Tag Files does "
                    "the whole list.",
        ),
        None,
        MenuItem(
            count_label(count, "Delete from Hydrus…", "Delete {n} files from Hydrus…"),
            lambda: window._delete_rows_from_hydrus(entries),
            tooltip="Tells Hydrus to delete these files, which is the only correct way to get "
                    "rid of something living in Hydrus's own store.\n\n"
                    "Deleting the file off disk instead leaves Hydrus's record behind, so it "
                    "goes on believing it holds a file that isn't there - a worse state than "
                    "either keeping it or deleting it properly.\n\n"
                    "Hydrus moves them to its trash, so this is undoable from Hydrus itself "
                    "until the trash is emptied.",
        ),
        MenuItem(
            count_label(count, "Remove selected file", "Remove {n} selected files"),
            lambda: window._remove_rows(entries),
            tooltip="Takes them out of THIS list only. The files, and Hydrus, are untouched.",
        ),
    ]
    return items


def engine_items(window, rows) -> List[Optional[MenuItem]]:
    count = len(rows)
    # Resolved once, at build time - see the comment in row_items() for why.
    entries = list(window.table_model.entries_at(r.row() for r in rows))
    items: List[Optional[MenuItem]] = []
    for spec in ENGINE_ITEMS:
        if spec is None:
            items.append(None)
            continue
        engine, label, tooltip = spec
        items.append(MenuItem(
            label,
            lambda e=engine: window._research_rows_with_engine(entries, e),
            tooltip=tooltip,
        ))
    items.append(None)
    items.append(MenuItem(
        count_label(count, "Search with Opposite Engine",
                    "Search {n} images with Opposite Engine"),
        lambda: window._research_rows_with_opposite_engine(entries),
        tooltip="Per row, uses whichever engine did NOT find that row's current match - so a "
                "SauceNAO match gets retried on IQDB and vice versa. Rows with no match yet "
                "fall back to whichever engine isn't your configured primary.",
    ))
    return items


def _add(menu: Optional[QMenu], items, handlers: Dict) -> None:
    """Adds items to a menu, recording each one's handler.

    The handler map is what replaces the old dispatch chain: an action
    that reaches the menu necessarily has something to run.

    `menu` is Optional because Qt types addMenu() that way; nothing to add
    to is nothing to do.
    """
    if menu is None:
        return
    for item in items:
        if item is None:
            menu.addSeparator()
            continue
        action = menu.addAction(item.label)
        action.setEnabled(item.enabled)
        if item.tooltip:
            action.setToolTip(item.tooltip)
        handlers[action] = item.handler


def build(window, rows) -> tuple:
    """The menu for the current selection, and its action -> handler map."""
    menu = QMenu(window)
    handlers: Dict = {}

    _add(menu.addMenu("Select by Status"), selection_items(window), handlers)
    _add(menu.addMenu("Select by Sent State"), sent_state_items(window), handlers)
    _add(menu.addMenu("Select by Cache State"), cache_state_items(window), handlers)
    _add(menu.addMenu("Select by Review State"), review_state_items(window), handlers)

    if not rows:
        # Nothing selected: the "select by" entries above are the only
        # things that can do anything.
        return menu, handlers

    menu.addSeparator()
    items = row_items(window, rows)
    # The engine sub-menu sits after Re-search/Reset, which are the first
    # two entries.
    _add(menu, items[:2], handlers)
    engine_menu = menu.addMenu("Search with a specific engine")
    if engine_menu is not None:
        engine_menu.setToolTip(
            "Runs a fresh search using only the engine you pick, ignoring your configured "
            "primary/fallback order for this one search. Results bypass the cache."
        )
    _add(engine_menu, engine_items(window, rows), handlers)
    _add(menu, items[2:], handlers)
    return menu, handlers


def show(window, pos) -> None:
    """Builds the menu, shows it, and runs whatever was picked."""
    rows = window.table.selectionModel().selectedRows()
    menu, handlers = build(window, rows)
    chosen = menu.exec(window.table.viewport().mapToGlobal(pos))
    handler = handlers.get(chosen)
    if handler is not None:
        handler()
