"""fx/draw.py - canvas painters, the FxCanvas base, CRT and CrtGlass (G-04, G-14).
(B15 removed the banned P2 painters - brackets, registration cross, hazard, chevron, alert plate, status flag,
ticks / caret, redaction, display word, contour emblem, preview card, the one-canvas Backdrop and its scan3
stipple - with the user's yes; see design archive, backups/B15del_*.)

Rules every painter here keeps
------------------------------
* Tones are EXPLICIT: every painter takes the tones dict ``t`` (palette.tones(...) /
  FxCanvas.tones()) and, where CRT matters, an explicit ``crt`` level (0 OFF, 1 LOW,
  2 FULL). No painter reads palette.current(), prefs or any other global.
* Static: this module never schedules a Tk timer (only fx/clock.py may
  schedule). Animated callers drive painters through the clock.
* Every item a painter creates carries ``tag`` (str or tuple; default "fx") plus a
  kind tag ("plate", "bloom", "rule", "rect", "seg", "scan", "mark", "word", "crt"),
  so a redraw is ``canvas.delete(tag)`` + paint again.
* Three registers (§2 rule 1): structure in dim 1 px, signal in bright, status in
  ok / warn_eff / danger, always paired with a shape. Apparatus strokes (crosses,
  rulers, emblem limb, crosshair, dimension lines) use the instrument ink t['ref'] /
  t['ref_ghost'] - never text, fills, status or interactive borders (G-02).
* Text <= 10 pt uses the value tier or brighter; dim is never small text. Numerals are
  always Consolas (Bahnschrift digits are proportional - §5.3).
* CRT (G-14, read at paint by FxCanvas / CrtGlass, passed in here): OFF crisp; LOW = the
  same-hue bloom (B11, R2 R-10 / 2.11: bloom_in 1 px then bloom_out 2 px on both sides of a
  square plate's stroke, draw_bloom); FULL = LOW + scanline slicing (ground colour 1 px
  every 3 px) across display words >= 15 pt drawn on a canvas. Never on Label/Text/Entry,
  the transcript, or text < 15 pt. B11 applies it to the kit's LOADING plate and screen
  titles through CrtGlass (attach_crt, only while the FX is live) and to the Settings 08
  preview box (an FxCanvas); one prefs subscription per store repaints them all live.
* Budget (§5.4): <= 6 ms per painter, <= 400 items per fx canvas. FxCanvas.paint()
  times every paint and logs an overrun once per class (PAINT_LOG keeps the samples).

Pixel conventions (measured on this Tk 8.6 / Windows build, see test_draw.py):
a fill-only rectangle (x0,y0,x1,y1) covers columns x0..x1-1; a rectangle WITH an outline
covers x0..x1 inclusive; a 1 px line's end point is exclusive; a polygon's fill covers
x0..x1-1 and its outline x0..x1 inclusive. rect_px() hides this: w x h px exactly.
"""

import collections
import math
import os
import sys
import threading
import time
import tkinter as tk
import weakref

from . import geometry as geo
from . import palette as _palette
from . import fonts as _fonts

try:  # the fx seam (get / FX). Never fails in the app; tolerate odd loaders.
    from . import get as _fx_get
    from . import FX as _FX
except Exception:  # pragma: no cover
    _fx_get = None
    _FX = None

__all__ = [
    "PAINT_LOG",
    "display_font", "font_points", "C7", "C8", "C9", "C9B", "C10B",
    "rect_px", "draw_plate", "draw_double_rule", "draw_segments",
    "draw_scanlines", "draw_bar_mark", "draw_label_in_gap_rule",
    "apply_crt",
    "FxCanvas", "registered", "redraw_all", "crt_level_of", "tones_of",
    "draw_bloom", "bloom_tones", "BLOOM_IN_W", "BLOOM_OUT_W", "CRT_TAG", "CrtGlass", "attach_crt",
]

C7 = ("Consolas", 7)
C8 = ("Consolas", 8)
C9 = ("Consolas", 9)
C9B = ("Consolas", 9, "bold")
C10B = ("Consolas", 10, "bold")


# (painter/canvas class, ms, items, reason) - bounded; read by tests and the verifier
PAINT_LOG = collections.deque(maxlen=512)

_logged = set()


def _log_once(key, msg):
    if key in _logged or len(_logged) >= 1024:
        return
    _logged.add(key)
    try:
        sys.stderr.write("fx.draw: %s\n" % (msg,))
        sys.stderr.flush()
    except Exception:
        pass


def _strict():
    return os.environ.get("LOCALAI_FX_STRICT") == "1"


def _crt(v):
    """Coerce a CRT level: 0/1/2 as given; anything else (None, bool, junk) -> LOW."""
    if isinstance(v, int) and not isinstance(v, bool) and v in geo.CRT_LEVELS:
        return v
    return geo.CRT_LOW


