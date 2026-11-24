"""kit/glyphs.py - the rail glyphs and the power mark (BUILD_PLAN §1.3).

ONLY these: the 20x20 rail glyphs HOME (FRAME: square + 3 px top band), CHATS (two tags),
PROJ (three bars), CONFIG (two sliders) and POWER (notched octagon + bar), plus the 15x15
power mark a power-down button carries. Rules, flags, segments and the bar mark come from
fx/draw.py (kit/painters.py). Geometry is copied from the approved renderers
(r2/shared_glyphs.glyph / danger_mark, r3/r3_home.glyph; see design archive).

Every orthogonal stroke of FRAME and the slider knobs is an exact-pixel filled rectangle;
the tags, bars and octagon are 1 px lines, exactly as approved. Pure Canvas painters: no
timers, no global state. Each returns the list of item ids it created.
"""

GLYPHS = ("home", "chats", "proj", "config", "power")
POWER_MARK_W = 15        # the button power mark (15x15)


def _tags(tag, kind):
    if tag is None:
        return (kind,)
    if isinstance(tag, (tuple, list)):
        return tuple(tag) + (kind,)
    return (tag, kind)


def glyph(cv, kind, x, y, col, tag="kit"):
    """Draw rail glyph `kind` with its top-left at (x, y) in colour col. -> item ids.
    Unknown kind -> ValueError (never a silent blank cell)."""
    tags = _tags(tag, "glyph")
    ids = []

    def L(*p):
        ids.append(cv.create_line(*[v + (x if i % 2 == 0 else y) for i, v in enumerate(p)],
                                  fill=col, width=1, tags=tags))

    def F(x0, y0, x1, y1):           # filled pixels [x0, x1) x [y0, y1)
        ids.append(cv.create_rectangle(x + x0, y + y0, x + x1, y + y1, fill=col, outline="", tags=tags))

    def B(x0, y0, x1, y1):           # 1 px hollow box over pixels [x0, x1) x [y0, y1)
        F(x0, y0, x1, y0 + 1)
        F(x0, y1 - 1, x1, y1)
        F(x0, y0, x0 + 1, y1)
        F(x1 - 1, y0, x1, y1)

    if kind == "home":               # FRAME: square + 3 px filled top band
        B(1, 1, 20, 20)
        F(1, 1, 20, 4)
    elif kind == "chats":            # two speech tags (approved G-06)
        L(1, 2, 13, 2, 17, 5, 13, 8, 1, 8, 1, 2)
        L(19, 12, 7, 12, 3, 15, 7, 18, 19, 18, 19, 12)
    elif kind == "proj":             # three stacked bars
        for yy in (2, 8, 14):
            L(2, yy, 18, yy, 18, yy + 4, 2, yy + 4, 2, yy)
    elif kind == "config":           # two sliders: line + knob each
        for yy, kx in ((6, 6), (14, 14)):
            L(1, yy, 20, yy)
            F(kx - 1, yy - 2, kx + 2, yy + 3)
    elif kind == "power":            # notched octagon + bar
        L(8, 1, 7, 1, 1, 7, 1, 13, 7, 19, 13, 19, 19, 13, 19, 7, 13, 1, 12, 1)
        L(10, 0, 10, 9)
    else:
        raise ValueError("unknown glyph %r (expected one of %s)" % (kind, ", ".join(GLYPHS)))
    return ids


def power_mark(cv, x, cy, col, tag="kit"):
    """The 15x15 power pictogram a power-down button carries (approved danger_mark,
    power=True): left edge x, centred on row cy. -> (item ids, x after the mark)."""
    tags = _tags(tag, "mark")
    y = int(round(cy - 7))
    pts = (5, 0, 4, 0, 0, 4, 0, 10, 4, 14, 10, 14, 14, 10, 14, 4, 10, 0, 9, 0)
    ids = [cv.create_line(*[v + (x if i % 2 == 0 else y) for i, v in enumerate(pts)], fill=col, width=1,
                          tags=tags),
           cv.create_rectangle(x + 7, y - 1, x + 8, y + 7, fill=col, outline="", tags=tags)]
    return ids, x + POWER_MARK_W
