"""Sound Blaster Katana V2X wire protocol, as pure functions.

Portions of this file are a translation of the ``v2x`` crate
(https://git.dog/xx/v2x), used under the MIT License:

    Copyright (c) 2026 Rasmus Moorats

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.

How the device talks
--------------------
The soundbar's control port is a CDC ACM serial device that starts in a text
mode. A host proves itself with an AES-256-GCM challenge-response, sends
``SW_MODE1``, and from then on the port speaks binary frames --
``5A op length payload`` -- until the *soundbar* is power-cycled. It has its own
power supply, so rebooting the PC does not reset it: after the first successful
handshake, every later connection finds it already in binary mode.

Lighting is opcode ``0x3A``. Colours go on the wire as ABGR with alpha fixed at
``FF``, so red, green and blue travel in reverse.

What is here, and what deliberately is not
------------------------------------------
Only what a lighting daemon needs: lighting power, solo mode, the colour count,
a solo colour, the ping, and two read-only queries (firmware version and
colours) for readback. The crate can also upload firmware (the ``5B`` marker,
opcode ``0x53``) and read, write and execute device memory on patched firmware
(``0x54``). None of that belongs in a lighting daemon, so it does not exist
here, and a test fails if a builder for it appears.

Everything in this module is a pure function over bytes, so the whole protocol
is tested without a device. :mod:`ambience_rgb.katana` does the I/O.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VENDOR_ID = 0x041E
MODELS = {0x3283: "Sound Blaster Katana V2X", 0x3260: "Sound Blaster Katana V2"}

MARKER = 0x5A
# A frame with a u16 length, used only for firmware transfer. Never sent; parsed
# so that one arriving is skipped whole rather than misread from the middle.
MARKER_LONG = 0x5B

_OP_PING = 0x03
_OP_INFO = 0x09
_OP_LIGHTING = 0x3A

# Lighting sub-commands: the first payload byte of an 0x3A frame.
_SUB_POWER = 0x25
_SUB_MODE = 0x29
_SUB_PARAM = 0x2B
_SUB_PARAM_QUERY = 0x2C
_SUB_COLOUR_COUNT = 0x37

_PARAM_COLOURS = 0x01
_INFO_FIRMWARE = 0x02
_MODE_MOOD = 0x03  # "Solo" is Mood with a colour count of one.
_SLOTS = 7

# Up to this many payload bytes of a question are echoed at the front of its
# answer: sub-command, zone, parameter and first slot. See is_reply_to().
_SELECTOR_BYTES = 4

# The AES key is the challenge's two header bytes, these 28, then its two PID
# bytes. Published in the v2x crate and its write-up; it identifies the
# protocol, not a user, and every Katana of a model shares it.
_KEY_DATA = bytes.fromhex("d31a21279be346f0999d6ec4c3febe98901869c118fbb1256e0ce07b")

CHALLENGE_PREFIX = b"whoareyou"
# "whoareyou", header 2, PID 2, nonce 32, CR LF. The nonce is raw binary and may
# itself contain CR LF, so the challenge is read by this length, never as a line.
CHALLENGE_LENGTH = len(CHALLENGE_PREFIX) + 2 + 2 + 32 + 2
_IV_BYTES = 16
_GCM_NONCE_BYTES = 12


def _frame(op: int, *payload: int) -> bytes:
    return bytes([MARKER, op, len(payload), *payload])


def lighting_power(on: bool) -> bytes:
    """Turn the soundbar's lights on or off."""
    return _frame(_OP_LIGHTING, _SUB_POWER, 1 if on else 0)


def colour_count(count: int) -> bytes:
    """How many colour slots the current effect uses; solo uses one."""
    return _frame(_OP_LIGHTING, _SUB_COLOUR_COUNT, 0x00, count)


def solo_mode() -> bytes:
    """Select the static single-colour effect (Mood, with one colour)."""
    return _frame(_OP_LIGHTING, _SUB_MODE, 0x00, _MODE_MOOD)


def solo_colour(colour: tuple[int, int, int]) -> bytes:
    """Set the solo colour. Takes RGB; the device wants ABGR."""
    if any(not 0 <= channel <= 255 for channel in colour):
        raise ValueError(f"Colour channels must be 0-255, got {colour}")
    red, green, blue = colour
    return _frame(_OP_LIGHTING, _SUB_PARAM, 0x00, _PARAM_COLOURS, 0x01, 0xFF, blue, green, red)


def ping() -> bytes:
    return _frame(_OP_PING)


def firmware_version_query() -> bytes:
    return _frame(_OP_INFO, _INFO_FIRMWARE)


