"""Posting screen colours to Home Assistant's ``sb_katana`` integration.

``POST <url>/api/sb_katana/stream`` with ``{"rgb": [r, g, b]}`` and a long-lived
access token. That endpoint, not the ``sb_katana.stream_frame`` service, on
purpose: every service call is a recorded ``call_service`` event, and at 10 Hz
that is tens of thousands of database rows an hour. The endpoint records nothing.

The token is read from a file in this user's config directory, never from the
repository: the checkout lives on the drive both operating systems share, and no
secret goes there.
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
TOKEN_FILE_NAME = "ha-token"
STREAM_PATH = "/api/sb_katana/stream"


class HAStreamError(RuntimeError):
    """Home Assistant could not be reached, or refused a frame."""


def default_token_file(*, platform: str | None = None, environ: Mapping[str, str] | None = None) -> Path | None:
    directory = paths.config_dir(platform=platform, environ=environ)
    return None if directory is None else directory / TOKEN_FILE_NAME


def read_token(
    token_file: str | None = None, *, platform: str | None = None, environ: Mapping[str, str] | None = None
) -> str:
    """The token from ``$AMBIENCE_RGB_HA_TOKEN``, else from the token file."""

    environ = os.environ if environ is None else environ
    if environ.get(TOKEN_ENV, "").strip():
        return environ[TOKEN_ENV].strip()
    path = (
        paths.expand_user(Path(token_file))
        if token_file
        else default_token_file(platform=platform, environ=environ)
    )
    if path is None or not path.is_file():
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
        try:
            conn = self._connection()
            conn.request("POST", self._prefix + STREAM_PATH, body=body, headers=self._headers)
            response = conn.getresponse()
            payload = response.read()
        except (OSError, http.client.HTTPException) as error:
            self.close()
            raise HAStreamError(f"Home Assistant unreachable: {error}") from error
        if response.status != 200:
            self.close()
            raise HAStreamError(f"Home Assistant answered {response.status}: {payload[:200]!r}")
        try:
            return bool(json.loads(payload).get("accepted"))
        except ValueError as error:
            raise HAStreamError(f"Unexpected answer from Home Assistant: {payload[:200]!r}") from error

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
