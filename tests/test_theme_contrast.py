"""Theme contrast gate for opencode_deck/static/index.html (stdlib only).

Parses the six :root[data-theme="..."] blocks plus the :root fallback and
enforces WCAG 2.1 contrast ratios and CIELAB (CIE76) palette separation:

  1. --fg, --dim, --accent, --accent2, --warn, --err: >= 4.5:1 on --bg, --panel, --panel2
  2. --c0..--c11: >= 3:1 on --bg and --panel (non-text fills)
  3. --line on --panel: >= 3:1 (card borders visible)
  4. heatmap minimum-intensity cell (--c0 at --heat-alpha-min over --panel2) vs --panel2: >= 3:1
  5. CIE76 dE(c_i, c_j) >= 15 for every pair of c0-c3 (chart series stay distinct)
  6. structure: all six theme blocks define all 24 vars; --c0..--c4 are a logical
     copy of accent/accent2/purple/warn/err; the :root fallback mirrors dark.

If a theme fails, tune THAT theme's palette (or its --heat-alpha-min) until it
passes. Never lower these thresholds.
"""

import math
import os
import re
import unittest

CSS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "opencode_deck", "static", "index.html",
)

THEMES = ["dark", "light", "nord", "dracula", "everforest", "everforest-light"]
UI_VARS = ["bg", "panel", "panel2", "line", "fg", "dim",
           "accent", "accent2", "warn", "err", "purple"]
C_VARS = ["c" + str(i) for i in range(12)]
HEAT_VAR = "heat-alpha-min"
ALL_VARS = UI_VARS + C_VARS + [HEAT_VAR]

# --- thresholds: values, not vibes. Do not lower. ---
TEXT_MIN = 4.5        # WCAG 1.4.3 normal text
NONTEXT_MIN = 3.0     # WCAG 1.4.11 graphical objects
HEAT_MIN = 3.0        # faintest heat cell vs its panel
DE_MIN = 15.0         # CIE76 separation for the four stacked series


# ---------- color math (WCAG 2.1 / CIE, stdlib only) ----------

def hex_to_rgb(h):
    h = h.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    assert len(h) == 6, "bad hex: %r" % h
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _lin(c8):
    c = c8 / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb):
    r, g, b = rgb
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(rgb1, rgb2):
    l1, l2 = luminance(rgb1), luminance(rgb2)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def blend(fg_rgb, alpha, bg_rgb):
    """Alpha compositing in sRGB space (what the browser does for background colors)."""
    return tuple(round(a * alpha + b * (1 - alpha)) for a, b in zip(fg_rgb, bg_rgb))


def _rgb_to_xyz(rgb):
    r, g, b = (_lin(c) for c in rgb)
    return (
        r * 0.4124 + g * 0.3576 + b * 0.1805,
        r * 0.2126 + g * 0.7152 + b * 0.0722,
        r * 0.0193 + g * 0.1192 + b * 0.9505,
    )


def rgb_to_lab(rgb):
    # D65 reference white
    X, Y, Z = _rgb_to_xyz(rgb)
    Xr, Yr, Zr = (v / t for v, t in ((X, 0.95047), (Y, 1.0), (Z, 1.08883)))

    def f(v):
        return v ** (1 / 3) if v > 0.008856 else 7.787 * v + 16 / 116

    fx, fy, fz = f(Xr), f(Yr), f(Zr)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def de76(rgb1, rgb2):
    L1, a1, b1 = rgb_to_lab(rgb1)
    L2, a2, b2 = rgb_to_lab(rgb2)
    return math.sqrt((L1 - L2) ** 2 + (a1 - a2) ** 2 + (b1 - b2) ** 2)


# ---------- CSS parsing ----------

def parse_css():
    with open(CSS_PATH, encoding="utf-8") as f:
        css = f.read()
    blocks = {}
    for m in re.finditer(
        r':root\[data-theme="([a-z-]+)"\]\s*\{([^}]*)\}', css
    ):
        tid, body = m.group(1), m.group(2)
        blocks[tid] = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body))
    root_m = re.search(r":root\s*\{([^}]*)\}", css)
    assert root_m, "no :root fallback block"
    fallback = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", root_m.group(1)))
    return blocks, fallback


