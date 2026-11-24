"""fx/page08/crt.py - Settings 08 CRT texture and its preview box (B11; BUILD_PLAN section 4 B11, GAP_FX 2 and 4.1;
X-19 / D-25: the CRT ships with its control; approved r2_settings_sound / r2_settings_sound_end).

    (no title of its own: it continues DISPLAY MOTION)
    CRT texture       OFF  LOW  FULL
    Glow on the LOADING plate; Full adds scanlines on large titles and a blinking wait cursor. Never applied to
    message text.
    [  CHAT                          ]   REPLAY        <- the preview box: an fx.draw.FxCanvas

* The words show the stored level (prefs "fx.crt": 0 / 1 / 2, FULL by default, D-29). A pick writes
  prefs.set("fx.crt", level) - one change, one file write; re-picking the level shown writes nothing. It applies at
  once: the FxCanvas registry's one prefs subscription repaints every CRT surface (this preview, the LOADING plate
  while it is up, every screen title) and running wait cursors re-evaluate their blink (fx/motion.WaitCursor). The
  row follows the store (prefs.subscribe("fx.crt"), owner = the row): RESTORE DEFAULTS, a backup restore.
* The preview box (OI-19 / GAP_FX 4.1, replacing ST-05's sweep demo): 360 x 46, a 1 px dim outline with the
  dim stroke's own bloom at LOW / FULL, and the title CHAT in the display face at L (15 pt: the size of the Chat
  title and the smallest the CRT slices, so FULL shows here exactly what it does to a title). REPLAY types it on at
  the current motion level through fx.motion.type_on (FULL types, REDUCED settles, OFF shows it whole); a CRT pick
  made here, or a motion change, replays it 150 ms later (fx.clock.later owned by the box: the latest change wins).
  It never loops and nothing runs while page 08 is hidden. The canvas keeps 3 px round the box for the bloom. A pane
  narrower than box + REPLAY (200 %, a narrow window) narrows the box (down to 120 px), never hides REPLAY.
* Read-only page (kill switch): the same row, the words disabled showing the stored level, REPLAY disabled, the box
  drawn without CRT (the effects are off).
* RESTORE DEFAULTS resets this row: {"fx.crt": 2}; its caption names "CRT".
No after() here (the replay waits on fx.clock); no Variable or image left to the cyclic gc (page.string_var).
"""

import tkinter as tk

from .. import draw as _draw
from .. import geometry as _geo
from ..sound_motion_page import Section, register_section

try:        # the type-on cursor of canvas text is a drawn box (B10)
    from ..motion import CURSOR_TAG as _CURSOR_TAG, show_block_cursor as _show_block_cursor
except Exception:  # noqa: BLE001
    _CURSOR_TAG, _show_block_cursor = "fx_type_cursor", None

LEAF = "fx.crt"
LEVELS = (("0", "OFF"), ("1", "LOW"), ("2", "FULL"))
LABEL = "CRT texture"
# OI-19 / GAP_FX Q-3: the honest caption (the frame's "Scanlines and glow on plates, panels and empty screens."
# overstates what Tk draws)
CAPTION = ("Glow on the LOADING plate; Full adds scanlines on large titles and a blinking wait cursor. "
           "Never applied to message text.")
REPLAY = "REPLAY"
PREVIEW_TITLE = "CHAT"
PREVIEW_SIZE = "L"            # display L = 15 pt: the Chat title's size, the smallest the CRT slices
PREVIEW_W, PREVIEW_H = 360, 46    # the approved ST-05 box (r2 mock_settings_sound)
PREVIEW_MIN_W = 120           # the narrowest box (CHAT still fits): a narrow pane narrows the box, not REPLAY
PREVIEW_TEXT_X = 14           # the title 14 px inside the box
PREVIEW_STROKE = 1
PREVIEW_GAP = 16              # caption -> the box (the frame)
REPLAY_GAP = 18               # the box -> REPLAY (the frame)
REPLAY_DELAY_MS = 150         # a change made here replays the title this long after the LAST change
REPLAY_SLOT = "replay"
TYPE_SLOT = "title"


def _level(v):
    """A stored crt value as the words' value: "0" / "1" / "2" (anything else: the default, FULL)."""
    if isinstance(v, int) and not isinstance(v, bool) and v in _geo.CRT_LEVELS:
        return str(v)
    return "2"


