"""fx/palette.py - the tone ladder and status palette (spec G-01 / FD-4a, tokens §5.2).

Pure: no Tk, no I/O, no threads. Every tone is derived from theme.PHOSPHOR_PRESETS
(imported, never duplicated) plus the fixed surfaces and status colours in theme.py.

    tones(name, ink)   -> dict    pure; cached internally, every call returns a fresh copy
    snapshot(theme)    -> dict    tones(theme.phosphor_name, ink), stored as the current palette
    current()          -> dict    a copy of the last snapshot (amber/phosphor if none yet)
    status(kind)       -> dict    STATUS_SHAPES colours for one kind (from current(), or t=...)
    shape_for(kind)    -> str     the shape code for one kind
    assert_floor(t)    -> list    contrast / ordering / distance failures ([] when OK); never raises

Importable both as ``desktop.fx.palette`` (package-relative theme import) and as a
loose file (falls back to ``desktop.theme`` on sys.path, then to theme.py by path).
"""

import math
import os
import sys


# ---------------------------------------------------------------- theme source

def _load_theme_module():
    if __package__:
        try:
            from .. import theme as _t  # desktop.fx.palette -> desktop.theme
            return _t
        except (ImportError, ValueError):
            pass
    mod = sys.modules.get("desktop.theme")
    if mod is not None:
        return mod
    try:
        import desktop.theme as _t  # app/ on sys.path
        return _t
    except ImportError:
        pass
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "theme.py")
    spec = importlib.util.spec_from_file_location("_fx_palette_theme", path)
    _t = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_t)
    return _t


_theme = _load_theme_module()

# name -> (bright, dim); derived from theme.PHOSPHOR_PRESETS, same 5 presets.
PRESETS = {name: (p["bright"], p["dim"]) for name, p in _theme.PHOSPHOR_PRESETS.items()}
# Contract §2: "An unknown preset falls back to amber" - literally amber, independent of
# theme.DEFAULT_PHOSPHOR (which is also amber today but could change).
DEFAULT_PRESET = "amber" if "amber" in PRESETS else _theme.DEFAULT_PHOSPHOR

BG = _theme.SURFACE_BASE
PANEL = _theme.SURFACE_PANEL
PANEL_HOVER = _theme.SURFACE_PANEL_HOVER
OK = _theme.OK
WARN = _theme.WARN
DANGER = _theme.DANGER

# warn_eff is used when WARN would be indistinguishable from the preset's own signal
# colour (amber: WARN == bright). theme.WARN itself is unchanged (DD-12).
WARN_EFF_ALT = "#FF7A1A"

INKS = ("phosphor", "reference")

# A-2 instrument ink (opt-in REFERENCE): ref = mix(bg, hue, t) with
# contrast(ref, bg) = min(contrast(dim, bg), 3.0). The hues are design data; the
# resulting inks are the §5.2 table values and are pinned here (REF_INK) so the
# palette never depends on a bisection's last bit. reference_ink() reproduces them.
REF_HUE = {"amber": "#4F86A6", "green": "#A87A34", "cyan": "#B07A3A", "red": "#3E8E98", "white": "#4F86C8"}
REF_INK = {"amber": "#3B6177", "green": "#715224", "cyan": "#7A552A", "red": "#224549", "white": "#395E8B"}

# Contrast floors (§5.2 CONTRAST_FLOOR)
TEXT_FLOOR = 4.5      # text <= 10 pt
GRAPHIC_FLOOR = 3.0   # status / pending graphics vs their ground
WARN_DE_FLOOR = 25.0  # warn_eff vs bright and DANGER
REF_DE_STATUS_FLOOR = 40.0
REF_DE_DIM_FLOOR = 20.0
REF_CONTRAST_SLACK = 0.5


# ---------------------------------------------------------------- colour maths

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _rgb(h):
    # Strict: exactly 6 ASCII hex digits. int(x, 16) alone would also accept a sign,
    # surrounding whitespace, '_' separators and non-ASCII Unicode digits.
    if not isinstance(h, str):
        raise ValueError("colour must be a '#RRGGBB' string: %r" % (h,))
    s = h[1:] if h.startswith("#") else h
    if len(s) != 6 or not all(ch in _HEX_DIGITS for ch in s):
        raise ValueError("colour must be a '#RRGGBB' string: %r" % (h,))
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def _unit(t, what="t"):
    """float(t) clamped to 0..1; NaN is rejected (min/max would silently map it to an end)."""
    t = float(t)
    if math.isnan(t):
        raise ValueError("%s must be a number, got NaN" % what)
    return max(0.0, min(1.0, t))


