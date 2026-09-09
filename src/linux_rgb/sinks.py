"""Where colours go: OpenRGB's devices, and the Commander Core.

Both sinks present the same tiny interface, and both are **best effort**. A sink
that raises is logged once, marked degraded, and retried on a timer; it never
takes the daemon down. Losing the AIO must not stop the motherboard syncing, and
losing the OpenRGB server must not stop the AIO.

That is not defensive habit, it is the actual failure mode: the OpenRGB server
is a separate process that can be restarted or crash on its own (a RESIZEZONE on
this rig's Commander Core kills 1.0rc3 outright), and the AIO is a USB device
that can be unplugged.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol

from linux_rgb.config import (
    BUS_SMBUS,
    GROUP_EXCLUDED,
    GROUP_NEAR,
    CommanderConfig,
    Config,
    DeviceRule,
)
from linux_rgb.openrgb_client import OpenRGBClient, OpenRGBError

LOGGER = logging.getLogger("linux_rgb.sinks")

RGB = tuple[int, int, int]

# How long a failed sink waits before trying again. Long enough not to spin on a
# dead server, short enough that a restarted one is picked up without help.
RETRY_SECONDS = 10.0


class Sink(Protocol):
    name: str

    def write(self, near: RGB, far: RGB) -> None: ...
    def black(self) -> None: ...
    def close(self) -> None: ...


class ResilientSink:
    """Wraps a sink so its failures are isolated and it recovers on its own."""

    def __init__(self, sink: Sink, logger: logging.Logger | None = None) -> None:
        self._sink = sink
        self._logger = logger or LOGGER
        self._failed_at: float | None = None
        self.name = sink.name

    @property
    def degraded(self) -> bool:
        return self._failed_at is not None

    def _guard(self, action: str, call) -> bool:
        now = time.monotonic()
        if self._failed_at is not None and (now - self._failed_at) < RETRY_SECONDS:
            return False
        try:
            call()
        except Exception as error:  # noqa: BLE001 - a sink may not kill the daemon
            if self._failed_at is None:
                self._logger.warning("%s: %s failed (%s); will retry", self.name, action, error)
            self._failed_at = now
            return False
        if self._failed_at is not None:
            self._logger.info("%s: recovered", self.name)
            self._failed_at = None
        return True

    def write(self, near: RGB, far: RGB) -> bool:
        return self._guard("write", lambda: self._sink.write(near, far))

    def black(self) -> bool:
        return self._guard("black", self._sink.black)

    def close(self) -> None:
        try:
            self._sink.close()
        except Exception:  # noqa: BLE001 - closing must not raise
            self._logger.debug("%s: close failed", self.name, exc_info=True)


class OpenRGBSink:
    """Drives every OpenRGB controller that a config rule claims."""

    name = "openrgb"

    def __init__(self, config: Config, logger: logging.Logger | None = None) -> None:
        self._config = config
        self._logger = logger or LOGGER
        self._client: OpenRGBClient | None = None
        # device index -> list of (zone index or None, group, led count)
        self._targets: list[tuple[int, int | None, str, int, str]] = []
        self._last_write: dict[str, float] = {}

    def connect(self) -> None:
        client = OpenRGBClient(
            self._config.openrgb_host, self._config.openrgb_port, logger=self._logger
        ).connect()
        self._client = client
        self._targets = self._plan(client)
        if not self._targets:
            # The server answers its socket before device detection finishes, so
            # connecting early yields an empty controller list. Succeeding here
            # would cache "drive nothing" forever; raising lets the resilient
            # wrapper retry until detection has actually run.
            client.close()
            self._client = None
            raise OpenRGBError(
                "The OpenRGB server reported no drivable controllers -- "
                "detection may still be running"
            )
        for device_index in sorted({t[0] for t in self._targets}):
            # Devices sitting in a firmware effect ignore colour writes until
            # they are put into direct mode. The GPU ships on Rainbow and the
            # ASRock board on Off, so this is not a theoretical case.
            client.set_custom_mode(device_index)

    def _plan(self, client: OpenRGBClient) -> list[tuple[int, int | None, str, int, str]]:
        """Resolve controllers to (device, zone, group, leds, bus) write targets.

        A controller whose rule splits zones is planned per zone; one that does
        not is planned as a single whole-device write, which is one packet
        instead of six.
        """

        plan = []
        for controller in client.controllers:
            rule: DeviceRule | None = self._config.rule_for(controller.name)
            if rule is None:
                self._logger.info("OpenRGB: %s is unclaimed by any rule; skipping", controller.name)
                continue
            if rule.group == GROUP_EXCLUDED:
                self._logger.info("OpenRGB: %s is excluded by configuration", controller.name)
                continue

            if rule.zones:
                for zone in controller.zones:
                    group = rule.group_for_zone(zone.name)
                    if group == GROUP_EXCLUDED or zone.led_count == 0:
                        continue
                    plan.append((controller.index, zone.index, group, zone.led_count, rule.bus))
            elif controller.led_count > 0:
                plan.append((controller.index, None, rule.group, controller.led_count, rule.bus))
            else:
                self._logger.info(
                    "OpenRGB: %s reports zero LEDs; nothing to drive", controller.name
                )
        return plan

    def _interval(self, bus: str) -> float:
        return (
            self._config.smbus_interval_seconds
            if bus == BUS_SMBUS
            else self._config.usb_interval_seconds
        )

    def write(self, near: RGB, far: RGB) -> None:
        if self._client is None:
            self.connect()
        assert self._client is not None
        now = time.monotonic()
        for device, zone, group, leds, bus in self._targets:
            # Rate-limit per bus. SMBus is shared with sensor reads; the USB
            # devices have no such constraint and get the full rate.
            last = self._last_write.get(bus, 0.0)
            if now - last < self._interval(bus):
                continue
            colour = near if group == GROUP_NEAR else far
            if zone is None:
                self._client.update_leds(device, [colour] * leds)
            else:
                self._client.update_zone_leds(device, zone, [colour] * leds)
        for bus in {t[4] for t in self._targets}:
            if now - self._last_write.get(bus, 0.0) >= self._interval(bus):
                self._last_write[bus] = now

    def black(self) -> None:
        if self._client is None:
            self.connect()
        assert self._client is not None
        for device, zone, _group, leds, _bus in self._targets:
            if zone is None:
                self._client.update_leds(device, [(0, 0, 0)] * leds)
            else:
                self._client.update_zone_leds(device, zone, [(0, 0, 0)] * leds)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


class CommanderCoreSink:
    """Drives the Corsair AIO through our own hidraw driver."""

    name = "commander-core"

    def __init__(self, commander: CommanderConfig, logger: logging.Logger | None = None) -> None:
        self._config = commander
        self._logger = logger or LOGGER
        self._device = None

    def connect(self) -> None:
        from linux_rgb.commander_core import CommanderCore

        self._device = CommanderCore(logger=self._logger).open()

    def write(self, near: RGB, far: RGB) -> None:
        if self._device is None:
            self.connect()
        assert self._device is not None
        pump = near if self._config.pump_group == GROUP_NEAR else far
        fans = near if self._config.fan_group == GROUP_NEAR else far
        self._device.set_colour(pump, fans)

    def black(self) -> None:
        if self._device is None:
            self.connect()
        assert self._device is not None
        self._device.black()

    def close(self) -> None:
        if self._device is not None:
            self._device.close()
            self._device = None


def build_sinks(config: Config, logger: logging.Logger | None = None) -> list[ResilientSink]:
    """Every configured sink, each wrapped so one failure cannot stop the rest."""

    log = logger or LOGGER
    sinks: list[Sink] = [OpenRGBSink(config, log)]
    if config.commander.enabled:
        sinks.append(CommanderCoreSink(config.commander, log))
    return [ResilientSink(sink, log) for sink in sinks]
