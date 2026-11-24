"""kit/painters.py - the ONLY fx/draw.py painters and fx/geometry.py helpers the kit uses
(BUILD_PLAN §1.3, X-27).

These are the kept, pure painters: functions of (canvas, tones, ...) that schedule nothing
and read no fx state, so they work under NullFX and the kill switch. Every one is on the
GAP_FX §5 "Keep" list, so the B15 cleanup cannot remove them. tests/fx/test_kit.py calls
each of them on a bare tk.Tk() with fx.NULL; tests/fx/test_no_banned_devices.py checks that
the banned devices (chamfer, brackets, hazard, chevron, alert plate, contour emblem,
registration cross, preview card, Backdrop) are defined nowhere: B15 removed them, together
with the R1 status-flag painter. kit/flag.py draws the approved flag (r3_home.status_flag)
from geometry.flag_poly.
"""

from ..fx.draw import (  # noqa: F401 - re-exported for B2+ screens
    draw_bar_mark,
    draw_double_rule,
    draw_label_in_gap_rule,
    draw_segments,
    rect_px,
)
from ..fx.geometry import (  # noqa: F401
    band,
    dot_leader_count,
    flag_poly,
    segment_rects,
    segments_filled,
    tick_x,
    unspace,
)

PAINTERS = ("rect_px", "draw_double_rule", "draw_label_in_gap_rule", "draw_segments", "draw_bar_mark")
HELPERS = ("unspace", "flag_poly", "band", "tick_x", "segments_filled", "segment_rects", "dot_leader_count")

__all__ = list(PAINTERS + HELPERS)
