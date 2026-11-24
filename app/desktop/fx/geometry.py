"""fx/geometry.py - pure geometry for the fx primitive library (G-04; tokens AESTHETIC_SPEC §5.4).

PURE: no Tk, no I/O, no threads, no clock. Every function takes plain numbers and
returns plain numbers / tuples / lists, so it is unit-testable without a display and
safe to call from anywhere. fx/draw.py turns these into canvas items.

Conventions
-----------
* Coordinates are canvas pixels (floats allowed). Point lists are FLAT
  [x0, y0, x1, y1, ...] - the shape Tk's create_polygon / create_line accept.
* "Design pixels": every default below is the §5.4 token at 100 % scaling. A caller
  that honours DPI passes ``fx.px(n)`` (or ``px(n, scaling)`` here) for sizes.
* Orthogonal and 45 degree geometry only.

Contents
--------
tokens ............ STROKE_*, CTX_*, SEG_*, SCAN_*, CRT_*, WARN_PCT/DANGER_PCT,
                    PAINT_BUDGET_MS/ITEM_BUDGET
px(n, scaling)      design px -> device px (A-18)
flag_poly           KiCad net-label flag: out / in / bi
tick_x              pixel column of a pct mark
band                the colour band for an integer pct (A-8)
segments_filled / segment_rects   segmented meters
dot_leader_count    how many leader dots fit
bar_mark_runs       run-length rectangles of a bitmap mask (the static bar mark)
scanline_rows       rows sliced by the CRT FULL scanline treatment (G-14)
unspace             exact inverse of theme.letter_spaced (e16)

(B15 removed the banned P2 geometry - chamfer, brackets, registration cross, ruler ticks / caret / K labels,
hazard stripes, chevron, contour emblem - with the user's yes; see design archive, backups/B15del_*.)
"""

import math

__all__ = [
    "STROKE_HAIR", "STROKE_REF", "STROKE_PLATE", "STROKE_HERO",
    "CTX_SEGMENTS", "CTX_SEG_W", "CTX_SEG_GAP", "CTX_BAR_W",
    "SEG_W", "SEG_H", "SEG_GAP",
    "SCAN_PITCH",
    "CRT_OFF", "CRT_LOW", "CRT_FULL", "CRT_LEVELS", "SCANLINE_MIN_PT",
    "WARN_PCT", "DANGER_PCT", "PAINT_BUDGET_MS", "ITEM_BUDGET",
    "CURSOR_MARK",
    "px", "flag_poly",
    "tick_x", "band", "clamp_pct",
    "segments_filled", "segment_rects",
    "dot_leader_count", "bar_mark_runs",
    "scanline_rows", "unspace",
]

# ------------------------------------------------------------------ tokens (§5.4)

STROKE_HAIR, STROKE_REF, STROKE_PLATE, STROKE_HERO = 1, 2, 2, 3

# Context meter (A-8): 20 segments x 8 px + 2 px gap = 198 px; filled = pct*20//100
CTX_SEGMENTS, CTX_SEG_W, CTX_SEG_GAP = 20, 8, 2
CTX_BAR_W = CTX_SEGMENTS * (CTX_SEG_W + CTX_SEG_GAP) - CTX_SEG_GAP       # 198
# Generic segmented meter: 8 x 12, gap 2, count = floor((w + 2) / 10)
SEG_W, SEG_H, SEG_GAP = 8, 12, 2

WARN_PCT, DANGER_PCT = 75, 90

SCAN_PITCH = 3                            # CRT FULL: ground 1 px every 3 px

CRT_OFF, CRT_LOW, CRT_FULL = 0, 1, 2
CRT_LEVELS = (CRT_OFF, CRT_LOW, CRT_FULL)
SCANLINE_MIN_PT = 15                      # display words >= 15 pt only (G-14)

PAINT_BUDGET_MS = 6.0                     # per painter
ITEM_BUDGET = 400                         # per fx canvas