def _hex(c):
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(round(v)))) for v in c)


def mix(a, b, t):
    """Linear sRGB-space blend a -> b by t (clamped to 0..1; NaN -> ValueError).
    Returns '#RRGGBB'."""
    t = _unit(t)
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(ra[i] + (rb[i] - ra[i]) * t for i in range(3)))


def _lin(v):
    v /= 255.0
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def luminance(h):
    """WCAG relative luminance."""
    r, g, b = (_lin(v) for v in _rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    """WCAG contrast ratio (>= 1.0)."""
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _lab(h):
    r, g, b = (_lin(v) for v in _rgb(h))
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t):
        return t ** (1.0 / 3.0) if t > 216.0 / 24389.0 else (24389.0 / 27.0 * t + 16.0) / 116.0

    fx, fy, fz = f(x), f(y), f(z)
    return 116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz)


def delta_e(a, b):
    """CIE76 colour distance (Euclidean in CIELAB, D65)."""
    return math.dist(_lab(a), _lab(b))


def sat_falloff(hex_colour, k):
    """Red-shifted phosphor bloom tone (CRT halo). The dominant channel(s) (>= 90 % of
    the max) scale by k; the others fall off faster (k**3 for the weakest, k**2.6 for
    the middle), so a halo reddens as it fades. Tied weak channels share the weakest
    exponent (equal inputs fall off equally). Floored at bg per channel. k is clamped
    to 0..1; NaN -> ValueError.
    sat_falloff('#FFB000', 0.38) == '#610E08'."""
    k = _unit(k, "k")
    c = _rgb(hex_colour)
    mx = max(c)
    mn = min(c)
    out = []
    for i in range(3):
        if c[i] >= 0.9 * mx:
            e = 1.0
        else:
            e = 3.0 if c[i] == mn else 2.6
        out.append(c[i] * (k ** e))
    floor = _rgb(BG)
    return _hex(tuple(max(floor[i], out[i]) for i in range(3)))


def reference_ink(hue, dim, ground=BG):
    """A-2 rule: the largest t with contrast(mix(ground, hue, t), ground) <=
    min(contrast(dim, ground), 3.0) (bisection). Returns the ink hex."""
    target = min(contrast(dim, ground), 3.0)
    if contrast(hue, ground) <= target:
        return _rgb_norm(hue)
    lo, hi = 0.0, 1.0
    for _ in range(40):
        mid = (lo + hi) / 2.0
        if contrast(mix(ground, hue, mid), ground) <= target:
            lo = mid
        else:
            hi = mid
    return mix(ground, hue, lo)


def _rgb_norm(h):
    return _hex(_rgb(h))


def _mix_half_up(a, b, t):
    """mix() with round-half-up channels. Only used for `hot`: the §5.2 table's hot
    values (#99FFB3, #99F3FF) were computed half-up, while every other table value
    (e.g. value amber #C48500 = 196.5 -> 196) matches mix()'s round-half-even. No
    single rounding rule reproduces the whole table (reported to the orchestrator)."""
    t = _unit(t)
    ra, rb = _rgb(a), _rgb(b)
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(math.floor(ra[i] + (rb[i] - ra[i]) * t + 0.5))))
                                   for i in range(3))


def _value_tier(bright, dim):
    """First mix(dim, bright, k/20), k = 10..20, reaching 4.5:1 on panel (else bright)."""
    for k in range(10, 21):
        cand = mix(dim, bright, k / 20.0)
        if contrast(cand, PANEL) >= TEXT_FLOOR:
            return cand
    return bright


# B1 derived tokens, ported from the approved renderers (see design archive:
# r2/shared_glyphs.derive_tokens and r3/r3_home.derive_tokens). Only the rule pair and
# the bloom / unlit mixes are ported: the red / green tier lift stays out (OI-9).
RULE_OUT_MIN = 3.3     # outer line of the double rule: at least amber's dim (3.34:1 on bg)
RULE_IN_MIN = 2.3      # inner line: at least amber's rule_in (2.35:1 on bg)
BLOOM_OUT_T = 0.15     # same-hue CRT bloom, outer
BLOOM_IN_T = 0.40      # same-hue CRT bloom, inner
UNLIT_T = 0.45         # unlit meter segments / shuttle cells (solid blocks, no outline)


