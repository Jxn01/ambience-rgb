"""Corsair Commander Core wire protocol, as pure functions.

Nothing here touches hardware. Every function maps inputs to bytes, so the
framing can be tested byte-for-byte against the reference implementations
without an AIO plugged in -- which matters more than usual here, because the
lighting endpoint is **write-only**: the device will never tell us we got the
layout wrong, it will just light the wrong LEDs or none at all.

Sources, both read directly rather than guessed:

* **liquidctl** ``liquidctl/driver/commander_core.py`` -- the command envelope
  and the open/read/write/close endpoint dance, in working Python.
* **OpenRGB** ``Controllers/CorsairCommanderCoreController`` -- the lighting
  write, which liquidctl deliberately does not implement
  (its ``set_color`` is ``raise NotSupportedByDriver``).

Device: Corsair Commander Core, USB ``1b1c:0c32``, HID interface 0.
"""

from __future__ import annotations

# **64-byte reports. A wrong value gives no feedback.**
#
# The device's own HID report descriptor settles it -- `75 08 96 40 00` is
# Report Size 8 bits x Report Count 0x0040, so both the input and output reports
# are 64 bytes. OpenRGB agrees: PID 0x0C32 is its COMMANDER_CORE2 and selects
# PACKET_SIZE_V3 = 65, which is these 64 bytes plus hidapi's report-id byte.
#
# With a larger size everything still *looks* fine: the four-byte command
# header sits at the front of every packet, so it arrives intact, every command
# is acknowledged and LED counts read back correctly. Only the colour payload --
# which lives past the header and spans many packets -- is cut off, so the LEDs
# never light while the protocol reports perfect health. Nothing in the
# transport can detect this; only the hardware can, by staying dark.
#
# Read it from the descriptor rather than assuming:
#   od -An -tx1 /sys/class/hidraw/hidrawN/device/report_descriptor
REPORT_LENGTH = 64
PACKET_LENGTH = REPORT_LENGTH + 1

# -- commands ---------------------------------------------------------------
CMD_WAKE = (0x01, 0x03)
CMD_WAKE_DATA = (0x00, 0x02)
CMD_SLEEP = (0x01, 0x03)
CMD_SLEEP_DATA = (0x00, 0x01)
CMD_OPEN_ENDPOINT = (0x0D, 0x00)
CMD_CLOSE_ENDPOINT = (0x05, 0x01)
CMD_WRITE = (0x06, 0x00)
CMD_WRITE_MORE = (0x07, 0x00)
CMD_READ_INITIAL = (0x08, 0x00, 0x01)
CMD_READ_MORE = (0x08, 0x00, 0x02)
CMD_READ_FINAL = (0x08, 0x00, 0x03)

# -- endpoints and their data types -----------------------------------------
ENDPOINT_LED_COUNT = (0x20,)
DATA_TYPE_LED_COUNT = (0x0F, 0x00)
ENDPOINT_FAN_MODE = (0x1E,)
DATA_TYPE_FAN_MODE = (0x0D, 0x00)
ENDPOINT_DIRECT_COLOUR = (0x22,)
DATA_TYPE_DIRECT_COLOUR = (0x12, 0x00)

# -- lighting layout --------------------------------------------------------
PUMP_LEDS = 29
PORT_COUNT = 6
# Every port occupies 34 slots in the buffer whatever is plugged into it,
# because SetFanMode puts the controller in "6 QL fan" mode. Attached fans
# have 8 LEDs; the remaining slots must be written black, never left
# to carry colour, or ports appear to bleed into one another.
PORT_SLOTS = 34
COLOUR_BUFFER_LENGTH = PUMP_LEDS * 3 + PORT_COUNT * PORT_SLOTS * 3  # 699

RGB = tuple[int, int, int]


def command_packet(command: tuple[int, ...], data: bytes | tuple[int, ...] = ()) -> bytes:
    """One outgoing report.

    ``buf[0]`` is the hidapi report id and is always 0; ``buf[1]`` is always
    0x08 on the way out. The command goes at 2, its payload from 4.
    """

    buf = bytearray(PACKET_LENGTH)
    buf[1] = 0x08
    buf[2 : 2 + len(command)] = bytes(command)
    start = 2 + len(command)
    buf[start : start + len(data)] = bytes(data)
    return bytes(buf)