def colour_query() -> bytes:
    """Ask for all seven colour slots."""
    return _frame(_OP_LIGHTING, _SUB_PARAM_QUERY, 0x00, _PARAM_COLOURS, 0x01, _SLOTS)


def model_for_pid(pid: int) -> str | None:
    return MODELS.get(pid)


@dataclass(frozen=True)
class Challenge:
    header: bytes
    pid: bytes
    nonce: bytes


def parse_challenge(data: bytes) -> Challenge:
    """Split a ``whoareyou`` challenge into its fields, by position."""
    body = data[len(CHALLENGE_PREFIX):]
    if not data.startswith(CHALLENGE_PREFIX) or len(body) < CHALLENGE_LENGTH - len(CHALLENGE_PREFIX) - 2:
        raise ValueError(f"Not a whole challenge ({len(data)} bytes): {data[:16]!r}")
    return Challenge(header=bytes(body[0:2]), pid=bytes(body[2:4]), nonce=bytes(body[4:36]))


def build_key(header: bytes, pid: bytes) -> bytes:
    return bytes(header) + _KEY_DATA + bytes(pid)


def unlock_response(challenge: Challenge, iv: bytes | None = None) -> bytes:
    """IV, then the nonce sealed with AES-256-GCM under that IV: 64 bytes.

    Only the IV's first twelve bytes are the GCM nonce; all sixteen are sent.
    Without an explicit ``iv`` one is drawn at random, redrawn until the whole
    response is free of CR and LF -- see the test of the same name for why.
    """
    aead = AESGCM(build_key(challenge.header, challenge.pid))
    while True:
        chosen = os.urandom(_IV_BYTES) if iv is None else iv
        response = chosen + aead.encrypt(chosen[:_GCM_NONCE_BYTES], challenge.nonce, None)
        if iv is not None or not (b"\r" in response or b"\n" in response):
            return response


def unlock_line(challenge: Challenge, iv: bytes | None = None) -> bytes:
    return b"unlock" + unlock_response(challenge, iv) + b"\r\n"


class FrameReader:
    """Reassembles frames from a byte stream that splits and merges them freely.

    Bytes before a frame marker are skipped: in binary mode stray text (a
    leftover CR LF from the handshake) can precede the first frame.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> None:
        self._buffer += data

    def clear(self) -> None:
        self._buffer.clear()

    def frames(self) -> list[bytes]:
        found = []
        while True:
            start = next(
                (i for i, byte in enumerate(self._buffer) if byte in (MARKER, MARKER_LONG)), None
            )
            if start is None:
                self._buffer.clear()
                return found
            del self._buffer[:start]
            length = _frame_length(self._buffer)
            if length is None or len(self._buffer) < length:
                return found
            found.append(bytes(self._buffer[:length]))
            del self._buffer[:length]


def _frame_length(buffer: bytearray) -> int | None:
    if buffer[0] == MARKER:
        return 3 + buffer[2] if len(buffer) >= 3 else None
    return 4 + struct.unpack_from("<H", buffer, 2)[0] if len(buffer) >= 4 else None


def is_reply_to(frame: bytes, request: bytes) -> bool:
    """Whether ``frame`` is the answer to ``request``.

    A reply repeats its question's opcode and up to its first four payload bytes
    -- the selector: sub-command, zone, parameter, first slot. What follows is
    the answer. So the colour and speed queries, which share sub-command 0x2C,
    are told apart by the parameter byte, and a firmware reply can never be
    taken for a colour reply.
    """
    echoed = min(len(request) - 3, _SELECTOR_BYTES)
    return (
        len(frame) >= 3 + echoed
        and frame[0] == MARKER
        and frame[1] == request[1]
        and frame[3:3 + echoed] == request[3:3 + echoed]
    )


def parse_firmware_version(frame: bytes) -> str:
    """``5A 09 len 02 n <n bytes, NUL-terminated>`` -> ``"1.3.230619.1820"``."""
    if not is_reply_to(frame, firmware_version_query()) or len(frame) < 5:
        raise ValueError(f"Not a firmware version reply: {frame.hex(' ')}")
    return frame[5:5 + frame[4]].split(b"\0", 1)[0].decode("ascii", "replace")


def parse_colours(frame: bytes) -> list[tuple[int, int, int]]:
    """Every colour slot in a colour-query reply, as RGB, in slot order."""
    if not is_reply_to(frame, colour_query()):
        raise ValueError(f"Not a colour reply: {frame.hex(' ')}")
    body = frame[7:3 + frame[2]]
    return [(body[i + 3], body[i + 2], body[i + 1]) for i in range(0, len(body) - 3, 4)]