def _tags(tag, kind):
    if tag is None:
        tag = "fx"
    if isinstance(tag, (tuple, list)):
        return tuple(tag) + (kind,)
    return (tag, kind)


# ------------------------------------------------------------------ fonts

def display_font(widget, size="M"):
    """The named display font fxDisplay<size> when it exists on widget's interpreter
    (fx installed), else the TERMINAL fallback tuple (fonts.FALLBACK). For words and
    titles only - never numerals."""
    name = _fonts.display(size)
    try:
        widget.tk.call("font", "configure", name, "-size")
        return name
    except tk.TclError:
        return _fonts.FALLBACK[name[len(_fonts.NAME_PREFIX):]]


def _measure(widget, font, text):
    """Pixel width of text in font on widget's interpreter (no Font object churn)."""
    return int(widget.tk.call("font", "measure", font, text))


_BASE_SCALING = 96.0 / 72.0         # Tk's "tk scaling" at 100 % (kit._BASE_SCALING)


def scale_ratio(widget):
    """Real display ratio of widget's interpreter: tk scaling / (96/72), never below 1.0 (kit.geom_ratio's rule, kept
    here because the kit may not be imported by a painter). B11-H11: the CRT's pixel sizes - the scanline pitch and
    line width - follow it like every other kit dimension (3 px / 1 px at 100 %, 6 / 2 at 200 %)."""
    try:
        s = float(widget.tk.call("tk", "scaling"))
    except Exception:  # noqa: BLE001
        return 1.0
    return max(1.0, s / _BASE_SCALING)


def scan_geometry(widget):
    """(pitch, line) px of the CRT FULL scanlines on widget's interpreter at its Tk scaling (B11-H11)."""
    r = scale_ratio(widget)
    return max(2, int(round(geo.SCAN_PITCH * r))), max(1, int(round(r)))


def font_points(widget, font):
    """Actual point size of a font spec on widget's interpreter (named fonts read
    live, so a face switch is seen). Pixel sizes come back in points too (Tk's
    `font actual -size`). Unknown -> 0."""
    try:
        return abs(int(widget.tk.call("font", "actual", font, "-size")))
    except (tk.TclError, ValueError, TypeError):
        return 0


# ------------------------------------------------------------------ basic shapes

def rect_px(cv, x, y, w, h, fill="", outline="", tag="fx", kind="rect", **kw):
    """A rectangle covering exactly w x h pixels from (x, y), with or without an
    outline (Tk's outline rectangle is one pixel larger than its fill-only one)."""
    if outline:
        coords = (x, y, x + w - 1, y + h - 1)
    else:
        coords = (x, y, x + w, y + h)
    return cv.create_rectangle(*coords, fill=fill, outline=outline, tags=_tags(tag, kind), **kw)


BLOOM_IN_W, BLOOM_OUT_W = 1, 2      # the approved bloom rings (r2 model_load): bloom_in 1 px, then bloom_out 2 px
CRT_TAG = "fx_crt"                  # every item a CrtGlass makes


def bloom_tones(t, outline=None, ground=None):
    """(bloom_out, bloom_in) for a stroke of `outline` on `ground` (R2 shared tokens, same hue: never the red-shifted
    halo_*). The bright stroke on bg uses the palette's own pair; any other stroke the same mixes (15 % / 40 % of the
    stroke into the ground), so a dim line blooms dimly - never brighter than its stroke."""
    bright, bg = t.get("bright"), t.get("bg")
    outline = bright if outline is None else outline
    ground = bg if ground is None else ground

    def same(a, b):
        return isinstance(a, str) and isinstance(b, str) and a.strip().upper() == b.strip().upper()
    if same(outline, bright) and same(ground, bg) and t.get("bloom_out") and t.get("bloom_in"):
        return t["bloom_out"], t["bloom_in"]
    try:
        return (_palette.mix(ground, outline, _palette.BLOOM_OUT_T), _palette.mix(ground, outline, _palette.BLOOM_IN_T))
    except (ValueError, TypeError, KeyError, IndexError):
        return t.get("bloom_out", ground), t.get("bloom_in", ground)


def _ring(cv, x0, y0, x1, y1, w, colour, tags):
    """A square ring `w` px wide exactly inside [x0, x1) x [y0, y1): four fill-only rectangles (exact pixels at any
    width; a ring too thick for its box is one filled rectangle). -> ids."""
    x0, y0, x1, y1, w = int(x0), int(y0), int(x1), int(y1), int(w)
    if w <= 0 or x1 <= x0 or y1 <= y0:
        return []
    if 2 * w >= min(x1 - x0, y1 - y0):
        return [cv.create_rectangle(x0, y0, x1, y1, fill=colour, outline="", width=0, tags=tags)]
    return [cv.create_rectangle(x0, y0, x1, y0 + w, fill=colour, outline="", width=0, tags=tags),
            cv.create_rectangle(x0, y1 - w, x1, y1, fill=colour, outline="", width=0, tags=tags),
            cv.create_rectangle(x0, y0 + w, x0 + w, y1 - w, fill=colour, outline="", width=0, tags=tags),
            cv.create_rectangle(x1 - w, y0 + w, x1, y1 - w, fill=colour, outline="", width=0, tags=tags)]


