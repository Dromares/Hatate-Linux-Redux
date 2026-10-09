"""The embedded MCP server: wires core/mcp_tools.py's registry and
gui/mcp_bridge.py's handlers up to an actual MCP connection over
streamable-HTTP, bound to loopback only.

`mcp` is an OPTIONAL dependency (see requirements.txt - it follows the
same "commented out, install it yourself" precedent as Playwright). This
module never imports it at module load time, only inside start(), so:

  * importing core.mcp_server (and therefore constructing a
    McpServerController) always succeeds, with or without the package -
    gui/main_window.py can build one unconditionally at startup and only
    find out whether it actually works when settings.mcp.enabled asks it
    to start.
  * the tool layer underneath it (core/mcp_tools.py, core/mcp_audit.py,
    gui/mcp_bridge.py) needs no mcp import to be tested at all.

Everything a tool call actually does lives in gui/mcp_bridge.py's
McpToolHandlers; this module's only job is protocol plumbing - listing
tools, dispatching a call to the matching handler method, converting the
plain dicts those handlers return into MCP content blocks, and checking
the bearer token on every request before any of that runs.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from .applog import get_logger
from .config import McpSettings
from . import mcp_tools

log = get_logger("mcp_server")

# Loopback only, always. Deliberately not a McpSettings field - see that
# dataclass's own docstring in core/config.py for why a setting that
# COULD be "0.0.0.0" is a setting that, for somebody, eventually will be.
HOST = "127.0.0.1"

# How long start() waits for uvicorn to actually bind the socket before
# giving up and reporting a failure - see the long comment in start() for
# why this wait exists at all.
BIND_TIMEOUT_SECONDS = 5.0

# core/mcp_tools.ToolSpec.name -> the McpToolHandlers method with the
# same behaviour. A plain dict rather than getattr(handlers, name)
# everywhere: it is the one place that has to agree with
# core/mcp_tools.TOOLS, so a typo here fails every tool-listing test
# immediately instead of 404-ing silently at call time.
_HANDLER_METHOD_NAMES = {
    "list_queue": "list_queue",
    "get_entry": "get_entry",
    "get_candidates": "get_candidates",
    "get_images": "get_images",
    "get_diff": "get_diff",
    "list_actions": "list_actions",
    "select_candidate": "select_candidate",
    "toggle_reviewed": "toggle_reviewed",
    "research": "research",
    "send_upload": "send_upload",
    "send_url": "send_url",
    "download_send": "download_send",
    "remove_row": "remove_row",
    "reset_result": "reset_result",
}


def dispatch(handlers: Any, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Calls the McpToolHandlers method for `tool_name` with `arguments`
    as keyword arguments. Split out from the MCP-specific server below so
    it can be unit-tested without the `mcp` package installed."""
    method_name = _HANDLER_METHOD_NAMES[tool_name]
    method = getattr(handlers, method_name)
    return method(**arguments)


class McpServerController:
    """Owns the running server's lifecycle. Every failure (the optional
    package missing, a blank token, the port already in use) is logged
    and recorded on `last_error` rather than raised - start() is called
    from the Qt event loop, both at startup and whenever Settings change,
    and an exception there must not take the whole app down with it."""

    def __init__(self, handlers: Any) -> None:
        self._handlers = handlers
        self._thread: Optional[threading.Thread] = None
        self._uvicorn_server: Any = None
        self.last_error: Optional[str] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, settings: McpSettings) -> bool:
        """Starts the server if enabled and not already running. Returns
        whether it is running once this call returns."""
        if self.running:
            return True
        if not settings.enabled:
            return False
        if not settings.token:
            self.last_error = "Blank bearer token: refusing to start the MCP server"
            log.error(self.last_error)
            return False

        try:
            app = _build_asgi_app(self._handlers, settings)
        except ImportError as exc:
            if _mcp_top_level_import_succeeds():
                # The package IS there - `exc` is e.g. mcp 2.x's own
                # `from mcp.server.fastmcp import FastMCP` shim raising
                # because FastMCP was renamed to MCPServer. Surface that
                # detail rather than telling the user to install
                # something they already have.
                self.last_error = (
                    f"MCP package installed but incompatible ({exc}) - run "
                    '`venv/bin/pip install "mcp>=1.2,<2"` to install a compatible version '
                    "(see requirements.txt)"
                )
            else:
                self.last_error = (
                    "MCP support not installed - run "
                    '`venv/bin/pip install "mcp>=1.2,<2"` to enable it '
                    "(see requirements.txt)"
                )
            log.error("%s (%s)", self.last_error, exc)
            return False
        except Exception as exc:  # noqa: BLE001 - reported on last_error, never raised into the GUI
            self.last_error = f"Could not start the MCP server: {exc}"
            log.exception(self.last_error)
            return False

        import uvicorn  # one of `mcp`'s own transitive dependencies for this transport

        config = uvicorn.Config(app, host=HOST, port=settings.port, log_level="warning")
        server = uvicorn.Server(config)
        self._uvicorn_server = server

        serve_error: List[BaseException] = []

        def _serve() -> None:
            try:
                server.run()
            except Exception as exc:  # noqa: BLE001 - the thread must not die silently and unexplained
                serve_error.append(exc)
                log.exception("MCP server thread exited unexpectedly")

        thread = threading.Thread(target=_serve, name="mcp-server", daemon=True)
        thread.start()

        # server.run() binds the socket on this background thread, inside
        # uvicorn's own asyncio loop, strictly after this method would
        # otherwise have already returned True. A bind failure (e.g. the
        # port already in use) used to be swallowed by _serve()'s own
        # except-and-log above with last_error left at None from the old
        # unconditional "success" path - unreportable to any caller. Wait
        # here for uvicorn to flip `server.started` (set once its socket is
        # actually listening) or for the thread to die trying, so a busy
        # port surfaces through last_error instead of vanishing.
        deadline = time.monotonic() + BIND_TIMEOUT_SECONDS
        while time.monotonic() < deadline and not server.started and thread.is_alive():
            time.sleep(0.01)

        if not server.started:
            self._uvicorn_server = None
            if serve_error:
                self.last_error = f"Could not start the MCP server: {serve_error[0]}"
            else:
                self.last_error = (
                    f"MCP server did not start listening on port {settings.port} "
                    f"within {BIND_TIMEOUT_SECONDS:g}s (port already in use?)"
                )
            log.error(self.last_error)
            return False

        self._thread = thread
        self.last_error = None
        log.info("MCP server listening on %s:%d (loopback only, enabled tools: %s)",
                 HOST, settings.port,
                 ", ".join(sorted(t.name for t in mcp_tools.TOOLS if mcp_tools.gate(t.name, settings) is None)))
        return True

    def stop(self, *, shutdown_invoker: bool = True) -> None:
        """Stops the server if running. Safe to call unconditionally,
        including when it was never started - e.g. from closeEvent.

        `shutdown_invoker` defaults to True because closeEvent is the
        only caller for which that is correct: the window (and the
        MainThreadInvoker it owns) is going away right after. restart()
        below passes False - the shared invoker is constructed exactly
        once per MainWindow lifetime and is never recreated, so shutting
        it down there would permanently break every future tool call
        needing GUI-thread marshaling, including the very first
        Settings-triggered restart() on a controller that was never
        started (DAN-912)."""
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._thread = None
        self._uvicorn_server = None
        if shutdown_invoker:
            self._handlers.invoker.shutdown()

    def restart(self, settings: McpSettings) -> bool:
        """Applies a settings change (port, token, enabled) that start()
        alone cannot pick up on a running server - called from Settings'
        apply path. Gating (allow_research etc.) and dry_run are read
        live on every call and need no restart; only enabled/port/token
        change what's actually listening."""
        self.stop(shutdown_invoker=False)
        return self.start(settings)


