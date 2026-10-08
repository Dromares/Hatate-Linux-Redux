"""Which site a URL is on, by its hostname.

Every parser used to ask whether its host appeared ANYWHERE in the URL.
That claimed pages on other sites: "x.com" is inside "hentaivox.com" and
"freeadultcomix.com", so the Twitter parser took both, and "rule34.xxx"
is in the PATH of r34.app's mirror pages, so the rule34 parser took
those. Each then fetched a page it could not read and found no tags -
and counted as a tag-giving site while doing it.
"""
from typing import Iterable
from urllib.parse import urlparse


def on_host(url: str, hosts: Iterable[str]) -> bool:
    """True if `url` is on one of `hosts` or a subdomain of one."""
    url = (url or "").strip()
    if not url:
        return False
    if "://" not in url:
        url = "http://" + url.lstrip("/")
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in hosts)
