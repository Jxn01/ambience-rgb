"""Entry point: ``linux-rgb [run|off|devices]``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from linux_rgb.config import ConfigError, load_config
from linux_rgb.daemon import Daemon, lights_off


def _configure_logging(verbose: bool) -> None:
    # Under systemd the journal adds its own timestamps, so the format stays
    # bare; run by hand it is still readable.
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def daemon_is_listening(socket_path) -> bool:
    """Whether a linux-rgb daemon is already bound to the publish socket.

    Used to keep the diagnostics away from hardware the daemon is driving. A
    datagram socket file exists whether or not anyone is bound to it, so the
    file's presence proves nothing; connecting to it does.
    """

    import socket as _socket

    probe = _socket.socket(_socket.AF_UNIX, _socket.SOCK_DGRAM)
    try:
        probe.connect(str(socket_path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _devices(config) -> int:
    """Print what would be driven, and how. The first thing to run when puzzled."""

    from linux_rgb.openrgb_client import OpenRGBClient, OpenRGBError

    print(f"socket: {config.socket_path}")
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
                    print(f"  {controller.name:<40} EXCLUDED")
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
    except OpenRGBError as error:
        print(f"  OpenRGB unavailable: {error}")

    if config.commander.enabled:
        from linux_rgb.commander_core import CommanderCore, CommanderCoreError

        header = (
            f"\n  Corsair Commander Core (own driver)      "
            f"pump={config.commander.pump_group} fans={config.commander.fan_group}"
        )
        if daemon_is_listening(config.socket_path):
            # Exactly one process may hold this device. Opening it here would
            # interleave reads with the daemon's and desync both.
            print(header)
            print("      held by the running daemon; not opened (stop linux-rgb to inspect)")
            return 0

        try:
            with CommanderCore(read_only=True) as device:
                print(header)
                print(f"      pump {device.pump_leds} LEDs, ports {device.port_leds}")
        except CommanderCoreError as error:
            print(f"\n  Commander Core unavailable: {error}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="linux-rgb", description=__doc__)
    parser.add_argument(
        "action",
        nargs="?",
        default="run",
        choices=("run", "off", "devices"),
        help="run the daemon (default), write black once, or list what would be driven",
    )
    parser.add_argument("--config", type=Path, default=None, help="path to config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)

    try:
        config = load_config(args.config)
    except ConfigError as error:
        logging.getLogger("linux_rgb").error("%s", error)
        return 2

    if args.action == "off":
        return lights_off(config)
    if args.action == "devices":
        return _devices(config)
    return Daemon(config).run()


if __name__ == "__main__":
    raise SystemExit(main())
