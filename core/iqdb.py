"""IQDB reverse image search.

IQDB (https://iqdb.org) accepts a multipart POST with the image file and
returns an HTML results page. There is no official JSON API, so we scrape
the "best match" table. IQDB's markup has stayed fairly stable for years,
but if they change their template this parser may need small selector
tweaks - it's isolated here so that's a one-file fix.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .applog import get_logger
from . import net
from .hard_timeout import HardTimeoutError
from .image_prep import prepare_upload_bytes
from .models import Tag
from .progress_ticker import OnTick, ProgressTicker
from .sites import guess_site_name

log = get_logger("iqdb")

IQDB_URL = "https://iqdb.org/"
IQDB_3D_URL = "https://3d.iqdb.org/"
USER_AGENT = net.USER_AGENT

# The least time an IQDB search gets, whatever the general search timeout
# says. MEASURED on 2026-09-24: 8 to 42 seconds per search, whatever the
# upload's size (an 800px, 121KB upload still took 20-25s) - the time is
# IQDB's own. A 15s general timeout failed every search that day, which
# the app reports as an error and retries, to the same end.
MIN_BUDGET_SECONDS = 45.0


@dataclass
class IqdbMatch:
    url: str                       # link to the booru/source page
    thumb_url: Optional[str]
    similarity: float              # percent, 0-100
    width: Optional[int]
    height: Optional[int]
    source_name: Optional[str]     # e.g. "Danbooru", "Gelbooru"
    is_best_match: bool
    unnamespaced_tags: List[Tag]


class IqdbError(Exception):
    pass


def search(
    image_path: str,
    timeout: float = 30.0,
    on_tick: Optional[OnTick] = None,
    base_url: str = IQDB_URL,
) -> List[IqdbMatch]:
    """Upload an image to IQDB (or a compatible instance) and return
    parsed matches, best match first.

    `base_url` selects the instance: the default is iqdb.org (2D anime
    art). Pass IQDB_3D_URL for 3d.iqdb.org, which uses the same markup
    against a 3D/CG index. If given, on_tick(label, remaining, total)
    fires roughly once a second while the request is in flight, purely
    for UI countdown feedback - a single HTTP request has no native
    progress of its own."""
    instance = "IQDB 3D" if "3d.iqdb" in base_url else "IQDB"
    timeout = max(timeout, MIN_BUDGET_SECONDS)
    log.debug("Uploading %s to %s (%s)", image_path, instance, base_url)
    try:
        data, filename = prepare_upload_bytes(image_path)
        files = {"file": (filename, data, "application/octet-stream")}
        with ProgressTicker(f"Waiting on {instance}", timeout, on_tick):
            resp = net.post(base_url, files=files, timeout=timeout, deadline=timeout)
    except OSError as exc:
        log.error("Could not read %s for %s upload: %s", image_path, instance, exc)
        raise IqdbError(f"Could not read image: {exc}") from exc
    except HardTimeoutError as exc:
        log.error("%s request for %s exceeded the hard deadline: %s", instance, image_path, exc)
        raise IqdbError(f"{instance} request timed out: {exc}") from exc
    except requests.RequestException as exc:
        log.error("%s request failed for %s: %s", instance, image_path, exc)
        raise IqdbError(f"{instance} request failed: {exc}") from exc

    log.debug("%s responded HTTP %d (%d bytes)", instance, resp.status_code, len(resp.content))
    if resp.status_code == 413:
        raise IqdbError(f"{instance} rejected the image as too large, even after downscaling")
    if resp.status_code != 200:
        raise IqdbError(f"{instance} returned HTTP {resp.status_code}")

    matches = _parse_results(resp.text, base_url=base_url)
    log.debug("%s HTML parsed into %d match(es)", instance, len(matches))
    return matches


def search_3d(image_path: str, timeout: float = 30.0, on_tick: Optional[OnTick] = None) -> List[IqdbMatch]:
    """IQDB's 3D/CG index at 3d.iqdb.org. Same protocol, different corpus."""
    return search(image_path, timeout=timeout, on_tick=on_tick, base_url=IQDB_3D_URL)


