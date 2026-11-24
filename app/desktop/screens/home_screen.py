"""Home (B4, D-18: the approved r3 Home): the A1 / HOME header with its ticking clock (weekday + date +
time), a Model/Backend/Uplink status-flag row, a System status card + Quick resume box pair, the six
numbered destinations (New session, Chats, Projects, Image uplink, Settings, Directory log) enlarged
to fill the screen, and a right-hand action column (Resume last / Export all / Shutdown) - real data
throughout, not mockup flavor text. Reachable via the rail's Home icon; not a forced gate - see
theme.py's launch_screen for the alternate "open to last chat" preference.

D-13: the boot-log strip is gone (Home reads no log file at all). D-12 L-02: the "System: ... Build:
local-01" line is gone. D-34: the status word stays "Offline". D-35: Shutdown opens the confirm.

Layout (approved pixels at 100 %, window x = Home x + 42): the header band (56 px), the flag row
(window y 66-86), then one grid whose columns line the top band up with the option boxes:
[index 54][box][gutter 53][box][gap 27][actions]. The two box columns share the width equally; the
action column is 173 px at 1100 wide and narrows (never below what its buttons need) on a narrow
window, so the right-hand column of boxes stays down to the 820 px floor. The box rows share the
height (kit.home: each at least 62 px), so all six boxes show whole, without scrolling, down to the
820x560 floor (B3h hand-off). Fonts stay fixed.

B4 fix ADV1-01/02/03 - the System status card is BOUNDED, the grid below it is protected:
  - body row 1 (the option grid + the action column) has a hard minimum: 17 px + max(three 62 px box
    rows + two 6 px gaps, the action column's 170 px), so neither the boxes nor the buttons can be
    squeezed by the card (_fit_rows);
  - the Model value (model_row_var: a 300-char model name, "unreachable: <400-char error>", a raw
    error page) wraps onto at most _MODEL_MAX_LINES lines, fewer when Home is short: as many as fit
    while every option box still shows its title and whole caption (OptionCell.text_need). The rest
    is elided in the middle; the whole value stays in model_row_var, the row's cget("text") and its
    hover tip (kit.home.RecordRow max_lines). The cap is computed from the body's size and the static
    card pieces only, never from the value, so it cannot feed back into the layout.

B4 fix ADV2-01/02: the action column's height is the sum of its scaled parts (_act_col_h), so no display
ratio leaves SHUTDOWN short; on the kit-less fallback path the Model value and the Quick resume title are
one line, elided in the middle to the room the grid columns give them (never wider than their box).

Stable public names (the audit session's tests\\test_window_fit strict Home block reads them):
    option_boxes    the six option box widgets, in order 01..06 (a property: follows re-renders)
    option_titles   their six title widgets, same order (a property)
    action_buttons  {"resume", "export_all", "shutdown"} -> the action buttons
    status_row      the status-flag row frame
    export_all_btn  the Export all button (its .config(state=, text=) writers are unchanged)

Implementation note - "Directory log" (an open item in the design plan):
resolved as opening this app's own data/desktop.log with the OS's default
text viewer, reusing the exact same os.startfile() pattern already
verified for the image-generation link (chat_view.py's
open_external_link) rather than inventing a new screen or backend
endpoint for it.
"""

import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog

from .. import theme as theme_mod
from .base import Screen, show_toast
from .directory_screen import DirectoryItem, DirectoryScreen

try:  # B4: the approved Home pieces (a guarded import: plain Tk widgets stand in without the kit)
    from ..kit import C9, C11B, geom_ratio as _kit_geom_ratio
    from ..kit.flag import StatusFlag as _StatusFlag
    from ..kit.header import home_header as _home_header
    from ..kit.home import BarMark as _BarMark
    from ..kit.home import GapRule as _GapRule
    from ..kit.home import RecordRow as _RecordRow
    from ..kit.widgets import KitButton as _KitButton
    from ..kit.widgets import StatusLine as _StatusLine
    from ..kit.widgets import TextAction as _TextAction
    _KIT_ERROR = None
except Exception as _exc:  # noqa: BLE001 - Home must open even if the kit cannot be imported
    _KIT_ERROR = _exc

_LOG_PATH = os.path.join(theme_mod.DATA_DIR, "desktop.log")

# approved r3 geometry, in pixels at 100 % (scaled by the display ratio)
_GUTTER_L, _GUTTER_R = 16, 15          # content x 58 .. 1085 at 1100 wide
_INDEX_W, _MID_W, _GAP_W = 54, 53, 27  # first index gutter, middle gutter (col 2's index), boxes -> actions
_ACT_W = 173                           # action column at 1100 wide (window x 912..1085)
_BODY_W = 1027                         # the grid's width at 1100 wide
_BTN_H, _BTN_GAP = 46, 16              # three 46 px buttons 16 px apart = box row 1 (170 px)
_CLOCK_FORMAT = "%a  %Y-%m-%d  %H:%M:%S"   # "SUN  2026-10-04  10:15:00" (upper-cased)
_MARK_MIN_CARD_W = 400                 # a narrower status card (a small window) gives the bar mark's room to the values
_MODEL_MAX_LINES = 3                   # the Model value's line cap on a tall Home (fewer when Home is short)
_BOX_ROW_MIN, _BOX_GAP_MIN = 62, 6     # directory_screen's home-style row floors (kit.home.MIN_BOX_H, the gap rows)
_GRID_TOP = 17                         # the option grid / action column top pad (approved: card -> row 1)

