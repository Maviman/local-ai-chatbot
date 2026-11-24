"""Unboxed numbered lists (B5, approved r2 Chats / Projects): an optional search
well with filter words beside it, then plain rows with numeric index hints for
the same bare-digit quick-select every other screen uses.

Row grammar (approved shared_glyphs.list_row): no box at rest; index C11B
`value` anchored right of the row (the 42 px index column), title C11 `bright`,
an optional tail C9 `value` at the right end (Chats: the updated time; Projects:
"2 chats"). Pitch 36 px, rows 32 px tall. Hover = a 1 px bright outline; keyboard
focus = a solid 2 px bright outline plus the index drawn as an inverse chip.
kind="heading" rows (Projects' group names) are plain C9 `value` text: not
focusable, no bindings, never counted by the digits.

The widget tree of a row is kept as before (tests and the harness walk it):
    row_container -> cell -> [index Label, row Frame -> text_col -> [title, tail]]
Every row event except the context menu goes through bind tags bound ONCE per
list (Tcl scripts calling one registered command), so drawing a row registers no
Python callbacks; a row with context items still carries its own Button-3 /
Shift-F10 / App bindings (test_shared_menu reads them from the row widget).

The search placeholder ("Search", `dim`) is an overlay Label over the Entry,
never inserted text, so it can never reach the search callback. The filter
chips are words (selected = bright + a 2 px underbar): one line beside the well
when they fit, otherwise 2-4 full-width lines under it (as many as leave the
list KEEP_ROWS rows), and
the folders that still do not fit go behind a last word "+N MORE" that opens
them in the shared menu (the selected word always stays on show); chip_row's
children are the chips only, in order. ProjectList adapts Projects'
DirectoryItem list to rows.

A long list is drawn in pages of ROW_PAGE rows. Page 0 is row_container (the
scroll area's own inner frame); every later page is its own Frame in its own
canvas window item at its model y, so no Tk window coordinate grows with the
list (Windows clips window coordinates at 32767 px) and a page lays out only its
own cells. Every cell has a fixed height, so the model y is exact; only the
pages near the view are drawn, in any order (a jump to the end draws the end),
and a page far from the view is parked (hidden, kept, so the view can come back
to it in a few ms) within ONE budget shared by every list in the process, past
which the parked pages farthest from any view are destroyed; a hidden list parks
nothing. Every Tk widget is a Windows window, and about 1650 drawn rows reached
the 10000 USER-object quota of a process (measured: then every new page took
15 s and windows failed to be made).
"""

import re
import time
import tkinter as tk
from tkinter import ttk

from .. import theme as theme_mod
from ..context_menu import SharedContextMenu
from .base import Screen, make_scrollable

try:  # the kit is the look; a guarded import, so a broken kit still leaves a working list
    from ..kit import C9, C9B, C10, C11, C11B, geom_ratio as _geom_ratio, tokens as _tokens
    from ..kit.widgets import ELLIPSIS, EmptyState, StatusLine, Well, elide_middle, one_line
except Exception:  # noqa: BLE001
    _tokens = None
    C9, C9B, C10, C11, C11B = (("Consolas", 9), ("Consolas", 9, "bold"), ("Consolas", 10), ("Consolas", 11),
                               ("Consolas", 11, "bold"))
    EmptyState = StatusLine = Well = None
    ELLIPSIS = "..."

    def _geom_ratio(_w):
        return 1.0

    def one_line(text):
        return " ".join(str(text or "").split())

    def elide_middle(text, fits):
        if fits(text):
            return text
        keep = len(text)
        while keep > 0:
            head = (keep + 1) // 2
            cand = text[:head] + ELLIPSIS + text[len(text) - (keep - head):] if keep - head else text[:head] + ELLIPSIS
            if fits(cand):
                return cand
            keep -= 1
        return ELLIPSIS


# ---- approved geometry (pixels at 100 %; scaled by the display ratio) ----
ROW_H = 32               # row box height
ROW_GAP = 4              # pitch 36
INDEX_COL = 42           # window x 58..100: the index column left of the row box
INDEX_PAD = 4            # the inverse index chip reaches 4 px around the digits
INDEX_GAP = 8            # chip right edge (x 92) -> row box (x 100)
TITLE_X = 16             # row box left -> title
TAIL_X = 12              # tail right edge -> row box right
LONG_TITLE = 440         # a title wider than this may not fit at the 820 px floor: it gets the eliding line
SEARCH_TOP = 13          # header band (56) -> search well (70): 14, less the 1 px of the screen's empty prompt slot
WELL_W, WELL_MIN_W = 400, 220
WELL_H = 26
CHIPS_X = 24             # well right (458) -> first filter word (482)
CHIP_GAP = 18            # between filter words
CHIP_RING = 2            # keyboard focus ring of a filter word (inside its gap)
CHIP_PADX = 2
CHIP_MAX_CHARS = 32      # a longer folder name is elided in the middle (full name in the hover tip)
MIN_CHIP_LINES, MAX_CHIP_LINES = 2, 4   # word lines under the well before the rest go behind "+N MORE" (ADV1-01)
KEEP_ROWS = 8            # ... as many lines in that range as leave the list this many rows of room
CHIP_LINE_GAP = 4        # between two lines of filter words
CHIP_UNDER_GAP = 6       # search well -> the first line of words under it
LIST_TOP_SEARCH = 18     # search well bottom (96) -> first cell (114; row box at 116)
LIST_TOP_PLAIN = 57      # header band (56) -> first cell (114) with no search well: 58, less the prompt slot's 1 px
HEADING_GAP = 24         # extra space above a later group heading (approved GROUP_GAP 42 - pitch 36 + ...)
HEADING_BELOW = 4        # heading text -> its first row's cell (row box 6 px below the text)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_STAMP_RX = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})")


def format_tail(stamp, style="short", now=None):
    """The row tail for a server timestamp ('2026-10-04 09:41:00', local time).
    style="short" (Chats): 'Oct 04  09:41' this year, 'Oct 04  2025' for another year.
    style="iso" (a project's chats): '2026-10-04  09:41'. '' when it cannot be read.
    English month names, never the locale's."""
    m = _STAMP_RX.match(str(stamp or ""))
    if not m:
        return ""
    year, month, day, hour, minute = (int(g) for g in m.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour <= 23 and minute <= 59):
        return ""
    if style == "iso":
        return "%04d-%02d-%02d  %02d:%02d" % (year, month, day, hour, minute)
    this_year = (now or time.localtime()).tm_year
    if year == this_year:
        return "%s %02d  %02d:%02d" % (_MONTHS[month - 1], day, hour, minute)
    return "%s %02d  %04d" % (_MONTHS[month - 1], day, year)


class ListRow:
    """context_items: optional list of (label, callback) shown as a context
    menu on right-click or keyboard menu keys (Shift-F10 / the App key) while
    the row has focus - tk.Menu is natively arrow-key navigable, so this
    keeps rename/delete-style row actions fully keyboard-reachable. An entry
    can also be (label, [(sub_label, sub_callback), ...]) for a cascading
    submenu - see context_menu.populate_menu.

    tail: optional short text at the row's right end (an updated time, "2 chats").
    A caption given without a tail is shown as the tail (nothing is dropped).
    kind="heading": a group heading - plain text, not focusable, no bindings,
    not numbered and not reached by the digits."""

    def __init__(self, label, caption="", on_activate=None, is_active=False, context_items=None, tail=None,
                 kind="row"):
        self.label = label
        self.caption = caption
        self.on_activate = on_activate
        self.is_active = is_active
        self.context_items = context_items or []
        self.tail = tail
        self.kind = "heading" if kind == "heading" else "row"


def _theme_tokens(theme):
    if _tokens is not None:
        try:
            return _tokens(theme)
        except Exception:  # noqa: BLE001
            pass
    get = (lambda k, d: getattr(theme, k, d))
    return {"bg": get("bg", "#000000"), "panel": get("panel", "#111111"), "bright": get("bright", "#FFB000"),
            "dim": get("dim", "#6A4A00"), "value": get("value", get("text", "#C08000"))}


