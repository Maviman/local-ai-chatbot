"""Numbered-grid pattern shared by Home's directory, Projects' grid, and
Settings' category column - the literal same shape three times over (boxed
item, numbered index that doubles as its digit shortcut, dim caption
beneath, a selected/primary solid-fill state distinct from a dashed-outline
keyboard-focus state). One implementation; each screen is a thin data
source (see home_screen.py / projects_screen.py / settings_screen.py).

style="home" (B4, D-18; only Home uses it): the enlarged r3 option boxes of kit.home.OptionCell in a
plain grid that FILLS the frame - no scroll area. Box rows share the height through grid weights
(each at least kit.home.MIN_BOX_H = 62 px), the row gaps (17 px at 1100x800) take a small share, the box
columns share the width equally between fixed index gutters (54 px, then 53 px between columns),
and the fonts stay fixed. Keyboard focus is a solid 2 px bright edge plus an inverse index chip,
visible on the primary box as well. self.boxes / self.titles / self.cells follow every re-render.
Groups are not drawn in this style (Home has none). If kit.home cannot be imported, the default
style stands in. Projects, Settings and Image uplink keep the default style.
"""

import tkinter as tk

from .. import theme as theme_mod
from .base import Screen, make_scrollable

try:  # B4: the Home style's option boxes (a guarded import: the default style stands in without it)
    from ..kit import geom_ratio as _kit_geom_ratio
    from ..kit import home as _kit_home
except Exception:  # noqa: BLE001
    _kit_home = None

BOX_MIN_WIDTH = 200
BOX_HEIGHT = 64


class DirectoryItem:
    """One numbered box. `group` is an optional header string - items are
    grouped for display but numbered continuously across the whole screen
    (bare-digit quick-select ignores group boundaries)."""

    def __init__(self, label, caption="", on_activate=None, is_primary=False, group=None, group_key=None):
        self.label = label
        self.caption = caption
        self.on_activate = on_activate
        self.is_primary = is_primary
        self.group = group
        # what tells one group from another (its id; the name when not given): two groups with the same name are
        # two groups, and a group named like the ungrouped block is not that block (B5 ADV1-08)
        self.group_key = group_key if group_key is not None else group


