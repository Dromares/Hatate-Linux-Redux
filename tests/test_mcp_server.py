"""core/mcp_server.py's protocol-independent half: dispatching a tool
call by name to the matching McpToolHandlers method (DAN-707).

Everything that actually needs the optional `mcp` package (building the
ASGI app, converting a result into MCP content blocks) is guarded and
skipped when it isn't installed - which, per requirements.txt, is the
normal state of this repo's own test environment.
"""
import re
import sys
import time
import types
import unittest
from typing import Any, Dict
from unittest import mock

from . import _path  # noqa: F401
from core import mcp_server, mcp_tools

# The one install command every user-facing surface must show. It has to be
# quoted - pasted bare, `>` and `<` are shell redirections - and pinned,
# because mcp 2.x renamed FastMCP and this app cannot run on it (DAN-1139).
PINNED_INSTALL = 'venv/bin/pip install "mcp>=1.2,<2"'

try:
    import mcp as _mcp_package  # noqa: F401
    HAVE_MCP = True
except ImportError:
    HAVE_MCP = False


class FakeHandlers:
    """Records every call it receives, mirroring McpToolHandlers'
    public method names without any of its gating/Qt machinery - this
    file is about dispatch, not about what each tool does."""

    def __init__(self):
        self.calls = []

    def _record(self, name, kwargs):
        self.calls.append((name, kwargs))
        return {"allowed": True, "tool": name, "kwargs": kwargs}

    def list_queue(self, **kwargs):
        return self._record("list_queue", kwargs)

    def get_entry(self, **kwargs):
        return self._record("get_entry", kwargs)

    def get_candidates(self, **kwargs):
        return self._record("get_candidates", kwargs)

    def get_images(self, **kwargs):
        return self._record("get_images", kwargs)

    def get_diff(self, **kwargs):
        return self._record("get_diff", kwargs)

    def list_actions(self, **kwargs):
        return self._record("list_actions", kwargs)

    def select_candidate(self, **kwargs):
        return self._record("select_candidate", kwargs)

    def toggle_reviewed(self, **kwargs):
        return self._record("toggle_reviewed", kwargs)

    def research(self, **kwargs):
        return self._record("research", kwargs)

    def send_upload(self, **kwargs):
        return self._record("send_upload", kwargs)

    def send_url(self, **kwargs):
        return self._record("send_url", kwargs)

    def download_send(self, **kwargs):
        return self._record("download_send", kwargs)

    def remove_row(self, **kwargs):
        return self._record("remove_row", kwargs)

    def reset_result(self, **kwargs):
        return self._record("reset_result", kwargs)


class TestHandlerMethodNamesAreComplete(unittest.TestCase):
    """The thing most likely to rot here: a tool added to
    core/mcp_tools.TOOLS without a matching entry in
    core/mcp_server._HANDLER_METHOD_NAMES, which would 404 at call time
    instead of failing a test."""

    def test_every_tool_has_a_handler_method_name(self):
        for spec in mcp_tools.TOOLS:
            self.assertIn(spec.name, mcp_server._HANDLER_METHOD_NAMES)

    def test_every_mapped_method_name_exists_on_a_real_handlers_object(self):
        fake = FakeHandlers()
        for method_name in mcp_server._HANDLER_METHOD_NAMES.values():
            self.assertTrue(hasattr(fake, method_name), method_name)

    def test_no_stray_entries_for_tools_that_do_not_exist(self):
        tool_names = {t.name for t in mcp_tools.TOOLS}
        for name in mcp_server._HANDLER_METHOD_NAMES:
            self.assertIn(name, tool_names)


class TestDispatch(unittest.TestCase):
    def test_dispatches_to_the_matching_method_with_keyword_arguments(self):
        fake = FakeHandlers()
        result = mcp_server.dispatch(fake, "get_entry", {"row": 3})
        self.assertEqual(fake.calls, [("get_entry", {"row": 3})])
        self.assertEqual(result["kwargs"], {"row": 3})

    def test_every_registered_tool_dispatches_without_error(self):
        fake = FakeHandlers()
        for spec in mcp_tools.TOOLS:
            mcp_server.dispatch(fake, spec.name, {})
        self.assertEqual(len(fake.calls), len(mcp_tools.TOOLS))


class FakeHandlersWithInvoker(FakeHandlers):
    """McpServerController.stop() reaches into handlers.invoker.shutdown()
    - give the lifecycle tests below something that has one, without
    pulling in the real Qt-based MainThreadInvoker."""

    def __init__(self):
        super().__init__()
        self.invoker = self

    def shutdown(self):
        self.calls.append(("invoker_shutdown", {}))


