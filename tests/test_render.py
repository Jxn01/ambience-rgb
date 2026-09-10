"""The colour treatment and the gradient, both pure."""

from __future__ import annotations

import colorsys
import unittest

from linux_rgb.render import blend, cohere, spread, treat


def saturation_of(colour):
    return colorsys.rgb_to_hsv(*(c / 255 for c in colour))[1]


def value_of(colour):
    return colorsys.rgb_to_hsv(*(c / 255 for c in colour))[2]


class TreatTest(unittest.TestCase):
    """🚨 Why this exists: a bulb and an LED showing the same numbers do not look
    the same. Pale shades that read as tinted on a diffuse lamp read as plain
    white on a point-source strip at full output."""

    def test_defaults_change_nothing(self) -> None:
        for colour in ((0, 0, 0), (255, 255, 255), (12, 200, 90)):
            self.assertEqual(treat(colour), colour)

    def test_saturation_pushes_a_pale_colour_away_from_grey(self) -> None:
        pale = (220, 235, 255)
        self.assertGreater(saturation_of(treat(pale, saturation=1.6)), saturation_of(pale))

    def test_gamma_above_one_raises_saturation(self) -> None:
        pale = (240, 255, 240)
        out = treat(pale, gamma=2.0)
        self.assertGreater(saturation_of(out), saturation_of(pale))
        self.assertEqual(out, (226, 255, 226))

    def test_gamma_does_NOT_dim_a_colour_already_at_peak(self) -> None:
        """⚠️ 1.0 ** gamma == 1.0. Gamma tints a blown-out frame, it does not
        calm one -- worth knowing before reaching for it to fix brightness."""

        pale = (240, 255, 240)
        self.assertEqual(value_of(treat(pale, gamma=2.0)), value_of(pale))

    def test_gamma_does_darken_anything_below_full(self) -> None:
        dim = (120, 100, 80)
        self.assertLess(value_of(treat(dim, gamma=2.0)), value_of(dim))

    def test_black_survives_everything(self) -> None:
        """"Off" is a colour this project writes constantly."""

        self.assertEqual(treat((0, 0, 0), saturation=2.5, gamma=2.2), (0, 0, 0))

    def test_grey_stays_grey_under_saturation(self) -> None:
        """There is no hue to push away from the mean."""

        self.assertEqual(treat((128, 128, 128), saturation=3.0), (128, 128, 128))

    def test_channels_never_leave_range(self) -> None:
        for colour in ((255, 0, 0), (1, 254, 128), (0, 0, 1), (250, 250, 200)):
            for channel in treat(colour, saturation=4.0, gamma=0.4):
                self.assertGreaterEqual(channel, 0)
                self.assertLessEqual(channel, 255)


