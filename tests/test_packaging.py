"""What pip may install -- which is the opposite on the two platforms.

On Linux the ``hid`` binding is the distribution's (``python-hidapi``),
reached through a ``--system-site-packages`` venv, and pip must never be allowed
near it: the PyPI package named ``hid`` is a DIFFERENT library that shadows the
system one and dies with ``module 'hid' has no attribute 'device'``. On Windows
there is no distribution package; the binding comes from pip, as ``hidapi`` --
which is cython-hidapi. Two exactly opposite rules, so both are pinned here
rather than remembered.
"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _dependencies() -> list[str]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]


def _name(requirement: str) -> str:
    return re.split(r"[\s;<>=!~\[]", requirement, maxsplit=1)[0].lower()


class HidBindingTest(unittest.TestCase):
    def test_the_package_named_hid_is_never_declared(self) -> None:
        self.assertNotIn("hid", [_name(requirement) for requirement in _dependencies()])

    def test_windows_gets_cython_hidapi_from_pip_and_only_windows(self) -> None:
        hidapi = [requirement for requirement in _dependencies() if _name(requirement) == "hidapi"]
        self.assertEqual(len(hidapi), 1, "exactly one hidapi requirement")
        self.assertRegex(hidapi[0], r";\s*sys_platform\s*==\s*['\"]win32['\"]\s*$")

    def test_nothing_else_is_declared(self) -> None:
        # The USB Katana driver's pyserial and cryptography left with it;
        # everything else is the standard library.
        self.assertEqual([_name(requirement) for requirement in _dependencies()], ["hidapi"])

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