def draw_bloom(cv, t, x0, y0, x1, y1, *, stroke, outline=None, ground=None, crt=geo.CRT_LOW, side="both",
               widths=None, tag="fx"):
    """The same-hue CRT bloom (R2 R-10 / 2.11; the approved r2 model_load renderer) of a square `stroke` px outline
    drawn exactly inside [x0, x1) x [y0, y1): at CRT LOW / FULL, bloom_in then bloom_out (`widths` = (in, out) px,
    default 1, 2) on the OUTSIDE of the stroke and/or (side "both" / "outside" / "inside") the INSIDE of it:
        bg | out 2 | in 1 | STROKE | in 1 | out 2 | bg
    OFF: nothing. The outside rings need in + out px of room around the box. -> ids (kind tag "bloom")."""
    if _crt(crt) < geo.CRT_LOW or not stroke:
        return []
    w_in, w_out = widths if widths else (BLOOM_IN_W, BLOOM_OUT_W)
    w_in, w_out, s = max(0, int(w_in)), max(0, int(w_out)), max(0, int(stroke))
    c_out, c_in = bloom_tones(t, outline, ground)
    tags = _tags(tag, "bloom")
    ids = []
    if side in ("both", "outside"):
        d = w_in + w_out
        ids += _ring(cv, x0 - d, y0 - d, x1 + d, y1 + d, w_out, c_out, tags)
        ids += _ring(cv, x0 - w_in, y0 - w_in, x1 + w_in, y1 + w_in, w_in, c_in, tags)
    if side in ("both", "inside"):
        ids += _ring(cv, x0 + s, y0 + s, x1 - s, y1 - s, w_in, c_in, tags)
        ids += _ring(cv, x0 + s + w_in, y0 + s + w_in, x1 - s - w_in, y1 - s - w_in, w_out, c_out, tags)
    return ids


def draw_plate(cv, t, x0, y0, x1, y1, *, outline=None, fill="", width=geo.STROKE_PLATE, crt=geo.CRT_LOW,
               ground=None, bloom=None, tag="fx"):
    """Square plate (D-11; B11 dropped the R1 chamfer): a `width` px outline exactly inside [x0, x1) x [y0, y1) (four
    fill-only rectangles: the ink box is exact at any width), an optional `fill` inside the stroke, and at CRT LOW /
    FULL the same-hue bloom on both sides of the stroke (draw_bloom; bloom_out / bloom_in for the bright stroke, the
    same mixes of any other; `bloom` = its (in, out) ring widths, default 1 / 2 px - pass them scaled at 200 %). The
    outer bloom needs in + out px of room around the box. -> ids (fill, blooms, outline)."""
    outline = t["bright"] if outline is None else outline
    crt = _crt(crt)
    width = max(0, int(width))
    ids = []
    if fill:
        ids.append(cv.create_rectangle(x0 + width, y0 + width, x1 - width, y1 - width, fill=fill, outline="", width=0,
                                       tags=_tags(tag, "plate")))
    if outline and width:
        if crt >= geo.CRT_LOW:
            ids += draw_bloom(cv, t, x0, y0, x1, y1, stroke=width, outline=outline, ground=ground, crt=crt,
                              widths=bloom, tag=tag)
        ids += _ring(cv, x0, y0, x1, y1, width, outline, _tags(tag, "plate"))
    return ids


def draw_double_rule(cv, t, x0, x1, y, *, color=None, inner=None, sep=3, tag="fx"):
    """Double-ruled frame edge (ref 04 bezel): a 1 px rule and a second one `sep`
    below it (inner colour defaults to ghost: decorative hairline)."""
    color = t["dim"] if color is None else color
    inner = t["ghost"] if inner is None else inner
    return [cv.create_line(x0, y, x1, y, fill=color, width=1, tags=_tags(tag, "rule")),
            cv.create_line(x0, y + sep, x1, y + sep, fill=inner, width=1, tags=_tags(tag, "rule"))]