def _lift(a, b, target, ground=BG):
    """The colour on the a -> b blend nearest to a whose contrast on ground reaches
    target (a itself when it already does). Approved renderers' _lift, 24 bisections."""
    if contrast(a, ground) >= target:
        return _rgb_norm(a)
    lo, hi = 0.0, 1.0
    for _ in range(24):
        mid = (lo + hi) / 2.0
        if contrast(mix(a, b, mid), ground) >= target:
            hi = mid
        else:
            lo = mid
    return mix(a, b, hi)


def _rule_pair(dim, value, ground=BG):
    """(rule_out, rule_in): rule_out = dim lifted toward value to >= 3.3:1 on bg;
    rule_in = rule_out at 75 % over bg, lifted toward rule_out to >= 2.3:1."""
    rule_out = _lift(dim, value, RULE_OUT_MIN, ground)
    rule_in = _lift(mix(ground, rule_out, 0.75), rule_out, RULE_IN_MIN, ground)
    return rule_out, rule_in


# ---------------------------------------------------------------- tones

_TONES_CACHE = {}


def _norm_name(name):
    if isinstance(name, str) and name in PRESETS:
        return name
    return DEFAULT_PRESET


def _norm_ink(ink):
    if isinstance(ink, str) and ink.strip().lower() == "reference":
        return "reference"
    return "phosphor"


def _compute(name, ink):
    bright, dim = PRESETS[name]
    ghost = mix(BG, dim, 0.40)
    if ink == "reference" and name in REF_INK:
        ref = REF_INK[name]
        ref_ghost = mix(BG, ref, 0.5)
    elif ink == "reference" and name in REF_HUE:
        ref = reference_ink(REF_HUE[name], dim)
        ref_ghost = mix(BG, ref, 0.5)
    else:
        # PHOSPHOR (default), or a preset with no reference hue: pure mono.
        ink = "phosphor"
        ref, ref_ghost = dim, ghost
    value = _value_tier(bright, dim)
    rule_out, rule_in = _rule_pair(dim, value)
    return {
        "preset": name,
        "ink": ink,
        "bg": BG,
        "panel": PANEL,
        "panel_hover": PANEL_HOVER,
        "bright": bright,
        "dim": dim,
        "value": value,
        "hot": _mix_half_up(bright, "#FFFFFF", 0.5),
        "ghost": ghost,
        "grid": mix(BG, dim, 0.22),
        "halo_in": sat_falloff(bright, 0.38),
        "halo_out": sat_falloff(bright, 0.20),
        "warn_eff": WARN_EFF_ALT if _rgb(WARN) == _rgb(bright) else WARN,
        "ok": OK,
        "danger": DANGER,
        "hazard": DANGER,
        "ref": ref,
        "ref_ghost": ref_ghost,
        # B1 (approved derive_tokens, no tier lift)
        "bloom_out": mix(BG, bright, BLOOM_OUT_T),
        "bloom_in": mix(BG, bright, BLOOM_IN_T),
        "unlit": mix(BG, dim, UNLIT_T),
        "rule_out": rule_out,
        "rule_in": rule_in,
    }


def tones(phosphor_name, ink="phosphor"):
    """Every tone for one preset. Pure. Unknown / non-string preset -> amber;
    unknown ink -> phosphor. Returns a fresh dict each call."""
    key = (_norm_name(phosphor_name), _norm_ink(ink))
    t = _TONES_CACHE.get(key)
    if t is None:
        t = _compute(*key)
        _TONES_CACHE[key] = t
    return dict(t)


_current = None


def snapshot(theme, ink="phosphor"):
    """Store tones(theme.phosphor_name, ink) as the current palette and return a copy.
    The stored dict is independent of the theme object: later mutations of the theme
    (set_phosphor etc.) do not change current(). A missing, unknown or unhashable
    phosphor_name falls back to amber; an exception raised by the theme object's own
    attribute access propagates (fx.install turns that into NullFX)."""
    global _current
    name = getattr(theme, "phosphor_name", None)
    t = tones(name, ink)
    _current = t
    return dict(t)


def set_ink(ink):
    """Recompute current() for a live instrument-ink change, keeping the snapshotted
    preset (never re-reads the theme object). Returns a copy."""
    global _current
    base = _current["preset"] if _current is not None else DEFAULT_PRESET
    _current = tones(base, ink)
    return dict(_current)


