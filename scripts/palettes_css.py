"""Generates web/app/palettes.css from web/lib/palettes.json: one colour theme per bookmaker palette (primary, secondary,
accent), for the dark and the light mode.

Per mode the generator keeps the brand's hue and only moves lightness (OKLCH) where contrast asks for it:
  - surfaces (background, cards, lines) take a faint tint of the primary colour (none when the primary is black/grey);
  - --accent (active navigation, links, buttons, focus marks): in light mode the primary colour, darkened until it reads
    at 4.5:1 on the cards (black stays black); in dark mode the first coloured brand colour (primary, secondary, accent)
    that reaches 4.5:1 with little lightening (Lottomatica's light blue, Sisal's lime), else the primary lightened;
  - --accent-ink is white or near-black, whichever reads better on the accent;
  - --brand-1/2/3 are the three colours as given (the stripe under the logo).
Text, the outcome colours (good / warn / bad) and the chart series are not touched: they carry meaning.

Run after editing palettes.json:  python scripts/palettes_css.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "web" / "lib" / "palettes.json"
OUT = ROOT / "web" / "app" / "palettes.css"
MIN_CONTRAST = 4.5
MAX_LIFT = 0.1  # dark mode: a brand colour lightened more than this looks washed out, the next brand colour is used instead


# ---------------------------------------------------------------- colour maths (sRGB <-> OKLab / OKLCH)
def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _gam(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def hex_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def rgb_hex(rgb) -> str:
    return "#" + "".join(f"{round(min(1, max(0, c)) * 255):02x}" for c in rgb)


def to_oklch(h: str) -> tuple[float, float, float]:
    r, g, b = (_lin(c) for c in hex_rgb(h))
    l_ = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m_ = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s_ = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    L = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    bb = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    return L, math.hypot(a, bb), math.degrees(math.atan2(bb, a)) % 360


def from_oklch(L: float, C: float, H: float) -> str:
    """OKLCH -> hex, chroma reduced until the colour fits in sRGB."""
    for _ in range(40):
        a, b = C * math.cos(math.radians(H)), C * math.sin(math.radians(H))
        l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
        m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
        s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
        rgb = (4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
               -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
               -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_)
        if all(-1e-4 <= c <= 1 + 1e-4 for c in rgb):
            return rgb_hex(tuple(_gam(min(1, max(0, c))) for c in rgb))
        C *= 0.93
    return rgb_hex(tuple(_gam(min(1, max(0, c))) for c in rgb))


def luminance(h: str) -> float:
    r, g, b = (_lin(c) for c in hex_rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def neutral(h: str) -> bool:
    return to_oklch(h)[1] < 0.04


def readable(h: str, bg: str, dark: bool) -> str:
    """The same hue, lightness moved (up on dark, down on light) until it reads at MIN_CONTRAST on bg."""
    L, C, H = to_oklch(h)
    out = h
    for _ in range(100):
        if contrast(out, bg) >= MIN_CONTRAST:
            return out
        L = min(0.97, L + 0.01) if dark else max(0.05, L - 0.01)
        out = from_oklch(L, C, H)
    return out


def alpha(h: str, a: float) -> str:
    r, g, b = (round(c * 255) for c in hex_rgb(h))
    return f"rgba({r}, {g}, {b}, {a})"


# ---------------------------------------------------------------- one palette, one mode
def tokens(colors: list[str], dark: bool) -> dict[str, str]:
    primary = colors[0]
    tint = next((c for c in colors if not neutral(c)), None) if neutral(primary) else primary
    H = to_oklch(tint)[2] if tint else 0.0
    c_surf = 0.0 if neutral(primary) else (0.02 if dark else 0.008)
    if dark:
        surf = {"--bg": 0.135, "--bg-2": 0.155, "--card": 0.175, "--card-2": 0.2, "--line": 0.27, "--line-strong": 0.35, "--track": 0.25}
    else:
        surf = {"--bg": 0.975, "--bg-2": 0.955, "--card-2": 0.985, "--line": 0.915, "--line-strong": 0.845, "--track": 0.93}
    out = {k: from_oklch(L, c_surf * (1.4 if "line" in k else 1), H) for k, L in surf.items()}
    if not dark:
        out["--card"] = "#ffffff"
    card = out["--card"]
    if not dark:  # the primary, darkened if needed (black stays black)
        acc = readable(primary, card, dark)
    else:  # the first brand colour that reads on the dark cards with little lightening, else the primary lightened
        coloured = [c for c in colors if not neutral(c)] or ["#ffffff"]
        near = [c for c in coloured if to_oklch(readable(c, card, dark))[0] - to_oklch(c)[0] <= MAX_LIFT]
        acc = readable(near[0] if near else coloured[0], card, dark)
    L, C, Hh = to_oklch(acc)
    strong = from_oklch(L - 0.06 if dark else L - 0.05, C, Hh)
    ink = "#ffffff" if contrast("#ffffff", acc) >= contrast("#0b0f12", acc) else "#0b0f12"
    out.update({"--accent": acc, "--accent-strong": strong, "--accent-ink": ink, "--accent-soft": alpha(acc, 0.14 if dark else 0.1)})
    out.update({f"--brand-{i + 1}": c.lower() for i, c in enumerate(colors)})
    return out


def css() -> str:
    pals = json.loads(SRC.read_text(encoding="utf-8"))
    lines = ["/* Generated by scripts/palettes_css.py from web/lib/palettes.json: do not edit by hand. */"]
    for p in pals:
        for dark in (True, False):
            sel = f':root[data-palette="{p["id"]}"]' + (':not([data-theme="light"])' if dark else '[data-theme="light"]')
            t = tokens(p["colors"], dark)
            lines.append(f"{sel} {{")
            lines += [f"  {k}: {v};" for k, v in t.items()]
            lines.append("}")
    return "\n".join(lines) + "\n"


def report() -> list[str]:
    """Contrast of each accent on its card and of the ink on the accent (all must be >= 4.5)."""
    out = []
    for p in json.loads(SRC.read_text(encoding="utf-8")):
        for dark in (True, False):
            t = tokens(p["colors"], dark)
            out.append(f"{p['name']:<12} {'scuro ' if dark else 'chiaro'} accent {t['--accent']} su card {contrast(t['--accent'], t['--card']):4.1f}, "
                       f"testo sull'accent {contrast(t['--accent-ink'], t['--accent']):4.1f}")
    return out


if __name__ == "__main__":
    OUT.write_text(css(), encoding="utf-8")
    print("\n".join(report()))
    print(f"scritto {OUT}")
