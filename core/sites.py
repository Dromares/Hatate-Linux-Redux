"""Canonical list of source sites the app recognises, used both to classify
IQDB/SauceNAO matches by which site they came from and to power the Sites
menu that lets the user choose which ones to keep as candidates.
"""
from __future__ import annotations

import re

from typing import Optional
from urllib.parse import urlparse

# Host -> friendly display name. Add an entry here to teach the whole app
# (matching, filtering, the Sites menu) about a new site in one place.
HOST_MAP = {
    "danbooru.donmai.us": "Danbooru",
    "gelbooru.com": "Gelbooru",
    "safebooru.org": "Safebooru",
    "rule34.xxx": "Rule34",
    # Classification only, and a distinct name from "Rule34" above -
    # rule34.xxx and Paheal are different sites with different posts,
    # and one label for both would make the Sites menu lie. Google Lens
    # is what surfaces these: its "Exact matches" tab names the post in
    # its title ("Post 5674224: ... - Rule 34 Paheal"), which is where
    # the URL is rebuilt from. See core/google_lens.py.
    "rule34.paheal.net": "Paheal",
    "paheal.net": "Paheal",
    # rule34.us is a third, unrelated site despite the name - its own
    # software and its own post ids - so it gets its own label too. It
    # has a parser (boorus/rule34us.py); like Paheal it is reached
    # through Google Lens's exact matches.
    "rule34.us": "Rule34.us",
    # Reddit posts have a parser (boorus/redditpost.py), read through each
    # post's public RSS feed. Google Lens is what surfaces them.
    "reddit.com": "Reddit",
    "redd.it": "Reddit",
    "xbooru.com": "Xbooru",
    "yande.re": "Yande.re",
    "konachan.com": "Konachan",
    "konachan.net": "Konachan",
    # Sankaku has a parser (boorus/sankaku.py), but an unusual one: its
    # legacy numeric ids - the only ones the search engines know - no
    # longer resolve to anything, so posts are found by the LOCAL FILE'S
    # md5 against its modern public API. That hits for a file downloaded
    # from Sankaku and misses for a re-encode, and a miss claims nothing.
    "sankakucomplex.com": "Sankaku Complex",
    "chan.sankakucomplex.com": "Sankaku Complex",
    # Also classification only, for the same structural reason as
    # MangaDex below: an e-Hentai URL points at a GALLERY (many pages),
    # not a single image, so it doesn't fit the one-match-one-image
    # model every parser here assumes. Labelling it means matches show
    # as "e-Hentai" rather than "Other" and can be unchecked in the
    # Sites menu.
    "e-hentai.org": "e-Hentai",
    "exhentai.org": "e-Hentai",
    "e621.net": "e621",
    "e926.net": "e621",
    "pixiv.net": "Pixiv",
    # Anime-Pictures is parsed through its JSON API (see
    # core/boorus/animepictures.py). The full-resolution file is taken
    # from the site's own download redirect rather than a constructed
    # CDN path, because that hostname has changed more than once.
    "anime-pictures.net": "Anime-Pictures",
    "e-shuushuu.net": "e-shuushuu",
    "zerochan.net": "Zerochan",
    # pawchive.pw has a parser (boorus/pawchive.py) - a Kemono fork whose
    # JSON API answers anonymously even though its HTML pages are gated
    # behind an account. pawchive.org shares the name and this label but
    # is a DIFFERENT site (it 404s on both the post routes and the API),
    # so it stays classification-only and no parser matches it.
    "pawchive.pw": "Pawchive",
    "pawchive.org": "Pawchive",
    # DeviantArt has a parser now (boorus/deviantart.py), but a limited
    # one by nature: its pages are JS-driven and it publishes no
    # booru-style tag vocabulary, so the public oEmbed endpoint it uses
    # yields the artist and a picture - not a tag list. It is also
    # availability-checked (see core/search_engine.py), since a deleted
    # deviation is exactly the dead link worth dropping.
    # A MangaDex chapter holds many images under one URL, which is why
    # this was classification-only for a long time. Multi-page support
    # made that reasoning obsolete: the chapter is treated as a
    # multi-page post and the page matching the local file is identified
    # by hashing, exactly as for Pixiv. See boorus/mangadex.py.
    "mangadex.org": "MangaDex",
    "deviantart.com": "DeviantArt",
    "fav.me": "DeviantArt",  # DeviantArt's own short-link domain
    # Tweet URLs have a parser now (boorus/twitter.py, via FxTwitter's
    # public API) - Twitter's own pages are JS-driven and need a login,
    # but that API answers anonymously with the author, the original image
    # and its real dimensions. A tweet still has no booru-style tag
    # vocabulary, so the author is the only tag. t.co and pbs.twimg.com
    # carry no tweet id to look up, so they stay classification-only.
    # fxtwitter/vxtwitter are the redirectors these engines often hand out
    # instead of x.com.
    "twitter.com": "Twitter",
    "x.com": "Twitter",
    "t.co": "Twitter",
    "pbs.twimg.com": "Twitter",
    "fxtwitter.com": "Twitter",
    "vxtwitter.com": "Twitter",
    "fixupx.com": "Twitter",
    "fixvx.com": "Twitter",
    # Classification only, and a common enough hit to be worth naming:
    # a MangaUpdates match is a SERIES entry (/series.html?id=...), not a
    # page of the manga, so it carries no image and no booru-style tags -
    # there is nothing for a parser to fetch. Naming it lets it be
    # unticked in the Sites menu instead of hiding among "Other".
    "mangaupdates.com": "MangaUpdates",
    # Same again: a MyAnimeList match is a database entry for a series
    # (/manga/123094/, /anime/1535/), not a page of it. Named so it can
    # be filtered deliberately rather than as part of "Other".
    "myanimelist.net": "MyAnimeList",
    # Classification only. trace.moe matches point at an AniList page
    # (the series, not a still image). Labelling them keeps them out of
    # "Other" and lets them be unchecked in the Sites menu.
    "anilist.co": "AniList",
    # Classification only, for the same structural reason as e-Hentai
    # above: a toon34.com URL (e.g. /porn-comics/finn-x-bronwyn/) is a
    # GALLERY page holding many images, not a single file, so it doesn't
    # fit the one-match-one-image model every parser here assumes.
    # Labelling it keeps it out of "Other" and tells the rest of the app
    # (downloads, Hydrus import) it's a page to open, not a file to fetch.
    "toon34.com": "Toon34",
}