def draw_label_in_gap_rule(cv, t, x0, x1, y, label, *, font=C8, color=None, rule=None, gap=6,
                           inset=12, align="left", tag="fx"):
    """A 1 px structure rule with a label knocked into a gap in it (ref 16).
    align 'left' puts the label `inset` px from x0, 'center' centres it, 'right'
    `inset` px from x1. Label in the value tier (small text), rule in dim.
    -> list of item ids (rule segments + text)."""
    color = t["value"] if color is None else color
    rule = t["dim"] if rule is None else rule
    try:
        lw = _measure(cv, font, label) if label else 0
    except Exception:
        lw = 7 * len(label or "")
    if align == "center":
        lx = (x0 + x1 - lw) / 2.0
    elif align == "right":
        lx = x1 - inset - lw
    else:
        lx = x0 + inset
    ids = []
    if label:
        if lx - gap > x0:
            ids.append(cv.create_line(x0, y, lx - gap, y, fill=rule, width=1, tags=_tags(tag, "rule")))
        if lx + lw + gap < x1:
            ids.append(cv.create_line(lx + lw + gap, y, x1, y, fill=rule, width=1, tags=_tags(tag, "rule")))
        ids.append(cv.create_text(lx, y, text=label, anchor="w", fill=color, font=font, tags=_tags(tag, "word")))
    else:
        ids.append(cv.create_line(x0, y, x1, y, fill=rule, width=1, tags=_tags(tag, "rule")))
    return ids


def draw_scanlines(cv, x0, y0, x1, y1, color, *, pitch=geo.SCAN_PITCH, width=1, tag="fx"):
    """CRT FULL slicing: a `width` px line (1 at 100 %) of `color` (the ground) every `pitch` rows
    across [x0, x1) x [y0, y1)."""
    return [cv.create_line(x0, y, x1, y, fill=color, width=width, tags=_tags(tag, "scan"))
            for y in geo.scanline_rows(y0, y1, pitch)]


# ------------------------------------------------------------------ display words + CRT


def apply_crt(cv, t, crt, items, *, ground=None, tag="fx", pitch=None, line=1):
    """Scanline-slice the given canvas TEXT items whose font is >= 15 pt, only at CRT
    FULL. `items` is an iterable of item ids or a tag. Any other item type, a smaller
    font or a lower level is left alone. ground: the colour under the text (default
    t['bg']). pitch / line: the scanline pitch and line width in px (the approved 3 / 1
    by default; a scaled caller passes scan_geometry(cv), B11-H11). -> ids of the scanline
    items created."""
    if _crt(crt) < geo.CRT_FULL:
        return []
    ground = t["bg"] if ground is None else ground
    pitch = geo.SCAN_PITCH if pitch is None else max(2, int(pitch))
    line = max(1, int(line))
    if isinstance(items, (str, int)):
        items = cv.find_withtag(items)
    out = []
    for i in items:
        try:
            if cv.type(i) != "text":
                continue
            if font_points(cv, cv.itemcget(i, "font")) < geo.SCANLINE_MIN_PT:
                continue
            bb = cv.bbox(i)
        except tk.TclError:
            continue
        if not bb:
            continue
        x0, y0, x1, y1 = bb
        out += draw_scanlines(cv, x0, y0, x1, y1, ground, pitch=pitch, width=line, tag=_tags(tag, "crt"))
    return out


# ------------------------------------------------------------------ meters

def draw_segments(cv, t, x0, y0, n, filled, color, *, seg_w=geo.CTX_SEG_W, seg_h=geo.SEG_H,
                  gap=geo.CTX_SEG_GAP, empty_outline=None, empty_fill=None, stipple_from=None, tag="fx"):
    """n segments, the first `filled` in `color` (fill-only, exactly seg_w x seg_h px),
    the rest as 1 px empty outlines (dim on panel). Filled segments with index >=
    stipple_from get stipple gray50 (A-8 warn-band shape cue). -> ids."""
    empty_outline = t["dim"] if empty_outline is None else empty_outline
    empty_fill = t["panel"] if empty_fill is None else empty_fill
    n = max(0, int(n))
    filled = max(0, min(n, int(filled)))
    ids = []
    for i, (a, b, c, d) in enumerate(geo.segment_rects(x0, y0, n, seg_w, seg_h, gap)):
        if i < filled:
            kw = {}
            if stipple_from is not None and i >= stipple_from:
                kw["stipple"] = "gray50"
            ids.append(rect_px(cv, a, b, seg_w, seg_h, fill=color, tag=tag, kind="seg", **kw))
        else:
            ids.append(rect_px(cv, a, b, seg_w, seg_h, fill=empty_fill, outline=empty_outline, tag=tag, kind="seg"))
    return ids


# ------------------------------------------------------------------ identity marks

