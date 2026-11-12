"""Turning one frame's colours into what an LED strip should actually show.

Two problems live here, and they are both about the difference between a bulb and
an LED.

**Colour.** MatterLights renders for bulbs: it hands Home Assistant a colour and
a brightness, and the bulb is a diffuse area source across a room. An addressable
LED is a point source at full output a metre from your face. The chromaticity can
be identical and the perception completely different -- a 6%-saturated pale green
reads as tinted on a lamp and as plain white on a strip. :func:`treat` exists to
push the LEDs somewhere that *looks* like what the lamps look like, which is not
the same as sending them the same numbers.

**Depth.** The engine clusters each frame into a weighted palette of up to four
colours and then collapses it to one colour per group, because six bulbs cannot
show more than six colours. A motherboard header can have 97 LEDs and a fan ring 8. :func:`spread` puts the palette back, as a gradient, so a strip
shows the frame's actual colours instead of their average.

Both are pure functions over plain tuples: no device, no socket, no config
object, so every rule here is testable without hardware.
"""

from __future__ import annotations

RGB = tuple[int, int, int]
PaletteEntry = tuple[RGB, float]

BLACK: RGB = (0, 0, 0)


def _clamp(value: float) -> int:
    return 0 if value < 0 else 255 if value > 255 else int(round(value))


def treat(colour: RGB, *, saturation: float = 1.0, gamma: float = 1.0) -> RGB:
    """Push a colour toward how the bulbs *look*, rather than what they are sent.

    ``saturation`` moves each channel away from the mean of the three -- the same
    transform MatterLights already applies to the bulbs' own colours, so this is
    a second helping of a known-good curve rather than a new idea. 1.0 disables
    it.

    ``gamma`` above 1.0 raises saturation by spreading the channels apart:
    raising each to a power costs the *weaker* channels proportionally more, so
    ``(240, 255, 240)`` becomes ``(226, 255, 226)`` -- 5.9% saturated to 11.4%.

    It does **not** dim a colour whose peak channel is already 255, because
    ``1.0 ** gamma == 1.0``. Gamma tints a blown-out frame; it does not calm one.
    Anything below full does darken. 1.0 disables it.

    Black stays black under both, which matters -- "off" is a colour this project
    writes constantly, and it must survive the pipeline untouched.
    """

    red, green, blue = colour
    if saturation != 1.0:
        midpoint = (red + green + blue) / 3
        red = midpoint + (red - midpoint) * saturation
        green = midpoint + (green - midpoint) * saturation
        blue = midpoint + (blue - midpoint) * saturation
    if gamma != 1.0:
        red = 255 * max(0.0, min(1.0, red / 255)) ** gamma
        green = 255 * max(0.0, min(1.0, green / 255)) ** gamma
        blue = 255 * max(0.0, min(1.0, blue / 255)) ** gamma
    return (_clamp(red), _clamp(green), _clamp(blue))


def blend(a: RGB, b: RGB, fraction: float) -> RGB:
    """``a`` moved ``fraction`` of the way toward ``b``."""

    if fraction <= 0.0:
        return a
    if fraction >= 1.0:
        return b
    return (
        _clamp(a[0] + (b[0] - a[0]) * fraction),
        _clamp(a[1] + (b[1] - a[1]) * fraction),
        _clamp(a[2] + (b[2] - a[2]) * fraction),
    )


CHANNEL_ORDERS = ("RGB", "RBG", "GRB", "GBR", "BRG", "BGR")
_CHANNEL_INDEX = {"R": 0, "G": 1, "B": 2}


def apply_channel_order(colour: RGB, order: str) -> RGB:
    """Reorder our ``(r, g, b)`` into the order a zone needs to be sent in.

    **Not every zone comes out the same way, even on one controller.** OpenRGB swaps red and green on the ASRock X870E Taichi's
    zones whenever the board's own table or its settings say so, and on the five
    that carry LEDs the swap turns red into green. ``openrgb_rg_swap`` pins that
    swap on -- it cannot be pinned off -- and the five are sent **GRB**, which
    swaps them back. Verified by sending pure primaries and looking: red showed
    green, green showed red, blue showed blue.

    The option is per ZONE, not per device, because a controller's zones need
    not agree: if only IO Cover and PCB are swapped, a pink screen at
    ``(255, 0, 96)`` comes out ``(0, 255, 96)``, a vivid green-cyan, on those two
    zones while every other zone shows the correct pink. The two swaps must move together; see
    ``openrgb_rg_swap`` in ``config.py``.

    ``order`` names which of our channels drives each of the device's outputs,
    so ``"GRB"`` means "this device's first output is green".
    """

    if order == "RGB":
        return colour
    return (
        colour[_CHANNEL_INDEX[order[0]]],
        colour[_CHANNEL_INDEX[order[1]]],
        colour[_CHANNEL_INDEX[order[2]]],
    )


