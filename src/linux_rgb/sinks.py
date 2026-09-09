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
HEALTH_TIMEOUT_SECONDS = 5.0

# ⚠️ Deliberately tolerant. A healthy server answers in well under 3 ms, so it
# is tempting to reconnect after one or two misses -- but this server also goes
# quiet for tens of seconds at a time under load while STILL APPLYING the writes
# we send it, so an eager check reconnects every ~20 s for nothing and each
# reconnect costs a full re-push. A genuine wedge never answers again, so it
# trips this anyway; it just takes half a minute longer to say so.
HEALTH_FAILURES_BEFORE_RECONNECT = 6


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
        # (device, segments, bus, per_zone); see _plan for what segments holds.
        self._targets: list[tuple[int, tuple, str, bool]] = []
        self._last_write: dict[str, float] = {}
        # Per-target key -> ((near, far), when it was last sent).
        self._sent: dict[tuple, tuple[tuple[RGB, RGB], float]] = {}
        self._checked_at = 0.0
        self._health_failures = 0

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
        for device_index in sorted({target[0] for target in self._targets}):
            # Devices sitting in a firmware effect ignore colour writes until the
            # DEVICE is put into direct mode -- not merely the server's model of
            # it. The GPU ships on Rainbow and the ASRock board on Off, so this
            # is not theoretical: with only SETCUSTOMMODE the GPU cycled its
            # rainbow for hours while every readback claimed it was in Direct.
            client.set_direct_mode(device_index)

    def _plan(self, client: OpenRGBClient) -> list[tuple[int, tuple, str, bool]]:
        """Resolve controllers to write targets: (device, segments, bus, per_zone).

        For a whole-device write ``segments`` is ``((group, led_count), ...)``
        covering the device's LEDs in order, so a split controller is still one
        packet. For the per-zone fallback it is ``((zone_index, group, count),)``.

        🚨 **Prefer one whole-device write to several per-zone writes.** Zones are
        contiguous ranges of the same LED array, so writing the array once says
        exactly the same thing in one packet instead of six -- and on this rig the
        per-zone path through OpenRGB's ASRock controller **stalls the server**.
        Measured: whole-device writes ran 15/15 frames clean where zone writes
        stalled within one or two, after which the server stopped applying
        anything on that connection and the case froze on an old colour.

        The exception is a device with an *excluded* zone. A whole-device write
        must supply a colour for every LED, so it cannot leave one zone alone;
        such a device keeps the per-zone path, and its risk.
        """

        plan: list[tuple[int, tuple, str, bool]] = []
        for controller in client.controllers:
            rule: DeviceRule | None = self._config.rule_for(controller.name)
            if rule is None:
                self._logger.info("OpenRGB: %s is unclaimed by any rule; skipping", controller.name)
                continue
            if rule.group == GROUP_EXCLUDED:
                self._logger.info("OpenRGB: %s is excluded by configuration", controller.name)
                continue

            if rule.zones:
                groups = [(zone, rule.group_for_zone(zone.name)) for zone in controller.zones]
                if any(group == GROUP_EXCLUDED for _zone, group in groups):
                    self._logger.info(
                        "OpenRGB: %s has excluded zones; writing it zone by zone", controller.name
                    )
                    for zone, group in groups:
                        if group == GROUP_EXCLUDED or zone.led_count == 0:
                            continue
                        plan.append(
                            (controller.index, ((zone.index, group, zone.led_count),), rule.bus, True)
                        )
                    continue
                segments = tuple(
                    (group, zone.led_count) for zone, group in groups if zone.led_count > 0
                )
                if segments:
                    plan.append((controller.index, segments, rule.bus, False))
            elif controller.led_count > 0:
                plan.append(
                    (controller.index, ((rule.group, controller.led_count),), rule.bus, False)
                )
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
        for device, segments, bus, per_zone in self._targets:
            # Rate-limit per bus. SMBus is shared with sensor reads; the USB
            # devices have no such constraint and get the full rate.
            last = self._last_write.get(bus, 0.0)
            if now - last < self._interval(bus):
                continue
            key = (device, segments[0][0] if per_zone else None)
            if not self._should_send(key, (near, far), now):
                continue
            self._send_colours(device, segments, per_zone, near, far)
            self._sent[key] = ((near, far), time.monotonic())
        for bus in {target[2] for target in self._targets}:
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
            self._health_failures += 1
            if self._health_failures < HEALTH_FAILURES_BEFORE_RECONNECT:
                # Writes continue through this: an unanswered probe means the
                # server is slow to TALK, not that it has stopped applying
                # colours, and the lights are the point.
                self._logger.debug(
                    "openrgb: no reply to check %d; still writing", self._health_failures
                )
                return
            self._logger.warning(
                "openrgb: server has not answered %d checks; dropping the connection. "
                "If this repeats, the server is wedged: "
                "systemctl --user restart linux-rgb-openrgb",
                self._health_failures,
            )
            self.close()
            raise
        self._health_failures = 0
        if lag > 0.5:
            self._logger.info("openrgb: server is %.2fs behind", lag)

    def _should_send(self, key: tuple, colours: tuple[RGB, RGB], now: float) -> bool:
        """Whether this target needs the packet, or already shows these colours.

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

        previous = self._sent.get(key)
        if previous is None:
            return True
        colours_before, when = previous
        if (now - when) >= REFRESH_SECONDS:
            return True
        return any(
            abs(a - b) >= COLOUR_EPSILON
            for colour, before in zip(colours, colours_before)
            for a, b in zip(colour, before)
        )

    def _send_colours(
        self, device: int, segments: tuple, per_zone: bool, near: RGB, far: RGB
    ) -> None:
        """Put this target's colours on the wire, in as few packets as possible."""

        assert self._client is not None
        if per_zone:
            for zone_index, group, count in segments:
                self._client.update_zone_leds(
                    device, zone_index, [near if group == GROUP_NEAR else far] * count
                )
            return
        colours: list[RGB] = []
        for group, count in segments:
            colours.extend([near if group == GROUP_NEAR else far] * count)
        self._client.update_leds(device, colours)

    def black(self) -> None:
        """Write black to every target unconditionally.

        Deliberately bypasses the change filter: black is what every exit path
        writes, and "we think it is already black" is not good enough when the
        alternative is leaving the machine lit after a shutdown.
        """

        if self._client is None:
            self.connect()
        assert self._client is not None
        for device, segments, _bus, per_zone in self._targets:
            self._send_colours(device, segments, per_zone, (0, 0, 0), (0, 0, 0))

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
