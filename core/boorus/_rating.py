"""A post's age rating, where the site actually states one.

Boorus classify a post's content - safe/questionable/explicit and the
like - and the tag that carries it into Hydrus ("rating:explicit") is one
users ask for. The awkward part is that the same one-letter code does not
mean the same thing everywhere: Danbooru's "s" is *sensitive* and e621's
"s" is *safe*, so a single shared letter table would silently mislabel
one of them. Each parser therefore hands over its OWN vocabulary, and
this module only normalizes the result into tag-shaped text.

Sites whose response has no rating field this app can point at contribute
nothing - the same rule core/google_lens.py applies. Guessing a rating is
worse than having none: it goes into the user's library looking like fact.

Two ways in:

- `from_code(raw, vocabulary)` for the JSON APIs, which state the rating
  as a code in a named field (Danbooru, e621).
- `from_label(text)` for the Danbooru 1.x descendants, whose Statistics
  sidebar spells it out in the same block the "Size: WxH" scrape already
  walks (see core/boorus/_sizes.py):

      Id: 3484690
      Posted: 2016-12-23 13:27:09
      Size: 1232x918
      Rating: Explicit
"""
from __future__ import annotations

import re
from typing import Mapping, Optional

# Anchored to the sidebar's own label rather than hunting the page for a
# rating word - a booru page is full of prose (comments, related tags),
# and a looser pattern would happily pick a word out of it. A single
# word only: the label holds one, and allowing more would swallow
# whatever element follows it when the markup puts them on one line.
RATING_LABEL_RE = re.compile(r"\bRating:\s*([A-Za-z]+)", re.IGNORECASE)


def _as_tag_name(value: str) -> Optional[str]:
    """Tag-shaped: lowercase, underscores for spaces. None if there is
    nothing usable left, so a blank field never becomes a blank tag."""
    name = value.strip().lower().replace(" ", "_")
    return name or None


def from_code(raw: object, vocabulary: Mapping[str, str]) -> Optional[str]:
    """A site's own rating code, translated through that site's own
    table. A value already spelled out in full ("explicit") passes
    through, since several of these APIs have widened from letters to
    words over time and both forms are unambiguous. An unrecognised
    single letter yields nothing rather than a guess - a new code means
    a category this app has never seen, and inventing a name for it is
    exactly the mislabelling this module exists to avoid.
    """
    if not isinstance(raw, str):
        return None
    code = raw.strip().lower()
    if not code:
        return None
    if code in vocabulary:
        return vocabulary[code]
    if code in set(vocabulary.values()):
        return code
    return None


def from_label(text: str) -> Optional[str]:
    """The rating off a "Rating: Explicit" Statistics line, or None."""
    match = RATING_LABEL_RE.search(text or "")
    if not match:
        return None
    return _as_tag_name(match.group(1))
