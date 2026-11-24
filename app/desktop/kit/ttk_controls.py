"""kit/ttk_controls.py - the approved ttk controls (BUILD_PLAN §1.3; ST-06).

    install_ttk(style, theme)

Called by screens/base.apply_ttk_theme after the flat chrome, on the clam theme, inside a
guarded try (any failure leaves the base look). It restyles the STOCK style names the app
already uses, so no screen changes for B1:

    Horizontal.TScale              ONE 2 px dim line + a solid 8x18 bright thumb; keyboard
                                   focus grows the thumb to 12x24 (no ring, no box); disabled =
                                   ghost line + a 1 px dim outline thumb (r2_slider_pair.png,
                                   r2kit.slider)
    Phosphor.Vertical.TScrollbar   thin: a 4 px dim thumb on the ground, no trough, no arrows;
    (and Vertical/Horizontal.TScrollbar)  value while hovered / dragged (r2_settings_general.png)
    TCombobox                      flat panel field, 1 px dim edge (bright on focus), value
                                   arrow; the drop-down list in the same inks
    Horizontal.TProgressbar        bright on unlit, no bevel
    Phosphor.Treeview              flat panel rows, flat heading, bright selection
    TNotebook / TNotebook.Tab      tabs as words: value, the selected one bright with a 2 px
                                   bright underbar; no tab boxes, no pane border
                                   (r2_projects_manage.png)

Element images are named Tcl photo images on the style's own interpreter (kit_*), so they
live as long as the interpreter, are shared by every widget, and a re-install with another
palette repaints them in place. Sizes follow the integer kit scale factor (crisp at 200 %).
Static: no timers.
"""

import tkinter as tk

from . import scale_factor, tokens

IMG_PREFIX = "kit_ttk_"
THUMB_W, THUMB_H = 8, 18          # slider thumb (rest)
FOCUS_W, FOCUS_H = 12, 24         # slider thumb (keyboard focus)
LINE_H = 2                        # the slider line
SB_W, SB_THUMB_W = 8, 4           # scrollbar width / its thumb (centred)
TAB_BAR_H = 2                     # the selected tab's underbar


def _img(tkapp, name, w, h):
    """(Re)create the named photo image `name` (blank, w x h) on this interpreter."""
    full = IMG_PREFIX + name
    tkapp.call("image", "create", "photo", full, "-width", w, "-height", h)
    tkapp.call(full, "blank")
    return full


def _fill(tkapp, img, col, x0, y0, x1, y1):
    if x1 > x0 and y1 > y0:
        tkapp.call(img, "put", col, "-to", x0, y0, x1, y1)


def _box(tkapp, img, col, x0, y0, x1, y1, w=1):
    _fill(tkapp, img, col, x0, y0, x1, y0 + w)
    _fill(tkapp, img, col, x0, y1 - w, x1, y1)
    _fill(tkapp, img, col, x0, y0, x0 + w, y1)
    _fill(tkapp, img, col, x1 - w, y0, x1, y1)


def _element(style, name, *args, **kw):
    """element create, once per theme: a re-install reuses the element (its images are
    repainted in place by name)."""
    try:
        if name in style.element_names():
            return name
    except tk.TclError:
        pass
    style.element_create(name, *args, **kw)
    return name


# Stretchable element images are built TILE px long: ttk tiles an image element across
# its length, so a 4 px image meant hundreds of tiles per slider/scrollbar and stalled
# Settings (0.15 s -> 1.17 s to open General). 512 px gives a handful of tiles.
TILE = 512


def _slider(style, t, f):
    tkapp = style.tk
    bw, bh = FOCUS_W * f, FOCUS_H * f          # every thumb image has the focus footprint
    tw, th = THUMB_W * f, THUMB_H * f
    ox, oy = (bw - tw) // 2, (bh - th) // 2
    rest = _img(tkapp, "slider", bw, bh)
    _fill(tkapp, rest, t["bright"], ox, oy, ox + tw, oy + th)
    focus = _img(tkapp, "slider_focus", bw, bh)
    _fill(tkapp, focus, t["bright"], 0, 0, bw, bh)
    dis = _img(tkapp, "slider_disabled", bw, bh)
    _box(tkapp, dis, t["dim"], ox, oy, ox + tw, oy + th, f)
    # The trough: opaque ground with the 2 px line through the middle, uniform across x so any
    # stretch keeps it exact.
    lh = LINE_H * f
    ly = (bh - lh) // 2
    line = _img(tkapp, "trough", TILE, bh)
    _fill(tkapp, line, t["bg"], 0, 0, TILE, bh)
    _fill(tkapp, line, t["dim"], 0, ly, TILE, ly + lh)
    line_dis = _img(tkapp, "trough_disabled", TILE, bh)
    _fill(tkapp, line_dis, t["bg"], 0, 0, TILE, bh)
    _fill(tkapp, line_dis, t["ghost"], 0, ly, TILE, ly + lh)
    _element(style, "Kit.Horizontal.Scale.trough", "image", line, ("disabled", line_dis),
             border=(1, 0), sticky="ew", width=4)
    _element(style, "Kit.Horizontal.Scale.slider", "image", rest, ("disabled", dis), ("focus", focus),
             sticky="")
    style.layout("Horizontal.TScale", [
        ("Kit.Horizontal.Scale.trough", {"sticky": "nswe", "children": [
            ("Kit.Horizontal.Scale.slider", {"side": "left", "sticky": ""})]})])
    style.configure("Horizontal.TScale", background=t["bg"], troughcolor=t["bg"], borderwidth=0)


