"""User-configurable tag rules: namespace remapping, blacklist filtering,
and the tags this app applies itself based on how a search turned out.

Namespace remapping
-------------------
Different sites use different namespace conventions for the same kind of
tag - most notably, booru sites (Danbooru, Gelbooru, e621, ...) use
"artist:" for the creator of a piece, while Hydrus's community Public Tag
Repository convention uses "creator:" instead. This lets the user rewrite
namespaces to match whatever convention they actually use in Hydrus,
rather than ending up with a mix of both across their library.

A rule can also map a namespace to an empty target, meaning "strip this
namespace entirely" - e.g. a rule of general -> "" turns
"general:bikini_top" into plain "bikini_top", since some PTR-style
tagging conventions don't use a "general:" namespace at all for common
descriptive tags.

Applied once, at the point tags are first pulled from a booru page or
search engine (see core/search_engine.py), and also to tags Hydrus
already has when auto-imported, and to manual inline tag edits - not to
newly-typed tags via "Add tags", which are treated as deliberate exact
input.

Blacklist filtering
-------------------
Boorus attach a lot of housekeeping tags that are noise in a personal
library - highres, commentary_request, bad_id, tagme, translated. The
blacklist drops those as they come in, so they never reach the tag list
or get sent to Hydrus.

Pattern rules, deliberately chosen to match how people actually think
about tags rather than how they're stored:

- A pattern with NO colon matches the tag's NAME, whatever its
  namespace. "highres" therefore blocks both `highres` and
  `meta:highres` - a user writing "highres" means the tag, not one
  particular namespaced spelling of it.
- A pattern WITH a colon matches the full "namespace:name" form, so
  "meta:highres" blocks only the namespaced one, and "meta:*" blocks
  that namespace wholesale. An unnamespaced tag can never match a
  colon pattern, since its display form has no colon to match against.
- `*` and `?` wildcards work in either form (`bad_*`, `*_request`).
- Matching ignores case, and treats spaces and underscores as the same
  character. That last one matters in practice: boorus write
  `bad_id` while Hydrus displays `bad id`, so a user copying a tag out
  of Hydrus would otherwise write a pattern that silently never fires.

Where this deliberately does NOT apply:

- Tags Hydrus already has on a file. Those are the current state of the
  library, and filtering them would misrepresent it - the tag would
  vanish from the panel while still sitting in Hydrus, and this app has
  no way to remove it there. Better to show the truth.
- Anything the user types themselves ("Add tags", inline edits).
  Silently deleting a tag someone deliberately typed is hostile; if
  they typed it, they meant it.

The rating tag
--------------
A matched booru post usually carries an age rating, and some users want
it in Hydrus as "rating:explicit". It is off by default, since it puts a
tag in the library that was never there before. When on, it is added to
the incoming list ahead of everything below, so it is an ordinary booru
tag from that point: blacklistable ("rating:*"), remappable, and coloured
like any other namespace. Only sites that STATE a rating contribute one -
see core/boorus/_rating.py.

Tags applied by rule
--------------------
Three configurable lists of tags the app adds itself according to how a
search turned out - found, not found, or found with few tags - so that
"everything this program could not place" becomes a tag you can search
your library for rather than a row colour. All three are empty by
default. See the block comment above OUTCOME_TAG_SOURCE below for why
that matters and how these interact with the tag-source filter.

Order of operations: namespace remapping runs FIRST, then the blacklist,
so patterns match the tag in its final displayed form - what you see in
the tag panel is what you write a pattern against. The consequence worth
knowing: if you remap artist -> creator, a blacklist pattern of
"artist:*" will never fire, because by the time the blacklist sees the
tag it is already "creator:...". Write "creator:*" instead.
"""
from __future__ import annotations

import fnmatch
from typing import List, NamedTuple, Optional, Tuple

from .applog import get_logger
from .config import Settings
from .models import ImageEntry, MatchStatus, Tag, TagSource

log = get_logger("tag_rules")


def apply_namespace_remap(tags: List[Tag], settings: Settings) -> List[Tag]:
    """Returns a new list with each tag's namespace rewritten according to
    settings.tag_namespace_remap, if enabled. Tags with no namespace, or
    a namespace not in the remap table, pass through unchanged. A rule
    mapping to an empty string strips the namespace entirely (the tag
    becomes unnamespaced)."""
    if not settings.enable_tag_namespace_remap or not settings.tag_namespace_remap:
        return tags

    remap = settings.tag_namespace_remap
    result = []
    for t in tags:
        if t.namespace and t.namespace in remap:
            new_namespace = remap[t.namespace] or None  # "" (strip) -> no namespace
            result.append(Tag(name=t.name, source=t.source, namespace=new_namespace))
        else:
            result.append(t)
    return result


DEFAULT_RATING_NAMESPACE = "rating"


