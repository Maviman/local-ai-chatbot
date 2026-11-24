"""kit/composer.py - the Chat status row and composer pieces (B7b; approved r2_chat / r2_chat_empty,
mock_chat.py, shared_glyphs.button / input_well / block_cursor / context_meter).

    StatusRow(master, theme)
        The one status row under the transcript (approved y 652): the status line on the left (a kit StatusLine:
        one line, elided in the middle, the whole text in a hover tip), '+ ADD FILES...' (a TextAction) 32 px
        before the context meter group, which ends at the right gutter. While the parts do not fit on one row
        (a narrow window, or a long meter caption) the meter takes a row of its own under the first, right-
        aligned, and wraps its caption if even that row is too narrow (meter.fit_to): never hidden, never cut
        (BF-10, D-27 #14).
    ActiveOnlyButton(KitButton)
        A KitButton that is on screen only while its state is not "disabled" (Chat's STOP: shown only while a
        reply or an image job runs). The owner's existing .configure(state=...) calls drive it (Override rule).
    AboveLine(tk.Frame)
        The editing / queued line: one C9B upper-case line and a CANCEL text action at its right end. Shown
        and hidden by the owner's existing pack(side="bottom", before=<anchor>) / pack_forget() calls; a pack
        "before" the anchor is read as "after" it, so with side="bottom" the line sits right ABOVE the anchor
        (the composer) instead of under it.
    ComposerPlaceholder(tk.Label)
        The empty composer's hint, drawn OVER the Text where its first character goes and never inserted, so
        it can never reach the text, a draft or a send. Shown while the Text is empty; wraps to the Text's
        width (never cut).
    caret_width(text_widget, font) -> int
        One character cell of `font` (about 8 px for Consolas 11): the width of Tk's -blockcursor caret.
    ChatStatusLine(StatusLine)
        The kit StatusLine for Chat's stream status: a two-line stage text too wide for the row is shortened at
        WORD boundaries (elide_stage), "GENERATING … 9.0 tok/s" rather than the character cut
        "GENERATING..…, 5.0 tok/s". Display only: cget("text") and the hover tip keep the whole text.

House rules: no after() (everything follows <Configure> / <Map> / <Unmap> / <<Modified>>), no Tk off the Tk
thread, no I/O. Pixel constants are approved pixels at 100 % and scale with kit.geom_ratio.
"""

import tkinter as tk

from . import C9, C9B, geom_ratio, tokens
from .widgets import ELLIPSIS, TA_PADX, KitButton, StatusLine, TextAction, _split_cnf, one_line

__all__ = ["StatusRow", "ActiveOnlyButton", "AboveLine", "ComposerPlaceholder", "caret_width", "ChatStatusLine",
           "elide_stage", "elide_prefixed_stage", "STATUS_GAP", "ACTION_GAP", "MIN_STATUS_CHARS", "ROW_GAP"]

STATUS_GAP = 12          # the status line's right edge -> the '+ ADD FILES...' hit area
ACTION_GAP = 32          # the '+ ADD FILES...' words -> the meter group (approved: 32 px)
MIN_STATUS_CHARS = 16    # one row only while the status line keeps at least this many C9B cells (else the meter wraps)
ROW_GAP = 2              # wrapped: between the status line's row and the meter's row
PLACEHOLDER_GAP = 2      # the placeholder wraps this far before the Text's inner right edge
CARET_GAP = 4            # focused: the placeholder starts this far after the insert caret's cell


def _alive(w):
    try:
        return bool(w is not None and w.winfo_exists())
    except tk.TclError:
        return False


def _pad_total(value):
    """The sum of a pack/grid padx value as pack_info() reports it (int, "10", "4 10", (4, 10))."""
    if value in (None, ""):
        return 0
    if isinstance(value, (tuple, list)):
        parts = value
    else:
        parts = str(value).split()
    try:
        nums = [int(float(p)) for p in parts]
    except (TypeError, ValueError):
        return 0
    if len(nums) == 1:
        return 2 * nums[0]
    return sum(nums[:2])


# ====================================================================== StatusRow

