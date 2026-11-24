"""desktop/kit - the static widget kit of the approved look (BUILD_PLAN §1.3, B1).

The kit is the LOOK, not an effect: it works under the kill switch (D-31, LOCALAI_FX=0 /
data/fx_disabled) and on a bare tk.Tk() with no fx installed. It never schedules a timer
(no after(); hover tips use enter/leave bindings), never touches Tk off the Tk thread and
does no I/O.

    ensure_fonts(root)   -> {size: font name}   lazy display-font setup (bare test roots work)
    tokens(theme)        -> dict                 every colour token the kit paints with
    scale_factor(widget) -> int                  integer pixel factor for drawn images (1 at 100 %)
    geom_ratio(widget) -> float, px(widget, n)   real display ratio (>= 1.0) for canvas geometry
    display_font(widget, size) -> font           named display font, or its Consolas fallback

Modules: widgets (KitButton, TextAction, lamp_images / lamp_check, Segmented, Well, SectionRow,
StatusLine, EmptyState), flag (StatusFlag), notice (ascii_box), legend, header, glyphs,
painters (the kept fx/draw.py painters + geometry helpers the kit may use) and
ttk_controls (install_ttk).

Override rule (X-1): every kit class that overrides configure / cget re-binds
``config = configure`` and ``__getitem__ = cget`` after the override (tkinter binds both at
class-definition time in Misc), accepts the dict form Misc.__setitem__ passes, accepts
mixed calls such as .config(state=..., text=...) and passes non-text options through.
"""

from ..fx import fonts as _fonts
from ..fx import palette as _palette

__all__ = ["ensure_fonts", "tokens", "scale_factor", "geom_ratio", "px", "display_font", "TOKEN_KEYS",
           "C7", "C8", "C8B", "C9", "C9B", "C10", "C10B", "C11", "C11B", "C13B"]

# The Consolas set every kit painter uses (numerals and small text are always Consolas).
C7 = ("Consolas", 7)
C8 = ("Consolas", 8)
C8B = ("Consolas", 8, "bold")
C9 = ("Consolas", 9)
C9B = ("Consolas", 9, "bold")
C10 = ("Consolas", 10)
C10B = ("Consolas", 10, "bold")
C11 = ("Consolas", 11)
C11B = ("Consolas", 11, "bold")
C13B = ("Consolas", 13, "bold")

TOKEN_KEYS = ("bg", "panel", "panel_hover", "bright", "dim", "value", "ghost", "warn_eff", "unlit",
              "rule_out", "rule_in", "bloom_out", "bloom_in", "ok", "danger", "warn")

_FONTS_ATTR = "_kit_fonts_ready"
_BASE_SCALING = 96.0 / 72.0


def _root_of(widget):
    try:
        return widget._root()
    except Exception:  # noqa: BLE001
        return widget


def ensure_fonts(root):
    """Normalise Tk's default named fonts and create the fxDisplay* faces on root's
    interpreter, once. Lazy and idempotent, so a kit widget on a bare tk.Tk() (a test
    root, no MainWindow, no fx) still finds its fonts. Keeps a face already applied
    (MainWindow / fx.install choose it); otherwise the default face. Never raises:
    returns {} when Tk refuses."""
    r = _root_of(root)
    try:
        if getattr(r, _FONTS_ATTR, False):
            return {s: _fonts.display(s) for s in _fonts.SIZES}
        face = _fonts.current_face(r) or _fonts.DEFAULT_FACE
        _fonts.normalise_default_fonts(r)
        names = _fonts.ensure(r, face)
        try:
            setattr(r, _FONTS_ATTR, True)
        except Exception:  # noqa: BLE001
            pass
        return names
    except Exception:  # noqa: BLE001 - fonts must never stop a widget being built
        return {}


def display_font(widget, size="M"):
    """The named display font fxDisplay<size> when it exists on widget's interpreter,
    else its Consolas fallback tuple (fonts.FALLBACK). Words and titles only."""
    name = _fonts.display(size)
    try:
        widget.tk.call("font", "configure", name, "-size")
        return name
    except Exception:  # noqa: BLE001
        return _fonts.FALLBACK[name[len(_fonts.NAME_PREFIX):]]


def tokens(theme):
    """Every colour token as a dict. Base: palette.tones(theme.phosphor_name) (unknown or
    missing -> amber). Every token the theme object itself carries (a real Theme, a test
    stub with only bright/dim/bg, or a plain dict) wins over the base, so a caller's own
    colours are honoured and a stub missing the B1 tiers still gets them."""
    if isinstance(theme, dict):
        get = theme.get
        name = theme.get("phosphor_name", theme.get("preset"))
    else:
        def get(key, default=None):
            return getattr(theme, key, default)
        name = getattr(theme, "phosphor_name", None)
    t = _palette.tones(name)
    t["warn"] = _palette.WARN
    for key in TOKEN_KEYS:
        v = get(key, None)
        if isinstance(v, str) and v:
            t[key] = v
    return t


def scale_factor(widget):
    """Integer pixel factor for kit-drawn images: round(tk scaling / (96/72)), at least 1.
    1 at 100 %, 2 at 200 %. Images stay crisp (whole pixels only)."""
    try:
        s = float(widget.tk.call("tk", "scaling"))
    except Exception:  # noqa: BLE001
        return 1
    return max(1, int(round(s / _BASE_SCALING)))


def geom_ratio(widget):
    """Real (non-integer) display ratio for kit canvas geometry: tk scaling / (96/72), never
    below 1.0. Fonts are in points and grow with tk scaling, so the fixed-pixel bands they sit
    in (flag height, header bands, legend row, paddings) grow by the same ratio. Exactly 1.0
    at 100 % (and in the DPI-unaware app today), so the approved geometry is unchanged there."""
    try:
        s = float(widget.tk.call("tk", "scaling"))
    except Exception:  # noqa: BLE001
        return 1.0
    return max(1.0, s / _BASE_SCALING)


def px(widget_or_ratio, n):
    """n approved pixels at widget's display ratio (geom_ratio), as an int. A float ratio
    may be passed instead of a widget."""
    r = widget_or_ratio if isinstance(widget_or_ratio, (int, float)) else geom_ratio(widget_or_ratio)
    return int(round(n * r))
