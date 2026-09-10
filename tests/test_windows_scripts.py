"""The Windows installers, checked from Linux.

The scheduled tasks are the Windows half's systemd units, and they get the same
treatment as ``test_unit_files.py``: checked as text for what must be there --
and, where a PowerShell is available, executed. ``tasks.ps1`` holds pure
functions that return the XML ``Register-ScheduledTask -Xml`` is given, so this
renders every task with a real pwsh and parses what came out. The XML that is
registered on Windows is the XML tested here.

Set ``AMBIENCE_RGB_PWSH`` to a pwsh binary to run those tests where none is on
PATH. On the Windows half ``pwsh`` is on PATH already.

⚠️ **What this cannot tell you:** that Task Scheduler accepts the registration,
that the triggers fire, or that OpenRGB's Windows build takes these flags. That
needs Windows, and is on the verification list in the README.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from ambience_rgb import hints

REPO = Path(__file__).resolve().parents[1]
WINDOWS = REPO / "scripts" / "windows"
SCRIPTS = sorted(WINDOWS.glob("*.ps1"))
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
PWSH = os.environ.get("AMBIENCE_RGB_PWSH") or shutil.which("pwsh")

SAMPLE_USER = r"HOST\someone"
SAMPLE_ROOT = r"C:\R&D\ambience-rgb"
SAMPLE_PYTHONW = r"C:\R&D\ambience-rgb\.venv\Scripts\pythonw.exe"


def read(name: str) -> str:
    return (WINDOWS / name).read_text(encoding="utf-8")


def code(text: str) -> str:
    """The script without its comments -- ``<# ... #>`` blocks and ``#`` lines."""

    text = re.sub(r"<#.*?#>", "", text, flags=re.S)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


class ScriptSetTest(unittest.TestCase):
    def test_the_expected_scripts_exist(self) -> None:
        self.assertEqual({path.name for path in SCRIPTS}, {"install.ps1", "uninstall.ps1", "tasks.ps1"})


class PortabilityTest(unittest.TestCase):
    """🚨 None of the author's paths may be baked in.

    On the Windows half the account is ``jxn`` while the profile folder is
    ``jxnpe`` -- and ``jxnpe`` is not an account Task Scheduler can map. A
    literal path is a trap for this machine, never mind anyone else's.
    """

    def test_no_script_hard_codes_a_profile_or_a_home(self) -> None:
        for path in SCRIPTS:
            text = path.read_text(encoding="utf-8")
            for needle in ("C:\\Users", "jxnpe", "/home/"):
                with self.subTest(script=path.name, needle=needle):
                    self.assertNotIn(needle, text)

    def test_every_script_is_plain_ascii(self) -> None:
        """🚨 Windows PowerShell 5.1 reads a .ps1 without a byte-order mark as
        the ANSI code page, not UTF-8. One em dash inside a string becomes
        mojibake there -- or a stray quote the parser then trips over. ASCII
        reads the same in every PowerShell."""

        for path in SCRIPTS:
            with self.subTest(script=path.name):
                path.read_bytes().decode("ascii")

    def test_the_scripts_that_act_stop_on_errors(self) -> None:
        for name in ("install.ps1", "uninstall.ps1"):
            with self.subTest(script=name):
                self.assertIn('$ErrorActionPreference = "Stop"', read(name))

    def test_registration_failures_are_not_swallowed(self) -> None:
        """Without -ErrorAction Stop an "Access is denied" is non-terminating,
        and the success banner prints straight over it."""

        for path in SCRIPTS:
            for line in code(path.read_text(encoding="utf-8")).splitlines():
                if "Register-ScheduledTask" in line:
                    with self.subTest(script=path.name, line=line.strip()):
                        self.assertIn("-ErrorAction Stop", line)


class InstallerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = read("install.ps1")

    def test_the_venv_does_not_see_system_site_packages(self) -> None:
        """🚨 The hidapi rule inverts on Windows.

        On Linux the ``hid`` binding is the distribution's and pip must never
        touch it; on Windows it comes from pip (``hidapi``) and nothing else.
        A venv that could see a system site-packages would bring back exactly
        the shadowing the Linux rule exists to prevent.
        """

        self.assertNotIn("--system-site-packages", code(self.text))
        self.assertIn("-m venv", self.text)

    def test_it_checks_the_hid_module_is_cython_hidapi(self) -> None:
        self.assertIn("hid.device", self.text)

    def test_the_daemon_runs_under_pythonw(self) -> None:
        # python.exe would put a console window on the desktop at every logon.
        self.assertIn(r"Scripts\pythonw.exe", self.text)

    def test_openrgb_settings_are_edited_by_the_shared_python_module(self) -> None:
        self.assertIn("ambience_rgb.openrgb_settings", self.text)
        self.assertNotIn("ConvertTo-Json", code(self.text), "its default depth of 2 flattens OpenRGB.json")

    def test_it_requires_elevation(self) -> None:
        self.assertIn("WindowsBuiltInRole]::Administrator", self.text)

    def test_the_account_comes_from_the_token_not_a_literal(self) -> None:
        self.assertIn("WindowsIdentity]::GetCurrent()", self.text)

    def test_it_looks_for_software_that_would_fight_for_the_hardware(self) -> None:
        # Both are set to start automatically on the author's Windows half.
        for service in ("CorsairService", "SignalRgb.Service"):
            with self.subTest(service=service):
                self.assertIn(service, self.text)
        self.assertIn("DisableCompetitors", self.text)

    def test_the_shutdown_task_is_handed_the_resolved_config(self) -> None:
        self.assertIn("resolved_config_path", self.text)

    def test_it_prints_what_matterlights_needs(self) -> None:
        self.assertIn("RGB_PUBLISH_SOCKET=", self.text)


class TaskNamesTest(unittest.TestCase):
    """One vocabulary for both halves: the task names are the unit names."""

    def test_the_scripts_and_hints_agree(self) -> None:
        text = read("tasks.ps1")
        for variable, name in (
            ("AmbienceDaemonTask", hints.DAEMON_TASK),
            ("AmbienceOpenRgbTask", hints.OPENRGB_TASK),
            ("AmbienceShutdownTask", hints.SHUTDOWN_TASK),
        ):
            with self.subTest(variable=variable):
                self.assertRegex(text, rf'\${variable}\s*=\s*"{re.escape(name)}"')

    def test_the_names_are_the_systemd_unit_names(self) -> None:
        units = {path.stem for path in (REPO / "systemd").glob("*.service")}
        self.assertEqual(units, {hints.DAEMON_TASK, hints.OPENRGB_TASK, hints.SHUTDOWN_TASK})

    def test_the_shipped_config_restarts_the_task_that_exists(self) -> None:
        config = tomllib.loads((REPO / "config.example.toml").read_text(encoding="utf-8"))
        windows = " ".join(config["openrgb"]["restart_command"]["windows"])
        self.assertIn(f"-TaskName {hints.OPENRGB_TASK}", windows)


