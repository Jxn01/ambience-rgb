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

# How often an UNCHANGED colour is written again. The devices need an occasional
# refresh so a controller that was reset or re-detected does not sit on a stale
# frame, but writing an unchanged colour every tick is what floods the server.
REFRESH_SECONDS = 2.0

# Smallest per-channel move worth a packet. The ambient colour jitters by a unit
# or two every frame on a static screen; without this, exact-equality filtering
# still resends all eleven targets every tick and the server is overrun anyway.
# Invisible on an LED, and matterlights applies the same idea to the bulbs.
COLOUR_EPSILON = 6

# How often to measure the server's lag, and the lag that counts as too far
# behind. Writes are fire-and-forget, so this round trip is the only honest
# signal; a wedged server is otherwise indistinguishable from a healthy one.
HEALTH_CHECK_SECONDS = 5.0
HEALTH_TIMEOUT_SECONDS = 2.0


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
        # Per-target (device, zone) -> (colour, when it was last sent).
        self._sent: dict[tuple[int, int | None], tuple[RGB, float]] = {}
        self._checked_at = 0.0

    def connect(self) -> None:
        client = OpenRGBClient(
            self._config.openrgb_host, self._config.openrgb_port, logger=self._logger
        ).connect()
        self._client = client
        self._sent.clear()
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
            # Devices sitting in a firmware effect ignore colour writes until the
            # DEVICE is put into direct mode -- not merely the server's model of
            # it. The GPU ships on Rainbow and the ASRock board on Off, so this
            # is not theoretical: with only SETCUSTOMMODE the GPU cycled its
            # rainbow for hours while every readback claimed it was in Direct.
            client.set_direct_mode(device_index)

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
        self._check_health(now)
        for device, zone, group, leds, bus in self._targets:
            # Rate-limit per bus. SMBus is shared with sensor reads; the USB
            # devices have no such constraint and get the full rate.
            last = self._last_write.get(bus, 0.0)
            if now - last < self._interval(bus):
                continue
            colour = near if group == GROUP_NEAR else far
            if not self._should_send(device, zone, colour, now):
                continue
            self._send_colour(device, zone, colour, leds)
        for bus in {t[4] for t in self._targets}:
            if now - self._last_write.get(bus, 0.0) >= self._interval(bus):
                self._last_write[bus] = now

    def _check_health(self, now: float) -> None:
        """Drop the connection when the server has fallen too far behind.

        Reconnecting throws the backlog away, which is exactly right for this
        stream: every queued packet is a stale frame, and the next tick carries
        a better one. Without this the daemon cannot tell a wedged server from a
        healthy one -- it just keeps writing into a buffer nobody drains, and
        the case shows a colour from minutes ago while every log line is clean.
        """

        if now - self._checked_at < HEALTH_CHECK_SECONDS:
            return
        self._checked_at = now
        assert self._client is not None
        try:
            lag = self._client.round_trip_seconds(HEALTH_TIMEOUT_SECONDS)
        except OpenRGBError:
            self._logger.warning("openrgb: server is not answering; dropping the connection")
            self.close()
            raise
        if lag > 0.5:
            self._logger.info("openrgb: server is %.2fs behind", lag)

    def _should_send(self, device: int, zone: int | None, colour: RGB, now: float) -> bool:
        """Whether this target needs the packet, or already shows this colour.

        🚨 **The write side of this protocol is fire-and-forget, so overrunning
        the server is invisible from here.** Every ``sendall`` returns in
        microseconds whatever the server is doing with the bytes; the OpenRGB
        server then applies them one at a time against real USB and SMBus
        devices, which is far slower. Writing all eleven targets every tick
        offered it ~43 packets a second, it drained them at a fraction of that,
        and the backlog grew without bound -- so the board displayed a colour
        from minutes ago. Across a display-off period it displayed black long
        after the screen came back, which reads exactly like "the RGB is dead".

        Sending only what changed makes a static screen almost free while a
        changing one still streams, and the refresh keeps a re-detected
        controller from sitting on a stale frame.
        """

        previous = self._sent.get((device, zone))
        if previous is None:
            return True
        colour_before, when = previous
        if (now - when) >= REFRESH_SECONDS:
            return True
        return any(abs(a - b) >= COLOUR_EPSILON for a, b in zip(colour, colour_before))

    def _send_colour(self, device: int, zone: int | None, colour: RGB, leds: int) -> None:
        assert self._client is not None
        if zone is None:
            self._client.update_leds(device, [colour] * leds)
        else:
            self._client.update_zone_leds(device, zone, [colour] * leds)
        self._sent[(device, zone)] = (colour, time.monotonic())

    def black(self) -> None:
        """Write black to every target unconditionally.

        Deliberately bypasses the change filter: black is what every exit path
        writes, and "we think it is already black" is not good enough when the
        alternative is leaving the machine lit after a shutdown.
        """

        if self._client is None:
            self.connect()
        assert self._client is not None
        for device, zone, _group, leds, _bus in self._targets:
            self._send_colour(device, zone, (0, 0, 0), leds)

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
