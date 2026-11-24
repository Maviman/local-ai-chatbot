"""kit/notice.py - the ASCII notice box (D-19 / D-26 / D-28; BUILD_PLAN §1.3, B1 + B3).

    ascii_box(tag, message, max_cols=72, measure=None) -> list[str]   top, body..., bottom
    text_cols(s) -> int                                  display columns of s (monospace cells)
    build_notice(root, theme, message, kind, max_cols=72) -> tk.Frame   the box widget (B3)
    rewrap_notice(frame, max_cols) -> bool               re-wrap a shown box (window resized, B3 ADV2-04)
    NoticeStack / stack_for(root)                        the severity-aware stack of 3 (B3, X-13)
    severity(kind), ink_token(kind), tag_for(kind)       the kind tables

B3 widget (build_notice): a tk.Frame on the theme's bg holding the box rows in the kind's ink
(error danger, warning warn_eff, success ok, anything else NOTE in bright), Consolas 10 (the lab's
13 px pre). The top and body rows are one Label (frame.notice_body); the bottom row is three
Labels side by side, so `[ x ]` is its own Label (frame.notice_close) that the caller binds. Every
Label has no padding, so the rows line up as one monospace block. frame.notice_height is the
block's pixel height, known at once (Label geometry is computed on configure), so a stack can be
laid out without update_idletasks(). The widget schedules nothing: the caller (screens/base.py
show_toast) owns the auto-dismiss timer and the placement.

NoticeStack is pure bookkeeping (no Tk call, no timer): add() returns the entries it evicted so
the caller closes them (cancelling their timers through their own frames), offsets() gives each
notice's distance from the bottom edge, newest at the bottom.

B10 (notice print-in, lab notify): build_notice ends by asking fx.motion to print the box in -
type_on(frame, per="notice"). The box frame (its top rule, the borders, the bottom row with
`[ x ]`) is whole from frame 0; the body text is blank, then printed behind the block cursor,
continuing across wrapped lines, max(4, ceil(n / 9)) characters a frame. REDUCED: the box appears
whole and its text settles from hot (110 ms); OFF / NullFX: whole at once. The frame is a motion
"typeable" (fx_type_text / fx_type_show / fx_type_gen / fx_ink); rewrap_notice bumps notice_gen,
so a re-wrap mid-print wins (the box is then shown whole). Still no timer here: fx.clock drives it.

The format is the lab's asciiBox (signal_lab.html, KIND_TAG / asciiBox; see design archive):

    +--[ ERROR ]-----------------+
    | Could not delete the chat. |
    +----------------------[ x ]+

    w      = max(longest body line + 4, len(tag) + 12)
    top    = "+--[ " + tag + " ]" + "-" * (w - len(tag) - 8) + "+"
    body   = "| " + line.ljust(w - 4) + " |"
    bottom = "+" + "-" * (w - 7) + "[ x ]+"

A one-line message up to max_cols gives exactly the lab's three rows (kept verbatim,
inner spacing included). Longer messages are word-wrapped at
max_cols per paragraph (textwrap, break_long_words=True, break_on_hyphens=False), never
truncated (D-27: the 12 s and 30 s messages show in full); a blank paragraph stays a blank
row. Tabs become spaces and CR/LF line breaks are normalised.

Widths are DISPLAY COLUMNS, not len() (B1 fix A10): a wide East Asian character (CJK, most
emoji) takes 2 cells, a combining mark / zero-width format character 0, an astral character
2, every other character 1. So a server error carrying a Windows path with a Japanese user name pads
to the same column count as its border rows. For plain one-cell text, columns == len(), so
the lab's rows are unchanged. How evenly the cells render on screen is then up to the font's
fallback glyphs: Consolas 9 draws CJK and emoji 12 px wide, not 2 x 7, so B3's build_notice
should pass measure= (pixel padding, within half a space) or draw the right border on its own.
"""

import textwrap
import time as _time
import tkinter as _tk
import unicodedata

from . import C10, tokens as _tokens

KIND_TAG = {"error": "ERROR", "warning": "WARNING", "success": "OK"}
NOTE_TAG = "NOTE"
MAX_COLS = 72
MIN_COLS = 8
MAX_BODY_ROWS = 30       # body lines a box shows at most; the rest become one "... (+N more lines)" row (BF-14)
MORE_ROW = "... (+%d more lines)"


def tag_for(kind):
    """The box tag for a notice kind: error ERROR, warning WARNING, success OK, else NOTE."""
    return KIND_TAG.get(kind, NOTE_TAG) if isinstance(kind, str) else NOTE_TAG