class _RealShutdownInvoker:
    """Unlike FakeHandlersWithInvoker's no-op shutdown() above, this backs
    onto a real ThreadPoolExecutor - the same primitive the real
    gui.mcp_bridge.MainThreadInvoker uses - so shutdown() genuinely
    disables future calls instead of just recording that it happened.
    FakeHandlersWithInvoker's stub can't catch DAN-912's restart() bug
    because a no-op shutdown has nothing to break; this one does."""

    def __init__(self):
        from concurrent.futures import ThreadPoolExecutor
        self._executor = ThreadPoolExecutor(max_workers=1)

    def call(self, fn):
        return self._executor.submit(fn).result(timeout=5)

    def shutdown(self):
        self._executor.shutdown(wait=False, cancel_futures=True)


class FakeHandlersWithRealInvoker(FakeHandlers):
    def __init__(self):
        super().__init__()
        self.invoker = _RealShutdownInvoker()


class TestServerLifecycle(unittest.TestCase):
    """core/mcp_server.McpServerController.start()/stop() - disabled and
    blank-token refusals, and (since `mcp` is deliberately NOT installed
    in this repo's own test/CI environment - see requirements.txt) the
    real "package not installed" degradation path, genuinely exercised
    rather than mocked."""

    def test_does_not_start_when_disabled(self):
        from core.config import McpSettings
        controller = mcp_server.McpServerController(FakeHandlersWithInvoker())
        self.assertFalse(controller.start(McpSettings(enabled=False)))
        self.assertFalse(controller.running)

    def test_refuses_a_blank_token(self):
        from core.config import McpSettings
        controller = mcp_server.McpServerController(FakeHandlersWithInvoker())
        self.assertFalse(controller.start(McpSettings(enabled=True, token="")))
        self.assertFalse(controller.running)
        self.assertIn("token", controller.last_error.lower())

    def test_a_non_blank_token_clears_a_previous_blank_token_error(self):
        from core.config import McpSettings
        controller = mcp_server.McpServerController(FakeHandlersWithInvoker())
        controller.start(McpSettings(enabled=True, token=""))
        self.assertIsNotNone(controller.last_error)
        # Whatever happens next (missing package, or a real start), the
        # stale blank-token message must not be what's left on last_error.
        controller.start(McpSettings(enabled=True, token="abc123"))
        if controller.last_error is not None:
            self.assertNotIn("Blank bearer token", controller.last_error)

    @unittest.skipIf(HAVE_MCP, "this asserts the degradation path taken when `mcp` is ABSENT")
    def test_reports_the_missing_optional_package_rather_than_raising(self):
        from core.config import McpSettings
        controller = mcp_server.McpServerController(FakeHandlersWithInvoker())
        self.assertFalse(controller.start(McpSettings(enabled=True, token="abc123")))
        self.assertFalse(controller.running)
        self.assertIn("not installed", controller.last_error)
        self.assertIn(PINNED_INSTALL, controller.last_error)

    def test_stop_is_safe_to_call_when_never_started(self):
        controller = mcp_server.McpServerController(FakeHandlersWithInvoker())
        controller.stop()  # must not raise
        self.assertFalse(controller.running)

    def test_restart_on_a_never_started_controller_does_not_kill_the_invoker(self):
        """DAN-912: restart() = stop() then start(). stop() used to call
        handlers.invoker.shutdown() unconditionally, including the very
        first time a user ever enables the server through Settings -
        which is a restart() on a controller that was never started.
        That permanently killed the shared MainThreadInvoker's
        ThreadPoolExecutor, since it is constructed once per MainWindow
        lifetime and never recreated. Regardless of whether start()
        itself goes on to succeed is irrelevant here (it's forced to fail
        below, so this doesn't depend on `mcp`/a real port being
        available) - a tool call marshaled through the invoker
        afterward must still work regardless."""
        from core.config import McpSettings

        handlers = FakeHandlersWithRealInvoker()
        controller = mcp_server.McpServerController(handlers)
        with mock.patch.object(mcp_server, "_build_asgi_app", side_effect=ImportError("no mcp")):
            controller.restart(McpSettings(enabled=True, token="abc123"))
        self.assertEqual(handlers.invoker.call(lambda: 42), 42)


class _FakeUvicornConfig:
    def __init__(self, app, host, port, log_level):
        self.app, self.host, self.port, self.log_level = app, host, port, log_level


class _FakeUvicornServerBindSucceeds:
    """Mirrors the one bit of real uvicorn.Server behaviour start() relies
    on: `.started` flips True once the (fake) bind completes, and `.run()`
    keeps the thread alive until `.should_exit` is set."""

    def __init__(self, config):
        self.config = config
        self.started = False
        self.should_exit = False

    def run(self):
        self.started = True
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline and not self.should_exit:
            time.sleep(0.01)


