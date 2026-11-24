"""kit/chat.py - the Chat screen's chrome (B7a; approved r2 mock_chat, r2kit.header_compact,
shared_glyphs.header_actions / status_mark / list_marker).

    ChatHeader(parent, theme, screen=None, wrap_below=900)
        The compact header (kit.header Header, kind "compact"): CHAT in display L, the conversation
        title beside it (set_subtitle: elided in the middle with the whole title in a hover tip, the
        kit Header's rule), and header text actions (add_action -> kit TextAction) right-aligned at
        the gutter, 16 px apart. When the content column is narrower than wrap_below px (or the
        words would not leave the title room beside them, e.g. at a large display scaling) the
        words move to their own row(s) under the band, right-aligned, and flow onto further rows
        while one row cannot hold them. They are never hidden or clipped.
    ChatEmptyState(master, theme, title="AWAITING INPUT", caption="")
        kit EmptyState (title + the block-cursor mark that B10 animates, X-15) whose caption (the
        conversation title) can change (set_caption) and wraps to the widget's width.
    mark_images(widget, ink) -> {"": blank, "unseen": hollow 7x7, "generating": solid 5x10}
        The sidebar's status marks as PhotoImages (MARK_W x MARK_H, transparent, right-aligned),
        cached per interpreter, display factor and ink (kit.widgets.lamp_images' pattern).

House rules: no after() (a resize relays the words out from <Configure>), no Tk off the Tk
thread, no I/O. Pixel constants are approved pixels at 100 % and scale with kit.geom_ratio.
"""

import tkinter as tk

from . import scale_factor
from .header import COMPACT_CY, RIGHT_PAD, SUBTITLE_GAP, TITLE_X, Header
from .widgets import TA_PADX, EmptyState, TextAction, _put_box

__all__ = ["ChatHeader", "ChatEmptyState", "mark_images", "MARK_W", "MARK_H", "WRAP_BELOW"]

WRAP_BELOW = 900         # content width (approved px) under which the header actions take their own row
ACTION_GAP = 4           # between two words' hit areas: the words are 16 px apart (6 px hit pad each side)
TITLE_ROOM = 120         # what the conversation title keeps beside the words on the band before they wrap
LINE_GAP = 2             # between two rows of wrapped words
WRAP_BOTTOM = 6          # the last row of wrapped words -> the bottom of the header

MARK_W, MARK_H = 7, 10   # the status-mark cell: hollow 7x7 square / solid 5x10 block, right-aligned in it
_MARK_ATTR = "_kit_chat_marks"


def _alive(w):
    try:
        return bool(w.winfo_exists())
    except tk.TclError:
        return False


class ChatHeader(Header):
    """The Chat header. Words are added with add_action(text, command) in reading order (the last
    one ends at the right gutter, x 1084 at 1100 wide; its invisible 6 px hit pad overhangs it).
    .wrapped tells whether they are on their own row(s) now; .action_lines is how many rows."""

    def __init__(self, parent, theme, title="CHAT", subtitle=None, screen=None, wrap_below=WRAP_BELOW, **kw):
        self._theme = theme
        self._wrap_below = wrap_below
        self._wrapped = False
        self._words = []
        self._lines = 1
        self._layout_key = None
        Header.__init__(self, parent, theme, title, kind="compact", subtitle=subtitle, screen=screen, **kw)
        self.bind("<Configure>", self._layout_actions, add="+")

    # ---------------------------------------------------------------- API
    @property
    def wrapped(self):
        return self._wrapped

    @property
    def action_lines(self):
        return self._lines if self._words else 0

    @property
    def action_widgets(self):
        return [w for w in self._words if _alive(w)]

    def add_action(self, text, command, **kw):
        """A header text action (kit TextAction, upper-cased) after the existing ones. -> the widget."""
        kw.setdefault("upper", True)
        word = TextAction(self.actions, self._theme, text=text, command=command, ground=self._t["bg"], **kw)
        word.bind("<Configure>", self._layout_actions, add="+")      # a relabel (THOUGHTS (12)) re-flows
        self._words.append(word)
        self._layout_actions()
        return word

    # ---------------------------------------------------------------- layout
    def _right_limit(self, w):
        """On their own row(s) the words leave the whole band to the title."""
        if self._wrapped:
            return w - self._s(RIGHT_PAD)
        return Header._right_limit(self, w)

    def _width(self):
        for w in (self, self.master):
            try:
                width = w.winfo_width()
            except (tk.TclError, AttributeError):
                continue
            if width > 1:
                return width
        return None

    def _should_wrap(self, width, total):
        s = self._s
        if width < s(self._wrap_below):
            return True
        try:
            chat_w = int(self.canvas.tk.call("font", "measure", self.title_font(), " ".join(self._title.split()).upper()))
        except tk.TclError:
            chat_w = s(45)
        need = s(TITLE_X) + chat_w + s(SUBTITLE_GAP) + s(TITLE_ROOM) + s(16) + total + (s(RIGHT_PAD) - s(TA_PADX))
        return need > width

    @staticmethod
    def _flow(widths, avail, gap):
        """Greedy rows in reading order; a word wider than a row gets a row of its own."""
        lines, cur, used = [], [], 0
        for i, ww in enumerate(widths):
            add = ww if not cur else gap + ww
            if cur and used + add > avail:
                lines.append(cur)
                cur, used = [i], ww
            else:
                cur.append(i)
                used += add
        if cur:
            lines.append(cur)
        return lines

    def _layout_actions(self, _e=None):
        words = self.action_widgets
        if not words:
            return
        s = self._s
        try:
            sizes = [(w.winfo_reqwidth(), w.winfo_reqheight()) for w in words]
        except tk.TclError:
            return
        gap = s(ACTION_GAP)
        widths = [ww for ww, _ in sizes]
        total = sum(widths) + gap * (len(widths) - 1)
        line_h = max(h for _, h in sizes)
        width = self._width()
        wrapped = width is not None and self._should_wrap(width, total)
        right = s(RIGHT_PAD) - s(TA_PADX)          # the last word's TEXT ends at the gutter
        avail = max(1, (width or 0) - s(TITLE_X) - right) if wrapped else total
        key = (wrapped, avail, tuple(sizes))
        if key == self._layout_key:
            return
        self._layout_key = key
        lines = self._flow(widths, avail, gap) if wrapped else [list(range(len(words)))]
        y = 0
        for line in lines:
            x = 0
            for i in reversed(line):
                words[i].place(relx=1.0, x=-x, y=y, anchor="ne")
                x += widths[i] + gap
            y += line_h + s(LINE_GAP)
        height = len(lines) * line_h + (len(lines) - 1) * s(LINE_GAP)
        self._lines = len(lines)
        a = self.actions
        try:
            if wrapped:
                a.configure(height=height, width=1)
                if a.winfo_manager() != "pack":
                    a.place_forget()
                    a.pack(side="top", fill="x", padx=(s(TITLE_X), right), pady=(0, s(WRAP_BOTTOM)))
            else:
                a.configure(height=height, width=total)
                if a.winfo_manager() != "place":
                    a.pack_forget()
                    a.place(relx=1.0, x=-right, y=s(COMPACT_CY), anchor="e")
                else:
                    a.place_configure(x=-right)
        except tk.TclError:
            return
        if wrapped != self._wrapped:
            self._wrapped = wrapped
            self._paint()           # the title's room changed


