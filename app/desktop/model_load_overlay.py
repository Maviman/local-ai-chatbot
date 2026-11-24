"""Full-window blocking overlay shown while llama-server is restarting to
load a model - for the duration of an eager model/vision switch
(AssetManagerPanel, see asset_manager.py) or a Settings save that changes
a restart-requiring field (context size, GPU layers, KV cache type - see
settings_screen.py's RESTART_KEYS).

Why this exists: navigating to another screen, or closing/shutting down
the app, while llama-server is mid-restart loading a (possibly many-GB)
model into VRAM has been reported to hang the whole app - and reportedly
the whole machine (found during live testing, 2026-08-04). This overlay
makes that window of time impossible to navigate out of: MainWindow's
rail, Alt+1..4/Esc/Ctrl+N, and the shutdown/close paths are all locked via
ctx.lock_navigation() for as long as the overlay is showing - see
main_window.py's _lock_navigation()/_unlock_navigation().

"Progress" is shown honestly - a live elapsed timer plus a tail of
llama-server's own real log output (the same technique home_screen.py's
boot log already uses for desktop.log) - not a fabricated percentage,
since llama.cpp doesn't expose a numeric one over the API. The intent is
also to keep the app visibly *alive* during the wait, so a user isn't
tempted to force-kill it via Task Manager thinking it's hung - which,
mid-restart, is exactly the action this whole feature exists to prevent.
"""

import os
import tkinter as tk

from . import theme as theme_mod
from .async_call import Latest
from .boot_fx import BootLog
from .logtail import read_log_tail

_LLAMA_LOG_PATH = os.path.join(theme_mod.DATA_DIR, "llama-server.log")

# How long the restart/model-switch flows keep waiting for llama-server to
# report healthy before giving up and releasing the navigation lock, in the 1 s
# polls they make. A load that FAILS ends early on its own - /api/health turns
# to "error" the moment the process dies - so this only bounds a load that is
# progressing normally, and a cold multi-GB model on a hard disk legitimately
# takes minutes (measured here: ~245 s for a 20 GB model at ~90 MB/s). The old
# 2-minute cap cut off perfectly healthy loads with a "taking a long time"
# warning.
RESTART_WAIT_SECONDS = 15 * 60

# B9 (approved r2_model_load): the title is drawn upper-case on the LOADING plate (D-27 keeps OVL-01's text);
# the warning is ONE paragraph with the D-12 wording (L-06: "the model server", the hard newline dropped).
TITLE = "Loading model"
WARNING = ("Switching models restarts the model server. Do not close the app or navigate away - "
           "interrupting a load mid-restart can hang the whole machine.")
LEDGER_LINES = 10


ELAPSED_TENTHS_MS = 100     # B10: at motion FULL the readout counts tenths, from fx.clock (lab startLoad)