class _FakeUvicornServerBindFails:
    """Mirrors a busy port: the bind inside run() raises before `.started`
    is ever set, exactly like a real uvicorn.Server hitting EADDRINUSE."""

    def __init__(self, config):
        self.config = config
        self.started = False
        self.should_exit = False

    def run(self):
        raise OSError(98, "Address already in use")


class TestPortBusyDetection(unittest.TestCase):
    """DAN-817 item 1 / DAN-822: start() launched _serve() on a background
    thread and returned True before the uvicorn bind actually happened, so
    a busy-port failure - caught only by _serve()'s own broad except,
    which never touched last_error - was unreportable to any caller. These
    fake out `uvicorn` itself (never installed in this repo's test env)
    so the bind-wait logic is exercised directly rather than inferred."""

    def _start(self, server_cls, **settings_kwargs):
        from core.config import McpSettings

        controller = mcp_server.McpServerController(FakeHandlersWithInvoker())
        fake_uvicorn = types.ModuleType("uvicorn")
        fake_uvicorn.Config = _FakeUvicornConfig
        fake_uvicorn.Server = server_cls
        with mock.patch.object(mcp_server, "_build_asgi_app", return_value=object()), \
                mock.patch.dict(sys.modules, {"uvicorn": fake_uvicorn}):
            result = controller.start(McpSettings(enabled=True, token="abc123", **settings_kwargs))
        return controller, result

    def test_a_busy_port_is_reported_through_last_error(self):
        controller, result = self._start(_FakeUvicornServerBindFails)
        self.assertFalse(result)
        self.assertFalse(controller.running)
        self.assertIsNotNone(controller.last_error)
        self.assertIn("Address already in use", controller.last_error)

    def test_a_successful_bind_is_reported_as_running_with_no_error(self):
        controller, result = self._start(_FakeUvicornServerBindSucceeds)
        try:
            self.assertTrue(result)
            self.assertTrue(controller.running)
            self.assertIsNone(controller.last_error)
        finally:
            controller.stop()


class TestMissingOrIncompatiblePackage(unittest.TestCase):
    """DAN-912 secondary finding: a plain `pip install mcp` installs mcp
    2.x, which renamed FastMCP to MCPServer - `_build_asgi_app`'s `from
    mcp.server.fastmcp import FastMCP` raises ImportError exactly like
    the package being absent, but start() reported the same "not
    installed" message either way, which is misleading when the package
    IS there, just the wrong major version."""

    def _start_with(self, import_error, mcp_module_present):
        from core.config import McpSettings

        controller = mcp_server.McpServerController(FakeHandlersWithInvoker())
        with mock.patch.object(mcp_server, "_build_asgi_app", side_effect=import_error), \
                mock.patch.object(
                    mcp_server, "_mcp_top_level_import_succeeds", return_value=mcp_module_present,
                ):
            controller.start(McpSettings(enabled=True, token="abc123"))
        return controller

    def test_a_genuinely_absent_package_still_says_not_installed(self):
        controller = self._start_with(ImportError("No module named 'mcp'"), mcp_module_present=False)
        self.assertIn("not installed", controller.last_error)
        self.assertIn(PINNED_INSTALL, controller.last_error)

    def test_an_installed_but_incompatible_package_says_so_instead(self):
        controller = self._start_with(
            ImportError(
                "No module named 'mcp.server.fastmcp'. This is mcp 2.x, where FastMCP "
                "was renamed to MCPServer"
            ),
            mcp_module_present=True,
        )
        self.assertIn("incompatible", controller.last_error)
        self.assertIn(PINNED_INSTALL, controller.last_error)
        self.assertNotIn("not installed", controller.last_error)

    def test_the_incompatible_message_survives_a_shell_paste(self):
        # The recovery command is user-facing copy; shlex must see it as a
        # single quoted spec, not `mcp` followed by redirections.
        import shlex
        controller = self._start_with(ImportError("renamed"), mcp_module_present=True)
        command = re.search(r"`([^`]+)`", controller.last_error).group(1)
        self.assertEqual(shlex.split(command), ["venv/bin/pip", "install", "mcp>=1.2,<2"])

    def test_helper_itself_says_true_when_mcp_is_importable(self):
        with mock.patch.dict(sys.modules, {"mcp": types.ModuleType("mcp")}):
            self.assertTrue(mcp_server._mcp_top_level_import_succeeds())

    def test_helper_itself_says_false_when_mcp_cannot_be_imported(self):
        # A None entry in sys.modules is the standard way to force an
        # import to fail regardless of what's really installed on disk.
        with mock.patch.dict(sys.modules, {"mcp": None}):
            self.assertFalse(mcp_server._mcp_top_level_import_succeeds())


