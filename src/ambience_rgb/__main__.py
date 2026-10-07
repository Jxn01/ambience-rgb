"""Entry point: ``ambience-rgb [run|off|devices]``."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

from ambience_rgb import hints, paths, transport
from ambience_rgb.config import ConfigError, load_config
from ambience_rgb.daemon import Daemon, lights_off

LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"
FILE_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 3


def _stream_handler(stream) -> logging.Handler:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    return handler


def _log_handler(log_file: Path | None) -> logging.Handler:
    stream = sys.stderr
    if log_file is None and stream is not None:
        # Under systemd the journal adds its own timestamps, so the format stays
        # bare; run by hand it is still readable.
        return _stream_handler(stream)
    # 🚨 pythonw -- what the Windows tasks run, so that no console window flashes
    # up at logon -- has no stderr at all. sys.stderr is None there, and a
    # stream handler on it drops every line without a word. So: a file.
    target = log_file if log_file is not None else paths.default_log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            target, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
        )
    except OSError:
        # Nowhere to write. Not being able to log is no reason to leave the
        # hardware undriven.
        return _stream_handler(stream) if stream is not None else logging.NullHandler()
    handler.setFormatter(logging.Formatter(FILE_LOG_FORMAT))
    return handler


def _configure_logging(verbose: bool, log_file: Path | None = None) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        handlers=[_log_handler(log_file)],
        force=True,
    )


def _devices(config) -> int:
    """Print what would be driven, and how. The first thing to run when puzzled."""

    from ambience_rgb.openrgb_client import OpenRGBClient, OpenRGBError

    print(f"socket: {config.endpoint}")
    print(f"  matterlights' .env needs:  RGB_PUBLISH_SOCKET={config.endpoint}")
    print(f"openrgb: {config.openrgb_host}:{config.openrgb_port}")
    print()

    try:
        with OpenRGBClient(config.openrgb_host, config.openrgb_port) as client:
            for controller in client.controllers:
                rule = config.rule_for(controller.name)
                if rule is None:
                    print(f"  {controller.name:<40} (no rule -- not driven)")
                    continue
                if rule.group == "excluded":
                    held = f" (held at #{'%02x%02x%02x' % rule.static})" if rule.static else ""
                    print(f"  {controller.name:<40} EXCLUDED{held}")
                    continue
                if rule.zones:
                    print(f"  {controller.name:<40} split by zone, bus={rule.bus}")
                    for zone in controller.zones:
                        group = rule.group_for_zone(zone.name)
                        print(f"      {zone.name:<32} {group:<9} {zone.led_count} LEDs")
                else:
                    print(
                        f"  {controller.name:<40} {rule.group:<9} bus={rule.bus} "
                        f"{controller.led_count} LEDs"
                    )
            if config.commander.enabled:
                from ambience_rgb.commander_core import CONTENTION_ADVICE, openrgb_has_it

                contenders = openrgb_has_it(controller.name for controller in client.controllers)
                if contenders:
                    print(f"\n  ⚠ {CONTENTION_ADVICE.format(names=', '.join(contenders))}")
    except OpenRGBError as error:
        print(f"  OpenRGB unavailable: {error}")

    # Exactly one process may hold each of these devices. Opening one here while
    # the daemon drives it would interleave with its traffic and desync both.
    held = transport.is_listening(config.endpoint)
    held_note = (
        "      held by the running daemon; not opened. To inspect it, stop the daemon:\n"
        f"          {hints.stop_daemon()}"
    )

    if config.commander.enabled:
        from ambience_rgb.commander_core import CommanderCore, CommanderCoreError

        header = (
            f"\n  Corsair Commander Core (own driver)      "
            f"pump={config.commander.pump_group} fans={config.commander.fan_group}"
        )
        if held:
            print(header)
            print(held_note)
        else:
            try:
                with CommanderCore(read_only=True) as device:
                    print(header)
                    print(f"      pump {device.pump_leds} LEDs, ports {device.port_leds}")
            except CommanderCoreError as error:
                print(f"\n  Commander Core unavailable: {error}")

    if config.katana_ha.enabled:
        _katana_ha(config.katana_ha)
    return 0


def _katana_ha(katana) -> None:
    """The soundbar through Home Assistant: ask HA, never the soundbar. Safe while the daemon runs."""

    from ambience_rgb.ha_stream import HAStreamClient, HAStreamError, read_token

    print(
        f"\n  Sound Blaster Katana (through Home Assistant) "
        f"group={katana.group}, at most {1 / katana.interval_seconds:g} Hz to {katana.url}"
    )
    try:
        client = HAStreamClient(katana.url or "", read_token(katana.token_file), katana.timeout_seconds)
        try:
            status = client.status()
        finally:
            client.close()
    except HAStreamError as error:
        print(f"      unavailable: {error}")
        return

    def yes(key: str) -> str:
        return "yes" if status.get(key) else "no"

    print(
        f"      Screen sync {'on' if status.get('screen_sync') else 'OFF'}, "
        f"receiving {yes('receiving')}, soundbar connected {yes('available')}"
    )
    print(
        f"      {status.get('frames_received', '?')} frames received, {status.get('frames_dropped', '?')} dropped, "
        f"{status.get('writes_sent', '?')} writes sent, {status.get('write_errors', '?')} write errors"
    )
    if not status.get("screen_sync"):
        print("      Warning: HA drops every colour while Screen sync is off. Switch it on in HA: any manual")
        print("      lighting change there turns it off.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ambience-rgb", description=__doc__)
    parser.add_argument(
        "action",
        nargs="?",
        default="run",
        choices=("run", "off", "devices"),
        help="run the daemon (default), write black once, or list what would be driven",
    )
    parser.add_argument("--config", type=Path, default=None, help="path to config.toml")
    parser.add_argument(
        "--stop-daemon",
        action="store_true",
        help="with off: first ask a running daemon to stop, and wait until it has let go "
        "of every device (what the Windows shutdown task runs)",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="log to this file, rotated, instead of stderr -- the default when there is "
        "no stderr at all, as under pythonw",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.stop_daemon and args.action != "off":
        parser.error("--stop-daemon only applies to off")

    _configure_logging(args.verbose, args.log_file)

    try:
        config = load_config(args.config)
    except ConfigError as error:
        logging.getLogger("ambience_rgb").error("%s", error)
        return 2

    if args.action == "off":
        return lights_off(config, stop_daemon=args.stop_daemon)
    if args.action == "devices":
        return _devices(config)
    return Daemon(config).run()


if __name__ == "__main__":
    raise SystemExit(main())