_ELLIPSIS = "\u2026"
_PLAIN_ROOM_START = 260                # the kit-less path's value room before the first body <Configure>

_kit_failure_reported = False


def _act_col_h(s):
    """The action column's height FROM ITS PARTS: three s(46) buttons + two s(16) gaps. Not s(170): s()
    rounds each part on its own, so at a non-integer ratio (125 %) the parts came to 1-2 px more than
    s(170) and pack took them from SHUTDOWN (B4 fix ADV2-01). Equal to 170 at 100 %."""
    return 3 * s(_BTN_H) + 2 * s(_BTN_GAP)


def _plain_line(text):
    """The kit-less path's single displayed line: every control character (line breaks, tabs, NUL, ESC,
    U+2028/9) becomes a space (kit.widgets.one_line's job; the plain path cannot rely on the kit)."""
    if not isinstance(text, str):
        text = str(text)
    return "".join(" " if (ord(ch) < 32 or 127 <= ord(ch) < 160 or ch in "\u2028\u2029") else ch for ch in text)


def _plain_elide(text, fits):
    """The longest middle-elided form of text (head + ELLIPSIS + tail) for which fits(candidate) holds;
    text itself when it fits. fits must be monotone in length."""
    if fits(text):
        return text
    n = len(text)
    lo, hi = 0, n - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        head = (mid + 1) // 2
        if fits(text[:head] + _ELLIPSIS + text[n - (mid - head):]):
            lo = mid
        else:
            hi = mid - 1
    if lo <= 0:
        return _ELLIPSIS
    head = (lo + 1) // 2
    return text[:head] + _ELLIPSIS + text[n - (lo - head):]


def _report_kit_failure():
    global _kit_failure_reported
    if _kit_failure_reported:
        return
    _kit_failure_reported = True
    try:
        sys.stderr.write("[kit] home: plain widgets stand in: %r\n" % (_KIT_ERROR,))
    except Exception:  # noqa: BLE001
        pass


