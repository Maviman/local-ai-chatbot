"""Boot / signal-acquiring loading flourishes.

These stylize *real* waits only (startup, conversation load, time-to-
first-token) - never an artificial delay added purely for effect. Callers
push real status lines / start-stop around a real in-flight operation;
nothing in this module fabricates time on its own.
"""

import time
import tkinter as tk

from . import theme as theme_mod

try:        # B10: SignalAcquiring's cursor is fx.motion.WaitCursor (a guarded import: the text alone without it)
    from .fx.motion import WaitCursor as _WaitCursor
except Exception:  # noqa: BLE001
    _WaitCursor = None

# B9 FIX1 (ADV1-02): BootLog.set_lines keeps at most this many characters of one line (the tail end, behind "…")
SET_LINES_MAX_CHARS = 4000
# B9-H4 (bug-fix session, from the B9 hand-off): and at most this many characters over ALL its lines - the newest
# lines are kept whole, the oldest kept line is cut from the front behind "…", older lines are dropped. Two lines of
# the per-line cap: a ten-row ledger never shows more. Tk lays glyph-heavy text (emoji / ZWJ sequences, tabs among
# words) out per glyph, so an unbounded tail of them froze the window for the whole load.
SET_LINES_MAX_TOTAL = 2 * SET_LINES_MAX_CHARS


class BootLog(tk.Text):
    """Scrolling monospace log tail (e.g. the Home screen's boot-log panel).
    A rendering surface only - callers push real status lines via
    write_line()."""

    def __init__(self, parent, theme, height=6, **kwargs):
        kwargs.setdefault("height", height)
        kwargs.setdefault("bg", theme.panel)
        kwargs.setdefault("fg", theme.dim)
        kwargs.setdefault("insertbackground", theme.bright)
        kwargs.setdefault("font", theme_mod.FONT_MONO)
        kwargs.setdefault("relief", "flat")
        kwargs.setdefault("borderwidth", 0)
        kwargs.setdefault("state", "disabled")
        kwargs.setdefault("wrap", "char")
        super().__init__(parent, **kwargs)

    def write_line(self, text):
        self._last_clean = None          # B9-H4: the next set_lines must lay out again
        self.configure(state="normal")
        stamp = time.strftime("%H:%M:%S")
        self.insert("end", f"[{stamp}] {text}\n")
        self.see("end")
        self.configure(state="disabled")

    def clear(self):
        self._last_clean = None          # B9-H4
        self.configure(state="normal")
        self.delete("1.0", "end")
        self.configure(state="disabled")

    def set_lines(self, lines):
        """Bulk-replace content from an external real source (e.g. the tail
        of data/desktop.log) - unlike write_line(), this doesn't add its own
        timestamp, since the lines already carry their own real content.

        B9 (approved r2 ledger): the newest line carries the tag "boot_newest" (bright: its colour is the
        caller's, else the insert colour, which BootLog sets to bright); older lines keep the widget's fg
        (the model-load card passes the value tier). Every line has a hanging indent ("boot_line": wrapped
        rows start 4 characters in) and no prefix. Terminal escapes (ANSI colour codes) and other control
        characters are dropped, and CR/LF endings are normalised; the words themselves are kept.
        B9 FIX1 (ADV1-02): one line longer than SET_LINES_MAX_CHARS keeps only its last characters behind a
        leading "…" - Tk's wrap of one huge line is superlinear and froze the UI ~2 s of every 2.5 s.
        B9 FIX2: trailing lines that are blank once cleaned are dropped (ADV2-01), and U+2028 / U+2029 are
        removed with the other control characters (ADV2-02)."""
        import re
        # CSI (7- and 8-bit), OSC (7- and 8-bit), then any other escape (ESC ( B, ESC 7/8, ESC =, ESC c ...) or a lone ESC
        ansi = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x9b[0-?]*[ -/]*[@-~]"
                          r"|(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?|\x1b[ -/]*[0-~]?")
        # C0 (but tab / LF), DEL, C1, and U+2028 / U+2029 (str.splitlines leaves them at a line's end; B9 FIX2, ADV2-02)
        ctrl = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")
        cap = SET_LINES_MAX_CHARS
        clean = []
        for line in lines or ():
            text = line if isinstance(line, str) else str(line)
            for part in text.rstrip("\r\n").split("\n"):   # a line holding newlines stays several lines
                part = ctrl.sub("", ansi.sub("", part))
                clean.append(part if len(part) <= cap else "…" + part[-(cap - 1):])
        while clean and not clean[-1].strip():   # B9 FIX2 (ADV2-01): a trailing blank / escape-only line never
            clean.pop()                          # takes the bright newest slot or a row of the box
        total = 0                                # B9-H4: the total cap, newest lines first
        for i in range(len(clean) - 1, -1, -1):
            total += len(clean[i])
            if total > SET_LINES_MAX_TOTAL:
                keep = len(clean[i]) - (total - SET_LINES_MAX_TOTAL)
                if keep > 1:
                    clean[i] = "…" + clean[i][-(keep - 1):]
                    del clean[:i]
                else:
                    del clean[:i + 1]
                break
        # B9-H4: the overlay re-applies the tail every 500 ms whether or not the log changed; the same lines are
        # never laid out twice (the layout, not the read, is what froze the window)
        if clean == getattr(self, "_last_clean", None):
            return
        self._last_clean = list(clean)
        try:
            hang = int(self.tk.call("font", "measure", self.cget("font"), "0000"))
        except (tk.TclError, ValueError):
            hang = 28
        self.tag_configure("boot_line", lmargin1=0, lmargin2=hang)
        try:
            newest_ink = self.tag_cget("boot_newest", "foreground")
        except tk.TclError:   # the tag does not exist yet (the caller set no colour)
            newest_ink = ""
        if not newest_ink:
            self.tag_configure("boot_newest", foreground=self.cget("insertbackground"))
        self.configure(state="normal")
        self.delete("1.0", "end")
        last = len(clean) - 1
        for i, text in enumerate(clean):
            # no newline after the newest line: an empty last row would take one of the box's visible lines
            self.insert("end", text if i == last else text + "\n",
                        ("boot_line", "boot_newest") if i == last else ("boot_line",))
        self.see("end")
        self.configure(state="disabled")