_BASE_SCALING = 96.0 / 72.0               # tk scaling at 100 % (px(n) == n)


# ------------------------------------------------------------------ helpers

def _num(v, what="value"):
    """float(v) for an int/float (bool rejected); ValueError for NaN/inf/non-numbers."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("%s must be a number, got %r" % (what, v))
    f = float(v)
    if math.isnan(f) or math.isinf(f):
        raise ValueError("%s must be finite, got %r" % (what, v))
    return f


def px(n, scaling=None):
    """Design pixels -> device pixels: round(n * scaling / (96/72)) (A-18).
    scaling None (or unusable) -> n unchanged. Half-up rounding."""
    if scaling is None:
        return n
    try:
        s = float(scaling)
        if not (s > 0) or math.isinf(s):
            return n
        return int(math.floor(float(n) * s / _BASE_SCALING + 0.5))
    except (TypeError, ValueError):
        return n


# ------------------------------------------------------------------ plates and flags


def _dedupe(pts):
    """Drop consecutive duplicate vertices (a chamfer of exactly half a side meets its
    neighbour), including the wrap-around pair."""
    out = []
    for i in range(0, len(pts), 2):
        p = (pts[i], pts[i + 1])
        if out and (out[-2], out[-1]) == p:
            continue
        out += [p[0], p[1]]
    if len(out) >= 4 and (out[0], out[1]) == (out[-2], out[-1]):
        out = out[:-2]
    return out


def flag_poly(x0, y0, x1, y1, direction="bi"):
    """KiCad net-label flag (ref 05): 'out' points right, 'in' points left, 'bi' both
    (the status-flag bi-hexagon). The point depth is half the height, clamped so the
    two points of a 'bi' flag never cross. Flat points, clockwise."""
    if direction not in ("out", "in", "bi"):
        raise ValueError("flag direction must be out/in/bi, got %r" % (direction,))
    x0, x1 = (x0, x1) if x0 <= x1 else (x1, x0)
    y0, y1 = (y0, y1) if y0 <= y1 else (y1, y0)
    h = y1 - y0
    w = x1 - x0
    p = min(h / 2.0, w / (2.0 if direction == "bi" else 1.0))
    ym = (y0 + y1) / 2.0
    if direction == "out":
        pts = [x0, y0, x1 - p, y0, x1, ym, x1 - p, y1, x0, y1]
    elif direction == "in":
        pts = [x0 + p, y0, x1, y0, x1, y1, x0 + p, y1, x0, ym]
    else:
        pts = [x0 + p, y0, x1 - p, y0, x1, ym, x1 - p, y1, x0 + p, y1, x0, ym]
    return _dedupe(pts)


# ------------------------------------------------------------------ rulers, carets, meters

def clamp_pct(pct):
    """A percent clamped to 0..100 (non-numbers -> ValueError)."""
    return max(0.0, min(100.0, _num(pct, "pct")))


def band(pct, warn=WARN_PCT, danger=DANGER_PCT):
    """'ok' | 'warn' | 'danger' for a percentage (the context meter's colour rule)."""
    p = clamp_pct(pct)
    if p >= danger:
        return "danger"
    if p >= warn:
        return "warn"
    return "ok"


def tick_x(pct, x0=0, width=CTX_BAR_W):
    """Integer pixel column for a mark at pct: x0 + floor(width * pct / 100). With the
    A-8 layout (20 x (8 + 2)) the 25/50/75/90 % columns all land in inter-segment gaps
    (49, 99, 148, 178 + x0); round() would put 25 % (49.5 -> 50) on a segment."""
    return int(math.floor(x0 + width * clamp_pct(pct) / 100.0))


def segments_filled(pct, n=CTX_SEGMENTS):
    """Filled segments for an integer-ish pct: floor(pct * n / 100), clamped to 0..n.
    Never overstates the value (A-8 / r2)."""
    p = clamp_pct(pct)
    n = int(n)
    return max(0, min(n, int(math.floor(p * n / 100.0 + 1e-9))))


def segment_rects(x0, y0, n, seg_w=CTX_SEG_W, seg_h=SEG_H, gap=CTX_SEG_GAP):
    """Pixel rectangles (x0, y0, x1, y1) with x1/y1 EXCLUSIVE (segment i covers
    columns x0 + i*(seg_w+gap) .. + seg_w - 1). draw.py maps this to Tk's fill-only /
    outline coordinate conventions so each segment is exactly seg_w x seg_h px."""
    pitch = seg_w + gap
    return [(x0 + i * pitch, y0, x0 + i * pitch + seg_w, y0 + seg_h) for i in range(int(n))]


# ------------------------------------------------------------------ textures


def dot_leader_count(avail_px, dot_px):
    """How many leader units of width dot_px fit in avail_px (>= 0)."""
    a = _num(avail_px, "avail_px")
    d = _num(dot_px, "dot_px")
    if d <= 0 or a <= 0:
        return 0
    return int(a // d)


# The '[▮]' bar mark (ref 18): 18 cols x 15 rows.
CURSOR_MARK = (
    "#####........#####",
    "#####........#####",
    "##..............##",
    "##..............##",
    "##....######....##",
    "##....######....##",
    "##....######....##",
    "##....######....##",
    "##....######....##",
    "##....######....##",
    "##....######....##",
    "##..............##",
    "##..............##",
    "#####........#####",
    "#####........#####",
)


def bar_mark_runs(mask=CURSOR_MARK, x0=0, y0=0, height=76, duty=0.5):
    """Rectangles (x0, y0, x1, y1; exclusive ends) for a bitmap mask drawn as
    horizontal bars: each mask row becomes a bar band of thickness round(pitch*duty)
    where pitch = height / rows; runs of '#' become one rectangle each. -> (rects,
    width_px)."""
    rows = len(mask)
    if rows == 0:
        return [], 0
    cols = max(len(r) for r in mask)
    pitch = float(height) / rows
    thick = max(1, int(math.floor(pitch * duty + 0.5)))
    cell = pitch * 0.9
    rects = []
    for r, line in enumerate(mask):
        y = int(math.floor(y0 + r * pitch + 0.5))
        c = 0
        n = len(line)
        while c < n:
            if line[c] == "#":
                s = c
                while c < n and line[c] == "#":
                    c += 1
                rects.append((int(math.floor(x0 + s * cell + 0.5)), y,
                              int(math.floor(x0 + c * cell + 0.5)), y + thick))
            else:
                c += 1
    return rects, int(math.floor(cols * cell + 0.5))


# ------------------------------------------------------------------ CRT

def scanline_rows(y0, y1, pitch=SCAN_PITCH):
    """Rows sliced by the CRT FULL treatment across [y0, y1): y0+1, y0+1+pitch, ...
    (one ground-coloured 1 px row every `pitch` px)."""
    pitch = int(pitch)
    if pitch < 2:
        raise ValueError("scanline pitch must be >= 2")
    y = int(math.floor(y0)) + 1
    end = int(math.ceil(y1))
    rows = []
    while y < end:
        rows.append(y)
        y += pitch
    return rows


# ------------------------------------------------------------------ text

def unspace(s, sep=" "):
    """Exact inverse of theme.letter_spaced (e16): for every T,
    unspace(letter_spaced(T, sep), sep) == T.upper(). A string is treated as
    letter-spaced when it has odd length and every odd index holds `sep`; anything
    else is returned unchanged. Only apply it to strings known to come from
    letter_spaced: a plain 'A B' (3 chars, middle space) is indistinguishable and
    reads back as 'AB'."""
    if not isinstance(s, str) or not isinstance(sep, str) or len(sep) != 1:
        return s
    if len(s) % 2 == 1 and all(s[i] == sep for i in range(1, len(s), 2)):
        return s[0::2]
    return s