def with_rating_tag(tags: List[Tag], rating: Optional[str], settings: Settings) -> List[Tag]:
    """Returns the list with the matched post's rating added as one
    namespaced tag, when the user has asked for it and the site actually
    stated one.

    Added BEFORE the remap and the blacklist run, deliberately: that is
    what makes it an ordinary booru tag from there on - "rating:*" in the
    blacklist drops it, and a remap rule renames its namespace, with no
    special case anywhere downstream.

    Returns the original list unchanged when the setting is off (the
    default) or the site said nothing, so the common path costs nothing
    and adds nothing.
    """
    if not settings.add_rating_tag or not rating:
        return tags

    namespace = (settings.rating_tag_namespace or "").strip() or DEFAULT_RATING_NAMESPACE
    return list(tags) + [Tag(name=rating, source=TagSource.BOORU, namespace=namespace)]


def _normalize(text: str) -> str:
    """Lowercases and treats spaces/underscores as equivalent, so a pattern
    copied out of Hydrus ("bad id") still matches a booru's own spelling
    ("bad_id") and vice versa."""
    return text.strip().lower().replace(" ", "_")


def is_tag_blacklisted(tag: Tag, patterns: List[str]) -> bool:
    """Whether a single tag matches any blacklist pattern. See the module
    docstring for the matching rules."""
    name = _normalize(tag.name)
    display = _normalize(tag.display)

    for raw_pattern in patterns:
        pattern = _normalize(raw_pattern)
        if not pattern:
            continue  # blank lines in the settings box
        # A colon in the pattern means the user is being specific about
        # the namespace, so match the full "namespace:name" form. Without
        # one, match the bare name and let it hit in any namespace.
        target = display if ":" in pattern else name
        if fnmatch.fnmatchcase(target, pattern):
            return True
    return False


def apply_tag_blacklist(tags: List[Tag], settings: Settings) -> List[Tag]:
    """Returns a new list with blacklisted tags removed. Returns the
    original list unchanged when the feature is off or no patterns are
    configured, so the common case costs nothing."""
    if not settings.enable_tag_blacklist or not settings.tag_blacklist:
        return tags

    patterns = settings.tag_blacklist
    kept = [t for t in tags if not is_tag_blacklisted(t, patterns)]

    dropped = len(tags) - len(kept)
    if dropped:
        log.debug("Tag blacklist dropped %d of %d incoming tag(s)", dropped, len(tags))
    return kept


def split_tag_text(text: str) -> Tuple[Optional[str], str]:
    """"artist:someone" -> ("artist", "someone"); "someone" -> (None, "someone").

    Splits on the FIRST colon only, so a name that itself contains one
    ("series:Re:Zero") keeps the rest intact. An empty namespace side
    ("​:name") is no namespace rather than an empty-string one, matching
    how "no namespace" is represented everywhere else.
    """
    if ":" in text:
        namespace, name = text.split(":", 1)
        return (namespace.strip() or None), name.strip()
    return None, text.strip()


class InlineEditResult(NamedTuple):
    tags: List[Tag]           # the entry's tag list after the edit
    action: str               # "removed" | "renamed"
    dropped_duplicate: bool   # an older tag the rename collided with went


def apply_inline_edit(
    tags: List[Tag], target: Tag, new_text: str, settings: Settings,
) -> InlineEditResult:
    """Applies an inline edit of one tag, and reports what happened.

    Clearing the text deletes the tag. Otherwise it is renamed in place,
    keeping its original source, with the same namespace remap rules that
    freshly-searched tags get - so typing "artist:someone" by hand becomes
    "creator:someone" too when that rule is on.

    If the rename collides with a tag the entry already has, the OLDER
    duplicate is dropped rather than leaving two identical tags.

    The target is mutated rather than replaced, deliberately: the widget
    holds a reference to this exact object, and swapping in a new one
    would leave the row pointing at a tag no longer in the list.
    """
    if not new_text.strip():
        return InlineEditResult([t for t in tags if t is not target], "removed", False)

    namespace, name = split_tag_text(new_text)
    remapped = apply_namespace_remap(
        [Tag(name=name, source=target.source, namespace=namespace)], settings,
    )[0]

    remaining = tags
    duplicate = next(
        (t for t in tags if t is not target and t.key() == (remapped.namespace, remapped.name)),
        None,
    )
    if duplicate is not None:
        remaining = [t for t in tags if t is not duplicate]

    target.namespace = remapped.namespace
    target.name = remapped.name
    return InlineEditResult(remaining, "renamed", duplicate is not None)