class StatusRow(tk.Frame):
    """StatusRow(master, theme, ground=None, bottom=0, right=0)

    bottom: the bottom pad the meter brings with it (ContextMeter.show() packs itself with pady=(0, 4)); the line
    and the action get the same pad, so the three share one centre line and the row keeps its height whether the
    meter is shown or not. right: the action's right pad while the meter is hidden (the meter's own right pack pad),
    so its word then ends at the same gutter as the meter's last word does.
    attach(line, action=None, meter=None) once: `line` and `action` are children of this row; the meter is a
    child of .meter_slot (it packs and unpacks itself there: ContextMeter.show() / hide()) and may offer
    natural_width() (its width with the caption on one line) and fit_to(width or None) (wrap the caption to
    a width). .wrapped tells whether the meter has its own row now.

    The one-row / two-row choice depends on WIDTHS only (the row's width, the meter's natural width and the
    action's width), never on the row's height or on the status text, so a streaming status never makes the
    row jump and re-laying out can never feed back into itself."""

    def __init__(self, master, theme, ground=None, bottom=0, right=0, **kw):
        self._t = tokens(theme)
        self._ground = ground or self._t["bg"]
        kw.setdefault("bg", self._ground)
        kw.setdefault("bd", 0)
        kw.setdefault("highlightthickness", 0)
        tk.Frame.__init__(self, master, **kw)
        self.ratio = geom_ratio(self)
        self._bottom = max(0, int(bottom))
        self._right = max(0, int(right))
        self.meter_slot = tk.Frame(self, bg=self._ground, bd=0, highlightthickness=0)
        self._line = self._action = self._meter = None
        self._wrapped = False
        self._key = None
        self.columnconfigure(0, weight=1)
        self.bind("<Configure>", self._relayout, add="+")

    def _s(self, n):
        return int(round(n * self.ratio))

    # ---------------------------------------------------------------- API
    @property
    def wrapped(self):
        return self._wrapped

    @property
    def line(self):
        return self._line

    @property
    def action(self):
        return self._action

    @property
    def meter(self):
        return self._meter

    def attach(self, line, action=None, meter=None):
        self._line, self._action, self._meter = line, action, meter
        line.grid(row=0, column=0, sticky="ew", pady=(0, self._bottom))
        if action is not None:
            action.grid(row=0, column=1, sticky="e", padx=(self._s(STATUS_GAP), 0), pady=(0, self._bottom))
            action.bind("<Configure>", self._relayout, add="+")
        self.meter_slot.grid(row=0, column=2, sticky="e")
        if meter is not None:
            for seq in ("<Configure>", "<Map>", "<Unmap>"):
                meter.bind(seq, self._relayout, add="+")
            try:
                meter.on_layout = self.relayout          # ContextMeter calls it after a new reading
            except AttributeError:
                pass
        self.relayout()

    def relayout(self):
        """Lay the parts out again now (also after the meter's caption changed)."""
        self._key = None
        self._relayout()

    # ---------------------------------------------------------------- layout
    def _meter_shown(self):
        m = self._meter
        try:
            return _alive(m) and m.winfo_manager() != ""
        except tk.TclError:
            return False

    def _meter_pads(self):
        try:
            return _pad_total(self._meter.pack_info().get("padx"))
        except (tk.TclError, AttributeError):
            return 0

    def _meter_natural(self):
        """The meter slot's width with the caption on one line (the meter's own pack padding included)."""
        m = self._meter
        nat = getattr(m, "natural_width", None)
        try:
            w = int(nat()) if nat is not None else int(m.winfo_reqwidth())
        except (tk.TclError, TypeError, ValueError):
            w = 0
        return w + self._meter_pads()

    def _min_status(self):
        try:
            cell = int(self.tk.call("font", "measure", C9B, "0"))
        except tk.TclError:
            cell = 7
        return MIN_STATUS_CHARS * cell

    def _relayout(self, _e=None):
        if self._line is None:
            return
        try:
            width = int(self.winfo_width())
        except tk.TclError:
            return
        width = width if width > 1 else None
        shown = self._meter_shown()
        nat = self._meter_natural() if shown else 0
        try:
            act = int(self._action.winfo_reqwidth()) if _alive(self._action) else 0
        except tk.TclError:
            act = 0
        key = (width, nat, act, shown)
        if key == self._key:
            return
        self._key = key
        s = self._s
        # the gap from the '+ ADD FILES...' words to the meter group: the action's 6 px hit pad and the meter's own
        # left pack padding already count toward it
        gap = max(0, s(ACTION_GAP) - s(TA_PADX) - (self._meter_pads() // 2 if shown else 0))
        need = nat + gap + act + s(STATUS_GAP) + self._min_status()
        wrapped = bool(shown and width is not None and width < need)
        slot = self.meter_slot
        try:
            if _alive(self._action):
                # alone on the right (meter hidden, or wrapped below), the action's word ends at the meter's gutter
                lone = (not shown) or wrapped
                self._action.grid_configure(padx=(s(STATUS_GAP), self._right if lone else 0))
            if wrapped:
                slot.grid_configure(row=1, column=0, columnspan=3, sticky="e", padx=0, pady=(s(ROW_GAP), 0))
            elif shown:
                slot.grid_configure(row=0, column=2, columnspan=1, sticky="e", padx=(gap, 0), pady=0)
            elif slot.winfo_manager() == "grid":
                # a hidden meter (a failed load / refresh): the slot leaves the grid, so it takes no room at all -
                # no blank band, no stray pixel column (pack keeps an emptied master at its last requested size,
                # Tk 8.6). The meter's next show() packs into the slot and its _render() calls relayout(), which
                # grids the slot back (the branches above).
                slot.grid_remove()
        except tk.TclError:
            return
        fit = getattr(self._meter, "fit_to", None)
        if shown and fit is not None:
            try:
                fit(max(1, width - self._meter_pads()) if wrapped else None)
            except tk.TclError:
                pass
        self._wrapped = wrapped


# ====================================================================== ActiveOnlyButton

class ActiveOnlyButton(KitButton):
    """ActiveOnlyButton(master, theme, text="", kind="secondary", **KitButton options)

    A KitButton shown only while its state is not "disabled". show_with(**pack_options) says how it is packed
    when shown (and applies the current state at once); after that every state change - .configure(state=),
    .config(state=), w["state"] = ... (the Override rule) - packs or pack_forget()s it. Hidden, it is
    disabled anyway: no click, no Return, no Tab stop."""

    def __init__(self, master, theme, text="", kind="secondary", **kw):
        self._show_pack = None
        # B7b fix ADV1-05: where the keyboard focus goes when the button hides while it holds it (the owner sets
        # it: Chat's composer); None = the next widget in the Tab order
        self.focus_on_hide = None
        KitButton.__init__(self, master, theme, text=text, kind=kind, **kw)

    def show_with(self, **pack_kw):
        self._show_pack = dict(pack_kw)
        self._sync_shown()

    @property
    def active(self):
        return str(self.cget("state")) != "disabled"

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, str):
            return KitButton.configure(self, cnf)
        merged = _split_cnf(cnf, kw)
        result = KitButton.configure(self, **merged)
        if "state" in merged:
            self._sync_shown()
        return result

    config = configure
    __getitem__ = KitButton.cget

    def _sync_shown(self):
        if self._show_pack is None:
            return
        try:
            managed = self.winfo_manager() == "pack"
            if self.active and not managed:
                self.pack(**self._show_pack)
            elif not self.active and managed:
                self._give_focus_away()
                self.pack_forget()
        except tk.TclError:
            pass

    def _give_focus_away(self):
        """B7b fix ADV1-05: a button about to hide that holds the keyboard focus (or is the focus its window gives
        back when re-activated) hands it on first, so the focus is never stranded on an unmapped widget."""
        try:
            holds = self.focus_get() is self or str(self.tk.call("focus", "-lastfor", self._w)) == str(self)
        except (tk.TclError, KeyError):
            return
        if not holds:
            return
        target = self.focus_on_hide
        try:
            if not _alive(target):
                target = self.tk_focusNext()
            if target is not None and target is not self:
                target.focus_set()
        except (tk.TclError, KeyError):
            pass


# ====================================================================== AboveLine

class AboveLine(tk.Frame):
    """AboveLine(master, theme, text, command, anchor=None, ink=None, ground=None)

    One C9B upper-case line (.label, in `ink`, default value) and a CANCEL text action (.cancel) at its right
    end, on the ground (no box). Packed by the owner with pack(side="bottom", before=anchor): that is read as
    after=anchor, which with side="bottom" stacks the line right above the anchor (the composer). Any other
    pack call is passed through unchanged."""

    def __init__(self, master, theme, text, command, anchor=None, ink=None, ground=None, **kw):
        t = tokens(theme)
        self._anchor = anchor
        ground = ground or t["bg"]
        kw.setdefault("bg", ground)
        kw.setdefault("bd", 0)
        kw.setdefault("highlightthickness", 0)
        tk.Frame.__init__(self, master, **kw)
        self.label = tk.Label(self, text=str(text).upper(), font=C9B, bg=ground, fg=ink or t["value"], bd=0, padx=0,
                              pady=0, anchor="w", justify="left")
        self.cancel = TextAction(self, theme, text="Cancel", upper=True, command=command, ground=ground)
        self.cancel.pack(side="right")                 # packed first: a narrow line squeezes the words, never CANCEL
        self.label.pack(side="left", fill="x", expand=True)
        self._wrap = None
        self.label.bind("<Configure>", self._fit, add="+")

    def _fit(self, e=None):
        """The words wrap to their width instead of being cut (D-27 #14)."""
        width = getattr(e, "width", None) or self.label.winfo_width()
        try:
            need = int(self.tk.call("font", "measure", C9B, str(self.label.cget("text"))))
        except tk.TclError:
            return
        wl = 0 if need <= width else max(1, int(width))
        if wl != self._wrap:
            self._wrap = wl
            self.label.configure(wraplength=wl)

    def pack_configure(self, cnf=None, **kw):
        kw = _split_cnf(cnf, kw)
        a = self._anchor
        before = kw.get("before")
        if a is not None and before is not None and str(before) == str(a) and kw.get("side") == "bottom":
            kw["after"] = kw.pop("before")
        return tk.Frame.pack_configure(self, **kw)

    pack = pack_configure


# ====================================================================== ComposerPlaceholder

class ComposerPlaceholder(tk.Label):
    """ComposerPlaceholder(master, theme, text_widget, text="", ground=None)

    master must be the Text's parent (place -in rule). The label is placed in_=text_widget at (0, 0), which for
    a Text is where its first character is drawn (inside its border and padding), and shown only while the
    Text is empty (<<Modified>>, bound with add="+" so the owner's own handler keeps running). It is never
    inserted into the Text: get("1.0", "end-1c") never holds it. A click on it focuses the Text. It wraps to
    the Text's inner width (<Configure>), so it is never cut. .shown tells whether it is on screen.
    While the Text has the keyboard focus the label starts after the insert caret's cell (.x), so it never
    covers the caret of an empty, focused composer (Chat focuses it on every show)."""

    def __init__(self, master, theme, text_widget, text="", ground=None, **kw):
        t = tokens(theme)
        self._text = text_widget
        self._wl = None
        self._width = None
        self._focused = False
        ground = ground or t["panel"]
        kw.setdefault("font", C9)
        kw.setdefault("fg", t["dim"])
        kw.setdefault("bg", ground)
        for opt, val in (("bd", 0), ("padx", 0), ("pady", 0), ("anchor", "nw"), ("justify", "left"),
                         ("cursor", "xterm"), ("highlightthickness", 0), ("takefocus", 0)):
            kw.setdefault(opt, val)
        tk.Label.__init__(self, master, text=text, **kw)
        text_widget.bind("<<Modified>>", self.sync, add="+")
        text_widget.bind("<Configure>", self._on_text_configure, add="+")
        text_widget.bind("<FocusIn>", lambda _e: self._set_focused(True), add="+")
        text_widget.bind("<FocusOut>", lambda _e: self._set_focused(False), add="+")
        self.bind("<Button-1>", self._focus_text)
        self.sync()

    @property
    def x(self):
        """Where the label starts inside the Text: 0, or past the caret's cell while the Text has the focus."""
        return self._caret_room() if self._focused else 0

    def _caret_room(self):
        """The caret's right edge at the first character: Tk draws it from x - insertwidth/2, a block cursor one
        cell + insertwidth wide, a bar insertwidth wide; then CARET_GAP."""
        tw = self._text
        try:
            iw = int(float(tw.cget("insertwidth")))
            block = str(tw.cget("blockcursor")).lower() in ("1", "true")
        except (tk.TclError, ValueError):
            iw, block = 2, False
        w = (caret_width(tw, tw.cget("font")) + iw - iw // 2) if block else (iw - iw // 2)
        return w + int(round(CARET_GAP * geom_ratio(self)))

    def _set_focused(self, on):
        if bool(on) == self._focused:
            return
        self._focused = bool(on)
        try:
            if self.shown:
                self.place_configure(x=self.x)
        except tk.TclError:
            pass
        self._rewrap()

    @property
    def shown(self):
        try:
            return self.winfo_manager() == "place"
        except tk.TclError:
            return False

    def _empty(self):
        try:
            return bool(self._text.compare("end-1c", "==", "1.0"))
        except tk.TclError:
            return False

    def sync(self, _e=None):
        """Show while the Text is empty, hide as soon as it holds anything."""
        try:
            if self._empty():
                if not self.shown:
                    self.place(in_=self._text, x=self.x, y=0)
                    self.lift(self._text)
            elif self.shown:
                self.place_forget()
        except tk.TclError:
            pass

    def _inner_width(self, width):
        tw = self._text
        try:
            pad = int(float(tw.cget("bd"))) + int(float(tw.cget("highlightthickness"))) + int(float(tw.cget("padx")))
        except (tk.TclError, ValueError):
            pad = 0
        return int(width) - 2 * pad

    def _on_text_configure(self, e=None):
        width = getattr(e, "width", None) or self._text.winfo_width()
        if width <= 1:
            return
        self._width = width
        self._rewrap()

    def _rewrap(self):
        if self._width is None:
            return
        wl = max(1, self._inner_width(self._width) - self.x - int(round(PLACEHOLDER_GAP * geom_ratio(self))))
        if wl != self._wl:
            self._wl = wl
            try:
                self.configure(wraplength=wl)
            except tk.TclError:
                pass

    def _focus_text(self, _e=None):
        try:
            self._text.focus_set()
            self._text.mark_set("insert", "1.0")
        except tk.TclError:
            pass
        return "break"


def caret_width(widget, font):
    """One character cell of `font` in pixels (the block insert caret; about 8 px for Consolas 11), at least 2."""
    try:
        return max(2, int(widget.tk.call("font", "measure", font, "0")))
    except tk.TclError:
        return 2


# ====================================================================== ChatStatusLine

STAGE_MAX_WORDS = 10     # a stage text with more words than this keeps the kit's character cut
STAGE_MAX_WORD = 24      # ... or with a longer word (a path, a URL)


def _is_number(word):
    w = word.rstrip(",;")
    return bool(w) and w[0].isdigit() and all(c.isdigit() or c in ".,%" for c in w)


def _stage_units(words):
    """The words, with each number joined to the word after it ('41 tokens,', '9.0 tok/s'): a count is never shown
    without its unit, nor a unit without its count."""
    units, i = [], 0
    while i < len(words):
        if _is_number(words[i]) and i + 1 < len(words):
            units.append(words[i] + " " + words[i + 1])
            i += 2
        else:
            units.append(words[i])
            i += 1
    return units


def elide_stage(text, fits):
    """A shortened form of a one-line STAGE text ('GENERATING...  41 tokens, 9.0 tok/s', 'QUEUED  waiting for a
    free slot') that fits(s) accepts, cut at word boundaries: the first word (the stage, its trailing dots
    dropped), an ellipsis, then as many whole units of the end (a number with its unit, or a word) as fit -
    'GENERATING … 9.0 tok/s', 'PROCESSING … 12,304 tokens', 'QUEUED … for a free slot' - or, when no unit fits,
    the stage word alone ('GENERATING …'). Never a cut inside a number, never a count or a unit on its own.
    None for a one-word or path-like text, or when not even the stage word fits (the caller keeps its own cut)."""
    words = str(text).split()
    if len(words) < 2 or len(words) > STAGE_MAX_WORDS or max(len(w) for w in words) > STAGE_MAX_WORD:
        return None
    units = _stage_units(words)
    head = units[0].rstrip(".,:;") or units[0]
    for m in range(len(units) - 1, -1, -1):
        cand = ("%s %s %s" % (head, ELLIPSIS, " ".join(units[len(units) - m:]))) if m else "%s %s" % (head, ELLIPSIS)
        if fits(cand):
            return cand
    return None


def _prefixed_stage(text):
    """The stage words of a one-line upper-case stage text: 'PREFIX: STAGE WORDS' (Chat's image-job stages, 'IMAGE
    GEN: RESTORING CHAT MODEL...') without its prefix, or a plain one of two words or more ('WAITING FOR RESPONSE',
    the wait texts); else None: a two-line text, a lower-case one (saved: / export failed: ...), or one with a
    path-like word or a slash."""
    s = str(text)
    if "\n" in s or "\r" in s or "\\" in s or "/" in s:
        return None
    prefix, rest = s.split(": ", 1) if ": " in s else ("", s)
    words = rest.split()
    if (not words or len(words) > STAGE_MAX_WORDS or max(len(w) for w in words) > STAGE_MAX_WORD
            or not rest.isupper() or (prefix and not prefix.isupper()) or (not prefix and len(words) < 2)):
        return None
    return " ".join(words)


def elide_prefixed_stage(text, fits):
    """B7b fix ADV1-06: a shortened form of a one-line upper-case stage text that fits(s) accepts. A 'PREFIX: '
    goes first (the hover tip and cget keep the whole text), then the stage words whole, or cut at word boundaries
    as elide_stage cuts them ('RESTORING ... MODEL...'): the kit's character cut read 'IMAGE GEN:...T MODEL...' for
    both RESTORING and UNLOADING CHAT MODEL. A plain wait text is cut the same way ('WAITING ... RESPONSE', not
    'WAITING F...RESPONSE', ADV1-01's cursor room). None when the text is not of that form or nothing fits."""
    rest = _prefixed_stage(text)
    if rest is None:
        return None
    if fits(rest):
        return rest
    return elide_stage(rest, fits)


class ChatStatusLine(StatusLine):
    """ChatStatusLine(master, theme=None, text="", **StatusLine options)

    Chat's rail_status. Everything is the kit StatusLine's (one line, the Override rule, the hover tip with the
    whole text, cget("text") the writer's text), except how a too-wide STAGE text is shortened. The writers'
    two-line texts ('GENERATING...\\n41 tokens, 9.0 tok/s', 'THINKING...', 'PROCESSING PROMPT...', 'QUEUED\\n
    waiting for a free slot') are cut at word boundaries (elide_stage); the kit's character cut in the middle
    could read 'GENERATING..…, 5.0 tok/s' or join a count to the wrong unit. A one-line image-generation stage
    ('IMAGE GEN: RESTORING CHAT MODEL...') drops its prefix first, then is cut the same way, and so is a one-line
    upper-case wait text ('WAITING FOR RESPONSE'; elide_prefixed_stage; ADV1-06). Other one-line texts (a saved:
    path, an export error) keep the kit's cut, which keeps both ends of a path. set_reserve(px) keeps room free at the right end while SignalAcquiring's wait cursor stands there
    (ADV1-01)."""

    _reserve_px = 0          # B7b fix ADV1-01: room kept free at the right end (the wait cursor's), see set_reserve

    def set_reserve(self, px):
        """Keep `px` at the line's right end free (SignalAcquiring's block cursor and its gap stand there): the text
        is elided to the width less `px`. 0 gives the whole width back. Re-renders only on a change."""
        px = max(0, int(px))
        if px != self._reserve_px:
            self._reserve_px = px
            self._render()

    def _avail(self):
        avail = StatusLine._avail(self)
        if avail is None or not self._reserve_px:
            return avail
        return max(1, avail - self._reserve_px)

    def _render(self):
        StatusLine._render(self)
        full = self._full if isinstance(self._full, str) else str(self._full)
        # only a text the kit really cut (is_elided() is also true for any whole two-line text: its one-line form
        # differs from the writer's text) and only a two-line stage text, or a one-line upper-case stage text
        if self._shown == one_line(full):
            return
        two_line = "\n" in full.replace("\r", "\n").strip("\n")
        if not two_line and _prefixed_stage(full) is None:
            return
        avail = self._avail()
        if avail is None:
            return
        try:
            font = tk.Label.cget(self, "font")

            def fits(s):
                return int(self.tk.call("font", "measure", font, s)) <= avail
            shorter = elide_stage(full, fits) if two_line else elide_prefixed_stage(full, fits)
        except tk.TclError:
            return
        if shorter is not None and shorter != self._shown:
            self._shown = shorter
            try:
                tk.Label.configure(self, text=shorter)
            except tk.TclError:
                pass