class ListScreen(Screen):
    ROW_PAGE = 100           # rows drawn at once (see set_rows)
    # Rows kept parked - a page the view left is hidden, not destroyed, so coming back to it costs a few ms instead of the
    # ~0.6 s a page of 100 rows takes to make (B5 FIX1 item 2). Hidden windows still hold Windows USER handles (about
    # six a row), whose quota of 10,000 a process once froze the app (ADV1-02b), so the budget is ONE for the whole
    # process: every list (Chats, Projects, a project's chats) parks into it, and past it the parked pages farthest from
    # any view are destroyed (_trim_parked). A list that is hidden - its screen swapped out, Projects' other view shown -
    # keeps no parked page at all (release_parked): the budget is for the lists on show.
    PARK_BUDGET = 300
    _live = {}               # id -> every ListScreen made and not destroyed: the budget is shared across them

    def __init__(self, parent, ctx, on_search_change=None, show_search=True, empty_text=None, empty_caption=None):
        super().__init__(parent, ctx)
        theme = ctx.theme
        self._t = t = _theme_tokens(theme)
        self._ratio = _geom_ratio(self)
        self._on_search_change = on_search_change
        self._suppress_search_trace = False
        self.search_entry = None
        self.search_well = None
        self.search_placeholder = None
        self.empty_text = empty_text
        self.empty_caption = empty_caption      # "Press + NEW CHAT to start one": what to do about the empty state
        self.empty_state = None
        self._show_search = show_search
        # The one context menu shared by every row and chip, filled in only
        # when one is actually opened - see context_menu.py for why.
        self._menu = SharedContextMenu(self, theme)
        self._recs = {}            # widget path -> row record (row, text_col, title and tail paths)
        self._rec_by_n = {}        # row number (1..) -> row record, for the rows drawn so far
        self._chip_layout_key = None
        self._chip_mode = "beside"
        self._more_chip = None     # "+N MORE" (made on first need; a sibling of chip_row placed in it)
        self._more_items = []
        self._bar_images = {}
        self._metrics = None
        self._pages = []           # [{"start", "end", "y", "h", "frame", "item"}] of the current rows
        self._n_rows = 0
        self._layout = None
        ListScreen._live[id(self)] = self
        # a hidden list parks nothing: its parked pages go the moment it is unmapped. Tk unmaps every descendant of a
        # frame taken off the screen (measured), so this fires for the router's screen swap and Projects' grid / detail
        # swap alike; Chats and Projects call on_hide() on their lists too (the explicit path, same effect).
        self.bind("<Unmap>", lambda _e: self.release_parked(), add="+")

        # one registered command + three bind tags per list: rows cost no Python callbacks
        self._cmd = self.register(self._on_row_event)
        uid = "%x" % id(self)
        self._tag_row, self._tag_part, self._tag_wheel = "KitListRow" + uid, "KitListPart" + uid, "KitListWheel" + uid
        self._tag_page, self._tag_spacer = "kitpage" + uid, "kitspacer" + uid      # canvas item tags
        c = self._cmd
        for seq, act in (("<Return>", "activate"), ("<KP_Enter>", "activate"), ("<space>", "activate"),
                         ("<FocusIn>", "focusin"), ("<FocusOut>", "focusout"), ("<Button-1>", "click")):
            self.bind_class(self._tag_row, seq, "%s %s %%W" % (c, act))
        # Tab / Shift-Tab step row to row themselves: a page scrolled out of view is unmapped by the canvas,
        # and Tk's own traversal skips unmapped windows (B5 fix ADV1-02)
        for seq, act in (("<Tab>", "tab"), ("<Shift-Tab>", "btab")):
            self.bind_class(self._tag_row, seq, 'if {"[%s %s %%W]" == "break"} break' % (c, act))
        self.bind_class(self._tag_row, "<Enter>", "%s enter %%W" % c)
        self.bind_class(self._tag_row, "<Leave>", "%s leave %%W %%d" % c)
        self.bind_class(self._tag_part, "<Button-1>", "%s click %%W" % c)
        for tag in (self._tag_row, self._tag_part, self._tag_wheel):
            self.bind_class(tag, "<MouseWheel>", "%s wheel %%W %%D" % c)

        if show_search:
            self.search_row = tk.Frame(self, bg=t["bg"])
            self.search_row.pack(fill="x", padx=self._s(16), pady=(self._s(SEARCH_TOP), 0))
            self.search_var = tk.StringVar()
            self._build_search_well(self.search_row)
            self.chip_row = tk.Frame(self.search_row, bg=t["bg"], height=1)
            self.chip_row.grid(row=0, column=1, sticky="new", padx=(self._s(CHIPS_X - CHIP_RING - CHIP_PADX), 0))
            self.search_row.columnconfigure(1, weight=1)
            self.search_row.bind("<Configure>", self._fit_search_row, add="+")
            self.search_var.trace_add("write", self._on_search_var_changed)
            self.search_var.trace_add("write", lambda *_a: self._sync_placeholder())
            list_top = self._s(LIST_TOP_SEARCH)
        else:
            self.search_row = None
            self.chip_row = tk.Frame(self, bg=t["bg"], height=1)      # packed by set_chips when it gets chips
            list_top = self._s(LIST_TOP_PLAIN)
        self.chip_row.bind("<Configure>", lambda _e: self._layout_chips(), add="+")
        self.bind("<Configure>", lambda _e: self._layout_chips(), add="+")     # the line cap follows the height
        self._list_top = list_top

        outer, inner = make_scrollable(self, theme)
        outer.pack(fill="both", expand=True, padx=self._s(16), pady=(list_top, self._s(16)))
        self._list_outer = outer
        self.row_container = inner
        self._canvas = inner.master
        # the scroll area's own scrollbar and the inner frame's canvas item (make_scrollable keeps them local): a list taller
        # than its view packs the bar BEFORE its rows are drawn, at the width they will keep (_show_scrollbar_early)
        self._scrollbar = next((w for w in self._canvas.master.winfo_children() if isinstance(w, ttk.Scrollbar)), None)
        self._inner_item = next((i for i in self._canvas.find_all() if self._canvas.type(i) == "window"), None)
        self._page_width_hint = None       # the width pages are drawn at until the canvas has followed the bar (then None)
        # the canvas and the inner frame scroll on the wheel through a widget binding (make_scrollable's), so
        # only the outer frame takes the list's wheel tag: a second handler doubled the step (B5 fix ADV1-06).
        # That widget binding is REPLACED by the list's own handler, so the gaps and the bare canvas scroll as
        # the rows do: one handler that adds sub-notch deltas up (precision touchpads; B5 fix ADV2-04)
        self._wheel_acc = 0
        self._add_tag(outer, self._tag_wheel)
        for w in (self._canvas, inner):
            w.bind("<MouseWheel>", "%s wheel %%W %%D" % self._cmd)
        self._spacer = None        # canvas item: an invisible rectangle as tall as the whole list (multi-page lists)
        self._canvas.bind("<Configure>", self._on_canvas_configure, add="+")
        # chain the scroll area's own yscrollcommand (it auto-hides the scrollbar), and draw the pages of a
        # long list that come near the view
        self._yscroll_orig = str(self._canvas.cget("yscrollcommand"))
        self._canvas.configure(yscrollcommand=self._on_yscroll)

    # ------------------------------------------------------------------ helpers
    def destroy(self):
        ListScreen._live.pop(id(self), None)
        super().destroy()

    def on_hide(self):
        """The list is being swapped out (Chats / Projects call this from their own on_hide): its parked pages go with it."""
        self.release_parked()
        super().on_hide()

    def _s(self, n):
        return int(round(n * self._ratio))

    def _viewable(self):
        try:
            return bool(self.winfo_viewable())
        except tk.TclError:
            return False

    def _add_tag(self, widget, tag):
        try:
            tags = widget.bindtags()
            widget.bindtags((tags[0], tag) + tuple(tags[1:]))
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ search well
    def _build_search_well(self, row):
        t = self._t
        box = tk.Frame(row, bg=t["bg"], width=self._s(WELL_W), height=self._s(WELL_H), bd=0, highlightthickness=0)
        box.grid(row=0, column=0, sticky="nw")
        box.pack_propagate(False)
        self.search_box = box
        if Well is not None:
            well = Well(box, self.ctx.theme, ground=t["bg"])
            body = well.body
        else:   # no kit: a plain 1 px frame stands in
            well = tk.Frame(box, bg=t["dim"], bd=0, highlightthickness=0)
            body = tk.Frame(well, bg=t["panel"], bd=0, highlightthickness=0)
            body.pack(fill="both", expand=True, padx=1, pady=1)
        well.pack(fill="both", expand=True)
        self.search_well = well
        # the Entry fills the well's body (window x 60..456): its flat border is the text inset, sized so
        # the well keeps the approved 26 px at 100 % (an Entry asks line + 2 + 2 x border: 15 + 2 + 2 x 2)
        try:
            ls = int(self.tk.call("font", "metrics", C10, "-linespace"))
        except tk.TclError:
            ls = 15
        inset = max(1, (self._s(WELL_H) - 4 - (ls + 2)) // 2)
        self.search_entry = tk.Entry(
            body, textvariable=self.search_var, bg=t["panel"], fg=t["bright"], insertbackground=t["bright"],
            selectbackground=t["bright"], selectforeground=t["bg"], relief="flat", bd=inset, highlightthickness=0,
            font=C10,
        )
        self.search_entry.pack(fill="both", expand=True)
        if hasattr(well, "attach"):
            well.attach(self.search_entry)
        # the placeholder is an overlay, never text in the Entry: typing "Search" searches for "Search"
        self.search_placeholder = tk.Label(body, text="Search", bg=t["panel"], fg=t["dim"], font=C10, bd=0,
                                           padx=0, pady=0, cursor="xterm")
        self.search_placeholder.bind("<Button-1>", lambda _e: self.search_entry.focus_set())
        self.search_entry.bind("<FocusIn>", lambda _e: self._sync_placeholder(), add="+")
        self.search_entry.bind("<FocusOut>", lambda _e: self._sync_placeholder(), add="+")
        self._placeholder_x = inset + 1
        self._sync_placeholder()
        # the box is as tall as the approved well, or as the Entry needs (large fonts / scaling)
        try:
            need = self.search_entry.winfo_reqheight() + 4
            if need > self._s(WELL_H):
                box.configure(height=need)
        except tk.TclError:
            pass

    def _sync_placeholder(self):
        ph, entry = self.search_placeholder, self.search_entry
        if ph is None or entry is None:
            return
        try:
            focused = self.focus_get() is entry
        except (KeyError, tk.TclError):
            focused = False
        try:
            show = not self.search_var.get() and not focused
            if show:
                ph.place(in_=entry, x=self._placeholder_x, rely=0.5, anchor="w")
            elif ph.winfo_manager():
                ph.place_forget()
        except tk.TclError:
            pass

    def _fit_search_row(self, event=None):
        """The well is 400 px wide, narrower (never below 220) when the row cannot hold it and
        the filter words: geometry only (the row's width), so it never feeds back."""
        try:
            w = event.width if event is not None else self.search_row.winfo_width()
        except tk.TclError:
            return
        if w <= 1:
            return
        target = self._well_width(w)
        try:
            if int(self.search_box.cget("width")) != target:
                self.search_box.configure(width=target)
        except tk.TclError:
            pass
        self._layout_chips()

    def _well_width(self, row_width):
        return max(self._s(WELL_MIN_W), min(self._s(WELL_W), int(row_width * 0.5)))

    def focus_search(self):
        if self.search_entry is not None:
            self.search_entry.focus_set()

    def _popup_context_menu(self, items, x, y):
        self._menu.popup(items, x, y)

    def _on_search_var_changed(self, *_args):
        if self._on_search_change and not self._suppress_search_trace:
            self._on_search_change(self.search_var.get())

    def set_search_text(self, text):
        if self.search_entry is None:
            return
        self._suppress_search_trace = True
        self.search_var.set(text)
        self._suppress_search_trace = False

    # ------------------------------------------------------------------ filter words
    def set_chips(self, chips):
        """chips: list of (label, is_active, on_click) tuples, or
        (label, is_active, on_click, context_items) when the chip needs a
        right-click menu (e.g. "Rename folder"/"Delete folder"), e.g.
        [("All", True, cb), ("Research", False, cb, [("Rename folder", cb2),
        ("Delete folder", cb3)]), ("+", False, cb)] - the "+" entry (with no
        context_items) creates a new folder inline and is drawn as the words
        "+ FOLDER"; a folder named "+" (it has its menu) stays a folder word. Chips are plain
        Tab-order words (not digit-numbered - there are usually few of them
        and they aren't the screen's primary numbered content), so
        Tab/Enter/Space is the keyboard path here, matching "anything not
        directly numbered" in the keyboard system design. context_items
        reuses ListRow's right-click/Shift-F10/App-key tk.Menu mechanism
        (see set_rows below) so folder management stays keyboard-reachable
        too, not just mouse-only.

        B5 look: the selected word bright with a 2 px underbar, the others
        `value`; hover = bright ink and a 1 px ring, keyboard focus = a 2 px
        bright ring. A folder name longer than 32 characters is elided in the
        middle; cget("text") and the hover tip keep the whole name; a blank
        name shows as a dash. Words that do not fit go behind "+N MORE" (see
        _layout_chips). A chip that had the keyboard focus keeps it across the
        rebuild (the chip with the same label takes it)."""
        if self._update_chips_in_place(chips):
            return
        focus_label = None
        try:
            had = self.focus_get()
        except (KeyError, tk.TclError):
            had = None
        if had is not None and (getattr(had, "master", None) is self.chip_row or had is self._more_chip):
            focus_label = "\0more" if had is self._more_chip else getattr(had, "_kl_label", None)
        for child in self.chip_row.winfo_children():
            child.destroy()
        t = self._t
        if chips and self.search_row is None and not self.chip_row.winfo_manager():
            self.chip_row.pack(fill="x", padx=self._s(16), pady=(0, self._s(8)), before=self._list_outer)
        for chip_spec in chips:
            label, is_active, on_click = chip_spec[:3]
            context_items = chip_spec[3] if len(chip_spec) > 3 else None
            # the create word is the "+" entry WITHOUT a menu: a folder chip always carries Rename / Delete,
            # so a folder that is really named "+" stays a folder word (B5 fix ADV2-03)
            plus = label == "+" and not context_items
            if plus:
                text = "+ FOLDER"
            else:
                text = label if one_line(label).strip() else "\u2014"     # a blank name still shows (ADV1-09)
            font = C9B if plus else C9
            ink = t["bright"] if is_active else t["value"]
            shown = self._chip_shown(text, font)
            tw = self._measure(font, shown)
            opts = dict(bg=t["bg"], fg=ink, font=font, takefocus=1, cursor="hand2", bd=0,
                        highlightthickness=self._s(CHIP_RING), highlightbackground=t["bg"],
                        highlightcolor=t["bright"], padx=self._s(CHIP_PADX), pady=0,
                        image=self._bar_image(tw, is_active and not plus), compound="bottom")
            if StatusLine is not None:
                chip = StatusLine(self.chip_row, self.ctx.theme, text=text, max_chars=CHIP_MAX_CHARS, **opts)
            else:
                chip = tk.Label(self.chip_row, text=shown, **opts)
            chip._kl_width = tw + 2 * (self._s(CHIP_RING) + self._s(CHIP_PADX))
            chip._kl_tw = tw
            chip._kl_ink = ink
            chip._kl_label = label
            chip._kl_shown = shown
            chip._kl_plus = plus
            chip._kl_active = bool(is_active)
            chip._kl_click = on_click
            chip._kl_items = context_items or None
            chip._kl_hover = chip._kl_focus = False

            # the bindings read the chip's own _kl_click / _kl_items at event time, so _update_chips_in_place can swap them
            def activate(_e=None, c=chip):
                c._kl_click()

            chip.bind("<Button-1>", lambda e, c=chip, cb=activate: (c.focus_set(), cb()))
            chip.bind("<Return>", activate)
            chip.bind("<space>", activate)
            self._bind_chip_look(chip)

            if context_items:
                def post_at_pointer(event, c=chip):
                    c.focus_set()
                    self._popup_context_menu(c._kl_items or [], event.x_root, event.y_root)
                    return "break"

                def post_at_chip(_event=None, c=chip):
                    self._popup_context_menu(c._kl_items or [], c.winfo_rootx(), c.winfo_rooty() + c.winfo_height())
                    return "break"

                chip.bind("<Button-3>", post_at_pointer)
                chip.bind("<Shift-F10>", post_at_chip)
                chip.bind("<App>", post_at_chip)
        self._chip_layout_key = None
        self._layout_chips()
        if focus_label is not None:
            self._refocus_chip(focus_label)

    def _update_chips_in_place(self, chips):
        """The fast path of set_chips: the same words in the same order - a folder click, Return / space on a word, where
        only the selected word and the callbacks change - are updated where they are. Destroying and remaking every word
        cost ~0.8 s at 300 folders and ~2 s at 1000, on every click (B5 ADV2-01). -> True when done; False when the shape
        changed (a folder added, renamed, deleted or reordered, a menu gained or lost), which takes the full rebuild."""
        existing = [c for c in self.chip_row.winfo_children() if hasattr(c, "_kl_label")]
        if not existing or len(existing) != len(chips):
            return False
        specs = []
        for chip_spec in chips:
            label, is_active, on_click = chip_spec[:3]
            context_items = chip_spec[3] if len(chip_spec) > 3 else None
            specs.append((label, bool(is_active), on_click, context_items or None, label == "+" and not context_items))
        for c, (label, _active, _click, items, plus) in zip(existing, specs):
            if c._kl_label != label or c._kl_plus != plus or (c._kl_items is not None) != (items is not None):
                return False
        t = self._t
        changed = False
        for c, (label, is_active, on_click, items, plus) in zip(existing, specs):
            c._kl_click = on_click                   # the bindings read these at event time
            c._kl_items = items
            if c._kl_active != is_active:
                changed = True
                c._kl_active = is_active
                c._kl_ink = t["bright"] if is_active else t["value"]
                try:
                    c.configure(image=self._bar_image(c._kl_tw, is_active and not plus))
                except tk.TclError:
                    return False
                self._paint_chip(c)
        if changed:
            self._chip_layout_key = None            # the selected word must stay on show: "+N MORE" may have to change
            self._layout_chips()
        return True

    def _bind_chip_look(self, chip):
        """Hover = bright ink + a 1 px bright ring (seen in every phosphor, also where `value` and
        `bright` are close, ADV1-07); focus = the 2 px ring. The ring width and the pad add up to the
        same inset in every state, so the word never moves."""
        def on(attr, value):
            def handler(_e=None):
                setattr(chip, attr, value)
                self._paint_chip(chip)
            return handler

        chip.bind("<Enter>", on("_kl_hover", True), add="+")
        chip.bind("<Leave>", on("_kl_hover", False), add="+")
        chip.bind("<FocusIn>", on("_kl_focus", True), add="+")
        chip.bind("<FocusOut>", on("_kl_focus", False), add="+")

    def _paint_chip(self, chip):
        t = self._t
        ring, pad = self._s(CHIP_RING), self._s(CHIP_PADX)
        if getattr(chip, "_kl_focus", False):
            width, edge = ring, t["bg"]           # focused: Tk draws highlightcolor (bright), 2 px
        elif getattr(chip, "_kl_hover", False):
            width, edge = max(1, self._s(1)), t["bright"]
        else:
            width, edge = ring, t["bg"]
        ink = t["bright"] if getattr(chip, "_kl_hover", False) else getattr(chip, "_kl_ink", t["value"])
        try:
            chip.configure(fg=ink, highlightthickness=width, highlightbackground=edge, padx=pad + ring - width)
        except tk.TclError:
            pass

    def _refocus_chip(self, label):
        """Give the keyboard focus back after a rebuild (Return / space on a word re-renders the words,
        ADV1-10): the chip with the same label, else "+N MORE" when that label went behind it."""
        target = None
        if label != "\0more":
            for chip in self.chip_row.winfo_children():
                if getattr(chip, "_kl_label", None) == label:
                    target = chip
                    break
        if target is None or not target.winfo_manager():
            more = self._more_chip
            target = more if (more is not None and more.winfo_manager()) else target
        if target is not None and target.winfo_manager():
            try:
                target.focus_set()
            except tk.TclError:
                pass

    def _measure(self, font, text):
        try:
            return int(self.tk.call("font", "measure", font, text))
        except tk.TclError:
            return 7 * len(text)

    def _chip_shown(self, text, font):
        line = one_line(text)
        if len(line) > CHIP_MAX_CHARS:
            line = elide_middle(line, lambda s: len(s) <= CHIP_MAX_CHARS)
        return line

    def _bar_image(self, width, on):
        """A width x 3 image under a filter word: a blank row, then the 2 px underbar when `on`
        (transparent when off, so every word keeps the same height)."""
        width = max(1, int(width))
        key = (width, bool(on))
        img = self._bar_images.get(key)
        if img is None:
            img = tk.PhotoImage(master=self, width=width, height=self._s(3))
            if on:
                img.put(self._t["bright"], to=(0, self._s(1), width, self._s(3)))
            self._bar_images[key] = img
        return img

    def _flow(self, widths, width, max_lines):
        """Line-wrap `widths` into `width`: -> the (x, line) of each, or None when more than max_lines."""
        gap = self._chip_gap()
        out, x, line = [], 0, 0
        for w in widths:
            if x > 0 and x + w > width:
                x, line = 0, line + 1
                if line >= max_lines:
                    return None
            out.append((x, line))
            x += w + gap
        return out

    def _chip_gap(self):
        return max(0, self._s(CHIP_GAP) - 2 * (self._s(CHIP_RING) + self._s(CHIP_PADX)))

    def _layout_chips(self):
        """Lay the filter words out (B5 fix ADV1-01):
        - one line beside the well when they all fit there (the approved look; the line's text is
          centred on the well);
        - otherwise full-width lines under the well: 2 to 4 of them, as many as leave the list
          KEEP_ROWS rows of room (2 at the 820x520 floor);
        - and when even those cannot hold them, the folders that do not fit go behind a last word
          "+N MORE" that lists them in the shared menu (each with its own menu as a submenu), so
          every folder stays reachable. The selected word and "+ FOLDER" always stay on show.
        Geometry only: the decision comes from the search row's width, never from the chip row's,
        so moving the chip row cannot feed back into it."""
        chips = [c for c in self.chip_row.winfo_children() if hasattr(c, "_kl_width")]
        if self.search_row is not None:
            try:
                rw = self.search_row.winfo_width()
            except tk.TclError:
                return
            known = rw > 1
            full = rw if known else 10 ** 6
            beside = full - self._well_width(full) - self._s(CHIPS_X - CHIP_RING - CHIP_PADX) if known else full
        else:
            try:
                cw = self.chip_row.winfo_width()
            except tk.TclError:
                return
            full = beside = cw if cw > 1 else 10 ** 6
        cap = self._chip_line_cap(chips)
        key = (full, beside, cap, tuple(str(c) for c in chips))
        if key == self._chip_layout_key:
            return
        self._chip_layout_key = key
        gap = self._chip_gap()
        try:
            ls = int(self.tk.call("font", "metrics", C9, "-linespace"))
        except tk.TclError:
            ls = 14
        widths = [c._kl_width for c in chips]
        plan = None          # [(chip, x, line)]
        hidden = []
        mode = "beside"
        if self.search_row is None or sum(widths) + gap * max(0, len(widths) - 1) <= beside:
            if self.search_row is None:
                flow = self._flow(widths, full, 10 ** 6)
                plan = [(c, x, ln) for c, (x, ln) in zip(chips, flow)]
            else:
                x = 0
                plan = []
                for c, w in zip(chips, widths):
                    plan.append((c, x, 0))
                    x += w + gap
        else:
            mode = "under"
            flow = self._flow(widths, full, cap)
            if flow is not None:
                plan = [(c, x, ln) for c, (x, ln) in zip(chips, flow)]
            else:
                plan, hidden = self._plan_with_more(chips, full, cap)
        self._set_chip_mode(mode)
        y0 = 0
        if mode == "beside" and self.search_row is not None:
            y0 = max(0, int(round(self.search_box.winfo_reqheight() / 2.0 - self._s(CHIP_RING) - ls / 2.0)))
        placed = set()
        bottom = y0
        more = self._more_chip
        try:
            focused = self.focus_get()
        except (KeyError, tk.TclError):
            focused = None
        if hidden:
            more = self._ensure_more_chip()
            more.configure(text="+%d MORE" % len(hidden))
        try:     # one line pitch for all (a "+ FOLDER" / "+N MORE" word may be a pixel taller than a name)
            line_h = max([c.winfo_reqheight() for c, _x, _l in plan] + ([more.winfo_reqheight()] if hidden else [1]))
        except tk.TclError:
            line_h = ls
        line_step = line_h + self._s(CHIP_LINE_GAP)
        for chip, x, line in plan:
            try:
                h = chip.winfo_reqheight()
                y = y0 + line * line_step
                chip.place(x=x, y=y, width=chip._kl_width, height=h)
                placed.add(str(chip))
                bottom = max(bottom, y + h)
            except tk.TclError:
                pass
        lost_focus = False
        for chip in chips:
            if str(chip) not in placed:
                lost_focus = lost_focus or chip is focused
                if chip.winfo_manager():
                    chip.place_forget()
        self._more_items = hidden
        if hidden:
            x, line, w = self._more_pos
            try:
                h = more.winfo_reqheight()
                y = y0 + line * line_step
                more.place(in_=self.chip_row, x=x, y=y, width=w, height=h)
                bottom = max(bottom, y + h)
            except tk.TclError:
                pass
        elif more is not None and more.winfo_manager():
            lost_focus = lost_focus or more is focused
            more.place_forget()
        if lost_focus:
            # the focused word went behind "+N MORE" (or "+N MORE" itself went): an unmapped widget must not
            # keep the keyboard focus, where Return would act on an invisible word (B5 fix ADV2-02). The focus
            # goes to "+N MORE", which lists that word, else to the last word on show.
            target = more if hidden else (plan[-1][0] if plan else None)
            if target is not None:
                try:
                    target.focus_set()
                except tk.TclError:
                    pass
        try:
            if int(self.chip_row.cget("height")) != max(1, bottom):
                self.chip_row.configure(height=max(1, bottom))
        except tk.TclError:
            pass

    def _more_width(self, n):
        return self._measure(C9B, "+%d MORE" % n) + 2 * (self._s(CHIP_RING) + self._s(CHIP_PADX))

    def _chip_line_cap(self, chips):
        """How many word lines may go under the well: MIN..MAX_CHIP_LINES, as many as leave the list
        KEEP_ROWS rows. From this list's own height (set by its parent, never by the words), so it
        cannot feed back."""
        try:
            h = self.winfo_height()
            line = max([c.winfo_reqheight() for c in chips] or [1]) + self._s(CHIP_LINE_GAP)
            fixed = (self._s(SEARCH_TOP) + self.search_box.winfo_reqheight() + self._s(CHIP_UNDER_GAP)
                     + self._list_top + self._s(16)) if self.search_row is not None else self._list_top + self._s(24)
        except (tk.TclError, AttributeError):
            return MIN_CHIP_LINES
        if h <= 1:
            return MIN_CHIP_LINES
        pitch = self._s(ROW_H) + 2 * (self._s(ROW_GAP) // 2)
        room = h - fixed - KEEP_ROWS * pitch + self._s(CHIP_LINE_GAP)
        return max(MIN_CHIP_LINES, min(MAX_CHIP_LINES, room // max(1, line)))

    def _plan_with_more(self, chips, width, cap=MIN_CHIP_LINES):
        """The most leading folder words that fit `cap` lines together with the selected
        word, "+ FOLDER" and "+N MORE" (in that order at the end). -> (plan, hidden chips).

        The largest k (leading words shown) is found by bisection: adding a word to a line-wrapped
        sequence never takes fewer lines, so "fits in cap lines" holds for every k up to the answer.
        (A linear walk down from all the words, with a list-membership test per word, cost O(n^2 k):
        2.5 s per plan at 1000 folders, on every load and folder click; B5 fix ADV2-01.) "+N MORE"
        narrows by a digit as k grows past a power of ten, so k is then walked up while k + 1 still fits."""
        plus = [c for c in chips if getattr(c, "_kl_plus", False)]
        words = [c for c in chips if not getattr(c, "_kl_plus", False)]
        n = len(words)
        sel = next((i for i, c in enumerate(words) if getattr(c, "_kl_active", False)), None)
        word_w = [c._kl_width for c in words]
        plus_w = [c._kl_width for c in plus]

        def attempt(k, lines):
            idx = list(range(k)) + ([sel] if sel is not None and sel >= k else [])
            widths = [word_w[i] for i in idx] + plus_w + [self._more_width(n - len(idx))]
            return idx, widths, self._flow(widths, width, lines)

        if attempt(0, cap)[2] is None:
            k = 0
        else:
            lo, hi = 0, n          # attempt(lo) fits; the answer is in lo..hi
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if attempt(mid, cap)[2] is not None:
                    lo = mid
                else:
                    hi = mid - 1
            while lo < n and attempt(lo + 1, cap)[2] is not None:
                lo += 1
            k = lo
        idx, widths, flow = attempt(k, cap)
        if flow is None:          # not even one word fits: everything on as few lines as it takes
            flow = self._flow(widths, width, 10 ** 6)
        keep = set(idx)
        seq = [words[i] for i in idx] + plus
        hidden = [c for i, c in enumerate(words) if i not in keep]
        self._more_pos = (flow[-1][0], flow[-1][1], widths[-1])
        return [(c, x, ln) for c, (x, ln) in zip(seq, flow[:-1])], hidden

    def _set_chip_mode(self, mode):
        if mode == self._chip_mode or self.search_row is None:
            return
        self._chip_mode = mode
        try:
            if mode == "under":
                self.chip_row.grid_configure(row=1, column=0, columnspan=2, padx=0, pady=(self._s(CHIP_UNDER_GAP), 0))
            else:
                self.chip_row.grid_configure(row=0, column=1, columnspan=1,
                                             padx=(self._s(CHIPS_X - CHIP_RING - CHIP_PADX), 0), pady=0)
        except tk.TclError:
            pass

    def _ensure_more_chip(self):
        """The "+N MORE" word: a sibling of chip_row (chip_row keeps the chips only), placed in it,
        made once. Click, Return, space, Button-3, Shift-F10 and App open the hidden folders."""
        if self._more_chip is not None:
            return self._more_chip
        t = self._t
        more = tk.Label(self.chip_row.master, text="+0 MORE", bg=t["bg"], fg=t["value"], font=C9B, takefocus=1,
                        cursor="hand2", bd=0, highlightthickness=self._s(CHIP_RING), highlightbackground=t["bg"],
                        highlightcolor=t["bright"], padx=self._s(CHIP_PADX), pady=0,
                        image=self._bar_image(1, False), compound="bottom")
        more._kl_ink = t["value"]
        more._kl_hover = more._kl_focus = False
        self._bind_chip_look(more)

        def post(_e=None):
            self._post_more()
            return "break"

        for seq in ("<Button-1>", "<Return>", "<space>", "<Button-3>", "<Shift-F10>", "<App>"):
            more.bind(seq, post)
        self._more_chip = more
        return more

    def more_menu_items(self):
        """The shared-menu entries "+N MORE" opens: one per hidden folder, in order. A folder with
        its own menu (Rename / Delete) is a submenu led by "Open"; one without is a plain entry."""
        items = []
        for chip in self._more_items:
            label = getattr(chip, "_kl_shown", None) or "\u2014"
            click, extra = chip._kl_click, chip._kl_items
            items.append((label, [("Open", click)] + list(extra)) if extra else (label, click))
        return items

    def _post_more(self):
        more = self._more_chip
        if more is None or not self._more_items:
            return
        try:
            more.focus_set()
            x, y = more.winfo_rootx(), more.winfo_rooty() + more.winfo_height()
        except tk.TclError:
            return
        self._popup_context_menu(self.more_menu_items(), x, y)

    # ------------------------------------------------------------------ rows
    def set_rows(self, rows, reset_scroll=False):
        """rows: list of ListRow. Rows are numbered continuously top to bottom;
        headings (kind="heading") are not numbered and not bound to digits.

        reset_scroll: the rows are a NEW list (another folder, another search, another project) - it opens at its top and the
        keyboard focus goes to its first row. Off (the default) for a reload of the same list after a rename, move or delete,
        which keeps the offset and the focused row number (B5 fix ADV2-06).

        A long list is drawn ROW_PAGE rows at a time (see the module notes): page 0 at once,
        the others when the view comes near them (scrolling, the wheel, Tab), each in its own
        canvas window item, with an invisible rectangle as tall as the whole list so the
        scrollbar shows all of it from the start. Measured: 1000 rows took 10-12 s to draw in
        one go (B5 adversarial case), and one frame holding them all stopped painting past
        32767 px (ADV1-02).

        The digits are bound only while this list is the shown (packed) view: a late result for
        a hidden view (Projects' grid while its detail shows) must not take the digits over
        (ADV1-04).

        A row that had the keyboard focus hands it on to the row with the same number in the new
        list (the last row when the list got shorter): destroying it dropped the focus to the
        toplevel after every rename, move, delete or search reload (B5 fix ADV2-06)."""
        try:
            had = self._recs.get(str(self.focus_get()))
        except (KeyError, tk.TclError):
            had = None
        focus_n = had["n"] if had is not None else None
        self._clear_rows()
        s = self._s
        cell_h, head_h = self._row_metrics()
        half = s(ROW_GAP) // 2
        pitch = cell_h + 2 * half
        n_rows = sum(1 for r in rows if r.kind != "heading")
        self._n_rows = n_rows
        self._layout = lay = {
            "rows": rows, "cell_h": cell_h, "half": half,
            "icol": max(s(INDEX_COL), self._measure(C11B, "%02d" % max(1, n_rows)) + 2 * s(INDEX_PAD) + s(INDEX_GAP)),
            "long_title": s(LONG_TITLE), "top": str(self.winfo_toplevel()), "busy": False, "drawn": 0,
            "ys": [], "ns": [], "page_of": [], "pos_of_n": [], "digit_w": {},
        }
        # the model: every slot's y and height (fixed by construction), the row numbers and the pages
        y = n = count = 0
        start = 0
        pages = []
        for pos, r in enumerate(rows):
            if count == self.ROW_PAGE:
                pages.append({"start": start, "end": pos})
                start, count = pos, 0
            lay["ys"].append(y)
            lay["page_of"].append(len(pages))
            if r.kind == "heading":
                lay["ns"].append(0)
                y += head_h + (s(HEADING_GAP) if pos else 0) + s(HEADING_BELOW)
            else:
                n += 1
                count += 1
                lay["ns"].append(n)
                lay["pos_of_n"].append(pos)
                y += pitch
        if rows:
            pages.append({"start": start, "end": len(rows)})
        for pg in pages:
            pg["y"] = lay["ys"][pg["start"]]
            pg["h"] = (lay["ys"][pg["end"]] if pg["end"] < len(rows) else y) - pg["y"]
            pg["frame"] = pg["item"] = None
            pg["made"] = False
            pg["parked"] = False           # made, but hidden (see _evict_page)
            pg["cells"] = None             # page 0 only: its cells and their pack options while parked
        lay["total_h"] = y
        self._pages = pages
        lay["first_heading_h"] = (head_h + s(HEADING_BELOW)) if rows and rows[0].kind == "heading" else 0
        if reset_scroll:
            # a new list opens at its top (the old one's offset used to carry over, so a folder or search change
            # opened the new list at its end), and its first row takes the focus a row had
            try:
                self._canvas.yview_moveto(0.0)
            except tk.TclError:
                pass
            if focus_n is not None:
                focus_n = 1
        self._show_scrollbar_early(y)
        if len(pages) > 1:
            self._spacer = self._canvas.create_rectangle(0, 0, 1, y, outline="", fill="", tags=(self._tag_spacer,))
        if pages:
            self._render_page(0)
            self._ensure_view_pages()
        self._update_scrollregion()
        # a list that starts with a group heading draws it in the space above its first row,
        # so the first row box sits at the same place with or without headings (approved: y 116)
        want = max(0, self._list_top - lay["first_heading_h"])
        if want != getattr(self, "_list_top_now", self._list_top):
            try:
                self._list_outer.pack_configure(pady=(want, s(16)))
                self._list_top_now = want
            except tk.TclError:
                pass
        self._set_empty(not rows)
        callbacks = [r.on_activate or (lambda: None) for r in rows if r.kind != "heading"][:10]
        if self.winfo_manager():
            self.bind_digits(callbacks)
            if focus_n is not None and n_rows:
                self._refocus_row(min(focus_n, n_rows))

    def _refocus_row(self, n):
        lay = self._layout
        self._render_page(lay["page_of"][lay["pos_of_n"][n - 1]])
        rec = self._rec_by_n.get(n)
        if rec is None:
            return
        try:
            rec["row"].focus_set()
        except tk.TclError:
            return
        self._see(rec)

    def _clear_rows(self):
        for child in self.row_container.winfo_children():
            child.destroy()
        for pg in self._pages[1:]:
            if pg.get("frame") is not None:
                try:
                    pg["frame"].destroy()
                except tk.TclError:
                    pass
        try:
            self._canvas.delete(self._tag_page)
            self._canvas.delete(self._tag_spacer)
        except tk.TclError:
            pass
        self._pages = []
        self._spacer = None
        self._recs = {}
        self._rec_by_n = {}
        self._n_rows = 0

    def _row_metrics(self):
        """(row cell height, heading text height): fixed per list, measured once from real Labels
        (never mapped), so every slot's model y is exact."""
        if self._metrics is None:
            s = self._s
            probe = tk.Label(self, text="00", font=C11B, bd=0, padx=s(INDEX_PAD), pady=1)
            idx_h = probe.winfo_reqheight()
            probe.configure(text="X", font=C9, padx=0, pady=0)
            head_h = probe.winfo_reqheight()
            probe.destroy()
            self._metrics = (max(s(ROW_H), idx_h), head_h)
        return self._metrics

    def rows_pending(self):
        """How many rows (headings included) are not drawn now (not yet, or no longer: far from the view)."""
        lay = getattr(self, "_layout", None)
        return 0 if lay is None else len(lay["rows"]) - lay["drawn"]

    def _render_page(self, k):
        """Draw page k (its rows and the headings among them) unless it is drawn already."""
        if not (0 <= k < len(self._pages)):
            return
        page, lay = self._pages[k], self._layout
        if lay["busy"]:
            return
        if page["made"]:
            if page["parked"]:
                self._unpark_page(k)
            return
        lay["busy"] = True
        try:
            if k == 0:
                frame = self.row_container
            else:
                frame = tk.Frame(self._canvas, bg=self._t["bg"], bd=0, highlightthickness=0)
                frame.bindtags((str(frame), self._tag_wheel, "Frame", lay["top"], "all"))
            page["frame"] = frame
            page["made"] = True
            rows = lay["rows"]
            for pos in range(page["start"], page["end"]):
                if rows[pos].kind == "heading":
                    self._make_heading(frame, rows[pos], pos)
                else:
                    self._make_row(frame, lay["ns"][pos], rows[pos], pos, k)
            lay["drawn"] += page["end"] - page["start"]
            if k:
                cw = self._page_width_hint or max(1, self._canvas.winfo_width())
                page["item"] = self._canvas.create_window(0, page["y"], window=frame, anchor="nw", width=cw,
                                                          height=page["h"], tags=(self._tag_page,))
                # keep the pages' stacking (= Tab) order the list order, whatever order they are drawn in
                later = next((p["frame"] for p in self._pages[k + 1:] if p["made"]), None)
                if later is not None:
                    frame.lower(later)
            self._update_scrollregion()
        finally:
            lay["busy"] = False

    def _evict_page(self, k):
        """Park page k - hide its widgets, keep them - so the view can come back to it in a few ms (page 0: its cells are
        unpacked and remembered with their pack options; a later page: its canvas item is hidden). Past PARK_BUDGET
        parked rows in the whole process the parked pages farthest from their view are destroyed for real (_trim_parked),
        a hidden list's page is destroyed outright (a hidden list parks nothing), and a page that cannot be parked is
        destroyed as before. _render_page brings a parked page back."""
        page, lay = self._pages[k], self._layout
        if not page["made"] or page["parked"] or lay["busy"]:
            return
        if not self._viewable():
            self._destroy_page(k)
            return
        try:
            if k == 0:
                cells = []
                for child in self.row_container.winfo_children():
                    info = child.pack_info() if child.winfo_manager() == "pack" else None
                    cells.append((child, {kk: v for kk, v in info.items() if kk != "in"} if info else None))
                    child.pack_forget()
                page["cells"] = cells
            elif page["item"] is not None:
                self._canvas.itemconfigure(page["item"], state="hidden")
            else:
                raise tk.TclError("no canvas item")
        except tk.TclError:
            self._destroy_page(k)
            return
        page["parked"] = True
        lay["drawn"] -= page["end"] - page["start"]
        ListScreen._trim_parked()

    def _unpark_page(self, k):
        """Show a parked page again, as it was (page 0's cells packed back in order)."""
        page, lay = self._pages[k], self._layout
        try:
            if k == 0:
                for child, info in page["cells"] or []:
                    if info is not None:
                        child.pack(**info)
                page["cells"] = None
            elif page["item"] is not None:
                self._canvas.itemconfigure(page["item"], state="normal")
        except tk.TclError:
            self._destroy_page(k)
            return
        page["parked"] = False
        lay["drawn"] += page["end"] - page["start"]

    def release_parked(self):
        """Destroy every parked page of this list - a hidden list must not hold any: the budget is for the lists on show.
        The drawn pages near its view stay, so showing the list again changes nothing on screen."""
        for k, pg in enumerate(self._pages):
            if pg["made"] and pg["parked"]:
                self._destroy_page(k)

    @classmethod
    def _trim_parked(cls):
        """Keep the parked rows of EVERY live list under PARK_BUDGET: a hidden list's parked pages go first (it should
        hold none), then the pages farthest from their own list's view, whichever list they belong to."""
        parked = []          # (0 = its list is hidden, -distance from its list's view, list, page k, rows)
        for lst in list(cls._live.values()):
            try:
                if not lst.winfo_exists():
                    cls._live.pop(id(lst), None)
                    continue
                hidden = not lst.winfo_viewable()
                top = float(lst._canvas.canvasy(0))
            except tk.TclError:
                continue
            for k, pg in enumerate(lst._pages):
                if pg["made"] and pg["parked"]:
                    parked.append((0 if hidden else 1, -abs(pg["y"] - top), lst, k, pg["end"] - pg["start"]))
        total = sum(p[4] for p in parked)
        if total <= cls.PARK_BUDGET:
            return
        for _hidden, _far, lst, k, rows in sorted(parked, key=lambda p: (p[0], p[1])):
            if total <= cls.PARK_BUDGET:
                break
            lst._destroy_page(k)
            total -= rows

    def _destroy_page(self, k):
        """Destroy page k's widgets for real (page 0: row_container's children; a later page: its frame and canvas
        item). Its rows come back through _render_page when the view returns to them."""
        page, lay = self._pages[k], self._layout
        if not page["made"]:
            return
        for pos in range(page["start"], page["end"]):
            rec = self._rec_by_n.pop(lay["ns"][pos], None) if lay["ns"][pos] else None
            if rec is not None:
                for path in rec["paths"]:
                    self._recs.pop(path, None)
        try:
            if k == 0:
                for child in self.row_container.winfo_children():
                    child.destroy()
            else:
                if page["item"] is not None:
                    self._canvas.delete(page["item"])
                page["frame"].destroy()
                page["frame"] = page["item"] = None
        except tk.TclError:
            pass
        if not page["parked"]:
            lay["drawn"] -= page["end"] - page["start"]
        page["made"] = page["parked"] = False
        page["cells"] = None

    def _update_scrollregion(self):
        try:
            box = self._canvas.bbox("all")
            if box:
                self._canvas.configure(scrollregion=box)
        except tk.TclError:
            pass

    def _show_scrollbar_early(self, total_h):
        """A list taller than its view gets its scrollbar BEFORE its rows are drawn, and the rows are drawn at the width they
        will keep. The scroll area's own auto-hiding bar (base.make_scrollable's yscroll_set) only appears on the first
        yscrollcommand, after the rows were laid out at full width: the second layout then shifted every row tail left and
        left a doubled last digit behind for ~0.3 s (B5 gate G4c). yscroll_set stays the fallback, and still hides the bar
        again when the list gets short. Nothing here calls update()."""
        sb, cv = self._scrollbar, self._canvas
        if sb is None:
            return
        try:
            ch = cv.winfo_height()
            if ch <= 1 or total_h <= ch or sb.winfo_manager() == "pack":
                return
            sb.pack(side="right", fill="y", before=cv)
            width = max(1, cv.winfo_width() - sb.winfo_reqwidth())
            self._page_width_hint = width
            if self._inner_item is not None:
                cv.itemconfigure(self._inner_item, width=width)
            cv.itemconfigure(self._tag_page, width=width)
        except tk.TclError:
            pass

    def _ensure_view_pages(self):
        """Draw every page within one view above or below the visible part of the list."""
        if len(self._pages) < 2 or self._layout["busy"]:
            return
        try:
            cv = self._canvas
            ch = max(1, cv.winfo_height())
            top = cv.canvasy(0)
        except tk.TclError:
            return
        lo, hi = top - ch, top + 2 * ch
        for k, pg in enumerate(self._pages):
            if (not pg["made"] or pg["parked"]) and pg["y"] < hi and pg["y"] + pg["h"] > lo:
                self._render_page(k)
        # ... and destroy the pages that are now two views or more away (never the one holding the focus)
        keep_lo, keep_hi = top - 2 * ch, top + 3 * ch
        try:
            focus_rec = self._recs.get(str(self.focus_get()))
        except (KeyError, tk.TclError):
            focus_rec = None
        for k, pg in enumerate(self._pages):
            if pg["made"] and not (pg["y"] < keep_hi and pg["y"] + pg["h"] > keep_lo) \
                    and not (focus_rec is not None and focus_rec["page"] == k):
                self._evict_page(k)

    def _on_yscroll(self, first, last):
        try:
            self.tk.call(self._yscroll_orig, first, last)      # the scroll area's own auto-hiding scrollbar
        except tk.TclError:
            pass
        if len(self._pages) > 1:
            self._ensure_view_pages()

    def _on_canvas_configure(self, event):
        self._page_width_hint = None       # the canvas has its real width now (see _show_scrollbar_early)
        if len(self._pages) > 1:
            try:
                self._canvas.itemconfigure(self._tag_page, width=event.width)
            except tk.TclError:
                pass

    def _make_heading(self, frame, row_data, pos):
        t, s, lay = self._t, self._s, self._layout
        top_gap = s(HEADING_GAP) if pos else 0
        _cell_h, head_h = self._row_metrics()
        cell = tk.Frame(frame, bg=t["bg"], height=head_h + top_gap + s(HEADING_BELOW))
        cell.pack_propagate(False)
        cell.pack(fill="x")
        text = one_line(row_data.label).strip() or "\u2014"   # a group with no name still separates
        if StatusLine is not None and self._measure(C9, text) > lay["long_title"]:
            # a long group name is the eliding line (middle ellipsis, whole name in cget and the hover tip),
            # as a long title is: a plain Label was cut off mid-word at the cell edge (B5 fix ADV2-05)
            lbl = StatusLine(cell, self.ctx.theme, text=text, bg=t["bg"], fg=t["value"], font=C9, bd=0, padx=0,
                             pady=0)
            lbl.pack(fill="x", padx=(lay["icol"], 0), pady=(top_gap, s(HEADING_BELOW)))
        else:
            lbl = tk.Label(cell, text=text, bg=t["bg"], fg=t["value"], font=C9, bd=0, padx=0, pady=0, anchor="w")
            lbl.pack(anchor="w", padx=(lay["icol"], 0), pady=(top_gap, s(HEADING_BELOW)))
        for w, cls in ((cell, "Frame"), (lbl, "Label")):
            w.bindtags((str(w), self._tag_wheel, cls, lay["top"], "all"))

    def _make_row(self, frame, n, row_data, pos, page_k):
        t, s, lay = self._t, self._s, self._layout
        top = lay["top"]
        cell = tk.Frame(frame, bg=t["bg"], height=lay["cell_h"])
        cell.pack_propagate(False)       # a fixed height: the page model's y is exact
        cell.pack(fill="x", pady=lay["half"])
        idx_text = "%02d" % n
        widths = lay["digit_w"]
        iw = widths.get(len(idx_text))
        if iw is None:
            iw = widths[len(idx_text)] = self._measure(C11B, idx_text) + 2 * s(INDEX_PAD)
        idx = tk.Label(cell, text=idx_text, bg=t["bg"], fg=t["value"], font=C11B, bd=0, padx=s(INDEX_PAD),
                       pady=1)      # the focus chip: digits + 4 px each side, line + 2 px tall
        # the index column (icol, sized for the longest index) ends INDEX_GAP before the row box: packed,
        # not gridded (cheaper for long lists), with the lead pad that right-aligns the digits
        idx.pack(side="left", padx=(max(0, lay["icol"] - s(INDEX_GAP) - iw), s(INDEX_GAP)))
        row = tk.Frame(cell, bg=t["bg"], height=s(ROW_H), takefocus=1, highlightthickness=1,
                       highlightbackground=t["bg"], highlightcolor=t["bright"])
        row.pack_propagate(False)
        row.pack(side="left", fill="x", expand=True)
        text_col = tk.Frame(row, bg=t["bg"], highlightthickness=1, highlightbackground=t["bg"],
                            highlightcolor=t["bg"])
        text_col.pack(fill="both", expand=True)
        title_text = row_data.label if row_data.label is not None else ""
        plain = one_line(title_text)
        if StatusLine is not None and (plain != title_text or self._measure(C11, plain) > lay["long_title"]):
            title = StatusLine(text_col, self.ctx.theme, text=title_text, bg=t["bg"], fg=t["bright"], font=C11,
                               bd=0, padx=0, pady=0)
        else:
            title = tk.Label(text_col, text=title_text, bg=t["bg"], fg=t["bright"], font=C11, bd=0, padx=0,
                             pady=0, anchor="w")
        tail_text = row_data.tail if row_data.tail else (row_data.caption or "")
        tail = None
        if tail_text:
            tail = tk.Label(text_col, text=tail_text, bg=t["bg"], fg=t["value"], font=C9, bd=0, padx=0, pady=0,
                            anchor="e")
            tail.pack(side="right", padx=(s(24), s(TAIL_X) - 2))
        title.pack(side="left", fill="x", expand=True, padx=(s(TITLE_X) - 2, 0 if tail else s(TAIL_X) - 2))

        row.bindtags((str(row), self._tag_row, "Frame", top, "all"))
        parts = [text_col, title] + ([tail] if tail is not None else [])
        for w in parts:
            w.bindtags((str(w), self._tag_part, w.winfo_class(), top, "all"))
        for w, cls in ((cell, "Frame"), (idx, "Label")):
            w.bindtags((str(w), self._tag_wheel, cls, top, "all"))
        rec = {"n": n, "pos": pos, "page": page_k, "y": lay["ys"][pos] + lay["half"], "cell": cell, "idx": idx,
               "row": row, "text_col": text_col, "data": row_data, "hover": False, "focus": False,
               "paths": [str(w) for w in [row] + parts]}
        for w in [row] + parts:
            self._recs[str(w)] = rec
        self._rec_by_n[n] = rec

        if row_data.context_items:
            c = self._cmd
            menu_script = 'if {"[%s menu %%W %%X %%Y]" == "break"} break' % c
            for w in [row] + parts:
                w.bind("<Button-3>", menu_script)
            # Keyboard path while the row has focus: Shift-F10 and the
            # dedicated menu key (Tk keysym "App") both post the menu.
            key_script = 'if {"[%s kmenu %%W]" == "break"} break' % c
            row.bind("<Shift-F10>", key_script)
            row.bind("<App>", key_script)

    def _set_empty(self, empty):
        """The empty state (NO CHATS / NO PROJECTS) sits over the list area, never inside
        row_container (an empty list has no children there)."""
        if not self.empty_text:
            return
        if not empty:
            if self.empty_state is not None and self.empty_state.winfo_manager():
                self.empty_state.place_forget()
            return
        if self.empty_state is None:
            title = str(self.empty_text).upper()
            if EmptyState is not None:
                try:
                    self.empty_state = EmptyState(self, self.ctx.theme, title, caption=self.empty_caption or None,
                                                  ground=self._t["bg"])
                    self._list_outer.bind("<Configure>", lambda e: self._fit_empty_caption(), add="+")
                except Exception:  # noqa: BLE001
                    self.empty_state = None
            if self.empty_state is None:
                self.empty_state = tk.Label(self, text=title, bg=self._t["bg"], fg=self._t["bright"],
                                            font=("Consolas", 13, "bold"))
        self.empty_state.place(in_=self._list_outer, x=self._s(INDEX_COL + TITLE_X), y=self._s(ROW_GAP // 2))
        self.empty_state.lift()
        self._fit_empty_caption()

    def _fit_empty_caption(self):
        """The empty state's caption wraps to the list's width (whole at the 820 px floor, never cut)."""
        es = self.empty_state
        cap = getattr(es, "caption_label", None) if es is not None else None
        if cap is None:
            return
        try:
            width = self._list_outer.winfo_width() - self._s(INDEX_COL + TITLE_X) - self._s(16)
            cap.configure(wraplength=max(self._s(160), width))
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ row events (one command per list)
    def _on_row_event(self, action, path, *args):
        if action == "wheel":
            try:
                delta = int(float(args[0]))
            except (IndexError, ValueError):
                return None
            if delta:
                # a notch is 120; a precision touchpad sends small deltas that add up to it. Scroll whole units
                # as the sum reaches them and keep the rest (it was one unit per event: 10 deltas of -12 moved
                # ten units, ADV2-04); a turn of direction drops the rest
                acc = self._wheel_acc
                if acc and (acc > 0) != (delta > 0):
                    acc = 0
                acc += delta
                units = int(acc / 120)
                self._wheel_acc = acc - units * 120
                if units:
                    try:
                        self._canvas.yview_scroll(-units, "units")
                    except tk.TclError:
                        pass
            return None
        rec = self._recs.get(path)
        if rec is None:
            return None
        row = rec["row"]
        data = rec["data"]
        if action == "activate":
            if data.on_activate:
                data.on_activate()
            return "break"
        if action == "click":
            try:
                row.focus_set()
            except tk.TclError:
                pass
            if data.on_activate:
                data.on_activate()
            return None
        if action in ("tab", "btab"):
            return self._step_focus(rec, 1 if action == "tab" else -1)
        if action == "focusin":
            rec["focus"] = True
            self._paint_row(rec)
            self._see(rec)
            return None
        if action == "focusout":
            rec["focus"] = False
            self._paint_row(rec)
            return None
        if action == "enter":
            rec["hover"] = True
            self._paint_row(rec)
            return None
        if action == "leave":
            if args and args[0] == "NotifyInferior":
                return None          # the pointer went onto the row's own title / tail
            rec["hover"] = False
            self._paint_row(rec)
            return None
        if action == "menu" and data.context_items:
            try:
                row.focus_set()
            except tk.TclError:
                pass
            self._popup_context_menu(data.context_items, int(args[0]), int(args[1]))
            return "break"
        if action == "kmenu" and data.context_items:
            self._popup_context_menu(data.context_items, row.winfo_rootx() + 60, row.winfo_rooty() + row.winfo_height())
            return "break"
        return None

    def _step_focus(self, rec, step):
        """Tab / Shift-Tab from a row: to the next / previous row (drawing its page and scrolling it
        into view first, so it is mapped when it takes the focus). Past either end: Tk's own
        traversal (None)."""
        n = rec["n"] + step
        if not (1 <= n <= self._n_rows):
            return None
        lay = self._layout
        pos = lay["pos_of_n"][n - 1]
        self._render_page(lay["page_of"][pos])
        target = self._rec_by_n.get(n)
        if target is None:
            return None
        self._see(target)
        try:
            self.update_idletasks()          # the canvas maps the page that just came into view
            target["row"].focus_set()
        except tk.TclError:
            return None
        return "break"

    def _paint_row(self, rec):
        """rest: no box; hover: 1 px bright outline; focus: 2 px bright outline + the inverse index chip."""
        t = self._t
        focus, hover = rec["focus"], rec["hover"]
        edge = t["bright"] if (focus or hover) else t["bg"]
        ring = t["bright"] if focus else t["bg"]
        try:
            rec["row"].configure(highlightbackground=edge, highlightcolor=edge)
            rec["text_col"].configure(highlightbackground=ring, highlightcolor=ring)
            rec["idx"].configure(bg=t["bright"] if focus else t["bg"], fg=t["bg"] if focus else t["value"])
        except tk.TclError:
            pass

    def _see(self, rec):
        """Scroll the list so a row (its record) is wholly visible. Positions come from the model
        (exact: fixed cell heights), so this works before the row's page is laid out."""
        cv = self._canvas
        try:
            region = [float(v) for v in str(cv.cget("scrollregion")).split()]
            total = region[3] - region[1] if len(region) == 4 else 0.0
            ch = cv.winfo_height()
            if total <= ch or total <= 1:
                return
            top = rec["y"] - (region[1] if len(region) == 4 else 0.0)
            bot = top + self._layout["cell_h"]
            y0 = cv.canvasy(0) - region[1]
            if top < y0:
                cv.yview_moveto(top / total)
            elif bot > y0 + ch:
                cv.yview_moveto((bot - ch) / total)
        except (tk.TclError, ValueError, KeyError):
            pass

    @property
    def row_cells(self):
        """The row cells drawn now, in list order, headings excluded (a new list after every set_rows;
        a list longer than ROW_PAGE has only the pages near the view drawn)."""
        return [self._rec_by_n[n]["cell"] for n in sorted(self._rec_by_n)]


class ProjectList(ListScreen):
    """Projects' list (B5): one column of rows under group headings (ACTIVE OPS / UNGROUPED),
    each with its chat count as the tail. set_items(items) is the adapter for the
    DirectoryItem list ProjectsScreen._apply_grid builds: an item's group gives the
    headings (adjacent items with the same group share one; group None = no heading),
    its caption gives the tail, its label and on_activate the row. Digits number the
    projects only."""

    def __init__(self, parent, ctx, empty_text="No projects", empty_caption="Press + NEW PROJECT to make one"):
        super().__init__(parent, ctx, show_search=False, empty_text=empty_text, empty_caption=empty_caption)
        self._items = []

    def set_items(self, items):
        self._items = list(items)
        rows = []
        unset = object()
        current = unset
        for item in self._items:
            group = getattr(item, "group", None)
            # headings follow the group's KEY (its id), not its name: two groups named the same used to share one heading,
            # and a group named like the ungrouped block merged with it (B5 ADV1-08)
            key = getattr(item, "group_key", group)
            if key != current:
                current = key
                if group is not None:
                    rows.append(ListRow(one_line(group).strip().upper(), kind="heading"))
            rows.append(ListRow(item.label, on_activate=item.on_activate, tail=getattr(item, "caption", "") or None))
        self.set_rows(rows)