def _parse_results(html: str, base_url: str = IQDB_URL) -> List[IqdbMatch]:
    soup = BeautifulSoup(html, "lxml")
    matches: List[IqdbMatch] = []

    tables = soup.select("div#pages table")
    log.debug("Found %d result table(s) in IQDB response", len(tables))

    if not tables and "iqdb" not in html.lower():
        # Zero result tables is normal for a genuine "no matches found"
        # page - but if the response doesn't even mention "iqdb" anywhere
        # (which any real page from the site would, in its title/footer/
        # branding regardless of outcome), this almost certainly isn't
        # IQDB's real results page at all - more likely a CAPTCHA,
        # rate-limit notice, or some other block page that still returned
        # HTTP 200. Treat it as a real failure rather than silently
        # returning "confirmed zero matches", which would otherwise get
        # cached as NOT_FOUND and stay stuck there indefinitely.
        log.warning("IQDB response has no result tables and doesn't look like a real IQDB page - "
                    "treating as a failure rather than a confirmed empty result")
        raise IqdbError("IQDB's response didn't look like a real results page (possible block/CAPTCHA/rate limit)")

    # IQDB renders each result as a <table> with class "iqdb-match"-ish rows.
    # Structure (as of the classic iqdb.org theme):
    #   <div id="pages"><table>...<tr><th>...Best match / Additional match...
    #   <td class="image"><a href="...">img</a></td>
    #   <td>... size ... <td>... similarity% ...
    # A row can contain more than one <a> (e.g. a raw-filename download link
    # alongside the actual source-page link), so we can't just take the
    # first one - only accept hrefs that are genuinely absolute URLs.
    for i, table in enumerate(tables):
        header = table.find("th")
        is_best = bool(header and "best match" in header.get_text(strip=True).lower())

        image_link = table.select_one("td.image a")
        if not image_link:
            log.debug("Table %d: no td.image a element, skipping (markup may have changed)", i)
            continue

        url = None
        for candidate in table.find_all("a"):
            href = candidate.get("href")
            if href and _looks_like_url(href):  # type: ignore[arg-type]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute
                url = "https:" + href if href.startswith("//") else href  # type: ignore[operator, union-attr]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute
                break
        if not url:
            # No usable source-page URL on this row (e.g. site changed its
            # markup) - skip rather than surface a bogus non-URL value.
            log.debug("Table %d: no absolute-URL <a> found among %d link(s), skipping",
                      i, len(table.find_all("a")))
            continue

        img = image_link.find("img")
        thumb_url = None
        if img and img.get("src"):
            # IQDB serves thumbnails as ROOT-relative paths
            # ("/danbooru/3/5/7/hash.jpg"), not absolute URLs. Only the
            # protocol-relative ("//host/...") case was handled before, so
            # every IQDB thumbnail failed later with "No scheme supplied" -
            # meaning no preview image and no format/size info for any IQDB
            # match. urljoin handles all three shapes correctly.
            thumb_url = urljoin(base_url, img["src"].strip())  # type: ignore[union-attr]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute

        text = table.get_text(" ", strip=True)

        similarity = _extract_percent(text)
        width, height = _extract_dimensions(text)
        source_name = _guess_source_name(url)  # type: ignore[arg-type]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute

        if similarity is None:
            log.debug("Table %d: could not extract similarity %% from row text", i)

        log.debug(
            "Table %d: parsed match url=%s site=%s similarity=%s best=%s",
            i, url, source_name, similarity, is_best,
        )

        matches.append(
            IqdbMatch(
                url=url,  # type: ignore[arg-type]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute
                thumb_url=thumb_url,
                similarity=similarity or 0.0,
                width=width,
                height=height,
                source_name=source_name,
                is_best_match=is_best,
                unnamespaced_tags=[],
            )
        )

    matches.sort(key=lambda m: (not m.is_best_match, -m.similarity))
    return matches


def _looks_like_url(href: str) -> bool:
    """True only for hrefs that are genuinely absolute URLs (http(s):// or
    protocol-relative //host/...), never a bare filename or relative path -
    those aren't safe to hand to QDesktopServices.openUrl()."""
    href = href.strip()
    if href.startswith("//"):
        return True
    if href.startswith("http://") or href.startswith("https://"):
        return True
    return False


def _extract_percent(text: str) -> Optional[float]:
    import re
    m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*similarity", text, re.IGNORECASE)
    return float(m.group(1)) if m else None


def _extract_dimensions(text: str) -> tuple[Optional[int], Optional[int]]:
    import re
    m = re.search(r"(\d+)\s*[x\u00d7]\s*(\d+)", text)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _guess_source_name(url: str) -> Optional[str]:
    return guess_site_name(url)