@unittest.skipUnless(PWSH, "no PowerShell; set AMBIENCE_RGB_PWSH to a pwsh binary")
class PowerShellTest(unittest.TestCase):
    def _run(self, script: str, env: dict | None = None) -> str:
        result = subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, **(env or {})},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def _parse_errors(self, source_expression: str, env: dict | None = None) -> str:
        return self._run(
            "$errors = $null; $tokens = $null; "
            f"[System.Management.Automation.Language.Parser]::{source_expression}, [ref]$tokens, [ref]$errors) | Out-Null; "
            "$errors | ForEach-Object { $_.Extent.StartLineNumber.ToString() + ': ' + $_.Message }",
            env,
        ).strip()

    def test_every_script_parses(self) -> None:
        for path in SCRIPTS:
            with self.subTest(script=path.name):
                self.assertEqual(self._parse_errors(f"ParseFile('{path}'"), "")

    def test_the_windows_restart_command_parses(self) -> None:
        config = tomllib.loads((REPO / "config.example.toml").read_text(encoding="utf-8"))
        command = config["openrgb"]["restart_command"]["windows"][-1]
        self.assertEqual(self._parse_errors("ParseInput($env:AMBIENCE_COMMAND", {"AMBIENCE_COMMAND": command}), "")

    def _render(self, call: str) -> ET.Element:
        xml = self._run(f". '{WINDOWS / 'tasks.ps1'}'; {call}").strip()
        # Declared UTF-16, as Task Scheduler expects; parse it as exactly that.
        return ET.fromstring(xml.encode("utf-16"))

    @staticmethod
    def _text(root: ET.Element, path: str) -> str:
        node = root.find(path, NS)
        assert node is not None, f"missing {path}"
        return node.text or ""

    def test_the_daemon_task(self) -> None:
        root = self._render(
            f"New-AmbienceDaemonTaskXml -UserId '{SAMPLE_USER}' -Pythonw '{SAMPLE_PYTHONW}' "
            rf"-RepoRoot '{SAMPLE_ROOT}' -LogFile 'C:\Logs & Things\ambience-rgb.log'"
        )
        self.assertEqual(self._text(root, "t:Triggers/t:LogonTrigger/t:UserId"), SAMPLE_USER)
        self.assertEqual(self._text(root, "t:Principals/t:Principal/t:UserId"), SAMPLE_USER)
        self.assertEqual(self._text(root, "t:Principals/t:Principal/t:LogonType"), "InteractiveToken")
        self.assertEqual(self._text(root, "t:Principals/t:Principal/t:RunLevel"), "LeastPrivilege")
        # 🚨 Task Scheduler stops any task after 72 hours unless told otherwise.
        self.assertEqual(self._text(root, "t:Settings/t:ExecutionTimeLimit"), "PT0S")
        self.assertEqual(self._text(root, "t:Settings/t:MultipleInstancesPolicy"), "IgnoreNew")
        self.assertEqual(self._text(root, "t:Settings/t:DisallowStartIfOnBatteries"), "false")
        self.assertIsNotNone(root.find("t:Settings/t:RestartOnFailure", NS))
        self.assertEqual(self._text(root, "t:Actions/t:Exec/t:Command"), SAMPLE_PYTHONW, "& must survive the XML")
        self.assertEqual(self._text(root, "t:Actions/t:Exec/t:WorkingDirectory"), SAMPLE_ROOT)
        arguments = self._text(root, "t:Actions/t:Exec/t:Arguments")
        self.assertIn("-m ambience_rgb", arguments)
        self.assertIn(r'--log-file "C:\Logs & Things\ambience-rgb.log"', arguments)
        self.assertTrue(arguments.endswith(" run"), arguments)

    def test_the_openrgb_task(self) -> None:
        root = self._render(
            rf"New-AmbienceOpenRgbTaskXml -UserId '{SAMPLE_USER}' -OpenRgb 'C:\Program Files\OpenRGB\OpenRGB.exe' "
            r"-WorkingDirectory 'C:\Program Files\OpenRGB' -Port 6742"
        )
        # SMBus -- the RAM and the GPU -- needs OpenRGB elevated on Windows.
        self.assertEqual(self._text(root, "t:Principals/t:Principal/t:RunLevel"), "HighestAvailable")
        self.assertEqual(self._text(root, "t:Principals/t:Principal/t:LogonType"), "InteractiveToken")
        self.assertEqual(self._text(root, "t:Settings/t:ExecutionTimeLimit"), "PT0S")
        self.assertEqual(
            self._text(root, "t:Actions/t:Exec/t:Arguments"),
            "--server --server-host 127.0.0.1 --server-port 6742",
            "OpenRGB's own default host is 0.0.0.0 -- lighting control on the LAN",
        )
        self.assertEqual(self._text(root, "t:Actions/t:Exec/t:Command"), r"C:\Program Files\OpenRGB\OpenRGB.exe")
        self.assertEqual(self._text(root, "t:Actions/t:Exec/t:WorkingDirectory"), r"C:\Program Files\OpenRGB")

    def test_the_shutdown_task(self) -> None:
        root = self._render(
            f"New-AmbienceShutdownTaskXml -Pythonw '{SAMPLE_PYTHONW}' -RepoRoot '{SAMPLE_ROOT}' "
            rf"-Config 'C:\R&D\config.toml' -LogFile 'C:\Logs\lights-off.log'"
        )
        subscription = self._text(root, "t:Triggers/t:EventTrigger/t:Subscription")
        self.assertIn("EventID=1074", subscription)
        self.assertIn("User32", subscription)
        ET.fromstring(subscription)  # it is XML inside XML; it must survive both
        # SYSTEM, in session 0: by event 1074 the user's session is already
        # being torn down and Windows will not start a process in it. The SID,
        # because the account's NAME is localised.
        self.assertEqual(self._text(root, "t:Principals/t:Principal/t:UserId"), "S-1-5-18")
        self.assertEqual(self._text(root, "t:Principals/t:Principal/t:RunLevel"), "HighestAvailable")
        self.assertEqual(self._text(root, "t:Settings/t:ExecutionTimeLimit"), "PT1M", "bounded: the machine is going down")
        arguments = self._text(root, "t:Actions/t:Exec/t:Arguments")
        self.assertIn(r'--config "C:\R&D\config.toml"', arguments)
        self.assertIn(r'--log-file "C:\Logs\lights-off.log"', arguments)
        self.assertTrue(arguments.endswith(" off --stop-daemon"), arguments)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