def _ch_cols(ch):
    o = ord(ch)
    if o < 0x300:                        # ASCII / Latin-1 / Latin Extended: one cell (fast path)
        return 1
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F") or o > 0xFFFF:
        return 2
    return 1


def text_cols(s):
    """Display columns of s in a monospace grid (wide = 2, combining / zero-width = 0)."""
    return sum(_ch_cols(ch) for ch in s)


def _clean(s):
    s = "" if s is None else str(s)
    s = s.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
    # other control characters would break the fixed-width rows
    return "".join(ch if (ch == "\n" or ch >= " ") else " " for ch in s)


def _wrap_cols(para, cols):
    """Greedy word wrap by display columns (only for paragraphs with non-one-cell characters);
    a word wider than a row is broken by columns. Nothing is dropped but the break spaces."""
    lines, line, lc = [], "", 0
    for word in para.split(" "):
        if word == "":
            continue
        wc = text_cols(word)
        if line and lc + 1 + wc <= cols:
            line, lc = line + " " + word, lc + 1 + wc
            continue
        if line:
            lines.append(line)
            line, lc = "", 0
        while wc > cols:
            take, tc = "", 0
            for ch in word:
                c = _ch_cols(ch)
                if take and tc + c > cols:
                    break
                take, tc = take + ch, tc + c
            lines.append(take)
            word = word[len(take):]
            wc = text_cols(word)
        line, lc = word, wc
    if line or not lines:
        lines.append(line)
    return lines


def wrap_lines(message, max_cols=MAX_COLS, max_rows=None):
    """The body lines of a notice: each \\n-paragraph wrapped at max_cols display columns,
    nothing dropped - except past max_rows lines, when given: the first max_rows - 1 lines and one
    "... (+N more lines)" row then stand for the rest (elide_lines, BF-14; the caller keeps the message)."""
    cols = max(MIN_COLS, int(max_cols)) if max_cols else MAX_COLS
    lines = []
    for para in _clean(message).split("\n"):
        if para.strip() == "":
            lines.append("")
            continue
        pc = text_cols(para)
        if pc <= cols:                   # fits: verbatim, exactly as the lab prints it
            lines.append(para)
            continue
        if pc == len(para):              # one cell per character: textwrap, as before
            lines.extend(textwrap.wrap(para, width=cols, break_long_words=True, break_on_hyphens=False,
                                       replace_whitespace=False, drop_whitespace=True) or [""])
        else:
            lines.extend(_wrap_cols(para, cols))
    return elide_lines(lines, max_rows)[0]


def elide_lines(lines, max_rows):
    """(the lines capped at max_rows, how many lines the last one stands for): past the cap, the first
    max_rows - 1 lines plus one "... (+N more lines)" row; (the lines, 0) when nothing is hidden or no cap
    (None, 0, 1 or not a number) was given."""
    lines = list(lines)
    try:
        cap = int(max_rows) if max_rows is not None else 0
    except (TypeError, ValueError):
        cap = 0
    if cap < 2 or len(lines) <= cap:
        return lines, 0
    hidden = len(lines) - (cap - 1)
    return lines[:cap - 1] + [MORE_ROW % hidden], hidden


def ascii_box(tag, message, max_cols=MAX_COLS, measure=None, max_rows=None):
    """The notice box rows (pure). tag: the word in the top rule ('ERROR', ...).
    Every row has the same display width (text_cols). max_rows (optional) caps the body lines, the
    last one standing for the rest (elide_lines, BF-14).

    measure (optional, for B3's widget): a callable str -> rendered pixel width (e.g. Tk's
    `font measure` for the notice font). The body rows are then padded so their right border
    lands as close to the border rows' right end as one space allows (within half a space),
    and the border rows grow when a row renders wider than them. This is for fonts whose
    fallback glyphs are not exactly two cells wide (Consolas 9 draws CJK and emoji 12 px,
    not 2 x 7). For one-cell text the rows are identical to the measure=None rows."""
    return box_rows(tag, wrap_lines(message, max_cols, max_rows), measure)


