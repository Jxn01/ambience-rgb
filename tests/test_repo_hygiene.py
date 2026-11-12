"""Nothing secret-shaped may be tracked by git.

**A `.gitignore` entry of `.env` does not cover `.env.bak-040827`.**

The ignore rule says `.env`, which is an exact filename match, so a timestamped
backup of a live `.env` walks straight past it. This test keeps credentials out
of the tracked tree. It is deliberately identical to the one in matterlights:
the two projects are deployed together, and a rule that only one of them
enforces is a rule that the other will eventually break.

It checks the two independent ways a secret gets in: by **filename** (a file that
is obviously a credential store) and by **content** (a token-shaped string in a
file nobody thought of as a credential store).

**It is a `unittest.TestCase`, not bare pytest functions,** because the
documented command is `python -m unittest discover -s tests` and that command
does not see pytest functions at all: a guard the documented command skips
guards nothing. pytest collects a `TestCase` just the same.

**On the patterns being built by concatenation:** a literal JWT prefix written
out in this file would be found by this file's own sweep, and the test would fail
forever on itself. Assembling the needle at runtime means it never exists contiguously on disk.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Filenames that are credential stores by nature. `.env.example` is the one
# deliberate exception: it is the documented template and carries placeholders.
SECRET_FILENAMES = re.compile(
    r"(^|/)("
    r"\.env(\..+)?"
    r"|.*\.(pem|key|p12|pfx|keystore)"
    r"|credentials?(\..*)?"
    r"|secrets?(\..*)?"
    r"|\.?[\w-]*token"
    r")$",
    re.IGNORECASE,
)
FILENAME_ALLOWED = {".env.example"}

# Assembled at runtime -- see the module docstring. A Home Assistant long-lived
# access token is a JWT, so this is the exact shape of the token that leaked.
# It is scanned for in EVERY tracked file, because the whole point is that the
# file nobody thought of as a credential store is the one that gets you.
JWT = re.compile("ey" + r"J[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")

# The `KEY=value` scan needs BOTH halves to fire, and is restricted to env-style
# files. Each filter alone was useless when tried:
#
#   * value shape alone flagged every long config line -- `LIGHT_ZONE_LAYOUT`,
#     `HA_LIGHT_ENTITIES` -- which are not secrets and never will be;
#   * key name alone flagged `get_value("HA_TOKEN")` in the config loader and
#     `"HA_TOKEN=test-token"` in four test fixtures.
#
# A guard that cries wolf is a guard somebody deletes. So: the NAME says the
# field is a credential, the VALUE says it is a real one, and the FILE is a
# place credentials actually live.
ENV_STYLE = re.compile(r"(^|/)\.env(\..+)?$|\.(env|ini|conf|cfg)$", re.IGNORECASE)
ASSIGNMENT = re.compile(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.+?)\s*$")
SECRET_KEY = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL)",
    re.IGNORECASE,
)

# Anything shorter than this cannot be a real credential; `test-token` is 10.
MIN_CREDENTIAL_LENGTH = 20
PLACEHOLDER_WORDS = (
    "replace", "your", "example", "changeme", "placeholder",
    "dummy", "fake", "sample", "redacted", "xxx", "todo", "none",
)


def _is_placeholder(value: str) -> bool:
    value = value.strip().strip("\"'")
    if len(value) < MIN_CREDENTIAL_LENGTH:
        return True
    if value.startswith("<") and value.endswith(">"):
        return True
    lowered = value.lower()
    return any(word in lowered for word in PLACEHOLDER_WORDS)


def _tracked_files() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
            capture_output=True, check=True, encoding="utf-8",
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:  # pragma: no cover - not a git checkout
        raise unittest.SkipTest(f"not a git checkout: {error}") from error
    return [name for name in out.split("\0") if name]


def _read(name: str) -> str | None:
    try:
        return (REPO_ROOT / name).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None  # binary or unreadable: nothing to read a token out of


class RepoHygieneTest(unittest.TestCase):
    def test_no_secret_shaped_filename_is_tracked(self) -> None:
        offenders = [
            name
            for name in _tracked_files()
            if SECRET_FILENAMES.search(name) and name not in FILENAME_ALLOWED
        ]
        self.assertFalse(
            offenders,
            "These files are tracked by git and look like credential stores:\n  "
            + "\n  ".join(offenders)
            + "\nUntrack them (`git rm --cached <file>`) and widen .gitignore.",
        )

    def test_no_tracked_file_contains_a_jwt(self) -> None:
        offenders = [
            name
            for name in _tracked_files()
            if (text := _read(name)) is not None and JWT.search(text)
        ]
        self.assertFalse(
            offenders, "JWT-shaped token found in tracked files:\n  " + "\n  ".join(offenders)
        )

    def test_no_env_style_file_assigns_a_real_credential(self) -> None:
        offenders: list[str] = []
        for name in _tracked_files():
            if not ENV_STYLE.search(name):
                continue
            text = _read(name)
            if text is None:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if line.lstrip().startswith("#"):
                    continue
                match = ASSIGNMENT.match(line)
                if not match or not SECRET_KEY.search(match.group(1)):
                    continue
                if not _is_placeholder(match.group(2)):
                    offenders.append(f"{name}:{number}: {match.group(1)} looks like a real value")
        self.assertFalse(
            offenders,
            "Real-looking credentials in tracked env-style files:\n  " + "\n  ".join(offenders),
        )

    def test_the_guards_still_bite(self) -> None:
        """A sweep over nothing, or a pattern that matches nothing, passes forever."""

        self.assertIn("tests/test_repo_hygiene.py", _tracked_files(), "the sweep sees this repo")
        self.assertTrue(SECRET_FILENAMES.search(".env.bak-040827"), "the file that actually leaked")
        self.assertTrue(SECRET_FILENAMES.search("deploy/.env.local"))
        self.assertTrue(SECRET_FILENAMES.search(".ha-token"), "the Home Assistant token for [katana_ha]")
        token = "ey" + "J" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20
        self.assertTrue(JWT.search(token), "the shape of the token that actually leaked")
        self.assertFalse(_is_placeholder("k" * 40), "a long random value is a real one")
        self.assertTrue(_is_placeholder("replace-with-your-long-lived-token"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
