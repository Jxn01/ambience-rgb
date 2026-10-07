"""Posting screen colours to Home Assistant's ``sb_katana`` integration.

``POST <url>/api/sb_katana/stream`` with ``{"rgb": [r, g, b]}`` and a long-lived
access token. That endpoint, not the ``sb_katana.stream_frame`` service, on
purpose: every service call is a recorded ``call_service`` event, and at 10 Hz
that is tens of thousands of database rows an hour. The endpoint records nothing.
``GET`` on the same path answers the stream's status, for ``ambience-rgb devices``.

The token is read from ``$AMBIENCE_RGB_HA_TOKEN``, or ``token_file`` in the config,
or else ``.ha-token`` at the root of this checkout -- gitignored, and on the drive
both operating systems share, so one file serves both halves of the rig.
"""

from __future__ import annotations

import http.client
import json
import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from ambience_rgb import paths

TOKEN_ENV = "AMBIENCE_RGB_HA_TOKEN"
TOKEN_FILE_NAME = ".ha-token"
STREAM_PATH = "/api/sb_katana/stream"
REPO_ROOT = Path(__file__).resolve().parents[2]


class HAStreamError(RuntimeError):
    """Home Assistant could not be reached, or refused a frame."""


def default_token_file(root: Path = REPO_ROOT) -> Path:
    return root / TOKEN_FILE_NAME


def read_token(
    token_file: str | None = None, *, environ: Mapping[str, str] | None = None, root: Path = REPO_ROOT
) -> str:
    """The token from ``$AMBIENCE_RGB_HA_TOKEN``, else ``token_file``, else ``.ha-token`` in the checkout."""

    environ = os.environ if environ is None else environ
    if environ.get(TOKEN_ENV, "").strip():
        return environ[TOKEN_ENV].strip()
    path = paths.expand_user(Path(token_file)) if token_file else default_token_file(root)
    if not path.is_file():
        raise HAStreamError(f"No Home Assistant token: set {TOKEN_ENV} or create {path}")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise HAStreamError(f"{path} is empty")
    return token


class HAStreamClient:
    """One keep-alive HTTP connection to HA, reopened after any failure."""

    def __init__(self, url: str, token: str, timeout_seconds: float = 2.0) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise HAStreamError(f"Not an http(s) URL: {url!r}")
        self._https = parts.scheme == "https"
        self._host = parts.hostname
        self._port = parts.port
        self._prefix = parts.path.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self._timeout = timeout_seconds
        self._conn: http.client.HTTPConnection | None = None

    def _connection(self) -> http.client.HTTPConnection:
        if self._conn is None:
            cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
            self._conn = cls(self._host, self._port, timeout=self._timeout)
        return self._conn

    def post(self, rgb: tuple[int, int, int]) -> bool:
        """Send one colour. Returns HA's ``accepted`` (False while Screen sync is off)."""

        body = json.dumps({"rgb": [int(c) for c in rgb]})
        return bool(self._request("POST", body).get("accepted"))

    def status(self) -> dict:
        """The stream's status: ``screen_sync``, ``receiving``, ``available`` and counters. Changes nothing."""

        return self._request("GET", None)

    def _request(self, method: str, body: str | None) -> dict:
        try:
            conn = self._connection()
            conn.request(method, self._prefix + STREAM_PATH, body=body, headers=self._headers)
            response = conn.getresponse()
            payload = response.read()
        except (OSError, http.client.HTTPException) as error:
            self.close()
            raise HAStreamError(f"Home Assistant unreachable: {error}") from error
        if response.status != 200:
            self.close()
            raise HAStreamError(f"Home Assistant answered {response.status}: {payload[:200]!r}")
        try:
            answer = json.loads(payload)
        except ValueError as error:
            raise HAStreamError(f"Unexpected answer from Home Assistant: {payload[:200]!r}") from error
        if not isinstance(answer, dict):
            raise HAStreamError(f"Unexpected answer from Home Assistant: {payload[:200]!r}")
        return answer

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