# --- Tags applied by rule (DAN-77) -----------------------------------
#
# Three lists of tags the app adds itself, chosen by how a search turned
# out: one for a match, one for no match, one for a match that came back
# with few tags. The point is a library you can search: `hatate:not
# found` as a real tag collects every image this program could not place,
# which is the set you would want to hand to a different engine or work
# through by hand, and which otherwise exists only as a row colour in a
# window you have to keep open.
#
# All three default to empty, so the feature does nothing at all until
# somebody configures it. That is deliberate and not just caution about
# defaults: these are tags no site put on the image and nobody typed, so
# writing any of them into a Hydrus library uninvited would be putting
# the app's own bookkeeping in among the user's data.
#
# They are written under TagSource.HATATE ("Hatate-linux"), which nothing
# else in the app produces. Two things follow from that, both of which
# the rule relies on:
#
#   * Replacing that source wholesale on each application makes the rule
#     idempotent for free. Re-searching an image that was not found and
#     now is drops `hatate:not found` and adds the found tags, instead of
#     leaving the image carrying both and claiming each.
#   * The tag-source filter from DAN-72 can keep them out of Hydrus
#     entirely - untick "Hatate-linux" with "also apply these sources to
#     tags sent" on, and they stay local to this app. That switch landing
#     first was the precondition for this feature existing.

#: The source every by-rule tag is written under. Nothing else in the app
#: writes this source; see the note above for what depends on that.
OUTCOME_TAG_SOURCE = TagSource.HATATE


def countable_tags(tags: List[Tag]) -> List[Tag]:
    """The tags that count towards `min_tags_for_good`.

    Everything except the app's own by-rule tags. Without this exclusion
    the low-tag rule feeds itself: a re-search of an image that got
    `hatate:few tags` last time counts that tag towards the threshold it
    is a report of, so enough by-rule tags would eventually push the
    entry over the line and silently promote it to GOOD - the rule
    changing the outcome it exists to describe.

    With the feature unconfigured there are no such tags and this returns
    the list unchanged, so the count is the one every build so far used.
    """
    return [t for t in tags if t.source is not OUTCOME_TAG_SOURCE]


def outcome_tags_for(
    status: MatchStatus, tag_count: int, settings: Settings,
) -> List[Tag]:
    """The by-rule tags an entry with this outcome should carry.

    `tag_count` is the number of real tags on the entry - what
    `countable_tags` returns the length of - measured before any by-rule
    tag is added.

    Which list applies:

      * GOOD or POOR -> `tags_for_found`. Both are matches; POOR means
        "found, worth a look", not "not found".
      * NOT_FOUND    -> `tags_for_not_found`.
      * ERROR        -> nothing. A search that failed has no outcome yet;
        it is retried normally, and tagging an image `hatate:not found`
        because SauceNAO timed out would be a claim about the image that
        the app has not actually made.
      * SEARCHING / NOT_SEARCHED -> nothing. Not outcomes.

    `tags_for_low_tag_count` is added ON TOP of the found tags, and only
    for a match. A not-found entry has no tags by definition, so it would
    satisfy any threshold trivially, and labelling it both "not found"
    and "few tags" says nothing the first tag did not.
    """
    names: List[str] = []

    if status in (MatchStatus.GOOD, MatchStatus.POOR):
        names.extend(settings.tags_for_found or ())
        if tag_count < settings.match_conditions.min_tags_for_good:
            names.extend(settings.tags_for_low_tag_count or ())
    elif status is MatchStatus.NOT_FOUND:
        names.extend(settings.tags_for_not_found or ())

    tags: List[Tag] = []
    seen = set()
    for raw in names:
        namespace, name = split_tag_text(raw or "")
        if not name:
            # Blank lines and a lone ":" in the settings box. The lists
            # are free text; an empty tag is not something to send.
            continue
        key = (namespace, name)
        if key in seen:
            continue
        seen.add(key)
        tags.append(Tag(name=name, source=OUTCOME_TAG_SOURCE, namespace=namespace))
    return tags


def apply_outcome_tags(entry: ImageEntry, settings: Settings) -> List[Tag]:
    """Puts the by-rule tags for `entry.status` onto `entry`, replacing any
    left by a previous search. Returns the tags applied.

    Call this once per entry at the point its status is final, and pass an
    entry whose real tags are already in place - the low-tag rule reads
    them. Safe to call for every outcome, including ERROR, and safe to
    call twice: the second call replaces the first call's tags with the
    same set.

    Deliberately not subject to the blacklist or the namespace remap. Both
    exist to tidy up tags arriving from somebody else's booru; these came
    from this user's own settings, so rewriting or dropping them would be
    overriding an instruction with a preference.
    """
    tags = outcome_tags_for(entry.status, len(countable_tags(entry.tags)), settings)
    if not tags and not any(t.source is OUTCOME_TAG_SOURCE for t in entry.tags):
        # Nothing to add and nothing to clear. The overwhelmingly common
        # case - the feature is off - and worth keeping free of a touch()
        # that would mark every searched entry dirty for the session store.
        return []
    entry.add_tags(tags, replace_source=OUTCOME_TAG_SOURCE)
    if tags:
        log.debug("%s: applied %d by-rule tag(s) for status=%s: %s",
                  entry.filename, len(tags), entry.status.value,
                  ", ".join(t.display for t in tags))
    return tags
