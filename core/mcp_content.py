"""Converts a McpToolHandlers result dict into MCP content blocks.

Split out of core/mcp_server.py so that module's top level stays free of
`mcp` imports (see its own docstring for why); this is the one place
that actually needs `mcp.types`, and it is imported lazily, from inside
a running server, after start() has already confirmed the package is
there.

The convention this depends on: any result key ending in `_image` whose
value is a dict with a `bytes` entry is image content, pulled out into
its own MCP image block and replaced in the text block by its remaining
metadata (the URL, whether it was full resolution, any fallback reason)
- never the raw bytes themselves, which JSON cannot represent and which
nobody reading the text block needs to see base64-encoded inline.
"""
from __future__ import annotations

import base64
import json
from typing import Any, Dict, List

_IMAGE_KEY_SUFFIX = "_image"


def to_content_blocks(result: Dict[str, Any]) -> List[Any]:
    from mcp import types

    blocks: List[Any] = []
    metadata: Dict[str, Any] = {}
    for key, value in result.items():
        if key.endswith(_IMAGE_KEY_SUFFIX) and isinstance(value, dict) and "bytes" in value:
            data = value.get("bytes")
            mime_type = value.get("mime_type") or "image/jpeg"
            remaining = {k: v for k, v in value.items() if k != "bytes"}
            if data:
                blocks.append(types.ImageContent(
                    type="image",
                    data=base64.b64encode(data).decode("ascii"),
                    mimeType=mime_type,
                ))
            else:
                remaining["fetch_failed"] = True
            metadata[key] = remaining
        else:
            metadata[key] = value

    blocks.insert(0, types.TextContent(type="text", text=json.dumps(metadata, default=str)))
    return blocks