class TestInstallCommandIsPinnedEverywhere(unittest.TestCase):
    """DAN-1139: PyPI's latest `mcp` is 2.x, which this app cannot run, so
    an unpinned `pip install mcp` in our own docs or error text sends every
    first-time user into the incompatible-package error."""

    _USER_FACING = ("README.md", "core/mcp_server.py", "gui/settings_dialog.py")
    _UNPINNED = re.compile(r"pip install\s+mcp(?![>=<~!])")

    @staticmethod
    def _read(relative):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, relative), encoding="utf-8") as handle:
            return handle.read()

    def test_every_user_facing_install_command_is_the_quoted_pinned_form(self):
        for relative in self._USER_FACING:
            with self.subTest(file=relative):
                text = self._read(relative)
                self.assertIn(PINNED_INSTALL, text)
                self.assertEqual(self._UNPINNED.findall(text), [])

    def test_no_install_command_is_left_with_a_bare_shell_redirection(self):
        for relative in self._USER_FACING:
            with self.subTest(file=relative):
                self.assertNotRegex(self._read(relative), r"pip install\s+mcp[>=<]")


def _annotated_fake_handlers():
    """A handlers object with every _HANDLER_METHOD_NAMES method present,
    like FakeHandlers above, but with a real `-> Dict[str, Any]` return
    annotation on each one - matching every actual gui.mcp_bridge.
    McpToolHandlers method - and no parameters, like every real tool
    call made with {} below. FakeHandlers' own bare `**kwargs` methods
    have no return annotation for functools.wraps to copy (so DAN-912's
    bug would go undetected using it) and FastMCP's own schema builder
    does not accept a bare **kwargs parameter the way a real handler's
    concrete signature works."""
    class Handlers:
        pass

    def _make(name):
        def method(self) -> Dict[str, Any]:
            return {"tool": name}
        method.__name__ = name
        return method

    for method_name in set(mcp_server._HANDLER_METHOD_NAMES.values()):
        setattr(Handlers, method_name, _make(method_name))
    return Handlers()


@unittest.skipUnless(HAVE_MCP, "the optional `mcp` package is not installed")
class TestRealToolRegistration(unittest.TestCase):
    """DAN-912 Defect A: _wrap_tool's @functools.wraps(method) copies the
    mirrored handler method's own return-type annotation onto call() -
    but call() actually returns a list of MCP content blocks, not that
    annotation's type. FastMCP (the real package) builds its output
    schema from whatever annotation call() carries and validates the
    REAL return value against it, so every tool call failed.
    TestContentBlocks below only drives to_content_blocks() directly,
    which never touches FastMCP's own schema machinery at all - this
    goes through the real mcp_app.add_tool()/call_tool() round trip
    core/mcp_server.py's own _register_tools() wires up in production
    (not a copy of its logic), so this exact gap cannot reopen
    silently."""

    def test_every_registered_tool_survives_a_real_fastmcp_round_trip(self):
        import asyncio
        from mcp.server.fastmcp import FastMCP

        app = FastMCP("test")
        mcp_server._register_tools(app, _annotated_fake_handlers())

        async def _call_all():
            for spec in mcp_tools.TOOLS:
                await app.call_tool(spec.name, {})

        asyncio.run(_call_all())  # must not raise ToolError


@unittest.skipUnless(HAVE_MCP, "the optional `mcp` package is not installed")
class TestContentBlocks(unittest.TestCase):
    def test_a_plain_result_becomes_one_text_block(self):
        from core.mcp_content import to_content_blocks
        blocks = to_content_blocks({"row": 1, "filename": "a.jpg"})
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].type, "text")
        self.assertIn("a.jpg", blocks[0].text)

    def test_an_image_key_becomes_an_image_block_plus_its_metadata(self):
        from core.mcp_content import to_content_blocks
        blocks = to_content_blocks({
            "row": 1,
            "local_image": {"bytes": b"\x89PNG", "mime_type": "image/png"},
        })
        image_blocks = [b for b in blocks if b.type == "image"]
        self.assertEqual(len(image_blocks), 1)
        self.assertEqual(image_blocks[0].mimeType, "image/png")
        text_block = next(b for b in blocks if b.type == "text")
        self.assertNotIn("PNG", text_block.text)  # raw bytes never leak into the text block

    def test_a_failed_fetch_with_no_bytes_produces_no_image_block(self):
        from core.mcp_content import to_content_blocks
        blocks = to_content_blocks({
            "match_image": {"bytes": None, "mime_type": "image/jpeg", "url": "https://x"},
        })
        self.assertEqual([b for b in blocks if b.type == "image"], [])
        text_block = next(b for b in blocks if b.type == "text")
        self.assertIn("fetch_failed", text_block.text)


if __name__ == "__main__":
    unittest.main()
