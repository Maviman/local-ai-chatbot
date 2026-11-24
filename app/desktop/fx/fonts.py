"""fx/fonts.py - display-face named fonts + one-time default-font normalisation
(spec G-03a/b / FD-4b, tokens §5.3).

Font policy: Consolas for everything read, typed or counted; one optional display
face (Bahnschrift SemiBold Condensed, "INDUSTRIAL") for titles, plate words and rail
labels only, falling back to the Consolas set ("TERMINAL") when the family is missing.

Nothing here touches Tk at import. Every function takes a live root (any widget works;
its interpreter is used). Named fonts are created with raw `font create` calls, never
through tkinter.font.Font wrappers (a wrapper that created a font deletes it again when
it is garbage-collected).
"""

import tkinter

FONT_NUMERIC = ("Consolas", 13, "bold")
FONT_NUMERIC_S = ("Consolas", 9, "bold")

DISPLAY_FAMILY = "Bahnschrift SemiBold Condensed"
DISPLAY_WEIGHT = "normal"  # the family itself is the SemiBold cut
DISPLAY_SIZES = {"XL": 22, "L": 15, "M": 12, "S": 10, "XS": 8}   # Bahnschrift SemiBold Condensed
FALLBACK = {"XL": ("Consolas", 15, "bold"), "L": ("Consolas", 13, "bold"), "M": ("Consolas", 10, "bold"),
            "S": ("Consolas", 9, "bold"), "XS": ("Consolas", 7, "normal")}
SIZES = ("XL", "L", "M", "S", "XS")
FACES = ("industrial", "terminal")
DEFAULT_FACE = "industrial"
NAME_PREFIX = "fxDisplay"

# §5.3 FONT_DEFAULTS: Tk's named default fonts -> Consolas (fixes the Segoe UI leaks of
# every widget created without an explicit font).
FONT_DEFAULTS = {
    "TkDefaultFont": ("Consolas", 9, "normal"),
    "TkMenuFont": ("Consolas", 9, "normal"),
    "TkCaptionFont": ("Consolas", 9, "normal"),
    "TkTooltipFont": ("Consolas", 9, "normal"),
    "TkTextFont": ("Consolas", 10, "normal"),
    "TkHeadingFont": ("Consolas", 9, "bold"),
    "TkSmallCaptionFont": ("Consolas", 8, "normal"),
}

_FAMILIES_ATTR = "_fx_font_families"
_FACE_ATTR = "_fx_display_face"
_MENU_OPT_ATTR = "_fx_menu_font_option"


def display(size):
    """Named-font name for a display size, e.g. display('M') -> 'fxDisplayM'
    (usable as font=...). Raises ValueError for an unknown size."""
    key = size.upper() if isinstance(size, str) else None
    if key not in DISPLAY_SIZES:
        raise ValueError("unknown display size %r (expected one of %s)" % (size, ", ".join(SIZES)))
    return NAME_PREFIX + key


def _tk_root(widget):
    return widget._root() if hasattr(widget, "_root") else widget


def _families(root, families=None):
    if families is not None:
        return frozenset(families)
    r = _tk_root(root)
    cached = getattr(r, _FAMILIES_ATTR, None)
    if cached is None:
        cached = frozenset(r.tk.splitlist(r.tk.call("font", "families")))
        try:
            setattr(r, _FAMILIES_ATTR, cached)
        except Exception:
            pass
    return cached


def has_family(root, family, families=None):
    """True if `family` is installed for this interpreter (tkfont.families(root)).
    Tk silently maps unknown families to Arial, so this check is the only guard.
    `families` injects a list (tests). Case-insensitive, like Tk's own matching."""
    if not isinstance(family, str) or not family:
        return False
    fams = _families(root, families)
    if family in fams:
        return True
    low = family.lower()
    return any(str(f).lower() == low for f in fams)


def _norm_face(face):
    if isinstance(face, str) and face.strip().lower() in FACES:
        return face.strip().lower()
    return DEFAULT_FACE


def resolve(face, size, root=None, families=None):
    """(family, size, weight) the named font for `size` gets under `face`. `size` is
    accepted exactly as display() accepts it (case-insensitive); unknown -> ValueError."""
    size = display(size)[len(NAME_PREFIX):]
    face = _norm_face(face)
    if (face == "industrial" and (root is not None or families is not None)
            and has_family(root, DISPLAY_FAMILY, families)):
        return (DISPLAY_FAMILY, DISPLAY_SIZES[size], DISPLAY_WEIGHT)
    return FALLBACK[size]


