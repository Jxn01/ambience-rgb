"""The Linux virtualenv lives OUTSIDE the checkout, decided in one place.

The checkout can be shared with Windows (one tree on an NTFS drive that both
halves of a dual boot mount), and Windows' venv is ``.venv`` in the checkout --
its scheduled tasks run ``.venv\\Scripts\\pythonw.exe``. A Linux venv in the same
place collides with it: one ``pyvenv.cfg``, and ``lib``/``Lib`` differing only
in case. So every Linux script and unit takes the venv from
``scripts/linux-venv.sh`` and none may name an in-tree ``.venv``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
UNITS = REPO / "systemd"

# An in-tree venv, spelled the ways a Linux script or unit would spell it.
IN_TREE_VENV = re.compile(r"(\$\{?REPO_ROOT\}?|\$\{?REPO\}?|@INSTALL_DIR@)/\.venv\b")

# Every Linux script that runs the venv's interpreter.
VENV_USERS = ("install.sh", "uninstall.sh", "integration.sh", "test-under-wine.sh")


def code_of(path: Path) -> str:
    """The file without its comment lines: comments may explain the old layout."""

    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def linux_side_files() -> list[Path]:
    return sorted(SCRIPTS.glob("*.sh")) + sorted(UNITS.glob("*.service"))


class InTreeVenvGuardTest(unittest.TestCase):
    def test_no_linux_script_or_unit_uses_an_in_tree_venv(self) -> None:
        for path in linux_side_files():
            with self.subTest(file=path.name):
                self.assertIsNone(IN_TREE_VENV.search(code_of(path)))

    def test_the_guard_pattern_still_bites(self) -> None:
        """Positive control: each spelling the guard exists for is caught."""

        for bad in (
            'VENV="$REPO_ROOT/.venv"',
            'PY="${REPO_ROOT}/.venv/bin/python"',
            'host_python="$REPO/.venv/bin/python"',
            "ExecStart=@INSTALL_DIR@/.venv/bin/python -m ambience_rgb run",
        ):
            with self.subTest(sample=bad):
                self.assertIsNotNone(IN_TREE_VENV.search(bad))

    def test_every_script_that_runs_the_venv_takes_it_from_the_one_place(self) -> None:
        for name in VENV_USERS:
            with self.subTest(script=name):
                code = code_of(SCRIPTS / name)
                self.assertIn("scripts/linux-venv.sh", code)
                self.assertIn("AMBIENCE_RGB_VENV", code)

    def test_units_run_the_venv_the_installer_fills_in(self) -> None:
        for path in sorted(UNITS.glob("*.service")):
            code = code_of(path)
            if "-m ambience_rgb" not in code:
                continue  # the OpenRGB server unit runs OpenRGB, not us
            with self.subTest(unit=path.name):
                self.assertIn("@VENV_DIR@/bin/python -m ambience_rgb", code)


@unittest.skipIf(sys.platform == "win32" or shutil.which("bash") is None, "needs bash")
class DefaultLocationTest(unittest.TestCase):
    def resolve(self, **env: str) -> str:
        clean = {k: v for k, v in os.environ.items() if k not in ("AMBIENCE_RGB_VENV", "XDG_DATA_HOME")}
        clean.update(env)
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; printf %s "$AMBIENCE_RGB_VENV"', "_", str(SCRIPTS / "linux-venv.sh")],
            env=clean, capture_output=True, encoding="utf-8", check=True,
        )
        return result.stdout

    def test_the_default_is_under_the_users_data_dir_not_the_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            venv = self.resolve(HOME=home)
            self.assertEqual(venv, f"{home}/.local/share/venvs/ambience-rgb")
            self.assertFalse(Path(venv).is_relative_to(REPO))

    def test_xdg_data_home_is_honoured(self) -> None:
        self.assertEqual(self.resolve(XDG_DATA_HOME="/data"), "/data/venvs/ambience-rgb")

    def test_an_explicit_override_wins(self) -> None:
        self.assertEqual(self.resolve(AMBIENCE_RGB_VENV="/opt/v", XDG_DATA_HOME="/data"), "/opt/v")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