def endpoint_write_packets(
    endpoint: tuple[int, ...], data_type: tuple[int, ...], data: bytes
) -> list[bytes]:
    """The full open -> write -> close sequence for one endpoint write.

    **No read-back.** liquidctl's ``_write_data`` begins by reading the
    endpoint to confirm the data type, which is a good guard for the endpoints
    it uses -- but the lighting endpoint never answers a read, so that check
    hangs. OpenRGB does open/write/close with no read, and so does this.

    The first packet carries a 6-byte header (declared length, two zero bytes,
    then the data type) and so fits ``REPORT_LENGTH - 9`` payload bytes;
    continuation packets are raw and fit ``REPORT_LENGTH - 3``.
    """

    packets = [command_packet(CMD_OPEN_ENDPOINT, endpoint)]

    data_len = len(data)
    start = 0
    while start < data_len:
        if start == 0:
            chunk = min(REPORT_LENGTH - 9, data_len)
            header = bytearray(4 + len(data_type))
            # Declared length counts the data type bytes as well.
            header[0:2] = (data_len + len(data_type)).to_bytes(2, "little")
            header[4 : 4 + len(data_type)] = bytes(data_type)
            packets.append(command_packet(CMD_WRITE, bytes(header) + data[:chunk]))
        else:
            chunk = min(REPORT_LENGTH - 3, data_len - start)
            packets.append(command_packet(CMD_WRITE_MORE, data[start : start + chunk]))
        start += chunk

    packets.append(command_packet(CMD_CLOSE_ENDPOINT))
    return packets


def colour_buffer(pump: list[RGB], ports: list[list[RGB]]) -> bytes:
    """The 699-byte direct-colour payload.

    Layout, fixed regardless of what is attached::

        0   .. 86    pump    29 LEDs
        87  .. 188   port 1  34 slots
        189 .. 290   port 2
        ...
        597 .. 698   port 6

    Colours beyond a port's real LED count are left black. Passing more colours
    than a port has slots is truncated rather than allowed to overrun into the
    next port, which would be silent and look like a wiring fault.
    """

    buf = bytearray(COLOUR_BUFFER_LENGTH)

    for index, (red, green, blue) in enumerate(pump[:PUMP_LEDS]):
        offset = index * 3
        buf[offset] = red
        buf[offset + 1] = green
        buf[offset + 2] = blue

    base = PUMP_LEDS * 3
    for port_index in range(min(len(ports), PORT_COUNT)):
        port_base = base + port_index * PORT_SLOTS * 3
        for index, (red, green, blue) in enumerate(ports[port_index][:PORT_SLOTS]):
            offset = port_base + index * 3
            buf[offset] = red
            buf[offset + 1] = green
            buf[offset + 2] = blue

    return bytes(buf)


def fan_mode_payload() -> bytes:
    """OpenRGB's ``SetFanMode`` payload for the non-XT Commander Core.

    Forces "6 QL fan" mode so every RGB port exposes its full 34 slots. Without
    it the controller only accepts colour for the ports it thinks are populated.
    """

    buf = bytearray(15)
    buf[0] = 0x07
    buf[1] = 0x01
    buf[2] = 0x08
    for index in range(3, 15, 2):
        buf[index] = 0x01
        buf[index + 1] = 0x06
    return bytes(buf)


def parse_led_counts(data: bytes) -> list[int | None]:
    """LED counts per channel from an ``ENDPOINT_LED_COUNT`` read.

    Channel 0 is the pump; 1..6 are the RGB ports. A channel reports a
    connection state of 2 when something is attached; anything else yields
    ``None`` rather than 0, so "nothing plugged in" stays distinguishable from
    "attached but reporting zero LEDs".

    Read this **before** forcing fan mode. Afterwards the controller can
    report the 34 slots it exposes rather than the LEDs that physically exist,
    and writing colour to all 34 would light nothing while looking correct.
    """

    if not data:
        return []
    channels = data[0]
    counts: list[int | None] = []
    for index in range(channels):
        offset = 1 + index * 4
        if offset + 4 > len(data):
            break
        connected = int.from_bytes(data[offset : offset + 2], "little")
        leds = int.from_bytes(data[offset + 2 : offset + 4], "little")
        counts.append(leds if connected == 2 else None)
    return counts