def draw_bar_mark(cv, x0, y0, height, color, *, mask=geo.CURSOR_MARK, duty=0.5, tag="fx"):
    """Static bar-built mark (ref 18), exactly `height` tall. -> (ids, width_px)."""
    rects, width = geo.bar_mark_runs(mask, x0, y0, height, duty)
    ids = [cv.create_rectangle(a, b, c, d, fill=color, outline="", tags=_tags(tag, "mark")) for a, b, c, d in rects]
    return ids, width


# ------------------------------------------------------------------ FxCanvas + registry

_REGISTRY = weakref.WeakSet()
_SUBSCRIBED = weakref.WeakSet()       # prefs stores we listen to (one subscription per store)
# B11 (GAP_FX 5.2): fx.crt only. The instrument ink has no control (D-15) and stays inert: an ink change no longer
# repaints (FX._on_ink still updates the palette snapshot).
_LIVE_KEYS = ("fx.crt",)


def _fx_for(widget, fx=None):
    if fx is not None:
        return fx
    if _fx_get is None:
        return None
    try:
        return _fx_get(widget)
    except Exception:
        return None


def _enabled(fx):
    return bool(getattr(fx, "enabled", False))


def _is_fx(fx):
    try:
        return _FX is not None and isinstance(fx, _FX)
    except Exception:
        return False


def tones_of(fx, theme=None, preset=None, ink=None):
    """Tones for a canvas under `fx`, read NOW: preset = the FX's install-time palette
    snapshot (the phosphor is restart-applied) or theme.phosphor_name without fx;
    ink = prefs 'fx.instrument_ink' (live) or the snapshot's ink."""
    pal = None
    if _enabled(fx) or _is_fx(fx):          # a torn-down FX keeps its snapshot (never NullFX's amber)
        pal = getattr(fx, "palette", None)
        if not isinstance(pal, dict):
            pal = None
    if preset is None:
        if pal is not None:
            preset = pal.get("preset")
        elif theme is not None:
            preset = getattr(theme, "phosphor_name", None)
    if ink is None:
        prefs = getattr(fx, "prefs", None) if _enabled(fx) else None
        if prefs is not None:
            try:
                ink = prefs.get("fx.instrument_ink")
            except Exception:
                ink = None
        if ink is None and pal is not None:
            ink = pal.get("ink")
    return _palette.tones(preset, ink or "phosphor")


def crt_level_of(fx):
    """CRT level read NOW: prefs 'fx.crt' (0/1/2) when fx is enabled, else OFF.
    An unreadable or bad value reads as FULL, the schema default (D-29; the store itself
    already turns a bad stored value into the default on load)."""
    if not _enabled(fx):
        return geo.CRT_OFF
    prefs = getattr(fx, "prefs", None)
    if prefs is None:
        return geo.CRT_FULL
    try:
        v = prefs.get("fx.crt")
    except Exception:
        return geo.CRT_FULL
    if isinstance(v, bool) or not isinstance(v, int) or v not in geo.CRT_LEVELS:
        return geo.CRT_FULL
    return v


def _ensure_subscription(fx):
    """One prefs subscription per store for fx.crt (_LIVE_KEYS) -> redraw every live
    FxCanvas and CrtGlass of that store. Runs on the thread that changed the value
    (the Tk thread); redraw_all refuses other threads."""
    if not _enabled(fx):
        return
    prefs = getattr(fx, "prefs", None)
    if prefs is None or not hasattr(prefs, "subscribe"):
        return
    try:
        if prefs in _SUBSCRIBED:
            return
    except TypeError:
        return
    ref = weakref.ref(prefs)

    def on_change(path, value, ref=ref):
        p = ref()
        if p is not None:
            redraw_all(prefs=p, reason=str(path))
    try:
        for key in _LIVE_KEYS:
            prefs.subscribe(key, on_change)
        _SUBSCRIBED.add(prefs)
    except Exception as e:
        _log_once(("subscribe", type(e).__name__), "prefs subscription failed: %r - no live CRT/ink redraw" % (e,))


def registered(fx=None, prefs=None):
    """Live registered FxCanvases and CrtGlasses of the CALLING thread's Tk (optionally
    only those of one FX / prefs store). Canvases of another thread are never touched."""
    out = []
    me = threading.get_ident()
    for c in list(_REGISTRY):
        if c._thread != me or not c._alive():
            continue
        if fx is not None and c._fx is not fx:
            continue
        if prefs is not None and getattr(c._fx, "prefs", None) is not prefs:
            continue
        out.append(c)
    return out


def redraw_all(fx=None, prefs=None, reason="redraw"):
    """Repaint every live registered canvas (of one FX / store). A canvas destroyed
    by an earlier canvas's repaint is skipped. Only on the canvases' own thread.
    -> number of canvases repainted."""
    n = 0
    for c in sorted(registered(fx, prefs), key=lambda c: c._serial):
        if not c._alive():
            continue
        if c.paint(reason):
            n += 1
    return n


_serial_counter = [0]