def cohere(near: RGB, far: RGB, cohesion: float) -> tuple[RGB, RGB]:
    """Pull the two group colours toward each other by ``cohesion``.

    The ambience engine deliberately gives the near and far groups *different*
    palette colours -- near carries the screen's strongest components, far its
    overall mix -- which is what makes a room feel lit rather than washed. On
    hardware it can read as a fault instead: the motherboard showing cyan while
    the fans show pink looks like a bug, not a design.

    0.0 keeps them fully distinct (the engine's intent), 1.0 makes them
    identical (one colour everywhere). In between trades some of the depth for
    coherence across devices, which is a matter of taste and therefore config.
    """

    if cohesion <= 0.0:
        return near, far
    midpoint = (
        (near[0] + far[0]) // 2,
        (near[1] + far[1]) // 2,
        (near[2] + far[2]) // 2,
    )
    return blend(near, midpoint, cohesion), blend(far, midpoint, cohesion)


def _spans(weights: list[float], count: int) -> list[int]:
    """Apportion ``count`` LEDs across weights, largest remainder, at least one each."""

    total = sum(weights)
    if total <= 0:
        return [count] + [0] * (len(weights) - 1)
    exact = [weight / total * count for weight in weights]
    spans = [max(1, int(value)) for value in exact]
    # max(1, ...) can overshoot when there are more colours than LEDs; trim from
    # the weakest end until it fits, so a 2-LED device still gets the two
    # strongest colours rather than a silent overrun.
    while sum(spans) > count and len(spans) > 1:
        spans.pop()
    remainder = count - sum(spans)
    if remainder > 0:
        order = sorted(range(len(spans)), key=lambda i: exact[i] - int(exact[i]), reverse=True)
        for index in range(remainder):
            spans[order[index % len(order)]] += 1
    return spans


def spread(
    palette: list[PaletteEntry],
    base: RGB,
    count: int,
    *,
    bias: float = 0.0,
    treatment: dict | None = None,
) -> list[RGB]:
    """``count`` colours forming a gradient through ``palette``, pulled toward ``base``.

    ``bias`` is how strongly every LED is dragged back to the group's own colour:
    ``1.0`` reproduces the old flat behaviour exactly, ``0.0`` is the raw palette
    gradient. It exists because the near/far split is a deliberate choice about
    which hardware lights the screen and which lights the room, and a gradient
    that ignored it would throw that away.

    The gradient is interpolated between span *centres* rather than painted as
    hard bands: four colours across 97 LEDs in blocks would look like a flag, not
    an ambience.
    """

    treatment = treatment or {}
    if count <= 0:
        return []
    usable = [(colour, weight) for colour, weight in palette if weight > 0]
    if not usable or bias >= 1.0:
        return [treat(base, **treatment)] * count
    if len(usable) == 1 or count == 1:
        only = blend(usable[0][0], base, bias)
        return [treat(only, **treatment)] * count

    colours = [colour for colour, _weight in usable]
    spans = _spans([weight for _colour, weight in usable], count)
    colours = colours[: len(spans)]

    # Centre of each colour's span, in LED coordinates.
    centres: list[float] = []
    cursor = 0.0
    for span in spans:
        centres.append(cursor + span / 2.0 - 0.5)
        cursor += span

    out: list[RGB] = []
    for index in range(count):
        if index <= centres[0]:
            colour = colours[0]
        elif index >= centres[-1]:
            colour = colours[-1]
        else:
            slot = 0
            while slot + 1 < len(centres) and centres[slot + 1] < index:
                slot += 1
            left, right = centres[slot], centres[slot + 1]
            fraction = 0.0 if right == left else (index - left) / (right - left)
            colour = blend(colours[slot], colours[slot + 1], fraction)
        out.append(treat(blend(colour, base, bias), **treatment))
    return out