class SpreadTest(unittest.TestCase):
    """🚨 The whole point of payload v2: 97 LEDs showing one averaged colour is
    a waste of 97 LEDs."""

    PALETTE = [((255, 0, 0), 0.5), ((0, 0, 255), 0.3), ((0, 255, 0), 0.2)]

    def test_bias_one_reproduces_the_old_flat_behaviour_exactly(self) -> None:
        """The safety net: turning the feature off must be indistinguishable."""

        out = spread(self.PALETTE, (7, 8, 9), 20, bias=1.0)
        self.assertEqual(out, [(7, 8, 9)] * 20)

    def test_no_palette_falls_back_to_the_group_colour(self) -> None:
        self.assertEqual(spread([], (7, 8, 9), 5), [(7, 8, 9)] * 5)

    def test_zero_weights_fall_back_too(self) -> None:
        self.assertEqual(spread([((1, 2, 3), 0.0)], (7, 8, 9), 4), [(7, 8, 9)] * 4)

    def test_it_returns_exactly_the_led_count_asked_for(self) -> None:
        """An overrun here writes into the next zone's slots, silently."""

        for count in (1, 2, 3, 8, 29, 97, 120):
            self.assertEqual(len(spread(self.PALETTE, (0, 0, 0), count)), count)

    def test_a_count_of_zero_is_empty_not_an_error(self) -> None:
        self.assertEqual(spread(self.PALETTE, (1, 1, 1), 0), [])

    def test_more_colours_than_leds_still_fits(self) -> None:
        big = [((i, i, i), 0.25) for i in (10, 20, 30, 40)]
        self.assertEqual(len(spread(big, (0, 0, 0), 2)), 2)

    def test_the_gradient_actually_varies(self) -> None:
        out = spread(self.PALETTE, (0, 0, 0), 30, bias=0.0)
        self.assertGreater(len(set(out)), 3, "a gradient with three distinct values is bands")

    def test_it_is_smooth_rather_than_banded(self) -> None:
        """Four colours across 97 LEDs in blocks looks like a flag."""

        out = spread(self.PALETTE, (0, 0, 0), 60, bias=0.0)
        jumps = [
            max(abs(a - b) for a, b in zip(prev, nxt))
            for prev, nxt in zip(out, out[1:])
        ]
        self.assertLess(max(jumps), 40, "adjacent LEDs should not lurch")

    def test_the_strongest_colour_dominates(self) -> None:
        """Spans are apportioned by weight, so a 50% colour takes ~half the strip."""

        out = spread([((255, 0, 0), 0.9), ((0, 0, 255), 0.1)], (0, 0, 0), 40, bias=0.0)
        reddish = sum(1 for r, _g, b in out if r > b)
        self.assertGreater(reddish, len(out) // 2)

    def test_bias_pulls_toward_the_group_colour(self) -> None:
        """near/far is a deliberate choice about which hardware lights what."""

        far = spread(self.PALETTE, (0, 0, 0), 20, bias=0.9)
        near = spread(self.PALETTE, (0, 0, 0), 20, bias=0.1)
        self.assertLess(sum(sum(c) for c in far), sum(sum(c) for c in near))

    def test_treatment_is_applied_to_every_led(self) -> None:
        out = spread(self.PALETTE, (0, 0, 0), 10, bias=0.0, treatment={"gamma": 2.0})
        plain = spread(self.PALETTE, (0, 0, 0), 10, bias=0.0)
        self.assertNotEqual(out, plain)

    def test_black_base_at_full_bias_stays_black(self) -> None:
        """Every exit path writes black; the gradient must not resurrect it."""

        self.assertEqual(spread(self.PALETTE, (0, 0, 0), 12, bias=1.0), [(0, 0, 0)] * 12)


class BlendTest(unittest.TestCase):
    def test_endpoints(self) -> None:
        self.assertEqual(blend((0, 0, 0), (255, 255, 255), 0.0), (0, 0, 0))
        self.assertEqual(blend((0, 0, 0), (255, 255, 255), 1.0), (255, 255, 255))

    def test_midpoint(self) -> None:
        self.assertEqual(blend((0, 0, 0), (200, 100, 50), 0.5), (100, 50, 25))

    def test_out_of_range_fractions_are_clamped(self) -> None:
        self.assertEqual(blend((10, 10, 10), (20, 20, 20), -1.0), (10, 10, 10))
        self.assertEqual(blend((10, 10, 10), (20, 20, 20), 5.0), (20, 20, 20))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class CohereTest(unittest.TestCase):
    """Near and far are *meant* to differ -- but not so much it looks broken.

    The engine gives near the screen's strongest components and far its overall
    mix, which is what makes a room feel lit. On hardware, the motherboard
    showing cyan while the fans show pink reads as a fault rather than a design.
    """

    def test_zero_leaves_the_engine_s_intent_alone(self) -> None:
        near, far = (0, 255, 255), (255, 0, 255)
        self.assertEqual(cohere(near, far, 0.0), (near, far))

    def test_one_makes_them_identical(self) -> None:
        near, far = cohere((0, 255, 255), (255, 0, 255), 1.0)
        self.assertEqual(near, far)

    def test_partial_narrows_the_gap_without_closing_it(self) -> None:
        near, far = cohere((0, 255, 255), (255, 0, 255), 0.6)
        self.assertNotEqual(near, far)
        gap = sum(abs(a - b) for a, b in zip(near, far))
        self.assertLess(gap, sum(abs(a - b) for a, b in zip((0, 255, 255), (255, 0, 255))))

    def test_black_stays_black(self) -> None:
        self.assertEqual(cohere((0, 0, 0), (0, 0, 0), 1.0), ((0, 0, 0), (0, 0, 0)))

    def test_identical_inputs_are_unchanged(self) -> None:
        self.assertEqual(cohere((7, 8, 9), (7, 8, 9), 0.5), ((7, 8, 9), (7, 8, 9)))