def box_rows(tag, body, measure=None):
    """The rows of a box around body lines that are wrapped already (ascii_box less the wrapping; build_notice
    uses it with elide_lines, so it knows which lines the last body row stands for)."""
    tag = " ".join(_clean(tag).split()) or NOTE_TAG
    body = list(body) or [""]
    tc = text_cols(tag)
    w = max(max(text_cols(line) for line in body) + 4, tc + 12)

    def border(width):
        return ("+--[ " + tag + " ]" + "-" * (width - tc - 8) + "+", "+" + "-" * (width - 7) + "[ x ]+")
    top, bot = border(w)
    if measure is None:
        mid = ["| " + line + " " * (w - 4 - text_cols(line)) + " |" for line in body]
        return [top] + mid + [bot]
    sp = max(1, int(measure(" ")))
    dash = max(1, int(measure("-")))
    bare = [int(measure("| " + line + " |")) for line in body]
    target = int(measure(top))
    if max(bare) > target:
        w += -(-(max(bare) - target) // dash)
        top, bot = border(w)
        target = int(measure(top))
    mid = ["| " + line + " " * max(0, int(round((target - bw) / float(sp)))) + " |"
           for line, bw in zip(body, bare)]
    return [top] + mid + [bot]


# ====================================================================== B3: the widget and the stack

SEVERITY = {"error": 3, "warning": 2}          # success / note / anything else: 1
INK_TOKEN = {"error": "danger", "warning": "warn_eff", "success": "ok"}
NOTE_INK = "bright"
NOTICE_FONT = C10
STACK_MAX = 3
STACK_GAP = 8            # px between stacked notices
BOTTOM_GAP = 16          # px between the lowest notice and the legend row (fx_bottom_reserve)
CLOSE_TEXT = "[ x ]"


def severity(kind):
    """error 3 > warning 2 > success / note / unknown 1 (the eviction order, X-13)."""
    return SEVERITY.get(kind, 1) if isinstance(kind, str) else 1


def ink_token(kind):
    """The token a notice of this kind is drawn in."""
    return INK_TOKEN.get(kind, NOTE_INK) if isinstance(kind, str) else NOTE_INK


def build_notice(root, theme, message, kind, max_cols=MAX_COLS, font=NOTICE_FONT, max_rows=MAX_BODY_ROWS):
    """The notice box Frame (a child of root, not yet placed). Rows come from ascii_box with
    pixel padding (measure=), so wide CJK / emoji glyphs still line the right border up within
    half a space. Attributes: notice_body, notice_close (the `[ x ]` Label), notice_rows,
    notice_kind, notice_message (the message as given), notice_height (px). The body shows at most max_rows
    lines (BF-14: a 20,000-character message made a 5,000 px box, clipped off the top of the window): past
    that, the last body row is its own Label (notice_more) reading "... (+N more lines)", whose hover tip
    shows the lines it stands for (as many as the window holds; notice_hidden_text has them all, and
    notice_message is still the whole message), and notice_hidden is N."""
    t = _tokens(theme)
    ink = t.get(ink_token(kind)) or t["bright"]
    bg = t["bg"]
    frame = _tk.Frame(root, bg=bg, bd=0, highlightthickness=0)
    try:
        def measure(s):
            return int(frame.tk.call("font", "measure", font, s))
        lines = wrap_lines(message, max_cols)
        shown, hidden = elide_lines(lines, max_rows)
        rows = box_rows(tag_for(kind), shown, measure=measure)
        opts = dict(bg=bg, fg=ink, font=font, padx=0, pady=0, bd=0, highlightthickness=0)
        body = _tk.Label(frame, text="\n".join(rows[:-2] if hidden else rows[:-1]), justify="left", anchor="w", **opts)
        body.pack(side="top", anchor="w")
        more = None
        if hidden:
            more = _tk.Label(frame, text=rows[-2], justify="left", anchor="w", cursor="question_arrow", **opts)
            more.pack(side="top", anchor="w")
            _bind_more_tip(frame, more)
        foot = _tk.Frame(frame, bg=bg, bd=0, highlightthickness=0)
        foot.pack(side="top", anchor="w")
        bottom = rows[-1]
        left = _tk.Label(foot, text=bottom[:-len(CLOSE_TEXT) - 1], **opts)
        left.pack(side="left")
        close = _tk.Label(foot, text=CLOSE_TEXT, cursor="hand2", **opts)
        close.pack(side="left")
        right = _tk.Label(foot, text=bottom[-1:], **opts)
        right.pack(side="left")
        frame.notice_body = body
        frame.notice_close = close
        frame.notice_foot = (left, close, right)       # the bottom row: left run, `[ x ]`, right corner
        frame.notice_rows = rows
        frame.notice_kind = kind
        frame.notice_message = message
        frame.notice_font = font
        frame.notice_cols = max_cols
        frame.notice_max_rows = max_rows
        frame.notice_more = more                       # the "... (+N more lines)" row, or None
        frame.notice_hidden = hidden                   # the lines it stands for
        frame.notice_hidden_text = "\n".join(lines[len(shown) - 1:]) if hidden else ""
        frame.notice_height = _notice_height(frame)
        frame.notice_lines = list(shown)               # B10: the body lines as shown (the print-in's text)
        frame.notice_gen = 0                           # B10: bumped by rewrap_notice (the app wins)
        _make_typeable(frame)
    except Exception:
        try:
            frame.destroy()
        except _tk.TclError:
            pass
        raise
    _print_in(frame)
    return frame


# ---------------------------------------------------------------------- B10: the print-in typeable
TYPE_CURSOR = "█"      # the block cursor the body prints behind (fx.motion.CURSOR)


def _typed_lines(frame):
    """The body lines the print-in types: every shown line but a "... (+N more lines)" row (its own Label)."""
    lines = list(getattr(frame, "notice_lines", ()))
    return lines[:-1] if getattr(frame, "notice_hidden", 0) else lines


def _body_rows(frame):
    rows = frame.notice_rows
    return rows[:-2] if getattr(frame, "notice_hidden", 0) else rows[:-1]


def notice_type_text(frame):
    """The characters a print-in prints: the body lines' text, joined (borders and padding excluded)."""
    return "".join(_typed_lines(frame))


def notice_type_show(frame, k, cursor=False):
    """Draw the box with only the first k body characters printed (+ the block cursor where the next one goes);
    the top rule and the borders are whole, the unprinted cells blank. k None = the whole box."""
    rows = _body_rows(frame)
    lines = _typed_lines(frame)
    n = sum(len(line) for line in lines)
    if k is None or k >= n:
        text = "\n".join(rows)
    else:
        out = [rows[0]]
        left = max(0, int(k))
        placed = False
        for i, line in enumerate(lines):
            row = rows[1 + i]
            kk = max(0, min(len(line), left))
            left -= len(line)
            rest = line[kk:]
            cols = text_cols(rest)
            if cursor and not placed and rest:
                fill = TYPE_CURSOR + " " * max(0, cols - 1)
                placed = True
            else:
                fill = " " * cols
            out.append(row[:2] + line[:kk] + fill + row[2 + len(line):])
        text = "\n".join(out)
    frame.notice_body.configure(text=text)


def _notice_ink_get(frame):
    try:
        return frame.notice_body.cget("fg")
    except _tk.TclError:
        return ""


def _notice_ink_put(frame, colour):
    labels = [frame.notice_body] + list(getattr(frame, "notice_foot", ()))
    more = getattr(frame, "notice_more", None)
    if more is not None:
        labels.append(more)
    for lab in labels:
        try:
            lab.configure(fg=colour)
        except _tk.TclError:
            pass


def _make_typeable(frame):
    frame.fx_type_text = lambda f=frame: notice_type_text(f)
    frame.fx_type_show = lambda k, cursor=False, f=frame: notice_type_show(f, k, cursor)
    frame.fx_type_gen = lambda f=frame: getattr(f, "notice_gen", 0)
    frame.fx_ink = (lambda f=frame: _notice_ink_get(f), lambda c, f=frame: _notice_ink_put(f, c))


def _print_in(frame):
    """Ask fx.motion to print the new box in (a no-op under NullFX). Never raises."""
    try:
        from .. import fx as _fx
        _fx.get(frame).motion.type_on(frame, per="notice", slot="notice")
    except Exception:  # noqa: BLE001 - a notice is never lost to its motion
        try:
            notice_type_show(frame, None)
        except Exception:  # noqa: BLE001
            pass


def rewrap_notice(frame, max_cols):
    """Re-wrap a shown notice (a build_notice frame) at max_cols, e.g. after the window was resized
    (B3 ADV2-04: a box keeping the wrap it was built with ran off a narrower window). The same message
    and the same rows rule, so nothing is dropped or re-cased. Updates notice_rows, notice_cols and
    notice_height. -> True when the rows changed (the caller re-lays the stack out)."""
    if getattr(frame, "notice_cols", None) == max_cols:
        return False
    font = getattr(frame, "notice_font", NOTICE_FONT)

    def measure(s):
        return int(frame.tk.call("font", "measure", font, s))
    lines = wrap_lines(frame.notice_message, max_cols)
    shown, hidden = elide_lines(lines, getattr(frame, "notice_max_rows", MAX_BODY_ROWS))
    rows = box_rows(tag_for(frame.notice_kind), shown, measure=measure)
    frame.notice_cols = max_cols
    if rows == frame.notice_rows:
        return False
    frame.notice_gen = getattr(frame, "notice_gen", 0) + 1      # B10: a running print-in stops; the box is whole
    frame.notice_lines = list(shown)
    left, close, right = frame.notice_foot
    frame.notice_body.configure(text="\n".join(rows[:-2] if hidden else rows[:-1]))
    more = getattr(frame, "notice_more", None)
    if hidden and more is None:         # the re-wrap made the body longer than the cap: the row appears
        opts = dict(bg=frame.notice_body.cget("bg"), fg=frame.notice_body.cget("fg"), font=font, padx=0, pady=0,
                    bd=0, highlightthickness=0)
        more = _tk.Label(frame, text=rows[-2], justify="left", anchor="w", cursor="question_arrow", **opts)
        more.pack(side="top", anchor="w", before=left.master)
        _bind_more_tip(frame, more)
        frame.notice_more = more
    elif hidden:
        more.configure(text=rows[-2])
    elif more is not None:              # ...or short enough again: the row goes
        _hide_more_tip(frame)
        more.destroy()
        frame.notice_more = None
    frame.notice_hidden = hidden
    frame.notice_hidden_text = "\n".join(lines[len(shown) - 1:]) if hidden else ""
    bottom = rows[-1]
    left.configure(text=bottom[:-len(CLOSE_TEXT) - 1])
    right.configure(text=bottom[-1:])
    frame.notice_rows = rows
    frame.notice_height = _notice_height(frame)
    return True


def _notice_height(frame):
    h = int(frame.notice_body.winfo_reqheight()) + int(frame.notice_close.winfo_reqheight())
    more = getattr(frame, "notice_more", None)
    if more is not None:
        h += int(more.winfo_reqheight())
    return h


def _bind_more_tip(frame, more):
    """Hover on the "... (+N more lines)" row shows the lines it stands for beside the row (below when they
    fit there, else above), at most what the window holds - one more "... (+N more lines)" row stands for the
    rest; frame.notice_hidden_text has them all and frame.notice_message the whole message (D-27: nothing is
    unreachable). No timer: the tip goes on <Leave>, with the frame, and with a hidden row (the kit's tip
    registry drops it on the toplevel's <Unmap>, as a StatusLine's)."""
    more._kit_tip = None
    more._hide_tip = lambda f=frame: _hide_more_tip(f)       # what the kit's tip registry calls
    more.bind("<Enter>", lambda _e, f=frame: _show_more_tip(f), add="+")
    more.bind("<Leave>", lambda _e, f=frame: _hide_more_tip(f), add="+")
    frame.bind("<Destroy>", lambda e, f=frame: _hide_more_tip(f) if str(e.widget) == str(f) else None, add="+")


def _show_more_tip(frame):
    more = getattr(frame, "notice_more", None)
    if more is None or getattr(more, "_kit_tip", None) is not None:
        return
    text = getattr(frame, "notice_hidden_text", "")
    if not text:
        return
    tip = None
    try:
        top = more.winfo_toplevel()
        font = getattr(frame, "notice_font", NOTICE_FONT)
        tip = _tk.Label(top, text="", justify="left", anchor="w", font=font, bg=more.cget("bg"), fg=more.cget("fg"),
                        bd=0, padx=6, pady=4, highlightthickness=1, highlightbackground=more.cget("fg"))
        top_w, top_h = top.winfo_width(), top.winfo_height()
        ry0 = more.winfo_rooty() - top.winfo_rooty()
        ry1 = ry0 + more.winfo_height()
        rx = more.winfo_rootx() - top.winfo_rootx()
        below = top_h - (ry1 + 2)
        above = ry0 - 2
        try:
            ls = max(1, int(more.tk.call("font", "metrics", font, "-linespace")))
        except _tk.TclError:
            ls = 14
        fit = max(2, (max(above, below) - 10) // ls)         # the lines the larger side holds
        tip.configure(text="\n".join(elide_lines(text.split("\n"), fit)[0]))
        th, tw = tip.winfo_reqheight(), tip.winfo_reqwidth()
        y = ry1 + 2 if th <= below else max(0, ry0 - 2 - th)
        tip.place(x=max(0, min(rx, top_w - tw)), y=y)
        tip.lift()
        more._kit_tip = tip
        try:
            from .widgets import _tip_registry
            _tip_registry(top).add(more)
        except Exception:  # noqa: BLE001 - the registry is a convenience (a hidden row drops its tip)
            pass
    except _tk.TclError:
        more._kit_tip = None
        if tip is not None:
            try:
                tip.destroy()
            except _tk.TclError:
                pass


def _hide_more_tip(frame):
    more = getattr(frame, "notice_more", None)
    tip = getattr(more, "_kit_tip", None) if more is not None else None
    if more is not None:
        more._kit_tip = None
    if tip is not None:
        try:
            tip.destroy()
        except _tk.TclError:
            pass


class _Entry(object):
    __slots__ = ("frame", "kind", "severity", "expires", "height", "close")

    def remaining(self, now):
        return self.expires - now


class NoticeStack(object):
    """Up to max_n notices, newest at the bottom. A notice beyond max_n evicts the existing one
    with the lowest (severity, remaining time): error > warning > success / note, ties to the
    one closest to its own auto-dismiss. The newcomer is never the one evicted, so the newest is
    always shown, and a long error (Home's 30 s server error) outlives a burst of shorter errors
    or of warnings. Pure bookkeeping: no Tk call, no timer."""

    def __init__(self, max_n=STACK_MAX, gap=STACK_GAP, now=_time.monotonic):
        self.max_n = max(1, int(max_n))
        self.gap = int(gap)
        self._now = now
        self._entries = []

    def __len__(self):
        return len(self._entries)

    @property
    def frames(self):
        """The notices, oldest first."""
        return [e.frame for e in self._entries]

    def entry(self, frame):
        for e in self._entries:
            if e.frame is frame:
                return e
        return None

    def pick_evict(self):
        """The entry the next add() would evict (None while there is room)."""
        if len(self._entries) < self.max_n:
            return None
        now = self._now()
        return min(self._entries, key=lambda e: (e.severity, e.remaining(now)))

    def add(self, frame, kind, duration_ms, height, close):
        """Register a shown notice. -> the evicted entries (already removed from the stack); the
        caller calls each one's .close() so its timer is cancelled through its own frame."""
        evicted = []
        while len(self._entries) >= self.max_n:
            victim = self.pick_evict()
            self._entries.remove(victim)
            evicted.append(victim)
        e = _Entry()
        e.frame, e.kind, e.severity = frame, kind, severity(kind)
        e.expires = self._now() + max(0, int(duration_ms)) / 1000.0
        e.height, e.close = int(height), close
        self._entries.append(e)
        return evicted

    def total_height(self):
        """px from the newest notice's bottom edge to the oldest one's top edge."""
        if not self._entries:
            return 0
        return sum(e.height for e in self._entries) + self.gap * (len(self._entries) - 1)

    def fit(self, max_height):
        """Evict until the stack is at most max_height px tall (B3 ADV1-05: a stack taller than the
        window pushed the oldest box off its top edge). Same rule as add(): the lowest (severity,
        remaining) goes first and the newest is never evicted, so a lone notice taller than the room
        stays. max_height None (no size known yet) evicts nothing. -> the evicted entries (already
        removed); the caller closes each one."""
        evicted = []
        if max_height is None:
            return evicted
        while len(self._entries) > 1 and self.total_height() > max_height:
            now = self._now()
            victim = min(self._entries[:-1], key=lambda e: (e.severity, e.remaining(now)))
            self._entries.remove(victim)
            evicted.append(victim)
        return evicted

    def set_height(self, frame, height):
        """A notice was re-wrapped (rewrap_notice): its new height for offsets() / fit()."""
        e = self.entry(frame)
        if e is not None:
            e.height = int(height)

    def remove(self, frame):
        e = self.entry(frame)
        if e is None:
            return False
        self._entries.remove(e)
        return True

    def offsets(self):
        """[(frame, px from the bottom edge to the notice's bottom edge)], newest first (0)."""
        out, y = [], 0
        for e in reversed(self._entries):
            out.append((e.frame, y))
            y += e.height + self.gap
        return out


_STACK_ATTR = "_kit_notice_stack"


def stack_for(root):
    """The NoticeStack of root's window (created once, kept on the root)."""
    st = getattr(root, _STACK_ATTR, None)
    if not isinstance(st, NoticeStack):
        st = NoticeStack()
        try:
            setattr(root, _STACK_ATTR, st)
        except Exception:  # noqa: BLE001
            pass
    return st