def _configured(tk, name):
    """Current -family/-size/-weight of a named font, or None if it does not exist."""
    try:
        opts = tk.splitlist(tk.call("font", "configure", name))
    except tkinter.TclError:
        return None
    d = dict(zip(opts[0::2], opts[1::2]))
    return (str(d.get("-family", "")), str(d.get("-size", "")), str(d.get("-weight", "")))


def ensure(root, face=DEFAULT_FACE, *, families=None):
    """Create (or reconfigure) fxDisplayXL..XS on root's interpreter for `face`
    ('industrial' | 'terminal'; anything else -> industrial). Idempotent; a font whose
    configuration already matches is not touched. Returns {size: font name}."""
    r = _tk_root(root)
    tk = r.tk
    face = _norm_face(face)
    names = {}
    for s in SIZES:
        name = display(s)
        fam, size, weight = resolve(face, s, r, families)
        cur = _configured(tk, name)
        if cur is None:
            tk.call("font", "create", name, "-family", fam, "-size", size, "-weight", weight)
        elif cur != (fam, str(size), weight):
            tk.call("font", "configure", name, "-family", fam, "-size", size, "-weight", weight)
        names[s] = name
    try:
        setattr(r, _FACE_ATTR, face)
    except Exception:
        pass
    return names


def apply_face(root, face, *, families=None):
    """Live switch of the display face (subscribed to fx.display_face). Every widget
    using a fxDisplay* font re-lays-out through Tk's named-font mechanism. A destroyed
    root is ignored (no exception)."""
    try:
        ensure(root, face, families=families)
    except tkinter.TclError:
        return


def current_face(root):
    """The face last applied on this root ('industrial' | 'terminal'), or None."""
    return getattr(_tk_root(root), _FACE_ATTR, None)


def display_is_terminal(root):
    """True when the display face DRAWN on root is the Consolas set: the TERMINAL face chosen on purpose, or the
    INDUSTRIAL family (Bahnschrift) missing on this machine so the named fonts fell back. B11-H10: CRT FULL slices
    only a title drawn in Bahnschrift - Consolas 15 bold loses the bottom ink row of O / C / G / J / U under the
    3 px scanlines, whichever way it got there - so the glass caps FULL to LOW here. Read from the named XL font's
    family (what is drawn); before the named fonts exist, from the face and the installed families. Never raises."""
    try:
        r = _tk_root(root)
        cur = _configured(r.tk, display("XL"))
        if cur is not None:
            return str(cur[0]).lower() != DISPLAY_FAMILY.lower()
        return _norm_face(current_face(r)) == "terminal" or not has_family(r, DISPLAY_FAMILY)
    except Exception:  # noqa: BLE001
        return False


def normalise_default_fonts(root):
    """§5.3 FONT_DEFAULTS, once per interpreter (called by fx.install before screens
    exist). Each `font configure` is guarded by comparing the current configuration, so
    a re-call changes nothing and costs well under 1 ms. A missing named font is skipped.
    Returns the number of fonts changed.

    On Windows a font-less tk.Menu (and tk.OptionMenu's menu) defaults to the literal
    '{Segoe UI} 9', not TkMenuFont, so the TkMenuFont entry alone would not reach menus.
    An option-database entry *Menu.font -> TkMenuFont at widgetDefault priority (the
    lowest, so any explicit font= or app option still wins) closes that gap; it is added
    once per interpreter and is not counted as a font change."""
    r = _tk_root(root)
    tk = r.tk
    if not getattr(r, _MENU_OPT_ATTR, False):
        try:
            tk.call("option", "add", "*Menu.font", "TkMenuFont", "widgetDefault")
            setattr(r, _MENU_OPT_ATTR, True)
        except (tkinter.TclError, AttributeError):
            pass
    changed = 0
    for name, (fam, size, weight) in FONT_DEFAULTS.items():
        cur = _configured(tk, name)
        if cur is None:
            continue
        if cur == (fam, str(size), weight):
            continue
        try:
            tk.call("font", "configure", name, "-family", fam, "-size", size, "-weight", weight)
        except tkinter.TclError:
            continue
        changed += 1
    return changed