class ChatEmptyState(EmptyState):
    """EmptyState with a caption that can change and wraps to the widget's width (place it with a
    width, e.g. relwidth=1.0). .mark is the block cursor B10 animates (no timer here)."""

    def __init__(self, master, theme, title="AWAITING INPUT", caption="", **kw):
        EmptyState.__init__(self, master, theme, title, caption=caption or " ", **kw)
        self._wrap_w = None
        self.bind("<Configure>", self._on_configure, add="+")

    @property
    def caption(self):
        return str(self.caption_label.cget("text")) if self.caption_label is not None else ""

    def set_caption(self, text):
        if self.caption_label is not None:
            self.caption_label.configure(text=text or "")

    def _on_configure(self, e=None):
        width = getattr(e, "width", None) or self.winfo_width()
        if self.caption_label is None or width <= 1 or width == self._wrap_w:
            return
        self._wrap_w = width
        try:
            self.caption_label.configure(wraplength=width)
        except tk.TclError:
            pass


def mark_images(widget, ink, cell=None, pad_right=0):
    """{"": blank, "unseen": hollow 7x7 square (1 px), "generating": solid 5x10 block} in `ink`
    (x the integer kit factor), transparent elsewhere. Each image is `cell` = (w, h) px (default
    the bare MARK_W x MARK_H mark); the mark's right edge sits pad_right px from the image's right
    edge and it is centred on the image's middle row. A Label ignores padx / pady around an image,
    so the padding is part of the image. Cached on the root per factor, ink, cell and pad."""
    root = widget._root() if hasattr(widget, "_root") else widget
    f = scale_factor(root)
    mw, mh = MARK_W * f, MARK_H * f
    w, h = cell if cell else (mw, mh)
    w, h = max(int(w), mw + int(pad_right)), max(int(h), mh)
    key = (f, ink, w, h, int(pad_right))
    cache = getattr(root, _MARK_ATTR, None)
    if cache is None:
        cache = {}
        try:
            setattr(root, _MARK_ATTR, cache)
        except Exception:  # noqa: BLE001
            pass
    hit = cache.get(key)
    if hit is not None:
        return hit
    x1 = w - int(pad_right)                      # the marks' right edge
    y0 = (h - mh) // 2                           # the 10-row mark cell, centred
    blank = tk.PhotoImage(master=root, width=w, height=h)
    unseen = tk.PhotoImage(master=root, width=w, height=h)
    _put_box(unseen, x1 - 7 * f, y0 + 2 * f, x1, y0 + 9 * f, ink, f)
    block = tk.PhotoImage(master=root, width=w, height=h)
    block.put(ink, to=(x1 - 5 * f, y0, x1, y0 + mh))
    cache[key] = {"": blank, "unseen": unseen, "generating": block}
    return cache[key]
