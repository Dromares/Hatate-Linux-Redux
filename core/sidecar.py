"""Tag sidecar files: the tag list written as a text file next to the image.

Everything this app found used to leave it in one of two directions -
into a Hydrus library, or into a CSV/JSON report that describes the run
rather than the pictures. Neither helps the far more ordinary case of a
folder of images that some *other* tool is going to read: a training-set
script, a different tagger, Hydrus's own sidecar importer. All of those
read the same thing, which is a plain text file beside the image with one
tag per line. This writes that.

Two conventions exist for the name and both are in real use, so the style
is a setting rather than a guess:

  * `append`  - `cat.jpg` -> `cat.jpg.txt`. What Hydrus's sidecar importer
    looks for by default, and the safer of the two: the name contains the
    image's own extension, so it cannot collide with a file belonging to
    anything else, and two images that differ only in extension
    (`cat.jpg`, `cat.png`) get one sidecar each instead of fighting over
    one. This is the default for those two reasons.
  * `replace` - `cat.jpg` -> `cat.txt`. What the stable-diffusion training
    tooling expects. Available because that tooling will not read the
    other form, but it is the form that can land on a pre-existing
    `cat.txt` that has nothing to do with this app.

Which tags get written is not decided here: `tags_to_send` is, and always
has to be, the single answer to "what leaves this app". A sidecar that
disagreed with what the same run put in Hydrus would be worse than no
sidecar, because both look authoritative and only one can be right.

This is the first thing the app writes into the user's own picture
folders, and that shapes the split below. `plan()` touches no disk except
to ask whether each target already exists; it returns exactly what would
be written and where, so a caller can show that - and the count of files
it would clobber - and get an answer before anything is created.
`write()` then does only what the plan says.
"""
from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from .models import ImageEntry, tags_to_send

log = logging.getLogger(__name__)

DEFAULT_EXTENSION = ".txt"

STYLE_APPEND = "append"    # cat.jpg -> cat.jpg.txt
STYLE_REPLACE = "replace"  # cat.jpg -> cat.txt
STYLES = (STYLE_APPEND, STYLE_REPLACE)

OVERWRITE_ASK = "ask"
OVERWRITE_SKIP = "skip"
OVERWRITE_OVERWRITE = "overwrite"
OVERWRITE_POLICIES = (OVERWRITE_ASK, OVERWRITE_SKIP, OVERWRITE_OVERWRITE)


def style_of(settings: Optional[object]) -> str:
    """The naming style a settings object asks for.

    An unrecognised value falls back to the default rather than raising:
    this is a string in a JSON file that nothing validates on load, and a
    typo there should cost the user the less-common naming convention,
    not the feature.
    """
    style = getattr(settings, "sidecar_filename_style", None)
    return style if style in STYLES else STYLE_APPEND


def overwrite_policy_of(settings: Optional[object]) -> str:
    """The overwrite policy a settings object asks for, same leniency."""
    policy = getattr(settings, "sidecar_overwrite", None)
    return policy if policy in OVERWRITE_POLICIES else OVERWRITE_ASK


def sidecar_path(image_path: str, style: str = STYLE_APPEND,
                 extension: str = DEFAULT_EXTENSION) -> Path:
    """Where the sidecar for `image_path` goes."""
    path = Path(image_path)
    if style == STYLE_REPLACE:
        return path.with_suffix(extension)
    return path.with_name(path.name + extension)


def render(entry: ImageEntry, settings: Optional[object] = None) -> str:
    """The file's contents: one tag per line, newline-terminated.

    Duplicates are dropped, keeping the first occurrence. The same tag
    genuinely can arrive from two sources - a booru page and the copy
    already on the file in Hydrus - and a set of tags is what the reader
    on the other end is going to build anyway, so writing it twice only
    makes the file look wrong.

    The order is the entry's own, not sorted. It groups tags by where
    they came from, which is the order the tag list shows and the order
    a send uses, and re-sorting here would be this module inventing a
    view of the data that nothing else in the app holds.
    """
    seen = dict.fromkeys(t.display for t in tags_to_send(entry, settings))
    lines = [t for t in seen if t]
    return "".join(f"{line}\n" for line in lines)