def current():
    """A copy of the last snapshot; amber/phosphor tones if none was taken yet."""
    t = _current
    if t is None:
        return tones(DEFAULT_PRESET, "phosphor")
    return dict(t)


# ---------------------------------------------------------------- status shapes

# kind -> shape code (§5.2 STATUS_SHAPES). Status is never colour-only: in the red
# preset DANGER == bright, so 'on' and 'fault' differ by shape.
SHAPES = {
    "on": "filled",                 # filled bright + bg text
    "off": "outline",               # 1 px value outline + value text
    "pending": "outline_word",      # off + PENDING
    "busy": "outline_cursor",       # off + LOADING + 5x10 value cursor block
    "fault": "double_tips",         # DANGER outline + inner outline (3 px inset) + filled tips + DANGER text
    "error": "hazard_tab_double",   # alert: hazard tab + double outline (2 px + 1 px inset 4)
    "warning": "chevron_outline",   # alert: solid warn_eff chevron + 2 px outline
    "success": "plain_outline",     # alert: plain 1 px outline
    "redaction": "redaction",       # dim fill + 1 px value outline + PENDING
}
STATUS_KINDS = tuple(SHAPES)

# colour-bearing keys of a status() dict that are graphics (>= 3:1 vs their ground)
GRAPHIC_KEYS = ("fill", "stroke", "inner", "tips", "cursor", "chevron", "tab")


def shape_for(kind):
    """Shape code for a status kind; unknown kinds read as 'off' (outline)."""
    return SHAPES.get(kind, SHAPES["off"]) if isinstance(kind, str) else SHAPES["off"]


def status(kind, t=None):
    """Colours + geometry hints for one status kind, from current() (or tones t).

    Keys: kind, shape, ground (the colour it is drawn on), fill, stroke, stroke_w,
    inner, inner_inset, inner_w, tips, cursor, cursor_size, chevron, tab, tab_ground,
    text, text_on (the colour the text sits on), word, decor_fill (exempt), size.
    Unused keys are None. Unknown kind -> the 'off' recipe (with kind preserved)."""
    if t is None:
        t = current()
    k = kind if (isinstance(kind, str) and kind in SHAPES) else "off"
    bg, panel = t["bg"], t["panel"]
    d = {
        "kind": kind, "shape": SHAPES[k], "ground": bg,
        "fill": None, "stroke": None, "stroke_w": 1,
        "inner": None, "inner_inset": 0, "inner_w": 0,
        "tips": None, "cursor": None, "cursor_size": None,
        "chevron": None, "tab": None, "tab_ground": None,
        "text": None, "text_on": bg, "word": None,
        "decor_fill": None, "size": None,
    }
    if k == "on":
        d.update(fill=t["bright"], stroke=t["bright"], text=bg, text_on=t["bright"])
    elif k in ("off", "pending", "busy"):
        d.update(stroke=t["value"], text=t["value"])
        if k == "pending":
            d["word"] = "PENDING"
        elif k == "busy":
            d.update(word="LOADING", cursor=t["value"], cursor_size=(5, 10))
    elif k == "fault":
        d.update(stroke=t["danger"], inner=t["danger"], inner_inset=3, inner_w=1,
                 tips=t["danger"], text=t["danger"], word="ERROR")
    elif k == "error":
        d.update(ground=panel, text_on=panel, stroke=t["danger"], stroke_w=2,
                 inner=t["danger"], inner_inset=4, inner_w=1,
                 tab=t["hazard"], tab_ground=bg, text=t["danger"], word="ERROR")
    elif k == "warning":
        d.update(ground=panel, text_on=panel, stroke=t["warn_eff"], stroke_w=2,
                 chevron=t["warn_eff"], text=t["warn_eff"], word="WARNING")
    elif k == "success":
        d.update(ground=panel, text_on=panel, stroke=t["ok"], stroke_w=1,
                 text=t["ok"], word="DONE")
    elif k == "redaction":
        d.update(ground=panel, text_on=panel, stroke=t["value"], stroke_w=1,
                 decor_fill=t["dim"], text=t["value"], word="PENDING", size=(56, 10))
    return d


# ---------------------------------------------------------------- floor asserts

REQUIRED_KEYS = ("preset", "bg", "panel", "panel_hover", "bright", "dim", "value", "hot", "ghost",
                 "grid", "halo_in", "halo_out", "warn_eff", "ok", "danger", "hazard", "ref", "ref_ghost")