class HomeScreen(Screen):
    def __init__(self, parent, ctx):
        super().__init__(parent, ctx)
        theme = ctx.theme
        self._resume_conv_id = None
        self._clock_after_id = None
        # The status poll re-arms itself every 2 s while the model loads. The
        # pending timer is tracked (and each refresh numbered) so re-entering
        # this screen replaces the old chain instead of stacking another one.
        self._status_after_id = None
        self._status_seq = 0
        self._shown_error = None      # the model-server error already put on screen, so a poll every 2 s does not repeat it
        self._start_time = time.time()
        self._kit = _KIT_ERROR is None
        if not self._kit:
            _report_kit_failure()
        self._ratio = _kit_geom_ratio(self) if self._kit else 1.0
        s = self._s

        # ---- header: A1 code box, HOME, the boxed clock, double rule (D-12 L-02: no System/Build line)
        self.clock_var = tk.StringVar()
        self.header = self._make_header()

        # ---- status flags (D-17: quiet outlines, READY green, only an error filled)
        self.status_row = tk.Frame(self, bg=theme.bg)
        self.status_row.pack(fill="x", padx=(s(_GUTTER_L), s(_GUTTER_R)), pady=(s(10), s(9)))
        self.model_chip = self._make_toggle(self.status_row, "Model")
        self.backend_chip = self._make_toggle(self.status_row, "Backend")
        self.uplink_chip = self._make_toggle(self.status_row, "Uplink")
        # A2 (bug-fix batch 30): the RESTART word at the row's end - the way back from a model server that stopped or hangs
        self.restart_action = self._make_restart_action(self.status_row)

        # ---- one grid: [index][box][gutter][box][gap][actions]; the top band lines up with the boxes
        body = self._body = tk.Frame(self, bg=theme.bg)
        body.pack(fill="both", expand=True, padx=(s(_GUTTER_L), s(_GUTTER_R)), pady=(0, s(18)))
        # the two box columns get explicit, equal widths from _on_body_configure (grid's uniform= is not
        # honoured once the top band spans them: measured 384 / 336 px instead of 360 / 360)
        body.columnconfigure(0, minsize=s(_INDEX_W), weight=0)
        body.columnconfigure(2, minsize=s(_MID_W), weight=0)
        body.columnconfigure(4, minsize=s(_GAP_W), weight=0)
        body.rowconfigure(1, weight=1)
        self._cols = None
        self._body_h = None
        self._row1_min = None
        self._model_row = None
        self._card_rows = []
        self._plain_fit = []          # kit-less path only: (label, var, room) one-line values (fix ADV2-02)

        status_box = self._status_box = self._info_box(body, "System status", mark=True)
        status_box.grid(row=0, column=0, columnspan=2, sticky="new")
        self.uptime_var = tk.StringVar(value="00:00:00")
        self.model_row_var = tk.StringVar(value="checking...")
        self.context_row_var = tk.StringVar(value="-")
        self.backend_row_var = tk.StringVar(value=ctx.api.base_url)
        self._info_row(status_box, "Uptime", self.uptime_var)
        self._info_row(status_box, "Model", self.model_row_var)
        self._info_row(status_box, "Context", self.context_row_var)
        self._info_row(status_box, "Backend", self.backend_row_var)
        # A3: one dim line under the rows, packed only while the model is larger than RAM + VRAM (_set_fit_note)
        self.fit_note_var = tk.StringVar(value="")
        holder = getattr(status_box, "content", status_box)
        self.fit_note = tk.Label(holder, textvariable=self.fit_note_var, bg=theme.bg, fg=getattr(theme, "value", theme.dim),
                                 font=C9 if self._kit else theme_mod.FONT_CAPTION, anchor="w", justify="left", bd=0, padx=0, pady=0)
        self.fit_note.bind("<Configure>", lambda e: self.fit_note.configure(wraplength=max(s(120), e.width - s(4))), add="+")

        resume_box = self._resume_box = self._info_box(body, "Quick resume")
        resume_box.grid(row=0, column=3, columnspan=3, sticky="new")
        self.resume_var = tk.StringVar(value="no recent session")
        self.resume_cap_var = tk.StringVar(value="")
        panel = tk.Frame(resume_box.content, bg=theme.panel, highlightthickness=1, highlightbackground=theme.dim,
                         highlightcolor=theme.dim, cursor="hand2")
        panel.pack(fill="x")
        if self._kit:
            # one line, elided in the middle when long; the whole title is in its hover tip and cget("text")
            resume_title = _StatusLine(panel, theme, textvariable=self.resume_var, font=C11B, bg=theme.panel,
                                       fg=theme.bright, cursor="hand2", bd=0, padx=0, pady=0)
            cap_font = C9
        else:
            # B4 fix ADV2-02: one line, elided in the middle to the room the grid gives the box (never wider,
            # never taller); the whole title stays in resume_var
            resume_title = tk.Label(panel, bg=theme.panel, fg=theme.text, font=theme_mod.FONT_BODY_BOLD,
                                    anchor="w", justify="left", cursor="hand2", bd=0, padx=0, pady=0)
            self._plain_fit_add(resume_title, self.resume_var, self._plain_resume_room)
            cap_font = theme_mod.FONT_CAPTION
        resume_title.pack(fill="x", padx=s(11), pady=(s(20), 0))
        resume_cap = tk.Label(
            panel, textvariable=self.resume_cap_var, bg=theme.panel, fg=getattr(theme, "value", theme.dim),
            font=cap_font, anchor="w", cursor="hand2", bd=0, padx=0, pady=0,
        )
        resume_cap.pack(fill="x", padx=s(11), pady=(s(4), s(25)))
        self.resume_title = resume_title
        for w in (panel, resume_title, resume_cap):
            w.bind("<Button-1>", lambda e: self._on_resume(), add="+")

        # ---- the six options, enlarged to fill the space (D-18)
        self.directory = DirectoryScreen(body, ctx, columns=2, style="home")
        self.directory.grid(row=1, column=0, columnspan=4, sticky="nsew", pady=(s(_GRID_TOP), 0))

        # ---- action column: spans box row 1 at 100 %; its frame does not propagate, so the KitButton
        # faces (which request their painted size) can never hold the column wide
        self._act_w = s(_ACT_W)
        act = self._act = tk.Frame(body, bg=theme.bg, width=self._act_w, height=_act_col_h(s))
        act.pack_propagate(False)
        act.grid(row=1, column=5, sticky="n", pady=(s(_GRID_TOP), 0))
        resume_btn = self._rail_btn(act, "Resume last", self._on_resume)
        self.export_all_btn = self._rail_btn(act, "Export all", self._on_export_all)
        shutdown_btn = self._rail_btn(act, "Shutdown", lambda: self.ctx.shutdown(), danger=True, last=True)
        self.action_buttons = {"resume": resume_btn, "export_all": self.export_all_btn, "shutdown": shutdown_btn}
        self._act_min = max(self._natural_width(b) for b in self.action_buttons.values())
        body.columnconfigure(5, minsize=0, weight=0)
        body.bind("<Configure>", self._on_body_configure, add="+")

    # ---- stable public names (B4; tests\test_window_fit reads them) ----

    @property
    def option_boxes(self):
        """The six option box widgets, in order 01..06 (follows every re-render of the grid)."""
        return list(self.directory.boxes)

    @property
    def option_titles(self):
        """The six option title widgets, in the same order as option_boxes."""
        return list(self.directory.titles)

    # ---- small layout helpers ----

    def _s(self, n):
        return int(round(n * self._ratio))

    def _make_header(self):
        theme = self.ctx.theme
        if self._kit:
            try:
                header = _home_header(self, theme, title="HOME", code="A1", clock="", screen=self)
                header.pack(fill="x")
                return header
            except Exception:  # noqa: BLE001 - fall through to the plain header
                pass
        header = tk.Frame(self, bg=theme.bg)
        header.pack(fill="x", padx=16, pady=(16, 0))
        tk.Label(header, text="HOME", bg=theme.bg, fg=theme.bright, font=theme_mod.FONT_LABEL).pack(side="left")
        tk.Label(header, textvariable=self.clock_var, bg=theme.bg, fg=theme.dim, font=theme_mod.FONT_CAPTION).pack(
            side="right"
        )
        return header

    def _on_body_configure(self, event):
        """Column widths from the grid's real width: the action column narrows with the window (173 px at
        1100 wide, never below what its buttons need) and the two box columns share the rest equally."""
        if event.widget is not self._body:
            return
        s = self._s
        want = int(round(s(_ACT_W) * event.width / float(max(1, s(_BODY_W)))))
        act = max(self._act_min, min(s(_ACT_W), want))
        room = max(0, event.width - s(_INDEX_W) - s(_MID_W) - s(_GAP_W) - act)
        cols = (act, room // 2, room - room // 2)
        if cols != self._cols:
            self._cols = cols
            if act != self._act_w:
                self._act_w = act
                self._act.configure(width=act)
            self._body.columnconfigure(1, minsize=cols[1])
            self._body.columnconfigure(3, minsize=cols[2])
            for entry in self._plain_fit:          # kit-less path: re-elide to the new column widths
                self._plain_fit_one(entry)
        self._fit_rows(event.height)

    def _fit_rows(self, body_h=None):
        """B4 fix ADV1-01/02/03: protect the option grid and the action column from the card above.
        Row 1 gets a hard floor (62 px box rows, the 170 px action column); the Model value gets a line
        cap: the most lines (up to _MODEL_MAX_LINES) for which the card still leaves every box room for
        its title + whole caption, else one line. Geometry-only inputs: the body's height, the column
        widths, the fixed card pieces and the option captions - never the Model value itself."""
        if body_h is not None:
            self._body_h = int(body_h)
        body_h = self._body_h
        if not body_h or body_h <= 1:
            return
        s = self._s
        cells = list(getattr(self.directory, "cells", []) or [])
        cols = max(1, int(getattr(self.directory, "columns", 2) or 2))
        nrows = max(1, (len(cells) + cols - 1) // cols) if cells else 3
        gaps = (nrows - 1) * s(_BOX_GAP_MIN)
        act_h = _act_col_h(s)
        floor = s(_GRID_TOP) + max(nrows * s(_BOX_ROW_MIN) + gaps, act_h)
        if floor != self._row1_min:
            self._row1_min = floor
            self._body.rowconfigure(1, minsize=floor)
        row = self._model_row
        if row is None:
            return
        try:
            widths = [self._cols[1], self._cols[2]] if self._cols else [b.winfo_width() for b in cells[:cols]]
            need = s(_BOX_ROW_MIN)
            for i, cell in enumerate(cells):
                need = max(need, int(cell.text_need(widths[i % len(widths)] if widths else 1)))
            soft = s(_GRID_TOP) + max(nrows * need + gaps, act_h)
            box = self._status_box
            fixed = sum(int(r.winfo_reqheight()) for r in self._card_rows if r is not row)
            rule_h = int(box.rule.winfo_reqheight()) if getattr(box, "rule", None) is not None else 0
            bar_h = int(box.bar.winfo_reqheight()) if getattr(box, "bar", None) is not None else 0
            resume_h = int(self._resume_box.winfo_reqheight())
            row_h, ls = s(20), int(row.line_height)
            n = 1
            for k in range(_MODEL_MAX_LINES, 0, -1):
                card = rule_h + max(bar_h, fixed + row_h + (k - 1) * ls + 2)
                if max(card, resume_h) + soft <= body_h:
                    n = k
                    break
        except (tk.TclError, AttributeError, TypeError, ValueError):
            n = 1
        if row.cget("max_lines") != n:
            row.configure(max_lines=n)

    @staticmethod
    def _natural_width(btn):
        try:
            return int(btn.natural_size()[0])
        except Exception:  # noqa: BLE001 - a plain tk.Button (no kit)
            return int(btn.winfo_reqwidth())

    def _info_box(self, parent, title, mark=False):
        """A label-in-gap rule titled `title` over the box's content frame (box.content). mark=True puts
        the static bar mark left of the content (the System status card); it is decoration only, so on a
        card narrower than 400 px (a small window) it steps aside and the values get its room (a long
        value then wraps onto fewer lines and leaves the option boxes more height)."""
        theme = self.ctx.theme
        box = tk.Frame(parent, bg=theme.bg)
        box.bar = None
        if self._kit:
            box.rule = _GapRule(box, theme, title)
            box.rule.pack(fill="x")
        else:
            box.rule = tk.Label(
                box, text=title.upper(), bg=theme.bg, fg=theme.dim, font=theme_mod.FONT_LABEL,     # B15: no spacing
            )
            box.rule.pack(anchor="w", pady=(8, 4))
        if mark and self._kit:
            row = tk.Frame(box, bg=theme.bg)
            row.pack(fill="x")
            bar = box.bar = _BarMark(row, theme)
            bar.pack(side="left", anchor="n", padx=(self._s(4), self._s(16)))
            content = tk.Frame(row, bg=theme.bg, width=1)
            content.pack(side="left", fill="x", expand=True, anchor="n")
            box.bind("<Configure>", lambda e, b=bar, c=content: self._fit_card_mark(e, b, c), add="+")
        else:
            content = tk.Frame(box, bg=theme.bg)
            content.pack(fill="x")
        box.content = content
        return box

    def _fit_card_mark(self, event, bar, content):
        # geometry only (the card's width comes from the grid columns, never from its content): no feedback
        want = event.width >= self._s(_MARK_MIN_CARD_W)
        shown = bool(bar.winfo_manager())
        if want and not shown:
            bar.pack(side="left", anchor="n", padx=(self._s(4), self._s(16)), before=content)
        elif shown and not want:
            bar.pack_forget()

    def _make_restart_action(self, parent):
        """A2: the RESTART word (kit TextAction; a plain Label without the kit) after the status flags."""
        theme = self.ctx.theme
        if self._kit:
            action = _TextAction(parent, theme, text="Restart", upper=True, ground=theme.bg, command=self._on_restart_clicked)
        else:
            action = tk.Label(parent, text="RESTART", bg=theme.bg, fg=theme.dim, font=theme_mod.FONT_CAPTION, cursor="hand2")
            action.bind("<Button-1>", lambda e: self._on_restart_clicked())
        # placed at the row's right end, not packed: the word is 1 px taller than the flags and a packed one would
        # push every row below it down (fx/test_home_b4 H19 pins the geometry)
        action.place(in_=parent, relx=1.0, rely=0.5, anchor="e")
        return action

    def _on_restart_clicked(self):
        from ..model_server_control import restart_model_server   # noqa: PLC0415 - a late import keeps Home's own imports guarded
        restart_model_server(self, self.ctx, self.ctx.api, on_done=lambda ok: self._refresh_status() if self.is_visible() else None)

    def _set_fit_note(self, fit):
        """A3: the one-line note under the status rows while the model is larger than RAM + VRAM; its height is counted
        by _fit_rows only while it shows."""
        too_large = bool(isinstance(fit, dict) and fit.get("too_large"))
        text = ""
        if too_large:
            text = (f"This model is larger than RAM + VRAM ({float(fit.get('model_gb') or 0):.1f} GB vs "
                    f"{float(fit.get('memory_gb') or 0):.1f} GB) - it may fail to load or run very slowly.")
        self.fit_note_var.set(text)
        shown = bool(self.fit_note.winfo_manager())
        if too_large and not shown:
            self.fit_note.pack(fill="x", pady=(self._s(4), 0))
            if self.fit_note not in self._card_rows:
                self._card_rows.append(self.fit_note)
        elif shown and not too_large:
            self.fit_note.pack_forget()
            if self.fit_note in self._card_rows:
                self._card_rows.remove(self.fit_note)
        if shown != too_large:
            self._fit_rows()

    @staticmethod
    def _chip_word(status, health):
        """The Model flag's word: Ready / Loading... / Not responding (hung) / Stopped (it ran and died) / Error (it never
        started: no model, a DLL, its port) / Offline."""
        if status == "error":
            return "Stopped" if health.get("reason") == "stopped" else "Error"
        return {"starting": "Loading...", "hung": "Not responding"}.get(status, "Offline")

    def apply_watch_health(self, health):
        """A1: MainWindow's 10 s watcher hands its reading here - the flag follows it between Home's own polls."""
        try:
            status = health.get("status", "?")
            self._set_toggle(self.model_chip, status == "ok", "Ready", self._chip_word(status, health))
        except tk.TclError:
            pass

    def _info_row(self, parent, label, var):
        """KEY . . . . VALUE: the value right-aligned at the card's right edge; a value too long for
        its row (a 300-char model name, "unreachable: <400-char error>") wraps - never cut."""
        theme = self.ctx.theme
        holder = getattr(parent, "content", parent)
        if self._kit:
            # the Model value is the one that can be long: its line cap comes from _fit_rows (ADV1-01)
            row = _RecordRow(holder, theme, key=label, textvariable=var,
                             max_lines=_MODEL_MAX_LINES if label == "Model" else None)
            row.pack(fill="x")
            self._card_rows.append(row)
            if label == "Model":
                self._model_row = row
            return row
        row = tk.Frame(holder, bg=theme.bg)
        row.pack(fill="x", padx=10, pady=1)
        key = tk.Label(row, text=label, bg=theme.bg, fg=theme.dim, font=theme_mod.FONT_CAPTION)
        key.pack(side="left", anchor="n")
        # B4 fix ADV2-02 (the kit-less path): the value is ONE line, elided in the middle to the room the grid
        # gives the card. A wrapping label (wraplength=360) asked for more than the card's columns and pushed
        # the whole body grid, actions included, off the window's right edge at 984x520 / 820x560 / 820x520.
        # The whole value stays in var (model_row_var); the room is geometry-only, never the value.
        value = tk.Label(row, bg=theme.bg, fg=theme.text, font=theme_mod.FONT_CAPTION, anchor="e", bd=0,
                         padx=0, pady=0)
        value.pack(side="right")
        row.value = value
        self._plain_fit_add(value, var, lambda k=key: self._plain_card_room(k))
        return row

    # ---- the kit-less path's one-line values (B4 fix ADV2-02) ----

    def _plain_fit_add(self, label, var, room):
        entry = (label, var, room)
        self._plain_fit.append(entry)
        var.trace_add("write", lambda *_a, e=entry: self._plain_fit_one(e))
        self._plain_fit_one(entry)

    def _plain_fit_one(self, entry):
        label, var, room = entry
        try:
            px = max(1, int(room()))
            font = label.cget("font")

            def fits(t):
                return int(label.tk.call("font", "measure", font, "-displayof", label, t)) <= px

            label.configure(text=_plain_elide(_plain_line(var.get()), fits))
        except tk.TclError:          # the label is gone (Home destroyed) or the var was never set
            pass

    def _plain_card_room(self, key):
        """The System status card's value room: columns 0-1 (from _on_body_configure) less the row's
        10 px pads, the key and an 8 px gap. Before the first <Configure>: _PLAIN_ROOM_START."""
        if not self._cols:
            return _PLAIN_ROOM_START
        return self._s(_INDEX_W) + self._cols[1] - 2 * 10 - int(key.winfo_reqwidth()) - 8

    def _plain_resume_room(self):
        """The Quick resume title's room: columns 3-5 less the panel's 11 px pads and its 1 px ring."""
        if not self._cols:
            return _PLAIN_ROOM_START
        return self._cols[2] + self._s(_GAP_W) + self._cols[0] - 2 * self._s(11) - 2 - 2

    def _rail_btn(self, parent, text, command, primary=False, danger=False, last=False):
        theme = self.ctx.theme
        gap = 0 if last else self._s(_BTN_GAP)
        if self._kit:
            # RESUME LAST / EXPORT ALL secondary, SHUTDOWN the filled danger act with the power mark. The
            # Export all writers pass the plain upper-case words (B15: EXPORTING... / EXPORT ALL); a letter-spaced
            # text from an old caller still reads the same (KitButton un-spaces it)
            kind = "danger_fill" if danger else ("primary" if primary else "secondary")
            btn = _KitButton(parent, theme, text=text, kind=kind, upper=True, height=self._s(_BTN_H), command=command)
            btn.pack(fill="x", pady=(0, gap))
            return btn
        if primary:
            bg, fg, active_bg = theme.bright, theme.bg, theme.dim
        elif danger:
            bg, fg, active_bg = theme.danger, theme.bg, theme.dim
        else:
            bg, fg, active_bg = theme.panel, theme.text, theme.bright
        btn = tk.Button(
            parent, text=text.upper(), command=command, relief="flat",          # B15: no letter-spacing
            bg=bg, fg=fg, activebackground=active_bg, activeforeground=theme.bg,
            font=theme_mod.FONT_CAPTION, padx=8, pady=8,
            highlightthickness=1, highlightbackground=theme.danger if danger else theme.dim,
        )
        btn.pack(fill="x", pady=(0, gap))
        return btn

    # ---- lifecycle ----

    def legend_items(self):
        # the six directory boxes' digits + the router's global Alt+1..5 and Ctrl+N
        return self.directory.digit_legend("open") + [("ALT+1-5", "screens"), ("CTRL+N", "new session")]

    def on_show(self):
        self._set_items()
        self._fit_rows()          # the re-rendered option boxes: the Model line cap and the row-1 floor
        self._refresh_clock()
        self._refresh_status()
        self._refresh_quick_resume()

    def on_hide(self):
        super().on_hide()
        if self._clock_after_id is not None:
            self.after_cancel(self._clock_after_id)
            self._clock_after_id = None
        if self._status_after_id is not None:
            self.after_cancel(self._status_after_id)
            self._status_after_id = None
        self._status_seq += 1  # any poll already in flight is now stale
        self.directory.on_hide()

    # ---- directory grid ----

    def _set_items(self):
        items = [
            DirectoryItem(
                "New session", "start a fresh chat", on_activate=self.ctx.new_session, is_primary=True,
            ),
            DirectoryItem("Chats", "browse existing sessions", on_activate=lambda: self.ctx.go_to_screen("chats")),
            DirectoryItem(
                "Projects", "workspace-scoped conversations", on_activate=lambda: self.ctx.go_to_screen("projects"),
            ),
            DirectoryItem(
                "Image uplink", "backend, ComfyUI, external link", on_activate=lambda: self.ctx.go_to_screen("image_uplink"),
            ),
            DirectoryItem("Settings", "model, vision, advanced", on_activate=lambda: self.ctx.go_to_screen("settings")),
            DirectoryItem("Directory log", "open the startup/session log file", on_activate=self._on_open_directory_log),
        ]
        self.directory.set_items(items)

    def _on_open_directory_log(self):
        try:
            os.startfile(_LOG_PATH)
        except OSError:
            pass

    # ---- system status / quick resume (real data, threaded) ----

    def _refresh_clock(self):
        # one chain only: a second on_show() without an on_hide() in between replaces the pending tick
        if self._clock_after_id is not None:
            try:
                self.after_cancel(self._clock_after_id)
            except tk.TclError:
                pass
            self._clock_after_id = None
        stamp = time.strftime(_CLOCK_FORMAT).upper()          # "SUN  2026-10-04  10:15:00"
        self.clock_var.set(stamp)
        set_clock = getattr(self.header, "set_clock", None)
        if set_clock is not None:
            set_clock(stamp)
        elapsed = int(time.time() - self._start_time)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        self.uptime_var.set(f"{h:02d}:{m:02d}:{s:02d}")
        self._clock_after_id = self.after(1000, self._refresh_clock)

    def destroy(self):
        # on_hide() cancels the clock loop on normal screen swaps, but if
        # this screen is the visible one when the window is torn down the
        # after-callback would outlive the widget (see MainWindow.destroy).
        if self._clock_after_id is not None:
            try:
                self.after_cancel(self._clock_after_id)
            except tk.TclError:
                pass
            self._clock_after_id = None
        super().destroy()

    # ---- status toggle row (Model / Backend / Uplink) ----

    def _make_toggle(self, parent, label):
        """One status flag (kit.StatusFlag: the key and the semantic word in one approved flag). Its
        configure(text=) / cget("text") round-trip the word exactly (Loading... / Offline / Ready / Error)."""
        theme = self.ctx.theme
        if self._kit:
            chip = _StatusFlag(parent, theme, key=label, text="...")
            chip.pack(side="left", padx=(0, self._s(14)))
            return chip
        row = tk.Frame(parent, bg=theme.bg)
        row.pack(side="left", padx=(0, 24))
        tk.Label(
            row, text=label.upper(), bg=theme.bg, fg=theme.dim, font=theme_mod.FONT_CAPTION,     # B15: no spacing
        ).pack(side="left", padx=(0, 8))
        chip = tk.Label(row, text="...", bg=theme.panel, fg=theme.dim, font=theme_mod.FONT_CAPTION, padx=8, pady=2)
        chip.pack(side="left")
        return chip

    def _set_toggle(self, chip, active, on_text, off_text):
        # the flag derives its inks from the word (an "Error" flag is the filled danger one) and swallows
        # bg= / fg=; a plain Label chip (no kit) still takes them
        theme = self.ctx.theme
        if active:
            chip.configure(text=on_text, bg=theme.bright, fg=theme.bg)
        else:
            chip.configure(text=off_text, bg=theme.panel, fg=theme.dim)

    def _refresh_status(self):
        # Worker threads must never touch Tk directly (self.after() included -
        # calling it off the main thread is a real hazard, not just a style
        # preference, see asset_manager.py's _on_set_active); they only ever
        # q.put(...), and the poll loop below (scheduled from here, on the
        # main thread) is what actually updates the widget.
        if self._status_after_id is not None:
            self.after_cancel(self._status_after_id)
            self._status_after_id = None
        self._status_seq += 1
        q = queue.Queue()
        threading.Thread(target=self._status_worker, args=(q,), daemon=True).start()
        self.after(50, self._poll_status_queue, q, self._status_seq)

    def _status_worker(self, q):
        try:
            health = self.ctx.api.health()
            model_info = self.ctx.api.get_model_info()
        except Exception as e:
            q.put(("error", str(e)))
            return
        try:
            settings = self.ctx.api.get_settings()
            uplink_configured = bool((settings.get("image_gen_link") or "").strip())
        except Exception:
            uplink_configured = False
        q.put(("ok", health, model_info, uplink_configured))

    def _poll_status_queue(self, q, seq):
        if seq != self._status_seq:
            return  # a newer refresh (or leaving this screen) superseded this one
        try:
            result = q.get_nowait()
        except queue.Empty:
            self.after(50, self._poll_status_queue, q, seq)
            return
        if result[0] == "error":
            self.model_row_var.set(f"unreachable: {result[1]}")
            self._set_toggle(self.model_chip, False, "Ready", "Offline")
            self._set_toggle(self.backend_chip, False, "Online", "Offline")
            self._set_toggle(self.uplink_chip, False, "Linked", "Off")
            # Keep trying while this screen is up. This used to just stop, so a
            # single failed poll during startup (the backend is busy while the
            # model loads) left "unreachable" on screen forever, long after
            # everything was actually fine.
            if self.is_visible():
                self._status_after_id = self.after(2000, self._refresh_status)
            return
        _, health, model_info, uplink_configured = result
        status = health.get("status", "?")
        model = model_info.get("model") or "no model loaded - add one in Settings > Model"
        n_ctx = model_info.get("n_ctx_train")
        self.model_row_var.set(model)
        self._set_fit_note(model_info.get("fit"))
        self.context_row_var.set(f"{n_ctx:,} tok" if n_ctx else "-")
        # "Offline" is the wrong word for a model that's simply still loading -
        # on a first launch that can take minutes, and "Offline" reads as
        # "broken, try again", which is exactly when people relaunch.
        self._set_toggle(self.model_chip, status == "ok", "Ready", self._chip_word(status, health))
        # An "Error" chip alone says nothing: the server's explanation (what stopped it and what to do - a missing DLL, an unsupported
        # GPU, a taken port ...) is shown once, as a toast that stays long enough to read. Only its first paragraph: the rest is the log.
        if status == "error":
            reason = (health.get("error") or "").split("\n\n")[0].strip()
            if reason and reason != self._shown_error:
                self._shown_error = reason
                show_toast(self, self.ctx.theme, f"The model server is not running. {reason}", kind="error", duration_ms=30000)
        else:
            self._shown_error = None
        self._set_toggle(self.backend_chip, True, "Online", "Offline")  # a response arrived at all
        self._set_toggle(self.uplink_chip, uplink_configured, "Linked", "Off")
        # llama-server usually finishes coming up shortly after launch - keep
        # re-checking while this screen is showing so the landing surface
        # doesn't display a stale "starting" forever (same rationale as the
        # context meter's startup retry). Stops once ok/error or hidden.
        if status not in ("ok", "error") and self.is_visible():
            self._status_after_id = self.after(2000, self._refresh_status)

    def _refresh_quick_resume(self):
        q = queue.Queue()
        threading.Thread(target=self._quick_resume_worker, args=(q,), daemon=True).start()
        self.after(50, self._poll_quick_resume_queue, q)

    def _quick_resume_worker(self, q):
        try:
            conversations = self.ctx.api.list_conversations()
        except Exception:
            conversations = []
        q.put(conversations)

    def _poll_quick_resume_queue(self, q):
        try:
            conversations = q.get_nowait()
        except queue.Empty:
            self.after(50, self._poll_quick_resume_queue, q)
            return
        self._apply_quick_resume(conversations)

    def _apply_quick_resume(self, conversations):
        # Project chats belong to their project - Quick Resume never
        # surfaces one from outside it, same scope boundary the Chats
        # screen's "All" view already uses.
        conversations = [c for c in conversations if not c.get("project_id")]
        if not conversations:
            self._resume_conv_id = None
            self.resume_var.set("no recent session")
            self.resume_cap_var.set("")
            return
        # list_conversations is sorted by updated_at DESC.
        most_recent = conversations[0]
        self._resume_conv_id = most_recent["id"]
        self.resume_var.set(most_recent.get("title") or "New chat")
        updated = most_recent.get("updated_at")
        self.resume_cap_var.set(f"Updated {updated}" if updated else "")

    def _on_resume(self):
        if self._resume_conv_id is not None:
            self.ctx.go_to_chat(self._resume_conv_id)

    # ---- export all (real: loops the same per-conversation export the
    # Chat screen already uses, no fabricated bulk-export endpoint) ----

    def _on_export_all(self):
        self.export_all_btn.config(state="disabled", text="EXPORTING...")
        q = queue.Queue()
        threading.Thread(target=self._export_all_worker, args=(q,), daemon=True).start()
        self.after(50, self._poll_export_all_queue, q)

    def _export_all_worker(self, q):
        try:
            settings = self.ctx.api.get_settings()
            conversations = self.ctx.api.list_conversations()
        except Exception as e:
            q.put(("error", str(e)))
            return
        if settings.get("confirm_save_location"):
            # A per-conversation save dialog would mean one popup per chat -
            # ask once for a destination folder instead and drop every export
            # into it under its own suggested filename.
            q.put(("choose_dir", conversations))
            return
        ok, failed = 0, 0
        for conv in conversations:
            try:
                self.ctx.api._request(
                    "POST", f"/api/conversations/{conv['id']}/export", body={"format": "md"}
                )
                ok += 1
            except Exception:
                # Broad on purpose: a single bad export (e.g. a stale/unreachable
                # custom outputs path) must not kill this worker thread before
                # q.put(("done", ...)) runs, which would leave export_all_btn
                # disabled forever - see app/server.py's _dispatch_safely comment
                # for the request-crash class of bug this guards against.
                failed += 1
        q.put(("done", ok, failed))

    def _poll_export_all_queue(self, q):
        try:
            result = q.get_nowait()
        except queue.Empty:
            self.after(50, self._poll_export_all_queue, q)
            return
        if result[0] == "choose_dir":
            self._finish_export_all_with_dialog(result[1])
            return
        self.export_all_btn.config(state="normal", text="EXPORT ALL")
        if result[0] == "error":
            show_toast(self, self.ctx.theme, f"Could not list conversations: {result[1]}", kind="error")
            return
        _, ok, failed = result
        suffix = f", {failed} failed" if failed else ""
        show_toast(self, self.ctx.theme, f"Exported {ok} conversation(s){suffix}.", kind="success")

    def _finish_export_all_with_dialog(self, conversations):
        # Called back on the main thread via the .after() poll loop above, so
        # this Tk dialog call is safe here.
        if not conversations:
            self.export_all_btn.config(state="normal", text="EXPORT ALL")
            show_toast(self, self.ctx.theme, "No conversations to export.", kind="warning")
            return
        directory = filedialog.askdirectory(title="Choose a folder to export all chats into")
        if not directory:
            self.export_all_btn.config(state="normal", text="EXPORT ALL")
            show_toast(self, self.ctx.theme, "Export cancelled.", kind="warning")
            return
        q = queue.Queue()
        threading.Thread(
            target=self._export_all_to_dir_worker, args=(q, conversations, directory), daemon=True
        ).start()
        self.after(50, self._poll_export_all_queue, q)

    def _export_all_to_dir_worker(self, q, conversations, directory):
        ok, failed = 0, 0
        for conv in conversations:
            try:
                content, filename = self.ctx.api.get_raw(
                    f"/api/conversations/{conv['id']}/export?format=md"
                )
                path = os.path.join(directory, filename or f"conversation-{conv['id']}.md")
                with open(path, "wb") as f:
                    f.write(content)
                ok += 1
            except Exception:
                failed += 1
        q.put(("done", ok, failed))