class DirectoryScreen(Screen):
    def __init__(self, parent, ctx, columns=1, style="default"):
        super().__init__(parent, ctx)
        self.columns = max(1, columns)
        self._items = []
        self.style = "home" if (style == "home" and _kit_home is not None) else "default"
        # the rendered option boxes / their title widgets / OptionCells, in item order (Home style;
        # empty in the default style); replaced on every re-render, so read them, don't keep them
        self.boxes = []
        self.titles = []
        self.cells = []
        self._grid_rows = 0

        if self.style == "home":
            self.scroll_container = self      # no scroll area: the grid fills this frame
        else:
            outer, inner = make_scrollable(self, ctx.theme)
            outer.pack(fill="both", expand=True, padx=16, pady=16)
            self.scroll_container = inner

    def set_items(self, items):
        """items: flat list of DirectoryItem. Adjacent items sharing the
        same .group render under one header; numbering stays continuous
        across the whole list regardless of grouping."""
        self._items = items
        self._render()
        callbacks = [item.on_activate or (lambda: None) for item in items]
        self.bind_digits(callbacks)

    def _render(self):
        if self.style == "home":
            self._render_home()
            return
        for child in self.scroll_container.winfo_children():
            child.destroy()
        theme = self.ctx.theme

        _unset = object()
        current_group = _unset
        row = None
        col = 0
        for i, item in enumerate(self._items):
            key = getattr(item, "group_key", item.group)
            if key != current_group:
                current_group = key
                if item.group:
                    tk.Label(
                        self.scroll_container, text=item.group.upper(),     # B15: upper case, no letter-spacing
                        bg=theme.bg, fg=theme.dim, font=theme_mod.FONT_LABEL,
                    ).pack(anchor="w", pady=(10 if i else 0, 4))
                row = None
                col = 0
            if col == 0:
                row = tk.Frame(self.scroll_container, bg=theme.bg)
                row.pack(fill="x")
            cell = self._make_cell(row, i + 1, item)
            cell.pack(side="left", fill="both", expand=True, padx=4, pady=4)
            col = (col + 1) % self.columns

    def _render_home(self):
        """The B4 Home grid: per column an index gutter + a box column; per row a box row (weight 10,
        at least 62 px) and, between rows, a gap row (weight 1, at least 6 px). At 1100x800 that is
        the approved 170 px rows 17 px apart; at the 820x560 floor every box still shows whole."""
        for child in self.winfo_children():
            child.destroy()
        self.boxes, self.titles, self.cells = [], [], []
        theme = self.ctx.theme
        r = _kit_geom_ratio(self)

        def s(n):
            return int(round(n * r))
        cols = self.columns
        n = len(self._items)
        nrows = (n + cols - 1) // cols
        for c in range(cols):
            self.columnconfigure(2 * c, minsize=s(_kit_home.INDEX_COL_FIRST if c == 0 else _kit_home.INDEX_COL),
                                 weight=0, uniform="")
            self.columnconfigure(2 * c + 1, minsize=0, weight=1, uniform="homebox")
        for rr in range(max(2 * nrows - 1, self._grid_rows)):
            if rr >= 2 * nrows - 1:
                self.rowconfigure(rr, minsize=0, weight=0)          # rows left over from a longer list
            elif rr % 2 == 0:
                self.rowconfigure(rr, minsize=s(_kit_home.MIN_BOX_H), weight=10)
            else:
                self.rowconfigure(rr, minsize=s(6), weight=1)
        self._grid_rows = max(0, 2 * nrows - 1)
        for i, item in enumerate(self._items):
            row, col = divmod(i, cols)
            cell = _kit_home.OptionCell(
                self, theme, i + 1, item.label, item.caption, primary=item.is_primary,
                on_activate=item.on_activate,
                index_width=_kit_home.INDEX_COL_FIRST if col == 0 else _kit_home.INDEX_COL,
            )
            cell.index.grid(row=2 * row, column=2 * col, sticky="ns")
            cell.box.grid(row=2 * row, column=2 * col + 1, sticky="nsew")
            self.cells.append(cell)
            self.boxes.append(cell.box)
            self.titles.append(cell.title)

    def _make_cell(self, parent, number, item):
        # Index number lives outside the box (its own Label, left of the
        # canvas) rather than drawn inside it - the approved look. grid
        # (not pack) keeps the number top-aligned with the box regardless
        # of the box's own height.
        theme = self.ctx.theme
        cell = tk.Frame(parent, bg=theme.bg)
        cell.columnconfigure(1, weight=1)
        tk.Label(
            cell, text=f"{number:02d}", bg=theme.bg, fg=theme.dim, font=theme_mod.FONT_INDEX,
        ).grid(row=0, column=0, sticky="n", padx=(0, 8), pady=(12, 0))
        box = self._make_box(cell, item)
        box.grid(row=0, column=1, sticky="nsew")
        return cell

    def _make_box(self, parent, item):
        theme = self.ctx.theme
        canvas = tk.Canvas(parent, height=BOX_HEIGHT, bg=theme.bg, highlightthickness=0, takefocus=1)
        state = {"focused": False}

        def redraw(_event=None):
            canvas.delete("box")
            w = canvas.winfo_width() or BOX_MIN_WIDTH
            h = BOX_HEIGHT
            if item.is_primary:
                canvas.create_rectangle(1, 1, w - 1, h - 1, fill=theme.bright, outline=theme.bright, tags="box")
                text_color = theme.bg
                dim_color = theme.bg
            else:
                canvas.create_rectangle(1, 1, w - 1, h - 1, fill=theme.panel, outline=theme.dim, tags="box")
                text_color = theme.text
                dim_color = theme.dim
            if state["focused"]:
                # Drawn as a dashed ring distinct from the solid fill above,
                # so a keyboard user can see focus move independently of
                # what's currently selected - even on the same box at once.
                canvas.create_rectangle(3, 3, w - 3, h - 3, outline=theme.bright, width=2, dash=(4, 2), tags="box")
            canvas.create_text(12, 12, anchor="nw", text=item.label, fill=text_color, font=theme_mod.FONT_BODY_BOLD, tags="box")
            if item.caption:
                canvas.create_text(
                    12, 36, anchor="nw", text=item.caption, fill=dim_color, font=theme_mod.FONT_CAPTION,
                    width=max(w - 24, 40), tags="box",
                )

        def activate(_event=None):
            if item.on_activate:
                item.on_activate()

        def focus_in(_event=None):
            state["focused"] = True
            redraw()

        def focus_out(_event=None):
            state["focused"] = False
            redraw()

        canvas.bind("<Configure>", redraw)
        canvas.bind("<FocusIn>", focus_in)
        canvas.bind("<FocusOut>", focus_out)
        canvas.bind("<Return>", activate)
        canvas.bind("<space>", activate)
        canvas.bind("<Button-1>", lambda e: (canvas.focus_set(), activate()))

        return canvas