class SignalAcquiring:
    """A real-wait status ('SENDING REQUEST', 'WAITING FOR RESPONSE', ...)
    bound to a real in-flight operation. Call start() when the wait begins
    and stop() the instant the real result arrives - it never runs longer
    than the actual wait.

    B7b (approved r2_chat, R-6): static. The label shows base_text as it is
    (the old animated dots and their own after(400) loop are gone) and a
    steady block cursor stands right after the text. start() while running
    re-applies a changed base_text at once (one wait moves from SENDING
    REQUEST to WAITING FOR RESPONSE / PROCESSING PROMPT...), and only writes
    when it changed.

    B10: a thin wrapper over fx.motion.WaitCursor, which now owns the cursor
    (a small Frame cell in the label's ink, PLACED over the label, so the
    cursor never touches the label's text; the line keeps its room free while
    it stands; it blinks through fx.clock once B11 turns the CRT blink on). A
    kit StatusLine owns one WaitCursor (.fx_wait_cursor, which it also starts
    itself on a wait text); this wrapper drives that same one, so a line never
    shows two cursors. A plain Label gets its own. If fx/motion cannot be
    imported, the status text alone is shown (no cursor)."""

    CURSOR_W, CURSOR_H, CURSOR_GAP = 5, 10, 6     # approved block_cursor: 5 x 10, 6 px after the text

    def __init__(self, label_widget, base_text="ACQUIRING SIGNAL"):
        self.label = label_widget
        self.base_text = base_text
        self._running = False
        self._own = None

    @property
    def running(self):
        return self._running

    def _wait_cursor(self):
        try:
            wc = getattr(self.label, "fx_wait_cursor", None)     # a kit StatusLine's own: one cursor per line
        except tk.TclError:
            wc = None
        if wc is None and _WaitCursor is not None:
            if self._own is None:
                self._own = _WaitCursor(None, self.label)
            wc = self._own
        return wc

    @property
    def cursor(self):
        """The block cursor cell (a tk.Frame), or None before the first start()."""
        wc = self._wait_cursor()
        return wc.cursor if wc is not None else None

    def start(self):
        self._running = True
        try:
            if str(self.label.cget("text")) != str(self.base_text):
                self.label.config(text=self.base_text)
        except tk.TclError:
            return
        wc = self._wait_cursor()
        if wc is not None:
            wc.start()

    def stop(self, final_text=None):
        self._running = False
        wc = self._wait_cursor()
        if wc is not None:
            wc.stop()
        if final_text is not None:
            try:
                self.label.config(text=final_text)
            except tk.TclError:          # B7b fix ADV1-11: a destroyed label, like start() / the cursor
                pass