class CrtPreview(_draw.FxCanvas):
    """The preview box: an FxCanvas (registered, so a live fx.crt change repaints it) that is also a motion
    "typeable" (fx_type_text / fx_type_show / fx_type_gen / fx_ink), so REPLAY types its title on and a repaint
    mid-type keeps the typed frame (the scanlines always cover the whole title, so every frame is sliced)."""

    FX_TAG = "crt_preview"

    def __init__(self, parent, theme, ratio, **kw):
        self._r = ratio if isinstance(ratio, (int, float)) and ratio > 0 else 1.0
        self.box_w, self.box_h = self._px(PREVIEW_W), self._px(PREVIEW_H)
        self.stroke = max(1, self._px(PREVIEW_STROKE))
        self.margin = self._px(_draw.BLOOM_IN_W) + self._px(_draw.BLOOM_OUT_W)
        self.title = PREVIEW_TITLE
        self.title_item = None
        self._type_k = None
        self._type_cursor = False
        self._type_gen = 0
        kw.setdefault("takefocus", 0)
        _draw.FxCanvas.__init__(self, parent, theme, width=self.box_w + 2 * self.margin,
                                height=self.box_h + 2 * self.margin, **kw)

    def _px(self, n):
        return int(round(n * self._r))

    def box(self):
        """(x0, y0, x1, y1) of the box's outline on the canvas (the bloom needs `margin` px round it)."""
        m = self.margin
        return (m, m, m + self.box_w, m + self.box_h)

    def set_box_width(self, w):
        """A narrower (or the full 360 px) box: the canvas follows, and its <Configure> repaints it. -> changed."""
        w = max(1, int(w))
        if w == self.box_w:
            return False
        self.box_w = w
        try:
            self.configure(width=w + 2 * self.margin)
        except tk.TclError:
            return False
        return True

    def draw(self, t, crt):
        try:
            self.delete(_CURSOR_TAG)            # a cursor of a title item this repaint replaces
        except tk.TclError:
            pass
        x0, y0, x1, y1 = self.box()
        _draw.draw_plate(self, t, x0, y0, x1, y1, outline=t["dim"], width=self.stroke, crt=crt,
                         bloom=(self._px(_draw.BLOOM_IN_W), self._px(_draw.BLOOM_OUT_W)), tag=self.FX_TAG)
        font = _draw.display_font(self, PREVIEW_SIZE)
        item = self.create_text(x0 + self._px(PREVIEW_TEXT_X), (y0 + y1) / 2.0, text=self.title, anchor="w",
                                fill=t["bright"], font=font, tags=(self.FX_TAG, "word", "preview_title"))
        self.title_item = item
        # sliced across the WHOLE title, so the typed frames shown next are sliced too; the pitch and line width
        # follow the ratio like the box and its bloom (B11-H11: 3 / 1 at 100 %, 6 / 2 at 200 %)
        _draw.apply_crt(self, t, crt, [item], ground=t["bg"], tag=self.FX_TAG,
                        pitch=max(2, self._px(_draw.geo.SCAN_PITCH)), line=max(1, self._px(1)))
        if self._type_k is not None:
            self.itemconfigure(item, text=self.title[:self._type_k])
            if self._type_cursor and _show_block_cursor is not None:
                _show_block_cursor(self, item, True)

    # ---------------------------------------------------------------- the motion typeable
    def fx_type_text(self):
        return self.title

    def fx_type_gen(self):
        return self._type_gen

    def fx_type_show(self, k, cursor=False):
        if k is None or k >= len(self.title):
            self._type_k, self._type_cursor = None, False
        else:
            self._type_k, self._type_cursor = max(0, int(k)), bool(cursor)
        item = self.title_item
        if item is None:
            return
        try:
            self.itemconfigure(item, text=self.title if self._type_k is None else self.title[:self._type_k])
            if _show_block_cursor is not None:
                _show_block_cursor(self, item, self._type_k is not None and self._type_cursor)
        except tk.TclError:
            pass

    @property
    def fx_ink(self):
        """(getter, setter) of the title's ink, for the REDUCED colour settle."""
        def get():
            try:
                return self.itemcget(self.title_item, "fill") if self.title_item is not None else ""
            except tk.TclError:
                return ""

        def put(c):
            if self.title_item is not None:
                self.itemconfigure(self.title_item, fill=c)
        return (get, put)


