"""An image's width and height, read from the first bytes of the file.

For sites whose page or API states no dimensions but whose image host
honours Range requests - Pawchive's file host, Reddit's i.redd.it. An
image's header states its size, so a few KB are read (never the whole
file), and reading stops as soon as PIL knows it. A JPEG's size sits
after its EXIF/ICC blocks, which are usually a few KB but can run long,
hence the cap rather than a fixed small range.

Cached by URL. A network failure is not cached - a timeout says nothing
about the file - but a file whose size can't be read from its start is.
"""
from __future__ import annotations

from typing import Optional, Tuple

import requests

from .. import net
from ..applog import get_logger
from ..lru_cache import LRUCache

log = get_logger("boorus.remote_size")

MAX_BYTES = 512 * 1024
TIMEOUT = 10.0
CACHE_ENTRIES = 1024
CHUNK = 16 * 1024

_cache = LRUCache(CACHE_ENTRIES)


def image_size(url: str, user_agent: Optional[str] = None) -> Tuple[Optional[int], Optional[int]]:
    cached = _cache.get(url)
    if cached is not None:
        return cached

    from PIL import ImageFile

    headers = {"Range": f"bytes=0-{MAX_BYTES - 1}"}
    if user_agent:
        headers["User-Agent"] = user_agent
    size = None
    try:
        with net.get(url, stream=True, timeout=TIMEOUT, headers=headers) as resp:
            if resp.status_code in (200, 206):
                parser, read = ImageFile.Parser(), 0
                # A server ignoring Range answers 200 with the whole file;
                # the cap below still stops the read, so no special case.
                for chunk in resp.iter_content(CHUNK):
                    parser.feed(chunk)
                    read += len(chunk)
                    if parser.image is not None or read >= MAX_BYTES:
                        break
                if parser.image is not None:
                    size = parser.image.size
            else:
                log.debug("%s answered %s when asked for its first bytes", url, resp.status_code)
    except requests.RequestException as exc:
        log.debug("Could not read dimensions of %s: %s", url, exc)
        return None, None
    except (OSError, ValueError) as exc:
        # PIL giving up on a format it can't size from a prefix.
        log.debug("Could not read dimensions of %s: %s", url, exc)

    result = (int(size[0]), int(size[1])) if size and size[0] and size[1] else (None, None)
    _cache[url] = result
    return result
