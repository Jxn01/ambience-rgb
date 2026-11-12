"""Where colours go: OpenRGB's devices, the Commander Core, and the Katana (via HA).

Every sink presents the same tiny interface, and every one is **best effort**. A
sink that raises is logged once, marked degraded, and retried on a timer; it
never takes the daemon down. Losing the AIO must not stop the motherboard
syncing, and losing the OpenRGB server must not stop the AIO.

That is not defensive habit, it is the actual failure mode: the OpenRGB server
is a separate process that can be restarted or crash on its own (a RESIZEZONE on
the Commander Core kills 1.0rc3 outright), the AIO is a USB device that
can be unplugged, and Home Assistant -- which drives the soundbar -- is another
machine that can be rebooted or unreachable.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Protocol

from ambience_rgb.config import (
    BUS_SMBUS,
    GROUP_EXCLUDED,
    GROUP_NEAR,
    CommanderConfig,
    Config,
    DeviceRule,
    KatanaHAConfig,
)
from ambience_rgb.openrgb_client import OpenRGBClient, OpenRGBError
from ambience_rgb.render import apply_channel_order, spread

LOGGER = logging.getLogger("ambience_rgb.sinks")

RGB = tuple[int, int, int]

# How long a failed sink waits before trying again. Long enough not to spin on a
# dead server, short enough that a restarted one is picked up without help.
RETRY_SECONDS = 10.0

# The Commander Core needs a much shorter one. It reverts to its firmware
# pattern about ten seconds after the last commit, so a ten-second backoff is
# almost exactly long enough to guarantee the fans flash green on any hiccup.
# Its backoff must stay comfortably under that revert time.
COMMANDER_RETRY_SECONDS = 2.0

# How often an UNCHANGED colour is written again. The devices need an occasional
# refresh so a controller that was reset or re-detected does not sit on a stale
# frame, but writing an unchanged colour every tick is what floods the server.
REFRESH_SECONDS = 2.0

# Smallest per-channel move worth a packet. The ambient colour jitters by a unit
# or two every frame on a static screen; without this, exact-equality filtering
# still resends all eleven targets every tick and the server is overrun anyway.
# Invisible on an LED, and matterlights applies the same idea to the bulbs.
COLOUR_EPSILON = 3

# How often to measure the server's lag, and the lag that counts as too far
# behind. Writes are fire-and-forget, so this round trip is the only honest
# signal; a wedged server is otherwise indistinguishable from a healthy one.
HEALTH_CHECK_SECONDS = 5.0
HEALTH_TIMEOUT_SECONDS = 5.0

# Consecutive unanswered probes before the connection is dropped.
#
# A 300-second streaming run measured 60 health checks and 0 misses, so a high
# count (6) only delays noticing a real wedge -- half a minute of stale colour.
# 3 tolerates a genuine hiccup and still reports a wedged server in ~15 s.
HEALTH_FAILURES_BEFORE_RECONNECT = 3

# Reconnects inside this window before the server is declared wedged rather than
# merely slow. Reconnecting fixes a dropped connection; it does nothing for a
# server that answers promptly and drives nothing, which is a state this one
# reaches. At that point only a restart helps.
WEDGE_RECONNECTS = 3
WEDGE_WINDOW_SECONDS = 120.0

# Never restart the server more often than this, whatever happens. A restart
# loop would be worse than a frozen case: it also resets every device, including
# ones held at a static colour.
RESTART_COOLDOWN_SECONDS = 300.0

# Windows' CREATE_NO_WINDOW process-creation flag. Spelled out because the
# subprocess module only defines it on Windows, and the tests run everywhere.
CREATE_NO_WINDOW = 0x08000000


@contextmanager
def released_on_failure(sink) -> Iterator[None]:
    """Drop the sink's live handle if the operation inside fails.

    Shared by both sinks rather than repeated in each. ``ResilientSink`` catches a failure and retries on a timer, but it
    cannot reach inside to invalidate the handle -- so a socket or HID handle
    that has died gets reused for every retry and fails identically forever.

    For OpenRGB, after the server is restarted underneath a running daemon, the
    only other path that reconnects is the health check, six timeouts later:
    recovery took 71 s, against 11 s when the handle is dropped here. The
    Commander Core sink has no equivalent rescue -- an unplugged AIO would never
    be reopened.

    Any exception counts. A handle whose last operation failed is suspect
    whatever the reason, and reopening one is cheap.
    """

    try:
        yield
    except Exception:
        try:
            sink.release()
        except Exception:  # noqa: BLE001 - the original failure is the news
            pass
        raise


def _moved(before: tuple[RGB, ...], after: tuple[RGB, ...]) -> bool:
    """Whether any channel of any colour moved by at least COLOUR_EPSILON.

    Shared by every sink with a change filter, so "changed enough to send"
    means one thing everywhere.

    A different COUNT of colours is a move too. zip() alone stops at the shorter
    signature, so a colour joining the palette would otherwise count as no change.
    """

    if len(before) != len(after):
        return True
    return any(
        abs(a - b) >= COLOUR_EPSILON
        for colour, previous in zip(after, before)
        for a, b in zip(colour, previous)
    )


class Sink(Protocol):
    name: str

    def write(self, near: RGB, far: RGB, palette: list | None = None) -> None: ...
    def black(self) -> None: ...
    def release(self) -> None: ...
    def close(self) -> None: ...


class ResilientSink:
    """Wraps a sink so its failures are isolated and it recovers on its own."""

    def __init__(
        self,
        sink: Sink,
        logger: logging.Logger | None = None,
        retry_seconds: float = RETRY_SECONDS,
    ) -> None:
        self._sink = sink
        self._logger = logger or LOGGER
        self._failed_at: float | None = None
        self._retry_seconds = retry_seconds
        self.name = sink.name

    @property
    def degraded(self) -> bool:
        return self._failed_at is not None

    def _guard(self, action: str, call, *, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and self._failed_at is not None and (now - self._failed_at) < self._retry_seconds:
            return False
        try:
            call()
        except Exception as error:  # noqa: BLE001 - a sink may not kill the daemon
            # A forced attempt is logged every time: there is no later retry to
            # show up in the journal instead.
            if self._failed_at is None or force:
                self._logger.warning("%s: %s failed (%s); will retry", self.name, action, error)
            self._failed_at = now
            return False
        if self._failed_at is not None:
            self._logger.info("%s: recovered", self.name)
            self._failed_at = None
        return True

    def write(self, near: RGB, far: RGB, palette: list | None = None) -> bool:
        return self._guard("write", lambda: self._sink.write(near, far, palette))

    def black(self) -> bool:
        """Always attempted, backoff or not: black is what every exit path writes.

        SIGTERM can land mid-write -- hidapi reports the interrupted poll() as
        "read error" -- and that failure marks the sink degraded a fraction of a
        millisecond before the shutdown blacks it. With the backoff applied here
        the shutdown's black would be skipped without a word and the AIO would
        drift back to its firmware rainbow. The backoff is for ticks, which come
        again; an exit path does not.
        """

        return self._guard("black", self._sink.black, force=True)

    def release(self) -> None:
        try:
            self._sink.release()
        except Exception:  # noqa: BLE001 - releasing must not raise
            self._logger.debug("%s: release failed", self.name, exc_info=True)

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
        # How many controllers the current plan was built against. A change
        # means the device set moved under us -- see _check_health.
        self._planned_controllers = 0
        self._reconnects: list[float] = []
        # None, not 0.0. time.monotonic() is boot-relative, so a 0.0 sentinel
        # makes the cooldown swallow the FIRST restart for the first five
        # minutes of uptime -- exactly when a wedge at startup would matter.
        self._restarted_at: float | None = None

    def connect(self) -> None:
        client = OpenRGBClient(
            self._config.openrgb_host, self._config.openrgb_port, logger=self._logger
        ).connect()
        self._client = client
        if self._config.commander.enabled:
            from ambience_rgb.commander_core import CONTENTION_ADVICE, openrgb_has_it

            contenders = openrgb_has_it(controller.name for controller in client.controllers)
            if contenders:
                self._logger.error(
                    "openrgb: %s", CONTENTION_ADVICE.format(names=", ".join(contenders))
                )
        self._sent.clear()
        self._targets = self._plan(client)
        self._planned_controllers = len(client.controllers)
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
            # it. The GPU ships on Rainbow and the ASRock board on Off: with only
            # SETCUSTOMMODE the GPU keeps cycling its rainbow while every
            # readback claims it is in Direct.
            client.set_direct_mode(device_index)
        self._apply_static(client)

    def _apply_static(self, client: OpenRGBClient) -> None:
        """Set every ``static`` device to its fixed colour, once per connection.

        Deliberately here rather than in the tick loop, and deliberately repeated
        on every reconnect: OpenRGB's Direct mode does not persist, so restarting
        the server leaves these devices dark and an ``excluded`` rule means
        nothing ever puts them back -- including after our own recovery restart.
        """

        for controller in client.controllers:
            rule = self._config.rule_for(controller.name)
            if rule is None or rule.static is None or controller.led_count <= 0:
                continue
            client.set_direct_mode(controller.index)
            client.update_leds(controller.index, [rule.static] * controller.led_count)
            self._logger.info(
                "OpenRGB: %s held at static %s", controller.name, rule.static
            )

    def _plan(self, client: OpenRGBClient) -> list[tuple[int, tuple, str, bool]]:
        """Resolve controllers to write targets: (device, segments, bus, per_zone).

        For a whole-device write ``segments`` is ``((group, led_count), ...)``
        covering the device's LEDs in order, so a split controller is still one
        packet. For the per-zone fallback it is ``((zone_index, group, count),)``.

        **Prefer one whole-device write to several per-zone writes.** Zones are
        contiguous ranges of the same LED array, so writing the array once says
        exactly the same thing in one packet instead of six -- and the per-zone
        path through OpenRGB's ASRock controller **stalls the server**.
        Whole-device writes ran 15/15 frames clean where zone writes stalled
        within one or two, after which the server stopped applying anything on
        that connection and the case froze on an old colour.

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
                        plan.append((
                            controller.index,
                            ((zone.index, group, zone.led_count, rule.order_for_zone(zone.name)),),
                            rule.bus,
                            True,
                        ))
                    continue
                segments = tuple(
                    (group, zone.led_count, rule.order_for_zone(zone.name))
                    for zone, group in groups
                    if zone.led_count > 0
                )
                if segments:
                    plan.append((controller.index, segments, rule.bus, False))
            elif controller.led_count > 0:
                plan.append((
                    controller.index,
                    ((rule.group, controller.led_count, rule.channel_order),),
                    rule.bus,
                    False,
                ))
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

    @property
    def _treatment(self) -> dict:
        return {"saturation": self._config.saturation, "gamma": self._config.gamma}

    def write(self, near: RGB, far: RGB, palette: list | None = None) -> None:
        if self._client is None:
            self.connect()
        assert self._client is not None
        palette = palette or []
        now = time.monotonic()
        self._check_health(now)
        with released_on_failure(self):
            for device, segments, bus, per_zone in self._targets:
                # Rate-limit per bus. SMBus is shared with sensor reads; the USB
                # devices have no such constraint and get the full rate.
                last = self._last_write.get(bus, 0.0)
                if now - last < self._interval(bus):
                    continue
                key = (device, segments[0][0] if per_zone else None)
                # The palette is part of what gets written, so it is part of
                # what "unchanged" means. Leaving it out of the signature would
                # freeze the gradient the moment near and far happened to hold
                # still.
                signature = (near, far, *(colour for colour, _weight in palette))
                if not self._should_send(key, signature, now):
                    continue
                self._send_colours(device, segments, per_zone, near, far, palette)
                self._sent[key] = (signature, time.monotonic())
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
            lag, controllers = self._client.probe(HEALTH_TIMEOUT_SECONDS)
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
                "openrgb: server has not answered %d checks; dropping the connection",
                self._health_failures,
            )
            self.close()
            self._note_reconnect(now)
            raise
        self._health_failures = 0
        announced = getattr(self._client, "device_list_dirty", False)
        if announced or controllers != self._planned_controllers:
            # Not a wedge -- deliberately NOT counted by _note_reconnect, which
            # exists to escalate a server that keeps going unresponsive. This is a
            # healthy server whose device list moved, and one clean reconnect is
            # the whole fix. Counting it here would restart OpenRGB three device
            # changes into an otherwise perfect run.
            # Two independent signals, deliberately. The announcement is the
            # server's own word and catches a change that leaves the COUNT the
            # same -- one device swapped for another. The count is the backstop
            # for a notification we were not connected to hear.
            self._logger.info(
                "openrgb: device set changed (%s; planned for %d controllers, server "
                "now reports %d); reconnecting to re-plan",
                "server announced it" if announced else "count moved",
                self._planned_controllers,
                controllers,
            )
            self.close()
            raise OpenRGBError(
                f"The OpenRGB device set changed ({self._planned_controllers} -> "
                f"{controllers} controllers); reconnecting to re-plan"
            )
        if lag > 0.5:
            self._logger.info("openrgb: server is %.2fs behind", lag)

    def _note_reconnect(self, now: float) -> None:
        """Count reconnects, and restart the server if they are not helping."""

        self._reconnects = [
            stamp for stamp in self._reconnects if now - stamp < WEDGE_WINDOW_SECONDS
        ]
        self._reconnects.append(now)
        if len(self._reconnects) < WEDGE_RECONNECTS:
            return
        command = self._config.openrgb_restart_command
        if not command:
            from ambience_rgb import hints

            self._logger.error(
                "openrgb: %d reconnects in %.0fs and still not healthy -- the server is "
                "wedged. Restart it: %s (or set openrgb.restart_command to do it "
                "automatically)",
                len(self._reconnects),
                WEDGE_WINDOW_SECONDS,
                hints.restart_openrgb(),
            )
            self._reconnects.clear()
            return
        if self._restarted_at is not None and now - self._restarted_at < RESTART_COOLDOWN_SECONDS:
            return
        self._restarted_at = now
        self._reconnects.clear()
        self._logger.error(
            "openrgb: wedged after %d reconnects; restarting it with %s",
            WEDGE_RECONNECTS,
            " ".join(command),
        )
        import subprocess
        import sys

        # Under pythonw there is no console, so a console program started from
        # here -- PowerShell, on Windows -- gets a window of its own, flashing up
        # on the desktop at every restart. POSIX rejects the flag: Windows only.
        extra = {"creationflags": CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        try:
            subprocess.run(list(command), check=False, capture_output=True, timeout=30, **extra)
        except Exception:  # noqa: BLE001 - a failed restart must not kill the daemon
            self._logger.exception("openrgb: restart command failed")

    def _should_send(self, key: tuple, colours: tuple[RGB, RGB], now: float) -> bool:
        """Whether this target needs the packet, or already shows these colours.

        **The write side of this protocol is fire-and-forget, so overrunning
        the server is invisible from here.** Every ``sendall`` returns in
        microseconds whatever the server is doing with the bytes; the OpenRGB
        server then applies them one at a time against real USB and SMBus
        devices, which is far slower. Writing all eleven targets every tick
        offered it ~43 packets a second, it drained them at a fraction of that,
        and the backlog grew without bound -- so the board displayed a colour
        from minutes ago, even black long after the screen came back.

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
        return _moved(colours_before, colours)

    def _send_colours(
        self, device: int, segments: tuple, per_zone: bool, near: RGB, far: RGB,
        palette: list | None = None,
    ) -> None:
        """Put this target's colours on the wire, in as few packets as possible."""

        assert self._client is not None
        palette = palette or []
        bias = self._config.spread_bias
        treatment = self._treatment
        if per_zone:
            for zone_index, group, count, order in segments:
                base = near if group == GROUP_NEAR else far
                self._client.update_zone_leds(
                    device, zone_index,
                    [
                        apply_channel_order(colour, order)
                        for colour in spread(palette, base, count, bias=bias, treatment=treatment)
                    ],
                )
            return
        colours: list[RGB] = []
        for group, count, order in segments:
            base = near if group == GROUP_NEAR else far
            colours.extend(
                apply_channel_order(colour, order)
                for colour in spread(palette, base, count, bias=bias, treatment=treatment)
            )
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
        with released_on_failure(self):
            for device, segments, _bus, per_zone in self._targets:
                self._send_colours(device, segments, per_zone, (0, 0, 0), (0, 0, 0))

    def release(self) -> None:
        """Same as close() here: this sink writes nothing on the way out."""

        self.close()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


class CommanderCoreSink:
    """Drives the Corsair AIO through our own hidraw driver."""

    name = "commander-core"

    def __init__(
        self,
        commander: CommanderConfig,
        logger: logging.Logger | None = None,
        bias: float = 1.0,
        treatment: dict | None = None,
    ) -> None:
        self._config = commander
        self._logger = logger or LOGGER
        self._device = None
        self._bias = bias
        self._treatment = treatment or {}

    def connect(self) -> None:
        from ambience_rgb.commander_core import CommanderCore

        self._device = CommanderCore(logger=self._logger).open()

    def write(self, near: RGB, far: RGB, palette: list | None = None) -> None:
        if self._device is None:
            self.connect()
        assert self._device is not None
        palette = palette or []
        pump = near if self._config.pump_group == GROUP_NEAR else far
        fans = near if self._config.fan_group == GROUP_NEAR else far
        bias, treatment = self._bias, self._treatment
        # The pump ring is 29 LEDs and each fan is 8: enough to show a gradient
        # rather than a single tone, which is the whole point of the palette.
        pump_leds = spread(palette, pump, self._device.pump_leds, bias=bias, treatment=treatment)
        port_leds = [
            spread(palette, fans, count, bias=bias, treatment=treatment)
            for count in self._device.port_leds
        ]
        with released_on_failure(self):
            self._device.set_direct(pump_leds, port_leds)

    def black(self) -> None:
        if self._device is None:
            self.connect()
        assert self._device is not None
        with released_on_failure(self):
            self._device.black()

    def release(self) -> None:
        """Drop the handle without blacking it -- see CommanderCore.release."""

        if self._device is not None:
            self._device.release()
            self._device = None

    def close(self) -> None:
        if self._device is not None:
            self._device.close()
            self._device = None


class KatanaHASink:
    """The Katana through Home Assistant: colours are posted, HA drives the soundbar.

    Posting happens on a worker thread with a one-colour slot -- the newest
    colour always wins -- so a slow or absent HA never stalls the daemon's tick
    (the Commander Core reverts to its firmware pattern if it goes ten seconds
    unwritten). The tick only computes the colour and drops it in the slot.

    What gets posted:

    * a colour that has really moved (the same epsilon as every sink), at most
      every ``rate_hz``;
    * an unchanged colour again every ``keepalive_seconds``, because HA turns the
      lights off after 10 s without a frame (its hand-over);
    * **black once, then nothing**: a dark screen, a stale publisher or a stop
      lets HA's hand-over turn the lighting off, instead of keeping it lit black.

    A post that fails is raised from the next ``write`` so ResilientSink logs it
    and backs off; the worker keeps the newest colour and tries again.

    A post that HA *refuses* -- ``accepted: false``, because its Screen sync
    switch is off -- is not a failure: any manual lighting change in HA switches
    Screen sync off on purpose, and the next post after it is switched back on
    resumes at once. It is logged once per change instead, because otherwise the
    light bar simply stops following the screen and nothing says why.
    """

    name = "katana-ha"

    def __init__(
        self,
        katana: KatanaHAConfig,
        logger: logging.Logger | None = None,
        bias: float = 1.0,
        treatment: dict | None = None,
        *,
        client_factory=None,
        clock=time.monotonic,
        start_worker: bool = True,
    ) -> None:
        self._config = katana
        self._logger = logger or LOGGER
        self._bias = bias
        self._treatment = treatment or {}
        self._client_factory = client_factory or self._open_client
        self._clock = clock
        self._client = None
        self._sent: RGB | None = None
        self._posted_at: float | None = None
        self._slot: RGB | None = None
        self._error: Exception | None = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._start_worker = start_worker
        self._accepted: bool | None = None  # HA's answer to the last post; None until one lands

    def _open_client(self):
        from ambience_rgb.ha_stream import HAStreamClient, read_token

        return HAStreamClient(self._config.url or "", read_token(self._config.token_file), self._config.timeout_seconds)

    def _ensure_worker(self) -> None:
        if self._start_worker and (self._worker is None or not self._worker.is_alive()):
            self._stop.clear()
            self._worker = threading.Thread(target=self._run, name="katana-ha poster", daemon=True)
            self._worker.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            self.flush()

    def flush(self) -> None:
        """Post whatever is in the slot. The worker calls it; tests call it directly."""

        with self._lock:
            colour, self._slot = self._slot, None
        if colour is None:
            return
        try:
            if self._client is None:
                self._client = self._client_factory()
            accepted = self._client.post(colour)
        except Exception as error:  # noqa: BLE001 - surfaced through write()
            with self._lock:
                self._error = error
                if self._slot is None:
                    self._slot = colour  # try the newest colour again next time
            if self._client is not None:
                self._client.close()
            self._client = None
            return
        self._note_accepted(bool(accepted))

    def _note_accepted(self, accepted: bool) -> None:
        if accepted == self._accepted:
            return
        if not accepted:
            self._logger.warning(
                "katana-ha: Home Assistant is dropping the colours -- the Katana's Screen sync "
                "switch is off there (any manual lighting change turns it off). Switch it back on in HA."
            )
        elif self._accepted is False:
            self._logger.info("katana-ha: Screen sync is on again; Home Assistant is applying the colours")
        self._accepted = accepted

    def _queue(self, colour: RGB) -> None:
        with self._lock:
            self._slot = colour
        self._ensure_worker()
        self._wake.set()

    def _raise_pending_error(self) -> None:
        with self._lock:
            error, self._error = self._error, None
        if error is not None:
            raise error

    def write(self, near: RGB, far: RGB, palette: list | None = None) -> None:
        self._raise_pending_error()
        now = self._clock()
        base = near if self._config.group == GROUP_NEAR else far
        colour = spread(palette or [], base, 1, bias=self._bias, treatment=self._treatment)[0]
        if colour == (0, 0, 0):
            if self._sent != (0, 0, 0):
                self._queue(colour)
                self._sent, self._posted_at = colour, now
            return  # black once, then silence: HA's hand-over turns the lights off
        recent = self._posted_at is not None and now - self._posted_at < self._config.interval_seconds
        if recent:
            return
        moved = self._sent is None or _moved((self._sent,), (colour,))
        due = self._posted_at is None or now - self._posted_at >= self._config.keepalive_seconds
        if not moved and not due:
            return
        self._queue(colour)
        self._sent, self._posted_at = colour, now

    def black(self) -> None:
        """Exit paths: black once, at once. HA then turns the lights off after its hand-over."""

        if self._sent != (0, 0, 0):
            self._queue((0, 0, 0))
            self._sent = (0, 0, 0)
        if self._start_worker:
            # The process may be about to exit: give the worker a moment to send it.
            deadline = time.monotonic() + self._config.timeout_seconds
            while time.monotonic() < deadline:
                with self._lock:
                    if self._slot is None:
                        break
                time.sleep(0.02)

    def release(self) -> None:
        """Nothing held on the soundbar: HA owns it. Just stop posting."""

        self._stop.set()
        self._wake.set()

    def close(self) -> None:
        self.release()
        if self._worker is not None:
            self._worker.join(timeout=self._config.timeout_seconds + 1)
            self._worker = None
        if self._client is not None:
            self._client.close()
            self._client = None


def build_sinks(config: Config, logger: logging.Logger | None = None) -> list[ResilientSink]:
    """Every configured sink, each wrapped so one failure cannot stop the rest."""

    log = logger or LOGGER
    treatment = {"saturation": config.saturation, "gamma": config.gamma}
    sinks: list[ResilientSink] = [ResilientSink(OpenRGBSink(config, log), log)]
    if config.commander.enabled:
        # A shorter backoff, because this device forgets: see
        # COMMANDER_RETRY_SECONDS.
        sinks.append(
            ResilientSink(
                CommanderCoreSink(config.commander, log, config.spread_bias, treatment),
                log,
                COMMANDER_RETRY_SECONDS,
            )
        )
    if config.katana_ha.enabled:
        # The standard backoff: the soundbar holds its colour, so nothing is
        # lost on it while a retry waits.
        sinks.append(
            ResilientSink(KatanaHASink(config.katana_ha, log, config.spread_bias, treatment), log)
        )
    return sinks
