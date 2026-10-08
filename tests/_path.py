"""Puts the project root on sys.path so tests can `import core.*` when run
from anywhere, and points the config directory at a throwaway one.

Imported for its side effect by each test module, which is what makes it
the right place for both: it runs before any test module imports core,
and core.paths reads XDG_CONFIG_HOME once at import time.

The config redirect matters more than it looks. Without it a test that
calls search_image writes a real entry into the USER'S OWN search cache -
observed happening, and it then fed a fabricated result back to a later
run. The user's config file also holds their Hydrus and SauceNAO keys.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# setdefault, so a module that wants its own directory (test_gui_smoke)
# can still set one, and so a deliberate override from the environment
# is honoured.
os.environ.setdefault("XDG_CONFIG_HOME", tempfile.mkdtemp(prefix="hatate-tests-"))