def assert_floor(t):
    """Every §5.2 floor for one tones dict. Returns a list of failure strings ([] = OK).
    Never raises (malformed input is reported as a failure)."""
    try:
        return _assert_floor(t)
    except Exception as e:  # last line of defence (hostile dict subclass etc.)
        return ["assert_floor: malformed tones (%s)" % _safe_label(type(e).__name__)]


def _assert_floor(t):
    fails = []
    if not isinstance(t, dict):
        return ["tones is not a dict: %r" % (type(t).__name__,)]
    missing = [k for k in REQUIRED_KEYS if k not in t]
    if missing:
        return ["missing keys: %s" % ", ".join(missing)]
    p = _safe_label(t.get("preset"))

    def chk(label, cond):
        if not cond:
            fails.append("%s: %s" % (p, label))

    try:
        lum = {k: luminance(t[k]) for k in ("grid", "ghost", "dim", "value", "bright")}
        chk("ladder grid<ghost<dim<value<bright",
            lum["grid"] < lum["ghost"] < lum["dim"] < lum["value"] < lum["bright"])
        for ground in ("panel", "bg"):
            c = contrast(t["value"], t[ground])
            chk("value on %s %.2f < %.1f" % (ground, c, TEXT_FLOOR), c >= TEXT_FLOOR)
        c = contrast(t["warn_eff"], t["panel"])
        chk("warn_eff on panel %.2f < %.1f" % (c, TEXT_FLOOR), c >= TEXT_FLOOR)
        for ground in ("bg", "panel"):
            c = contrast(t["hazard"], t[ground])
            chk("hazard on %s %.2f < %.1f" % (ground, c, GRAPHIC_FLOOR), c >= GRAPHIC_FLOOR)
        for other in ("bright", "danger"):
            de = delta_e(t["warn_eff"], t[other])
            chk("warn_eff dE vs %s %.1f < %.0f" % (other, de, WARN_DE_FLOOR), de >= WARN_DE_FLOOR)
        for kind in STATUS_KINDS:
            s = status(kind, t)
            for key in GRAPHIC_KEYS:
                col = s.get(key)
                if col is None:
                    continue
                ground = s["tab_ground"] if key == "tab" else s["ground"]
                c = contrast(col, ground)
                chk("status %s.%s %s on %s %.2f < %.1f" % (kind, key, col, ground, c, GRAPHIC_FLOOR),
                    c >= GRAPHIC_FLOOR)
                if key == "tab":  # the tab also sits against the plate ground
                    c = contrast(col, s["ground"])
                    chk("status %s.tab on %s %.2f < %.1f" % (kind, s["ground"], c, GRAPHIC_FLOOR),
                        c >= GRAPHIC_FLOOR)
            if s.get("text") is not None:
                c = contrast(s["text"], s["text_on"])
                chk("status %s.text %s on %s %.2f < %.1f" % (kind, s["text"], s["text_on"], c, TEXT_FLOOR),
                    c >= TEXT_FLOOR)
        if t.get("ink") == "reference" or (t["ref"] != t["dim"]):
            rc, dc = contrast(t["ref"], t["bg"]), contrast(t["dim"], t["bg"])
            chk("ref on bg %.2f > dim %.2f + %.1f" % (rc, dc, REF_CONTRAST_SLACK), rc <= dc + REF_CONTRAST_SLACK)
            for label, col in (("WARN", WARN), ("warn_eff", t["warn_eff"]), ("OK", t["ok"]), ("DANGER", t["danger"])):
                de = delta_e(t["ref"], col)
                chk("ref dE vs %s %.1f < %.0f" % (label, de, REF_DE_STATUS_FLOOR), de >= REF_DE_STATUS_FLOOR)
            de = delta_e(t["ref"], t["dim"])
            chk("ref dE vs dim %.1f < %.0f" % (de, REF_DE_DIM_FLOOR), de >= REF_DE_DIM_FLOOR)
    except Exception as e:  # malformed colour etc.
        fails.append("%s: malformed tones (%s: %s)" % (p, type(e).__name__, _safe_label(e)))
    return fails


def _safe_label(x):
    """str(x) for a failure message; never raises (a hostile __str__ must not escape
    assert_floor)."""
    if type(x) is str:
        return x
    try:
        return str(x)
    except Exception:
        try:
            return "<unprintable %s>" % type(x).__name__
        except Exception:
            return "<unprintable>"