def theme_rgb(vals):
    """'--name' -> 'value' dict -> {name: rgb tuple}, heat -> float."""
    out = {}
    for var in ALL_VARS:
        val = vals["--" + var].strip()
        out[var] = float(val) if var == HEAT_VAR else hex_to_rgb(val)
    return out


class TestThemeStructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blocks, cls.fallback = parse_css()

    def test_six_themes_present(self):
        missing = [t for t in THEMES if t not in self.blocks]
        self.assertFalse(missing, "missing theme blocks: %s" % missing)

    def test_all_themes_define_all_vars(self):
        for tid in THEMES:
            for var in ALL_VARS:
                self.assertIn("--" + var, self.blocks[tid],
                              "%s misses --%s" % (tid, var))
            self.assertTrue(
                0.0 < float(self.blocks[tid]["--" + HEAT_VAR].strip()) <= 1.0,
                "%s heat-alpha-min out of range" % tid,
            )

    def test_fallback_mirrors_dark(self):
        for var in ALL_VARS:
            self.assertEqual(self.fallback["--" + var].strip(),
                             self.blocks["dark"]["--" + var].strip(),
                             ":root fallback --%s != dark block" % var)

    def test_c_slots_copy_accent_family(self):
        for tid in THEMES:
            b = self.blocks[tid]
            for slot, ui in (("c0", "accent"), ("c1", "accent2"),
                             ("c2", "purple"), ("c3", "warn"), ("c4", "err")):
                self.assertEqual(b["--" + slot].strip(), b["--" + ui].strip(),
                                 "%s: --%s != --%s" % (tid, slot, ui))


class TestThemeContrast(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blocks, _ = parse_css()
        cls.tints = {t: theme_rgb(b) for t, b in cls.blocks.items()}

    def _fail_msg(self, tid, label, ratio, minimum):
        return "theme %s: %s = %.2f:1 (need %.1f:1)" % (tid, label, ratio, minimum)

    def test_text_contrast(self):
        # gates apply to the six text-family colors on the three surfaces;
        # bg/panel/panel2 are the surfaces themselves, line has its own gate
        for tid, c in self.tints.items():
            for var in ("fg", "dim", "accent", "accent2", "warn", "err"):
                for bg in ("bg", "panel", "panel2"):
                    r = contrast(c[var], c[bg])
                    self.assertGreaterEqual(
                        r, TEXT_MIN,
                        self._fail_msg(tid, "--%s on --%s" % (var, bg), r, TEXT_MIN))

    def test_fill_contrast(self):
        for tid, c in self.tints.items():
            for var in C_VARS:
                for bg in ("bg", "panel"):
                    r = contrast(c[var], c[bg])
                    self.assertGreaterEqual(
                        r, NONTEXT_MIN,
                        self._fail_msg(tid, "--%s on --%s" % (var, bg), r,
                                      NONTEXT_MIN))

    def test_border_contrast(self):
        for tid, c in self.tints.items():
            r = contrast(c["line"], c["panel"])
            self.assertGreaterEqual(
                r, NONTEXT_MIN,
                self._fail_msg(tid, "--line on --panel", r, NONTEXT_MIN))

    def test_heatmap_floor(self):
        for tid, c in self.tints.items():
            a = c[HEAT_VAR]
            cell = blend(c["c0"], a, c["panel2"])
            r = contrast(cell, c["panel2"])
            self.assertGreaterEqual(
                r, HEAT_MIN,
                self._fail_msg(tid, "--c0@%.2f over --panel2" % a, r, HEAT_MIN))

    def test_palette_distinction(self):
        for tid, c in self.tints.items():
            for i in range(4):
                for j in range(i + 1, 4):
                    d = de76(c["c%d" % i], c["c%d" % j])
                    self.assertGreaterEqual(
                        d, DE_MIN,
                        "theme %s: c%d vs c%d dE76=%.1f (need %.0f)"
                        % (tid, i, j, d, DE_MIN),
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