class CrtSection(Section):
    noun = "CRT"

    def __init__(self, page, parent):
        from ...kit import geom_ratio, px
        from ...kit.widgets import Segmented, TextAction
        self.page = page
        t = page.t
        self.row = row = page.option_row(parent)       # continues DISPLAY MOTION: no title of its own
        # a label beside disabled words reads value, as Settings' own (B8 _sync_thinking_controls)
        self.label = page.label(row, LABEL, fg=t["bright"] if page.live else t["value"])
        self.label.grid(row=0, column=0, sticky="w")
        self.var = page.string_var(_level(self._stored()))
        self.seg = Segmented(row, page.theme, options=LEVELS, variable=self.var, command=self._on_pick,
                             ground=t["bg"], state="normal" if page.live else "disabled")
        self.seg.grid(row=0, column=1, sticky="w")
        self.caption = page.caption(parent, CAPTION)
        self.preview_row = box = tk.Frame(parent, bg=t["bg"])
        self.preview = CrtPreview(box, page.theme, geom_ratio(parent))
        m = self.preview.margin
        box.pack(fill="x", pady=(max(0, px(parent, PREVIEW_GAP) - m), 0))
        self.preview.pack(side="left")
        self.replay = TextAction(box, page.theme, text=REPLAY, command=self._on_replay, ground=t["bg"],
                                 state="normal" if page.live else "disabled")
        self._replay_gap = max(0, px(parent, REPLAY_GAP) - m)
        self.replay.pack(side="left", padx=(self._replay_gap, 0))
        self._box_max, self._box_min = self.preview.box_w, px(parent, PREVIEW_MIN_W)
        # the box narrows when the pane is narrower than box + REPLAY (200 %, a narrow window), so REPLAY is never
        # pushed off the pane; decided from the PANE's width (page.content_width(), set by the window), never from
        # this row's own width, which would feed back into itself (B10 BF2-1)
        box.bind("<Configure>", self._fit_preview, add="+")
        self._fit_preview()
        page.subscribe(LEAF, self._on_pref, owner=row)
        page.subscribe("fx.motion", self._on_motion, owner=row)
        self.replays = 0                                # REPLAY runs (tests / the harness read it)
        self.refresh()

    # ---------------------------------------------------------------- reading
    def _stored(self):
        try:
            return self.page.view.get(LEAF)
        except Exception:  # noqa: BLE001 - no store at all (the shadow could not be made): the default
            return 2

    def _fx(self):
        return getattr(self.preview, "fx", None) if self.preview is not None else None

    def _clock(self):
        clk = getattr(self._fx(), "clock", None)
        if clk is None or getattr(clk, "stopped", False):
            return None
        return clk

    # ---------------------------------------------------------------- writing
    def _on_pick(self):
        page = self.page
        if not page.live or self.seg is None:
            self.refresh()
            return
        try:
            value = int(self.var.get())
        except (TypeError, ValueError):
            self.refresh()
            return
        try:
            stored = page.prefs.get(LEAF)
        except Exception:  # noqa: BLE001
            stored = None
        if value == stored:
            self.refresh()              # re-picking the level shown: nothing to write
            return
        page.set_pref(LEAF, value)      # one write; the registry repaints every CRT surface at once
        self.refresh()
        self.schedule_replay()

    def _on_pref(self, _path, _value):
        self.refresh()

    def _on_motion(self, _path, _value):
        self.schedule_replay()          # the motion row (or RESTORE) changed the level: show it

    def _on_replay(self):
        self.replay_now()

    def _fit_preview(self, _e=None):
        """The box's width: the approved 360 px, or less when box + REPLAY would not fit the pane's content width."""
        if self.preview is None or self.replay is None:
            return
        try:
            # the row: [margin | box | margin] [gap] [REPLAY]
            room = (int(self.page.content_width()) - 2 * self.preview.margin - self._replay_gap
                    - int(self.replay.winfo_reqwidth()))
            self.preview.set_box_width(max(self._box_min, min(self._box_max, room)))
        except (tk.TclError, TypeError, ValueError):
            pass

    # ---------------------------------------------------------------- the preview's type-on
    def replay_now(self):
        """Type the preview's title on at the current motion level (fx.motion.type_on). -> the clock entry or None."""
        if not self.page.live or self.preview is None:
            return None
        clk = self._clock()
        if clk is not None:
            clk.cancel(self.preview, REPLAY_SLOT)
        motion = getattr(self._fx(), "motion", None)
        if motion is None:
            return None
        self.replays += 1
        try:
            return motion.type_on(self.preview, per="title", slot=TYPE_SLOT)
        except Exception:  # noqa: BLE001 - motion never raises; belt and braces
            return None

    def schedule_replay(self):
        """A change made here: replay REPLAY_DELAY_MS after the LAST change (one clock slot owned by the box), only
        while page 08 is shown and live."""
        page = self.page
        if not page.live or not getattr(page, "shown", False) or self.preview is None:
            return None
        clk = self._clock()
        if clk is None:
            return None
        return clk.later(self.preview, REPLAY_SLOT, REPLAY_DELAY_MS, lambda *_a: self.replay_now())

    # ---------------------------------------------------------------- Section hooks
    def defaults(self):
        return {LEAF: self.page.default(LEAF)}

    def refresh(self):
        if self.seg is None:
            return
        try:
            want = _level(self._stored())
            if self.var.get() != want:
                self.var.set(want)              # the Segmented follows its variable (repaint on write)
        except tk.TclError:
            pass

    def on_show(self):
        self.refresh()
        self._fit_preview()

    def on_hide(self):
        """Nothing runs while 08 is hidden: the pending replay is dropped and a running type-on finished."""
        clk = self._clock()
        if clk is not None and self.preview is not None:
            try:
                clk.cancel(self.preview, REPLAY_SLOT)
                clk.finish(self.preview, TYPE_SLOT)
            except Exception:  # noqa: BLE001
                pass

    def release(self):
        self.on_hide()
        for w in (self.seg, self.replay):
            if w is not None:
                try:
                    w.configure(command=None)
                except tk.TclError:
                    pass
        self.seg = self.replay = self.var = None
        self.preview = None
        self.page = _Released


class _ReleasedPage(object):
    """What a released section's page reference becomes: read-only, no store (a late callback does nothing)."""
    live = False
    shown = False
    prefs = None
    view = None

    @staticmethod
    def default(_leaf):
        return None

    @staticmethod
    def set_pref(_leaf, _value):
        return False


_Released = _ReleasedPage()

register_section("crt", 310, CrtSection)
