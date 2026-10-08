"""Names that are used but never imported or defined anywhere.

This exists because of a real crash: gui/settings_dialog.py called
QApplication.processEvents() while QApplication was never imported. It
compiled cleanly (Python resolves globals at call time, not compile
time), so every check in place at the time passed - including the AST
sweep for unresolved self.X attributes, which only looks at attribute
access. The name simply didn't exist, and the first person to press the
button got a NameError and an aborted process.

PyQt6 isn't installable in every environment this is developed in, so
"just import the module and see" isn't available as a check. This walks
the syntax tree instead, and is deliberately conservative: a name bound
ANYWHERE in the file - any import, assignment, def, class, parameter,
comprehension target, with/except/for target - is accepted without
worrying about scope. That won't catch a name used outside the scope
that binds it, but it does catch the case that actually bit, with no
false positives to train anyone into ignoring it.
"""
import ast
import builtins
import os
import unittest

from . import _path  # noqa: F401

PACKAGES = ("core", "gui", "workers")


def _bound_names(tree: ast.AST) -> set:
    """Every name bound anywhere in the module, by any means."""
    bound = set()

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
            bound.update(node.names)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            bound.add(node.name)

    return bound


def _used_names(tree: ast.AST) -> set:
    return {
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


def _python_files():
    for package in PACKAGES:
        for dirpath, _dirs, filenames in os.walk(package):
            if "__pycache__" in dirpath:
                continue
            for filename in sorted(filenames):
                if filename.endswith(".py"):
                    yield os.path.join(dirpath, filename)


class TestNoUndefinedNames(unittest.TestCase):
    def test_every_module_resolves_its_names(self):
        builtin_names = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
        problems = []

        for path in _python_files():
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), path)
            unresolved = _used_names(tree) - _bound_names(tree) - builtin_names
            for name in sorted(unresolved):
                problems.append(f"{path}: {name!r} is used but never imported or defined")

        self.assertEqual(problems, [], "\n" + "\n".join(problems))


class TestNoDuplicateDictKeys(unittest.TestCase):
    """A repeated key in a dict literal is silently accepted by Python -
    the later one wins and the earlier is discarded without a word.

    This exists because it happened: a second `"yande.re"` entry was
    added to the soft-404 marker table while an earlier one was already
    there, so the new markers were dropped on the floor and the table
    quietly kept describing something else.
    """

    def test_no_dict_literal_repeats_a_key(self):
        problems = []
        for path in _python_files():
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                seen = set()
                for key in node.keys:
                    if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                        continue
                    if key.value in seen:
                        problems.append(
                            f"{path}:{key.lineno}: duplicate key {key.value!r} - "
                            "the earlier entry is silently discarded"
                        )
                    seen.add(key.value)
        self.assertEqual(problems, [], "\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
