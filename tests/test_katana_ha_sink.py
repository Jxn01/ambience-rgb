"""The Katana through Home Assistant: what is posted, how often, and what is not.

HA's integration owns the soundbar over Bluetooth; this sink only posts colours to
its /api/sb_katana/stream endpoint. Three rules shape every test: a colour that
moved goes out (no faster than rate_hz), an unchanged one is re-posted every
keepalive so HA's 10 s hand-over does not fire, and black goes out once and is
then followed by silence, so the hand-over turns the lights off.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from ambience_rgb.config import ConfigError, parse_config
from ambience_rgb.ha_stream import TOKEN_ENV, HAStreamClient, HAStreamError, read_token
from ambience_rgb.sinks import KatanaHASink, build_sinks

FAR = (40, 50, 60)
NEAR = (10, 20, 30)
BLACK = (0, 0, 0)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _FakeClient:
    def __init__(self) -> None:
        self.posts: list[tuple[int, int, int]] = []
        self.fail = False
        self.closed = 0

    def post(self, rgb) -> bool:
        if self.fail:
            raise HAStreamError("HA down")
        self.posts.append(tuple(rgb))
        return True

    def close(self) -> None:
        self.closed += 1


SOCKET = {"socket": {"path": "/tmp/x.sock"}}


def _parse(payload: dict):
    return parse_config({**SOCKET, **payload})


def _config(**section):
    base = {"enabled": True, "url": "http://ha.local:8123"}
    base.update(section)
    return _parse({"katana": {"enabled": False}, "katana_ha": base})


def _sink(clock=None, **section):
    client = _FakeClient()
    sink = KatanaHASink(
        _config(**section).katana_ha, clock=clock or _Clock(), client_factory=lambda: client, start_worker=False
    )
    return sink, client


class KatanaHASinkTest(unittest.TestCase):
    def test_first_colour_is_posted(self):
        sink, client = _sink()
        sink.write(NEAR, FAR)
        sink.flush()
        self.assertEqual(client.posts, [FAR])  # the soundbar lights the desk: "far"

    def test_group_near(self):
        sink, client = _sink(group="near")
        sink.write(NEAR, FAR)
        sink.flush()
        self.assertEqual(client.posts, [NEAR])

    def test_rate_ceiling_and_newest_wins(self):
        clock = _Clock()
        sink, client = _sink(clock)
        sink.write(NEAR, (100, 0, 0))
        clock.now += 0.05  # inside 1/10 s
        sink.write(NEAR, (0, 100, 0))
        sink.flush()
        self.assertEqual(client.posts, [(100, 0, 0)])
        clock.now += 0.1
        sink.write(NEAR, (0, 0, 100))
        sink.flush()
        self.assertEqual(client.posts, [(100, 0, 0), (0, 0, 100)])

    def test_slot_holds_only_the_newest_until_flushed(self):
        clock = _Clock()
        sink, client = _sink(clock)
        for i in range(5):
            sink.write(NEAR, (50 + 10 * i, 0, 0))
            clock.now += 0.2
        sink.flush()
        self.assertEqual(client.posts, [(90, 0, 0)])

    def test_jitter_below_epsilon_is_not_reposted(self):
        clock = _Clock()
        sink, client = _sink(clock)
        sink.write(NEAR, (100, 100, 100))
        sink.flush()
        clock.now += 1
        sink.write(NEAR, (101, 100, 99))
        sink.flush()
        self.assertEqual(len(client.posts), 1)

    def test_unchanged_colour_is_kept_alive_for_the_handover(self):
        clock = _Clock()
        sink, client = _sink(clock)
        sink.write(NEAR, FAR)
        sink.flush()
        clock.now += 4.9
        sink.write(NEAR, FAR)
        sink.flush()
        self.assertEqual(len(client.posts), 1)
        clock.now += 0.2  # 5.1 s since the last post
        sink.write(NEAR, FAR)
        sink.flush()
        self.assertEqual(client.posts, [FAR, FAR])

    def test_black_is_posted_once_then_silence(self):
        clock = _Clock()
        sink, client = _sink(clock)
        sink.write(NEAR, FAR)
        sink.flush()
        for _ in range(10):
            clock.now += 3
            sink.write(BLACK, BLACK)
            sink.flush()
        self.assertEqual(client.posts, [FAR, BLACK])  # no keepalive: HA hands over to "off"

    def test_colour_after_black_resumes(self):
        clock = _Clock()
        sink, client = _sink(clock)
        sink.write(BLACK, BLACK)
        sink.flush()
        clock.now += 30
        sink.write(NEAR, FAR)
        sink.flush()
        self.assertEqual(client.posts, [BLACK, FAR])

    def test_black_on_exit_is_posted_once(self):
        sink, client = _sink()
        sink.write(NEAR, FAR)
        sink.flush()
        sink.black()
        sink.flush()
        sink.black()
        sink.flush()
        self.assertEqual(client.posts, [FAR, BLACK])

    def test_failure_surfaces_on_the_next_write_and_the_colour_is_retried(self):
        clock = _Clock()
        sink, client = _sink(clock)
        client.fail = True
        sink.write(NEAR, FAR)
        sink.flush()
        clock.now += 1
        with self.assertRaises(HAStreamError):
            sink.write(NEAR, FAR)
        client.fail = False
        sink.flush()  # the failed colour is still in the slot
        self.assertEqual(client.posts, [FAR])

    def test_release_holds_nothing(self):
        sink, client = _sink()
        sink.release()
        sink.close()
        self.assertEqual(client.posts, [])


class KatanaHAConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = _parse({}).katana_ha
        self.assertFalse(cfg.enabled)
        self.assertAlmostEqual(cfg.interval_seconds, 0.1)
        self.assertEqual(cfg.keepalive_seconds, 5.0)

    def test_url_required_when_enabled(self):
        with self.assertRaisesRegex(ConfigError, "katana_ha.url"):
            _parse({"katana_ha": {"enabled": True}})

    def test_usb_and_ha_paths_are_mutually_exclusive(self):
        with self.assertRaisesRegex(ConfigError, "only one may drive the soundbar"):
            _parse({"katana": {"enabled": True}, "katana_ha": {"enabled": True, "url": "http://h:8123"}})

    def test_keepalive_must_beat_the_handover(self):
        with self.assertRaisesRegex(ConfigError, "hand-over"):
            _config(keepalive_seconds=10)

    def test_build_sinks_adds_it_only_when_enabled(self):
        names = [s.name for s in build_sinks(_config())]
        self.assertIn("katana-ha", names)
        self.assertNotIn("katana", names)
        self.assertNotIn("katana-ha", [s.name for s in build_sinks(_parse({}))])

    def test_example_config_leaves_it_off(self):
        from ambience_rgb.config import EXAMPLE_CONFIG, load_config

        self.assertFalse(load_config(EXAMPLE_CONFIG).katana_ha.enabled)


class TokenTest(unittest.TestCase):
    def test_env_wins(self):
        self.assertEqual(read_token(environ={TOKEN_ENV: " abc "}), "abc")

    def test_file_in_config_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "ambience-rgb"
            directory.mkdir()
            (directory / "ha-token").write_text("tok\n", encoding="utf-8")
            self.assertEqual(read_token(platform="linux", environ={"XDG_CONFIG_HOME": tmp}), "tok")

    def test_missing_token_says_where(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(HAStreamError, "ha-token"):
            read_token(platform="linux", environ={"XDG_CONFIG_HOME": tmp})


class _Handler(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):  # noqa: N802 - http.server API
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).seen.append((self.path, self.headers["Authorization"], json.loads(body)))
        reply = json.dumps({"accepted": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


class HAStreamClientTest(unittest.TestCase):
    def test_posts_json_with_bearer_token_over_one_connection(self):
        server = HTTPServer(("127.0.0.1", 0), _Handler)
        _Handler.seen = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = HAStreamClient(f"http://127.0.0.1:{server.server_port}", "tok")
            self.assertTrue(client.post((1, 2, 3)))
            self.assertTrue(client.post((4, 5, 6)))
            client.close()
        finally:
            server.shutdown()
        self.assertEqual(
            _Handler.seen,
            [
                ("/api/sb_katana/stream", "Bearer tok", {"rgb": [1, 2, 3]}),
                ("/api/sb_katana/stream", "Bearer tok", {"rgb": [4, 5, 6]}),
            ],
        )

    def test_unreachable_raises(self):
        client = HAStreamClient("http://127.0.0.1:9", "tok", timeout_seconds=0.5)
        with self.assertRaises(HAStreamError):
            client.post((1, 2, 3))


if __name__ == "__main__":
    unittest.main()