class FxCanvas(tk.Canvas):
    """Base for fx canvases: paint() on <Configure> (size change), registered in a
    WeakSet so a live CRT change (prefs 'fx.crt', _LIVE_KEYS) repaints every canvas
    without a restart. No Tk timers: painting happens only in response to <Configure>,
    an explicit paint()/redraw(), or a prefs change. B11's caller: the Settings 08 CRT
    preview box (fx/page08/crt.py). Kit canvases the kit draws itself get a CrtGlass.

    Subclasses override draw(self, t, crt) and create items through the painters with
    tag=self.FX_TAG (default 'fx'); paint() deletes that tag first. Items NOT carrying
    FX_TAG (e.g. an embedded create_window) survive repaints.

    FxCanvas(parent, theme=None, *, fx=None, preset=None, ink=None, crt=None,
             ground='bg', **canvas_options)
      fx      the FX to read from (default fx.get(parent); NullFX -> CRT OFF, tones
              from theme.phosphor_name)
      preset / ink / crt   pin a value instead of reading it live (previews)
      ground  tone key (or colour) for the canvas background
    """

    FX_TAG = "fx"

    def __init__(self, parent, theme=None, *, fx=None, preset=None, ink=None, crt=None, ground="bg", **kw):
        self._fx = _fx_for(parent, fx)
        self._theme = theme
        self._preset = preset
        self._ink = ink
        self._crt_pin = crt
        self._ground_key = ground
        self._dead = False
        self._painted_size = None
        self._thread = threading.get_ident()
        _serial_counter[0] += 1
        self._serial = _serial_counter[0]
        self.paint_count = 0
        self.paint_errors = 0
        self.last_paint_ms = 0.0
        self.last_items = 0
        self.last_reason = None
        t = self.tones()
        kw.setdefault("bg", t.get(ground, ground) if isinstance(ground, str) else t["bg"])
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bd", 0)
        super().__init__(parent, **kw)
        self.bind("<Configure>", self._fx_on_configure, add="+")
        self.bind("<Destroy>", self._fx_on_destroy, add="+")
        _REGISTRY.add(self)
        _ensure_subscription(self._fx)

    # --- reading the live state ------------------------------------------------------
    @property
    def fx(self):
        return self._fx

    @fx.setter
    def fx(self, value):
        """Re-point the canvas at another FX (a subclass may assign self.fx); the
        live-redraw subscription follows."""
        self._fx = value
        _ensure_subscription(value)

    def tones(self):
        """Tones read NOW (live ink, snapshot preset, or the pinned values)."""
        return tones_of(self._fx, self._theme, self._preset, self._ink)

    def crt(self):
        """CRT level read NOW (or the pinned one)."""
        if self._crt_pin is not None:
            return self._crt_pin
        return crt_level_of(self._fx)

    def px(self, n):
        try:
            return self._fx.px(n) if self._fx is not None else n
        except Exception:
            return n

    def fx_size(self):
        """(w, h): the mapped size, or the requested size before the first layout.
        (Not named size(): tkinter's Misc.size is grid_size.)"""
        try:
            w, h = self.winfo_width(), self.winfo_height()
            if w <= 1 and h <= 1:
                w, h = int(float(self.cget("width"))), int(float(self.cget("height")))
            return w, h
        except (tk.TclError, ValueError):
            return 0, 0

    # --- lifecycle -------------------------------------------------------------------
    def _alive(self):
        if self._dead:
            return False
        try:
            return bool(self.winfo_exists())
        except Exception:
            return False

    def _fx_on_destroy(self, event):
        if event.widget is self:
            self._dead = True

    def _fx_on_configure(self, event):
        if event.widget is not self:
            return
        size = (event.width, event.height)
        if size != self._painted_size:
            self._painted_size = size
            self.paint("configure")

    # --- painting --------------------------------------------------------------------
    def draw(self, t, crt):
        """Subclass hook: create items with tag=self.FX_TAG. Default: nothing."""
        return None

    def redraw(self, reason="redraw"):
        return self.paint(reason)

    def paint(self, reason="paint"):
        """delete(FX_TAG) + draw(tones, crt), timed. -> True if painted. A canvas
        destroyed before or DURING the paint returns False quietly (no bgerror). Any
        other error is logged once per class (re-raised under LOCALAI_FX_STRICT=1)."""
        if threading.get_ident() != self._thread:
            _log_once(("paint-thread",), "FxCanvas.paint called off the Tk thread - refused")
            return False                         # checked FIRST: no Tk call from a worker
        if not self._alive():
            return False
        t0 = time.perf_counter()
        name = type(self).__name__
        try:
            t = self.tones()
            crt = self.crt()
            self.delete(self.FX_TAG)
            self.draw(t, crt)
        except tk.TclError as e:
            if not self._alive():
                return False                     # destroyed mid-paint: nothing to report
            self.paint_errors += 1
            _log_once(("paint", name, "tcl"), "%s.paint failed: %r" % (name, e))
            if _strict():
                raise
            return False
        except Exception as e:
            if not self._alive():
                return False
            self.paint_errors += 1
            _log_once(("paint", name, type(e).__name__), "%s.paint failed: %r" % (name, e))
            if _strict():
                raise
            return False
        ms = (time.perf_counter() - t0) * 1000.0
        if not self._alive():
            return False
        try:
            items = len(self.find_withtag(self.FX_TAG))
        except tk.TclError:
            return False
        self.paint_count += 1
        self.last_paint_ms = ms
        self.last_items = items
        self.last_reason = reason
        PAINT_LOG.append((name, round(ms, 3), items, reason))
        if ms > geo.PAINT_BUDGET_MS:
            _log_once(("budget-ms", name), "%s paint took %.2f ms (> %.0f ms budget)" % (name, ms, geo.PAINT_BUDGET_MS))
        if items > geo.ITEM_BUDGET:
            _log_once(("budget-items", name), "%s has %d fx items (> %d)" % (name, items, geo.ITEM_BUDGET))
        return True