def format_elapsed(seconds, tenths=False):
    """The ELAPSED readout: T+mm:ss (minutes keep counting past 59, so a 1 h 02 min load reads T+62:06).
    tenths=True (motion FULL, B10): T+mm:ss.t - truncated, never rounded up (59.96 s reads T+00:59.9)."""
    if tenths:
        try:
            t10 = max(0, int(float(seconds) * 10 + 1e-6))      # 1.0 s measured as 0.9999999 still reads .0
        except (TypeError, ValueError, OverflowError):
            t10 = 0
        return "T+%02d:%02d.%d" % (t10 // 600, (t10 % 600) // 10, t10 % 10)
    try:
        s = max(0, int(seconds))
    except (TypeError, ValueError, OverflowError):
        s = 0
    return "T+%02d:%02d" % (s // 60, s % 60)


class ModelLoadOverlay:
    def __init__(self, anchor, ctx, label):
        self.root = anchor.winfo_toplevel()
        self.ctx = ctx
        self._elapsed = 0
        self._clock_after_id = None
        self._tail_after_id = None
        self._fx_clock = None        # B10: fx.clock while the tenths loop runs (motion FULL at the start)
        self._t0 = None
        self._finished = False
        # The log is read on a worker thread: it sits on the disk this very load is
        # saturating, and reading it on the Tk thread (the whole file, every 500 ms)
        # froze the window for as long as the disk stayed busy.
        self._tail_loader = Latest(self.root, deliver_when=lambda: not self._finished)

        theme = ctx.theme
        ctx.lock_navigation()

        self.backdrop = tk.Frame(self.root, bg=theme.bg)
        self.backdrop.place(x=0, y=0, relwidth=1, relheight=1)
        self.backdrop.tkraise()
        # Deliberately no click-to-dismiss binding (unlike show_inline_confirm) -
        # this overlay is non-dismissable by design, see module docstring.

        # B9: the approved r2 card (kit/loadcard.py): the LOADING plate, the label, ELAPSED T+mm:ss, the warning
        # and the ledger. F-1: the overlay owns elapsed_var and releases it in finish(); the card creates no
        # tkinter Variable or Image of its own.
        self.elapsed_var = tk.StringVar(value=format_elapsed(0))
        self.card = None
        try:
            from .kit.loadcard import LoadCard
            self.card = LoadCard(self.backdrop, theme, TITLE, label, WARNING, self.elapsed_var,
                                 lambda parent: self._make_ledger(parent, theme))
            card = self.card
            self.boot_log = self.card.ledger
        except Exception as exc:  # noqa: BLE001 - a kit failure must never leave the load without its card
            self.card = None
            self.card_error = repr(exc)
            try:
                self.backdrop.unbind("<Configure>")   # a half-built card may have bound its refit here
            except tk.TclError:
                pass
            for child in self.backdrop.winfo_children():
                child.destroy()
            card = self._fallback_card(theme, label)

        # BF-17: remember where the keyboard was, so finish() can put it back there
        self._prev_focus = self._focus_now()
        card.focus_set()  # steal keyboard focus off whatever's underneath
        if not self._start_tenths():
            self._tick_clock()
        self._tail_log()

    @staticmethod
    def _make_ledger(parent, theme):
        """The ledger: today's BootLog, bg inside the card's 1 px dim box, Consolas 10. set_lines tags the
        newest line bright ("boot_newest"); the older lines keep the widget's fg, the value tier."""
        from .kit import C10, tokens
        t = tokens(theme)
        # B9-H1: not a Tab stop (takefocus=0); a click still focuses a Text on Windows, so the card swallows Tab too
        log = BootLog(parent, theme, height=LEDGER_LINES, width=1, bg=t["bg"], fg=t["value"],
                      insertbackground=t["bright"], font=C10, highlightthickness=0, cursor="arrow", takefocus=0)
        log.tag_configure("boot_newest", foreground=t["bright"])
        return log

    def _fallback_card(self, theme, label):
        """The pre-B9 widgets (with the D-12 wording), used only if the kit card could not be built."""
        card = tk.Frame(self.backdrop, bg=theme.bg, highlightthickness=1, highlightbackground=theme.dim, takefocus=1)
        card.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(card, text=TITLE.upper(), bg=theme.bg, fg=theme.bright,      # B15: no letter-spacing
                 font=theme_mod.FONT_HEADER).pack(padx=36, pady=(24, 6))
        tk.Label(card, text=label, bg=theme.bg, fg=theme.text, font=theme_mod.FONT_BODY_BOLD, wraplength=500,
                 justify="center").pack(padx=36)
        tk.Label(card, text=WARNING, bg=theme.bg, fg=theme.warn, font=theme_mod.FONT_CAPTION, justify="center",
                 wraplength=500).pack(padx=36, pady=(8, 10))
        tk.Label(card, textvariable=self.elapsed_var, bg=theme.bg, fg=theme.dim, font=theme_mod.FONT_CAPTION).pack()
        self.boot_log = BootLog(card, theme, height=LEDGER_LINES, takefocus=0)
        self.boot_log.configure(width=76)
        self.boot_log.pack(fill="both", expand=True, padx=36, pady=(10, 24))
        self._swallow_tabs(card)        # B9-H1: as the kit card does for itself
        return card

    def _focus_now(self):
        """The widget holding the keyboard focus, or None (BF-17)."""
        try:
            return self.root.focus_get()
        except (KeyError, tk.TclError):
            return None

    @staticmethod
    def _swallow_tabs(widget):
        """B9-H1: Tab / Shift-Tab bound to "break" on `widget` and everything inside it, so Tk's "all"-tag focus
        traversal never walks out to the widgets under the backdrop (it covers them, but does not stop traversal)."""
        stack = [widget]
        while stack:
            w = stack.pop()
            for seq in ("<Tab>", "<Shift-Tab>", "<ISO_Left_Tab>", "<Control-Tab>", "<Control-Shift-Tab>"):
                try:
                    w.bind(seq, lambda _e: "break", add="+")
                except tk.TclError:
                    continue
            try:
                stack.extend(w.winfo_children())
            except tk.TclError:
                pass

    def _tick_clock(self):
        if self._finished:  # F-1: never touch elapsed_var after finish() dropped it
            return
        self._elapsed += 1
        self.elapsed_var.set(format_elapsed(self._elapsed))
        self._clock_after_id = self.root.after(1000, self._tick_clock)

    def _fx(self):
        try:
            from . import fx as fx_mod
            return fx_mod.get(self.root)
        except Exception:  # noqa: BLE001
            return None

    def _start_tenths(self):
        """B10: at motion FULL, ELAPSED counts T+mm:ss.t every 100 ms on an fx.clock loop owned by the backdrop
        (dropped with it), measured on the clock's own time. -> False when fx is off, not FULL, or the loop was
        refused: today's 1 s root.after clock runs instead."""
        fx = self._fx()
        clk = getattr(fx, "clock", None) if getattr(fx, "enabled", False) else None
        if clk is None:
            return False
        try:
            if fx.level() != "full":
                return False
            self._t0 = clk.now()
            if clk.loop(self.backdrop, "elapsed", ELAPSED_TENTHS_MS, self._tick_tenths) is None:
                return False
        except Exception:  # noqa: BLE001 - a clock problem leaves today's 1 s clock
            return False
        self._fx_clock = clk
        self.elapsed_var.set(format_elapsed(0, tenths=True))
        return True

    def _tick_tenths(self, now):
        if self._finished or self.elapsed_var is None:  # F-1
            return
        fx = self._fx()
        full = fx is not None and fx.level() == "full"     # the watchdog / a Settings change: whole seconds
        self.elapsed_var.set(format_elapsed(max(0.0, float(now) - float(self._t0)), tenths=full))

    def _tail_log(self):
        # The next tick is armed first, so the cadence never depends on how long a
        # read takes; only one read is ever in flight (a stalled disk skips ticks
        # instead of piling up threads), and the lines are applied back on the Tk thread.
        self._tail_after_id = self.root.after(500, self._tail_log)
        if not self._tail_loader.busy:
            self._tail_loader.start(lambda: read_log_tail(_LLAMA_LOG_PATH, 14), self.boot_log.set_lines)

    def finish(self):
        """Tears the overlay down and unlocks navigation. Safe to call more
        than once (e.g. from more than one poll-loop exit branch) - only
        the first call does anything."""
        if self._finished:
            return
        self._finished = True
        self._tail_loader.invalidate()
        if self._clock_after_id is not None:
            try:
                self.root.after_cancel(self._clock_after_id)
            except tk.TclError:
                pass
        if self._tail_after_id is not None:
            try:
                self.root.after_cancel(self._tail_after_id)
            except tk.TclError:
                pass
        clk, self._fx_clock = self._fx_clock, None
        if clk is not None:
            try:
                clk.cancel(self.backdrop, "elapsed")       # B10 (the backdrop's destroy drops it too)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.backdrop.destroy()
        except tk.TclError:
            pass
        # BF-17: the keyboard goes back to the widget that had it before the card (if it still exists and shows);
        # otherwise it stays wherever Tk left it, as before
        prev, self._prev_focus = getattr(self, "_prev_focus", None), None
        try:
            if prev is not None and prev.winfo_exists() and prev.winfo_viewable():
                prev.focus_set()
        except (tk.TclError, AttributeError):
            pass
        # F-1: the overlay sits in a reference cycle (self -> _tail_loader -> deliver_when closure
        # -> self), so without this the StringVar would be finalised by the CYCLIC collector, on
        # whichever thread triggers it. A worker thread then makes a Tcl call that waits for the
        # Tk thread and the window freezes for good. Dropping it here frees it on the Tk thread.
        self.elapsed_var = None
        self.ctx.unlock_navigation()