def _scrollbars(style, t, f):
    tkapp = style.tk
    w, tw = SB_W * f, SB_THUMB_W * f
    ox = (w - tw) // 2
    trough = _img(tkapp, "sb_trough", w, TILE)
    _fill(tkapp, trough, t["bg"], 0, 0, w, TILE)
    thumb = _img(tkapp, "sb_thumb", w, TILE)
    _fill(tkapp, thumb, t["bg"], 0, 0, w, TILE)
    _fill(tkapp, thumb, t["dim"], ox, 0, ox + tw, TILE)
    hot = _img(tkapp, "sb_thumb_hot", w, TILE)
    _fill(tkapp, hot, t["bg"], 0, 0, w, TILE)
    _fill(tkapp, hot, t["value"], ox, 0, ox + tw, TILE)
    _element(style, "Kit.Vertical.Scrollbar.trough", "image", trough, border=(0, 1), sticky="ns", height=4)
    _element(style, "Kit.Vertical.Scrollbar.thumb", "image", thumb, ("pressed", hot), ("active", hot),
             border=(0, 1), sticky="nswe", height=4)
    layout = [("Kit.Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
        ("Kit.Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})]
    for name in ("Phosphor.Vertical.TScrollbar", "Vertical.TScrollbar"):
        style.layout(name, layout)
        style.configure(name, background=t["bg"], troughcolor=t["bg"], borderwidth=0)
    # horizontal twin (no app use today; keeps a stray horizontal bar in the same look)
    htrough = _img(tkapp, "sbh_trough", TILE, w)
    _fill(tkapp, htrough, t["bg"], 0, 0, TILE, w)
    hthumb = _img(tkapp, "sbh_thumb", TILE, w)
    _fill(tkapp, hthumb, t["bg"], 0, 0, TILE, w)
    _fill(tkapp, hthumb, t["dim"], 0, ox, TILE, ox + tw)
    hhot = _img(tkapp, "sbh_thumb_hot", TILE, w)
    _fill(tkapp, hhot, t["bg"], 0, 0, TILE, w)
    _fill(tkapp, hhot, t["value"], 0, ox, TILE, ox + tw)
    _element(style, "Kit.Horizontal.Scrollbar.trough", "image", htrough, border=(1, 0), sticky="ew", width=4)
    _element(style, "Kit.Horizontal.Scrollbar.thumb", "image", hthumb, ("pressed", hhot), ("active", hhot),
             border=(1, 0), sticky="nswe", width=4)
    style.layout("Horizontal.TScrollbar", [("Kit.Horizontal.Scrollbar.trough", {"sticky": "ew", "children": [
        ("Kit.Horizontal.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
    style.configure("Horizontal.TScrollbar", background=t["bg"], troughcolor=t["bg"], borderwidth=0)


def _combobox(style, t):
    style.configure("TCombobox", fieldbackground=t["panel"], background=t["panel"], foreground=t["bright"],
                    bordercolor=t["dim"], arrowcolor=t["value"], lightcolor=t["panel"], darkcolor=t["panel"],
                    selectbackground=t["panel"], selectforeground=t["bright"], insertcolor=t["bright"],
                    relief="flat", padding=(6, 3))
    style.map("TCombobox",
              fieldbackground=[("readonly", t["panel"]), ("disabled", t["panel"])],
              foreground=[("disabled", t["dim"]), ("readonly", t["bright"])],
              background=[("active", t["panel"]), ("pressed", t["panel"]), ("readonly", t["panel"]),
                          ("disabled", t["panel"])],
              bordercolor=[("disabled", t["ghost"]), ("focus", t["bright"]), ("active", t["bright"])],
              arrowcolor=[("disabled", t["dim"]), ("active", t["bright"]), ("focus", t["bright"])],
              lightcolor=[("focus", t["panel"])], darkcolor=[("focus", t["panel"])],
              selectbackground=[("focus", t["panel"])], selectforeground=[("focus", t["bright"])])
    try:
        root = style.master if style.master is not None else None
        if root is not None:
            for opt, val in (("*TCombobox*Listbox.background", t["panel"]),
                             ("*TCombobox*Listbox.foreground", t["bright"]),
                             ("*TCombobox*Listbox.selectBackground", t["bright"]),
                             ("*TCombobox*Listbox.selectForeground", t["bg"]),
                             ("*TCombobox*Listbox.borderWidth", 0),
                             ("*TCombobox*Listbox.highlightThickness", 0)):
                root.option_add(opt, val, "widgetDefault")
    except tk.TclError:
        pass


def _progressbar(style, t):
    style.configure("Horizontal.TProgressbar", background=t["bright"], troughcolor=t["unlit"],
                    bordercolor=t["unlit"], lightcolor=t["bright"], darkcolor=t["bright"], borderwidth=0)


def _treeview(style, t):
    style.configure("Phosphor.Treeview", background=t["panel"], fieldbackground=t["panel"],
                    foreground=t["bright"], bordercolor=t["dim"], lightcolor=t["panel"], darkcolor=t["panel"],
                    borderwidth=0, relief="flat")
    style.map("Phosphor.Treeview", background=[("selected", t["bright"])], foreground=[("selected", t["bg"])])
    style.configure("Phosphor.Treeview.Heading", background=t["panel"], foreground=t["value"],
                    bordercolor=t["panel"], lightcolor=t["panel"], darkcolor=t["panel"], relief="flat")
    style.map("Phosphor.Treeview.Heading", background=[("active", t["panel"])],
              foreground=[("active", t["bright"])])


def _notebook(style, t, f):
    tkapp = style.tk
    bar_on = _img(tkapp, "tab_bar_on", TILE, TAB_BAR_H * f)
    _fill(tkapp, bar_on, t["bright"], 0, 0, TILE, TAB_BAR_H * f)
    bar_off = _img(tkapp, "tab_bar_off", TILE, TAB_BAR_H * f)
    _fill(tkapp, bar_off, t["bg"], 0, 0, TILE, TAB_BAR_H * f)
    _element(style, "Kit.Notebook.underbar", "image", bar_off, ("selected", bar_on), border=(1, 0), sticky="ew", width=4)
    style.layout("TNotebook.Tab", [
        ("Notebook.tab", {"sticky": "nswe", "children": [
            ("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [
                ("Notebook.label", {"side": "top", "sticky": ""}),
                ("Kit.Notebook.underbar", {"side": "top", "sticky": "ew"})]})]})])
    pad = (0, 2, 25, 2)          # 25 px between words; the underbar spans the word only
    style.configure("TNotebook", background=t["bg"], bordercolor=t["bg"], lightcolor=t["bg"],
                    darkcolor=t["bg"], borderwidth=0, tabmargins=(0, 0, 0, 8), tabposition="nw")
    style.configure("TNotebook.Tab", background=t["bg"], foreground=t["value"], bordercolor=t["bg"],
                    lightcolor=t["bg"], darkcolor=t["bg"], borderwidth=0, padding=pad, focuscolor=t["bg"])
    style.map("TNotebook.Tab",
              background=[("selected", t["bg"]), ("active", t["bg"])],
              foreground=[("selected", t["bright"]), ("active", t["bright"]), ("disabled", t["dim"])],
              lightcolor=[("selected", t["bg"])], darkcolor=[("selected", t["bg"])],
              bordercolor=[("selected", t["bg"])],
              padding=[("selected", pad)], expand=[("selected", (0, 0, 0, 0))])


def install_ttk(style, theme):
    """Install the approved ttk controls on `style` (a ttk.Style on the clam theme) from
    `theme` (a Theme, a stub carrying only some tokens, or a tones dict). Idempotent; a
    re-install with another palette repaints the element images in place. Raises on a Tk
    refusal: the caller (base.apply_ttk_theme) keeps its base look then."""
    t = tokens(theme)
    f = scale_factor(style.master) if getattr(style, "master", None) is not None else 1
    _slider(style, t, f)
    _scrollbars(style, t, f)
    _combobox(style, t)
    _progressbar(style, t)
    _treeview(style, t)
    _notebook(style, t, f)
    return True