@dataclass(frozen=True)
class Planned:
    """One sidecar that would be written."""
    entry: ImageEntry
    path: Path
    body: str
    exists: bool


@dataclass(frozen=True)
class Plan:
    """What `write()` would do, and what it would refuse to do.

    The three "not written" lists are kept apart because they are three
    different things to tell a user: nothing to say, nothing to say it
    about, and a file of yours is in the way.
    """
    writes: List[Planned] = field(default_factory=list)
    without_tags: List[ImageEntry] = field(default_factory=list)
    missing_files: List[ImageEntry] = field(default_factory=list)

    @property
    def existing(self) -> List[Planned]:
        """The planned writes whose target is already on disk."""
        return [p for p in self.writes if p.exists]

    @property
    def directories(self) -> List[Path]:
        """Every folder this plan would write into, in first-seen order -
        what a confirmation prompt needs to name."""
        return list(dict.fromkeys(p.path.parent for p in self.writes))


def plan(entries: Iterable[ImageEntry], settings: Optional[object] = None,
         extension: str = DEFAULT_EXTENSION) -> Plan:
    """Work out every sidecar for `entries` without writing anything.

    An entry with no tags to write is not a sidecar with no lines in it:
    an empty file states that the image has no tags, which for an image
    this app has not searched yet is a claim it cannot make. It is
    reported as skipped instead.

    An entry whose file has gone missing is skipped too - the whole point
    of the file is that it sits beside the image, and beside nothing it is
    litter in a folder the user may have finished with.
    """
    result = Plan()
    style = style_of(settings)
    for entry in entries:
        if getattr(entry, "file_missing", False):
            result.missing_files.append(entry)
            continue
        body = render(entry, settings)
        if not body:
            result.without_tags.append(entry)
            continue
        target = sidecar_path(entry.path, style, extension)
        result.writes.append(
            Planned(entry=entry, path=target, body=body, exists=target.exists())
        )
    return result


@dataclass(frozen=True)
class Result:
    """What `write()` actually did."""
    written: List[Path] = field(default_factory=list)
    skipped_existing: List[Path] = field(default_factory=list)
    failures: List[Tuple[Path, str]] = field(default_factory=list)


def _write_new(target: Path, body: str) -> bool:
    """Create `target` only if it does not exist. False if it did.

    O_EXCL rather than a Path.exists() check: the check-then-write
    version can still clobber a file created in the gap between the two,
    and "never overwrite" is the promise this whole module is built
    around. The plan's `exists` flag is for telling the user in advance;
    this is what actually enforces it.
    """
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(body)
    return True


def _write_over(target: Path, body: str) -> None:
    """Replace `target`, atomically.

    Written to a temporary file in the same directory and renamed over
    the target, so an error or a kill part-way through leaves the old
    sidecar intact rather than a truncated one. Same directory because
    os.replace is only atomic within a filesystem, and these folders are
    often network shares.
    """
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", dir=target.parent,
        prefix=f".{target.name}.", suffix=".tmp", delete=False,
    )
    try:
        with handle:
            handle.write(body)
        os.chmod(handle.name, 0o644)  # NamedTemporaryFile creates 0600
        os.replace(handle.name, target)
    except OSError:
        try:
            os.unlink(handle.name)
        except OSError:  # pragma: no cover - nothing useful left to do
            pass
        raise


def write(sidecars: Iterable[Planned], overwrite: bool = False) -> Result:
    """Write the planned sidecars. Returns what happened to each.

    `overwrite=False` leaves an existing file exactly as it was and
    reports it; True replaces it. There is no third mode, because the
    decision belongs to whoever showed the plan.

    One unwritable file does not abandon the rest: a single read-only
    folder in a batch of thousands would otherwise lose every sidecar
    after it, and the caller can only report what it is told.
    """
    result = Result()
    for item in sidecars:
        try:
            if overwrite:
                _write_over(item.path, item.body)
                result.written.append(item.path)
            elif _write_new(item.path, item.body):
                result.written.append(item.path)
            else:
                result.skipped_existing.append(item.path)
        except OSError as exc:
            log.error("Could not write tag sidecar %s: %s", item.path, exc)
            result.failures.append((item.path, str(exc)))
    if result.written:
        log.info("Wrote %d tag sidecar file(s)", len(result.written))
    return result