# Ordered, de-duplicated list of friendly site names for menus/settings.
KNOWN_SITES = sorted(set(HOST_MAP.values()), key=str.lower)
OTHER_SITE = "Other"
ALL_SITE_OPTIONS = KNOWN_SITES + [OTHER_SITE]


SANKAKU_LEGACY_POST_RE = re.compile(
    r"^(https?://[^/]*sankakucomplex\.com)/post/show/(\d+)", re.IGNORECASE)

# JoyReactor/reactor.cc serve two files at the same path for a post: the
# one actually linked from the post page, watermarked with the site's own
# yellow "joyreactor" bar along the bottom, and an unwatermarked,
# full-quality original one path segment over, under a literal "full/"
# segment. CONFIRMED live pair:
#   https://img10.reactor.cc/pics/post/egoswans-...-8192103.jpeg (watermarked)
#   https://img10.reactor.cc/pics/post/full/egoswans-...-8192103.jpeg (clean)
# The negative lookahead keeps an already-full/ URL from being matched
# again (and thus left alone by canonicalize_url, same as anything else
# it doesn't recognise).
REACTOR_PICS_POST_RE = re.compile(
    r"^(https?://[^/]+/pics/post/)(?!full/)(.+)$", re.IGNORECASE)
REACTOR_HOSTS = ("reactor.cc", "joyreactor.com", "joyreactor.cc")


def _is_reactor_host(netloc: str) -> bool:
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return any(netloc == host or netloc.endswith("." + host) for host in REACTOR_HOSTS)


def canonicalize_url(url: str) -> str:
    """Rewrites known-stale or known-degraded URL forms to the one the
    site actually serves.

    SauceNAO's index still hands out Sankaku's deprecated
    /post/show/{id} route. Sankaku no longer maps it and bounces it to
    the browse index, so following that link never reaches the post -
    it's broken by FORMAT, independently of whether the post still
    exists. Rewriting to /en/posts/{id} gives the link a chance to work,
    and if the post really is gone the availability check still catches
    it.

    JoyReactor/reactor.cc results point at the watermarked copy of the
    file by default (see REACTOR_PICS_POST_RE above); rewritten to the
    full/ original so the user isn't handed the worse of the two files
    the site itself serves. If the full/ file turns out not to exist,
    the availability check (core/availability.py) falls the candidate
    back to this plain URL rather than reporting it dead outright - see
    _reactor_full_fallback there.

    Left alone when it doesn't match: a URL this doesn't recognise is
    returned untouched rather than guessed at.
    """
    if not url:
        return url
    match = SANKAKU_LEGACY_POST_RE.match(url)
    if match:
        host, post_id = match.group(1), match.group(2)
        return f"{host}/en/posts/{post_id}"
    reactor_match = REACTOR_PICS_POST_RE.match(url)
    if reactor_match:
        try:
            netloc = urlparse(url).netloc.lower()
        except ValueError:
            netloc = ""
        if netloc and _is_reactor_host(netloc):
            return reactor_match.group(1) + "full/" + reactor_match.group(2)
    return url


def guess_site_name(url: str) -> Optional[str]:
    """Best-effort site name for a match URL, based on its host.

    Matches the URL's hostname, not a substring of the whole URL.
    `t.co` as a substring would otherwise classify `anilist.co` (and
    anything else whose host merely contains those four characters) as
    Twitter.
    """
    if not url:
        return None
    try:
        netloc = urlparse(url).netloc.lower()
    except ValueError:
        return None
    if not netloc:
        return None
    if netloc.startswith("www."):
        netloc = netloc[4:]
    for host, name in HOST_MAP.items():
        if netloc == host or netloc.endswith("." + host):
            return name
    return None


def classify_site(source_name: Optional[str], url: str) -> str:
    """Resolves a candidate match to one of ALL_SITE_OPTIONS. Tries the
    URL's host first - this works uniformly for both IQDB and SauceNAO
    matches, since SauceNAO's own index labels don't always match our
    friendly names - falling back to source_name if it's already one of
    our known names, else the "Other" catch-all."""
    guessed = guess_site_name(url)
    if guessed:
        return guessed
    if source_name in KNOWN_SITES:
        return source_name
    return OTHER_SITE
