"""Every text-mode file or pipe names its encoding.

Leave it out and Python uses the locale's code page: UTF-8 on Linux, the ANSI
code page on Windows -- 1252 on this rig's Windows half, read from its
registry. The same line then reads a file one way here and another way there.
`⚠️` is the bytes `E2 9A A0 EF B8 8F`; cp1252 has no character for `8F` and
raises, while cp1250, Central Europe's, turns the lot into `âš ď¸Ź` without a
word. Linux can show neither, because its locale *is* UTF-8 -- so a suite that
is green here says nothing about it.

Naming it is the point, not UTF-8 everywhere. The files these projects write are
UTF-8, but a Windows console program writes a pipe in the console's OEM code
page -- 850 on that same machine -- which Python calls `"oem"`.

Python names every such call at runtime (`python -X warn_default_encoding`), but
only on a path some test executes. This reads the source instead, so a call
nothing runs is caught too: its first run found three the runtime flag had
missed, in matterlights' service control, one of them on the Windows-only path.
By syntax alone it flags these, when no encoding is passed:

* `open(...)` with no mode or a literal text mode;
* `path.open("w")` and the like -- a literal mode as the first argument.
  `device.open()` with no arguments is left alone, because it looks the same;
* `.read_text()` and `.write_text()`;
* `subprocess.run`, `Popen`, `check_output`, `call` and `check_call` with
  `text=True` or `universal_newlines=True`;
* `tempfile.NamedTemporaryFile`, `TemporaryFile` and `SpooledTemporaryFile` with
  a literal text mode (their default is binary);
* `logging.FileHandler` and its rotating and watched subclasses.

A call passing `**kwargs` is not flagged: the encoding may be in there. Nor is
`configparser`'s `read()`, which looks like every other `.read()`; the runtime
flag above is what finds that one.

It is deliberately identical in ambience-rgb and matterlights, which are
deployed together and run on both operating systems.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNED_DIRS = ("src", "tests", "scripts")

MODE = re.compile(r"[rwxabtU+]+")
SUBPROCESS_CALLS = {"run", "Popen", "check_output", "call", "check_call"}
TEMPFILE_CALLS = {"NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile"}
# The position each takes `encoding` at, when it is passed positionally.
LOG_FILE_HANDLERS = {
    "FileHandler": 2,
    "WatchedFileHandler": 2,
    "RotatingFileHandler": 4,
    "TimedRotatingFileHandler": 4,
}


def _text_mode(call: ast.Call, position: int, *, default_text: bool, mode_shaped: bool = False) -> bool:
    node = next((keyword.value for keyword in call.keywords if keyword.arg == "mode"), None)
    if node is None:
        if len(call.args) <= position:
            return default_text
        node = call.args[position]
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return False  # a computed mode: unknowable, and a guard that guesses cries wolf
    if mode_shaped and not MODE.fullmatch(node.value):
        return False
    return "b" not in node.value


def _is_true(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _leaves_encoding_to_locale(call: ast.Call, keywords: dict) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        name = func.id
        if name == "open":
            return len(call.args) < 4 and _text_mode(call, 1, default_text=True)
    elif isinstance(func, ast.Attribute):
        name = func.attr
        if name == "open":
            return len(call.args) < 3 and _text_mode(call, 0, default_text=False, mode_shaped=True)
        if name == "read_text":
            return not call.args
        if name == "write_text":
            return len(call.args) < 2
    else:
        return False
    if name in SUBPROCESS_CALLS:
        return _is_true(keywords.get("text")) or _is_true(keywords.get("universal_newlines"))
    if name in TEMPFILE_CALLS:
        return len(call.args) < 3 and _text_mode(call, 0, default_text=False)
    if name in LOG_FILE_HANDLERS:
        return len(call.args) <= LOG_FILE_HANDLERS[name]
    return False


def unnamed_encodings(tree: ast.AST) -> list[int]:
    """Line numbers of text-mode I/O calls that leave the encoding to the locale."""

    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        if "encoding" in keywords or None in keywords:  # None is **kwargs
            continue
        if _leaves_encoding_to_locale(node, keywords):
            lines.append(node.lineno)
    return sorted(lines)


def _python_files() -> list[Path]:
    files: list[Path] = []
    for directory in SCANNED_DIRS:
        root = REPO_ROOT / directory
        if root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
    return files


class TextEncodingTest(unittest.TestCase):
    def test_every_text_mode_call_names_its_encoding(self) -> None:
        offenders = []
        for path in _python_files():
            source = path.read_text(encoding="utf-8")
            lines = source.splitlines()
            for number in unnamed_encodings(ast.parse(source, filename=str(path))):
                where = path.relative_to(REPO_ROOT).as_posix()
                offenders.append(f"{where}:{number}: {lines[number - 1].strip()}")
        self.assertFalse(
            offenders,
            'Text I/O that leaves the encoding to the locale -- name it: "utf-8" for '
            'a file, "oem" for what a Windows console program writes:\n  '
            + "\n  ".join(offenders),
        )

    def test_the_scan_covers_the_source(self) -> None:
        scanned = {path.relative_to(REPO_ROOT).as_posix() for path in _python_files()}
        self.assertIn("tests/test_text_encoding.py", scanned)
        self.assertTrue(any(name.startswith("src/") for name in scanned), "nothing under src/ was read")

    def test_it_flags_each_shape(self) -> None:
        for case in (
            'open("x")',
            'open("x", "w")',
            'open("x", mode="a")',
            'Path("x").read_text()',
            'Path("x").write_text("y")',
            'Path("x").open("w")',
            'subprocess.run(["x"], text=True)',
            'subprocess.check_output(["x"], universal_newlines=True)',
            'tempfile.NamedTemporaryFile("w")',
            'logging.FileHandler("x")',
            'logging.handlers.RotatingFileHandler("x", maxBytes=1)',
        ):
            with self.subTest(case=case):
                self.assertEqual(unnamed_encodings(ast.parse(case)), [1])

    def test_it_leaves_the_rest_alone(self) -> None:
        for case in (
            'open("x", "rb")',
            'open("x", encoding="utf-8")',
            'open("x", "r", -1, "utf-8")',
            "open(path, mode)",
            "open(**options)",
            'Path("x").read_text(encoding="utf-8")',
            'Path("x").write_text("y", "utf-8")',
            "device.open()",
            "device.open(0x1B1C, 0x0C32)",
            'webbrowser.open("http://localhost")',
            'subprocess.run(["x"])',
            'subprocess.run(["x"], text=True, encoding="utf-8")',
            "tempfile.NamedTemporaryFile()",
            "tempfile.NamedTemporaryFile(delete=False)",
            'logging.FileHandler("x", encoding="utf-8")',
            "logging.StreamHandler()",
        ):
            with self.subTest(case=case):
                self.assertEqual(unnamed_encodings(ast.parse(case)), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