# ------------------------------------------------------------------ CrtGlass (B11: CRT on a kit canvas)

class CrtGlass(object):
    """CRT physics on the glass of a canvas another module draws (B11): the kit's LOADING plate and screen titles
    (R2 R-10: "in the glass, never in the reading text"). Made by attach_crt() only while the canvas's FX is live,
    and a member of the FxCanvas registry, so a live fx.crt change repaints it with every FxCanvas (one prefs
    subscription per store). The registry holds it weakly: the canvas's owner keeps the reference and calls paint()
    after every repaint of its own items (paint() is also how the level is applied).

        words   callable -> the canvas TEXT items to slice at FULL (apply_crt: text >= 15 pt only, so nothing
                else is ever touched), or None
        plate   callable -> (x0, y0, x1, y1, stroke[, outline]) of a square plate the owner draws exactly inside that
                box, or None: its bloom INSIDE the stroke is drawn here at LOW / FULL (draw_bloom side "inside")
        frames  (outer, inner) Frames wrapped around the canvas, whose highlight rings ARE the bloom outside the
                stroke (a canvas has no room outside itself): coloured bloom_out / bloom_in at LOW / FULL, their own
                bg at OFF. Their highlight thicknesses give the ring widths (in, out) of the inner bloom too.
        ground  the colour under the words (scanline ink); default the canvas bg

    Every item it makes carries CRT_TAG (+ "bloom" / "crt" / "scan"); paint() deletes CRT_TAG first, so nothing ever
    stacks. Blooms go below every other item, scanlines above the words. paint() is timed into PAINT_LOG like an
    FxCanvas paint. Static: no timer; it refuses to run off its Tk thread; it never raises on a destroyed canvas."""

    def __init__(self, canvas, fx, *, words=None, plate=None, frames=None, ground=None):
        self.canvas = canvas
        self._fx = fx
        self._words = words
        self._plate = plate
        self._frames = tuple(f for f in (frames or ()) if f is not None)
        self._ground = ground
        self._dead = False
        self._thread = threading.get_ident()
        _serial_counter[0] += 1
        self._serial = _serial_counter[0]
        self.paint_count = 0
        self.paint_errors = 0
        self.last_paint_ms = 0.0
        self.last_items = 0
        self.last_reason = None
        self.last_crt = None
        _REGISTRY.add(self)
        _ensure_subscription(fx)

    def __repr__(self):
        return "<fx.draw.CrtGlass on %s crt=%s>" % (getattr(self.canvas, "_w", "?"), self.last_crt)

    @property
    def fx(self):
        return self._fx

    def crt(self):
        return crt_level_of(self._fx)

    def tones(self):
        return tones_of(self._fx)

    def _alive(self):
        if self._dead:
            return False
        try:
            alive = bool(self.canvas.winfo_exists())
        except Exception:
            alive = False
        if not alive:
            self._dead = True
        return alive

    def _ground_colour(self):
        g = self._ground() if callable(self._ground) else self._ground
        if isinstance(g, str) and g:
            return g
        return self.canvas.cget("bg")

    def _ring_widths(self):
        """(in, out) px of the bloom rings: the frames' highlight thicknesses when given, else the approved 1 / 2."""
        if len(self._frames) == 2:
            try:
                return (int(float(self._frames[1].cget("highlightthickness"))),
                        int(float(self._frames[0].cget("highlightthickness"))))
            except (tk.TclError, ValueError):
                pass
        return BLOOM_IN_W, BLOOM_OUT_W

    def _paint_frames(self, t, crt, outline):
        if len(self._frames) != 2:
            return
        c_out, c_in = bloom_tones(t, outline, None)
        for frame, colour in zip(self._frames, (c_out, c_in)):
            try:
                if not frame.winfo_exists():
                    continue
                if crt < geo.CRT_LOW:
                    colour = frame.cget("bg")
                if frame.cget("highlightbackground") != colour or frame.cget("highlightcolor") != colour:
                    frame.configure(highlightbackground=colour, highlightcolor=colour)
            except tk.TclError:
                continue

    def paint(self, reason="paint"):
        """delete(CRT_TAG), then the bloom (LOW / FULL) and the scanlines (FULL) at the level read NOW. -> True if
        painted. A canvas destroyed before or during the paint returns False quietly (no bgerror)."""
        if threading.get_ident() != self._thread:
            _log_once(("glass-thread",), "CrtGlass.paint called off the Tk thread - refused")
            return False                         # checked FIRST: no Tk call from a worker
        if not self._alive():
            return False
        cv = self.canvas
        t0 = time.perf_counter()
        try:
            t = self.tones()
            crt = self.crt()
            if crt >= geo.CRT_FULL and _fonts.display_is_terminal(cv):
                # B11-H10: a title drawn in the Consolas set (the TERMINAL face chosen, or Bahnschrift missing) keeps
                # its round capitals' bottom curve in one ink row that FULL's 3 px slicing cuts away (O / C / G / J /
                # U read open-bottomed). The glass then shows LOW; the page-08 setting itself stays FULL (the UI's
                # ruling: FULL slices Bahnschrift only)
                crt = geo.CRT_LOW
            ground = self._ground_colour()
            cv.delete(CRT_TAG)
            box = self._plate() if callable(self._plate) else None
            outline = (box[5] if box and len(box) > 5 and box[5] else t["bright"])
            self._paint_frames(t, crt, outline)
            if crt >= geo.CRT_LOW and box:
                x0, y0, x1, y1, stroke = box[:5]
                if draw_bloom(cv, t, x0, y0, x1, y1, stroke=stroke, outline=outline, ground=ground, crt=crt,
                              side="inside", widths=self._ring_widths(), tag=CRT_TAG):
                    cv.tag_lower(CRT_TAG)        # under the plate's own items (it is all blooms so far)
            if crt >= geo.CRT_FULL and callable(self._words):
                items = [i for i in (self._words() or ()) if i is not None]
                if items:
                    pitch, line = scan_geometry(cv)         # B11-H11: 3 / 1 at 100 %, scaled with Tk
                    apply_crt(cv, t, crt, items, ground=ground, tag=CRT_TAG, pitch=pitch, line=line)
        except tk.TclError as e:
            if not self._alive():
                return False
            self.paint_errors += 1
            _log_once(("glass-paint", "tcl"), "CrtGlass.paint failed: %r" % (e,))
            if _strict():
                raise
            return False
        except Exception as e:
            if not self._alive():
                return False
            self.paint_errors += 1
            _log_once(("glass-paint", type(e).__name__), "CrtGlass.paint failed: %r" % (e,))
            if _strict():
                raise
            return False
        ms = (time.perf_counter() - t0) * 1000.0
        try:
            items = len(cv.find_withtag(CRT_TAG))
        except tk.TclError:
            return False
        self.paint_count += 1
        self.last_paint_ms = ms
        self.last_items = items
        self.last_reason = reason
        self.last_crt = crt
        PAINT_LOG.append(("CrtGlass", round(ms, 3), items, reason))
        if ms > geo.PAINT_BUDGET_MS:
            _log_once(("budget-ms", "CrtGlass"), "CrtGlass paint took %.2f ms (> %.0f ms budget)"
                      % (ms, geo.PAINT_BUDGET_MS))
        return True

    redraw = paint


def attach_crt(canvas, *, words=None, plate=None, frames=None, ground=None, fx=None):
    """B11: the CRT glass of `canvas` (see CrtGlass), made and painted once NOW when the canvas's FX (fx.get(canvas),
    or `fx`) is live -> the CrtGlass; else None: under the kill switch, NullFX, a torn-down FX or off the Tk thread
    nothing is made, so the canvas stays exactly its static self. The caller keeps the glass and calls glass.paint()
    after each repaint of its own items. Never raises."""
    try:
        f = _fx_for(canvas, fx)
        if not _enabled(f):
            return None
        glass = CrtGlass(canvas, f, words=words, plate=plate, frames=frames, ground=ground)
        glass.paint("attach")
        return glass
    except Exception as e:  # noqa: BLE001 - an effect never breaks the widget it decorates
        _log_once(("attach", type(e).__name__), "attach_crt failed: %r - no CRT on this canvas" % (e,))
        return None
