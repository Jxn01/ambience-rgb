"""Commands printed for a person must exist on that person's platform.

Before the Windows port, three files each carried their own ``systemctl`` line
-- the ``devices`` note, the OpenRGB wedge message, the hardware check. On
Windows each would have been advice to run a program that does not exist. They
all come from :mod:`ambience_rgb.hints` now, and the guards below keep the next
one there too.

The guards read the source as Python, not as text: a comment or a docstring may
explain what systemd does, but a string that reaches a person may not name a
platform's command outside the one module that knows which platform it is on.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from ambience_rgb import hints

SRC = Path(__file__).resolve().parents[1] / "src" / "ambience_rgb"
PLATFORM_COMMAND = re.compile(r"systemctl|Start-ScheduledTask|Stop-ScheduledTask|Disable-ScheduledTask")


def _docstring_nodes(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
        # Attribute docstrings: a bare string right after an assignment.
        body = getattr(node, "body", None)
        if isinstance(body, list):
            for previous, statement in zip(body, body[1:]):
                if (
                    isinstance(previous, (ast.Assign, ast.AnnAssign))
                    and isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Constant)
                ):
                    found.add(id(statement.value))
    return found


def _strings_naming_platform_commands(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    hits = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and PLATFORM_COMMAND.search(node.value)
        ):
            hits.append((node.lineno, node.value.strip()[:80]))
    return hits


class HintsTest(unittest.TestCase):
    def test_linux_speaks_systemd(self) -> None:
        self.assertIn("systemctl --user stop ambience-rgb", hints.stop_daemon("linux"))
        self.assertIn("systemctl --user start ambience-rgb", hints.start_daemon("linux"))
        self.assertIn("systemctl --user restart ambience-rgb-openrgb", hints.restart_openrgb("linux"))

    def test_windows_speaks_task_scheduler_and_never_systemd(self) -> None:
        texts = (hints.stop_daemon("win32"), hints.start_daemon("win32"), hints.restart_openrgb("win32"))
        for text in texts:
            with self.subTest(text=text):
                self.assertNotIn("systemctl", text)
        # Graceful: the daemon blacks and lets go. Stop-ScheduledTask may not be.
        self.assertIn("off --stop-daemon", hints.stop_daemon("win32"))
        self.assertIn("Start-ScheduledTask -TaskName ambience-rgb", hints.start_daemon("win32"))
        self.assertIn("ambience-rgb-openrgb", hints.restart_openrgb("win32"))


class PlatformCommandGuardTest(unittest.TestCase):
    def test_platform_commands_are_named_only_in_hints(self) -> None:
        # The integration checks print instructions to people too. The one
        # exception drives systemd units by design, and refuses to run on Windows.
        repo = SRC.parents[1]
        files = [path for path in sorted(SRC.rglob("*.py")) if path.name != "hints.py"]
        files += [
            path
            for path in sorted((repo / "tests" / "integration").glob("*.py"))
            if path.name != "check_failures.py"
        ]
        offenders = []
        for path in files:
            for line, text in _strings_naming_platform_commands(path.read_text(encoding="utf-8")):
                offenders.append(f"{path.relative_to(repo)}:{line}: {text}")
        self.assertEqual(offenders, [])

    def test_the_linux_only_check_refuses_windows_before_doing_anything(self) -> None:
        """The exemption above is safe only while check_failures.py refuses to run there."""

        text = (SRC.parents[1] / "tests" / "integration" / "check_failures.py").read_text(encoding="utf-8")
        refusal = text.index('if sys.platform == "win32":')
        self.assertLess(refusal, text.index("use_matterlights()\n"), "refuse before anything else runs")
        self.assertIn("raise SystemExit", text[refusal : refusal + 400])

    def test_the_guard_can_fail(self) -> None:
        source = 'def f():\n    """Explains systemctl."""\n    print("run: systemctl --user stop x")\n'
        self.assertEqual(len(_strings_naming_platform_commands(source)), 1, "the docstring is exempt, the print is not")


class UnixSocketGuardTest(unittest.TestCase):
    """``socket.AF_UNIX`` does not exist on Windows. Only the transport may reach for it."""

    @staticmethod
    def _uses_af_unix(source: str) -> bool:
        return any(
            isinstance(node, ast.Attribute) and node.attr == "AF_UNIX" for node in ast.walk(ast.parse(source))
        )

    def test_af_unix_is_reached_for_only_in_the_transport(self) -> None:
        offenders = [
            str(path.relative_to(SRC))
            for path in sorted(SRC.rglob("*.py"))
            if path.name != "transport.py" and self._uses_af_unix(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])

    def test_the_guard_can_fail(self) -> None:
        self.assertTrue(self._uses_af_unix("import socket\nsocket.socket(socket.AF_UNIX)\n"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