def _mcp_top_level_import_succeeds() -> bool:
    """Whether `import mcp` itself works, independent of the submodule
    `_build_asgi_app` actually needs. Distinguishes "the package truly
    isn't installed" from "it's installed, just an incompatible major
    version" (mcp 2.x renamed FastMCP to MCPServer) so start()'s
    ImportError handler doesn't tell the user to install something they
    already have."""
    try:
        import mcp  # noqa: F401
    except ImportError:
        return False
    return True


def _build_asgi_app(handlers: Any, settings: McpSettings) -> Any:
    """Builds the ASGI app a moment before serving it, inside start()'s
    try/except, so a missing `mcp` package is reported through
    last_error rather than raised out of module import."""
    from mcp.server.fastmcp import FastMCP

    mcp_app = FastMCP("hatate-linux")
    _register_tools(mcp_app, handlers)

    http_app = mcp_app.streamable_http_app()
    return _with_bearer_auth(http_app, settings.token)


def _register_tools(mcp_app: Any, handlers: Any) -> None:
    """Registers every tool in core/mcp_tools.TOOLS against `mcp_app`,
    converting each handler's plain-dict result into MCP content blocks.
    One generic registration loop rather than fourteen hand-written
    `@mcp_app.tool()` functions, so a tool added to the registry is a
    data change here, not a code change."""
    for spec in mcp_tools.TOOLS:
        description = spec.name.replace("_", " ")
        if spec.gate_field:
            description += f" - disabled until mcp.{spec.gate_field} is turned on"
        mcp_app.add_tool(
            _wrap_tool(handlers, spec.name), name=spec.name, description=description,
            # structured_output=False: call() below returns MCP content
            # blocks (a list), never the dict/etc. that @functools.wraps
            # copies onto it from the mirrored handler method's own
            # return annotation. Without this, FastMCP builds its output
            # schema from that COPIED annotation and then validates the
            # real (mismatched) return value against it, failing every
            # tool call, allowed or refused alike (DAN-912).
            structured_output=False,
        )


def _wrap_tool(handlers: Any, tool_name: str):
    """A function FastMCP can register as `tool_name`, that dispatches to
    the matching McpToolHandlers method and converts the result to MCP
    content blocks. **kwargs rather than a fixed signature: FastMCP
    reads a registered function's own signature to build its schema, and
    every McpToolHandlers method already has the real one (row: int,
    candidate_index: int, ...) - introspecting through here would need
    to fake that signature right back, so each handler method is
    registered directly as its own schema source via functools.wraps."""
    import functools

    from .mcp_content import to_content_blocks

    method = getattr(handlers, tool_name)

    @functools.wraps(method)
    def call(**kwargs: Any) -> List[Any]:
        result = dispatch(handlers, tool_name, kwargs)
        return to_content_blocks(result)

    return call


def _with_bearer_auth(app: Any, token: str) -> Any:
    """Wraps an ASGI app so every HTTP request must present
    `Authorization: Bearer <token>` or get a 401, before it ever reaches
    the MCP session. Plain ASGI (no Starlette/uvicorn-specific types),
    since this has to keep working across whatever version of those
    `mcp`'s own transitive dependencies pull in."""
    expected = f"Bearer {token}".encode("latin-1")

    async def wrapped(scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            headers = dict(scope.get("headers") or [])
            if headers.get(b"authorization") != expected:
                await send({
                    "type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"text/plain")],
                })
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
        await app(scope, receive, send)

    return wrapped
