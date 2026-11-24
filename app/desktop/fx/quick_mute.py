"""fx/quick_mute.py - Alt+M mutes or unmutes every cue, from any screen (B13; BUILD_PLAN §4 B13, spec ST-14, CR-23,
GAP_FX 4.1).

    install(fx)    -> QuickMute, or None     fx.install calls it once (a failure leaves Alt+M unbound; never fails install)
    bound(widget)  -> bool                   Alt+M is bound on widget's window (Settings 08's legend lists ALT+M only then)

Binding: <Alt-m> / <Alt-M> on the window's own toplevel bindtag ('.'). Every widget of the window carries that tag
after its own and its class tags and before 'all', and the handler returns 'break': that is load-bearing, because the
bare 'm' mnemonic of Projects detail is bound on 'all' (keymap.bind_mnemonic) and would otherwise open Manage project
too. A text field inserts nothing: Tk's Entry / Text class bindings ignore Alt+key. A confirm card's widgets carry
base.CONFIRM_MODAL_TAG, whose <Key> binding breaks before '.': the same handler is bound on that tag too (more specific
than <Key>), so Alt+M still toggles while a confirm is open (ST-14, intended) and nothing reaches the screen under it.

Toggle: prefs.set("fx.sound.enabled", not current) FIRST (the engine follows the store synchronously through its
bind_store subscription; Settings 08's master lamp through its own), then ONE notice:
    'SOUND OFF · ALT+M TO RESTORE'  kind success, 1800 ms, sound='silent'
    'SOUND ON'                      kind success, 1800 ms, sound='load_online'  (the one acknowledgement cue, CR-23)
The previous quick-mute notice is dismissed first, so a run of presses leaves one notice on screen. A held key's
auto-repeat toggles once: a press while the key is still down (no release since the last press, within HOLD_STALE_MS)
is swallowed, so holding Alt+M never flip-flops the sound.

Teardown: the bindings are removed and their Tcl commands deleted by fx.teardown (and by install's own undo path),
through the uninstall callable this module appends to fx._prefs_unsubs (the list fx runs at teardown step 4 and in
_undo_partial). No timer, no thread, no file I/O; Tk thread only.
"""

import sys

__all__ = ["SEQUENCES", "MUTED_TEXT", "UNMUTED_TEXT", "NOTICE_MS", "HOLD_STALE_MS", "QuickMute", "install", "bound"]

SEQUENCES = ("<Alt-m>", "<Alt-M>")
RELEASES = ("<KeyRelease-m>", "<KeyRelease-M>")
LEAF = "fx.sound.enabled"
MUTED_TEXT = "SOUND OFF · ALT+M TO RESTORE"
UNMUTED_TEXT = "SOUND ON"
NOTICE_MS = 1800
HOLD_STALE_MS = 1500        # a "held" press older than this is a new press (its release was missed: focus moved)


def _log(msg):
    try:
        sys.stderr.write("fx quick mute: " + str(msg).rstrip("\n") + "\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


class QuickMute(object):
    """The installed Alt+M of one FX (fx.quick_mute)."""

    def __init__(self, fx):
        self.fx = fx
        self.root = fx.root
        self.installed = False
        self.toggles = 0                 # presses that toggled (tests / the harness read it)
        self.swallowed = 0               # auto-repeat presses that did not
        self._held = False
        self._last_t = 0
        self._notice = None
        self._bound = []                 # (tag, sequence, funcid)

    # ---------------------------------------------------------------- binding
    def _bind(self, tag, seq, fn):
        root = self.root
        if tag == str(root):
            funcid = root.bind(seq, fn, add="+")
        else:
            funcid = root.bind_class(tag, seq, fn, add="+")
        if funcid:
            self._bound.append((tag, seq, funcid))

    def bind(self):
        top = str(self.root)
        for seq in SEQUENCES:
            self._bind(top, seq, self._on_press)
        for seq in RELEASES:
            self._bind(top, seq, self._on_release)
        modal = None
        try:
            from ..screens import base as _base      # lazy: screens.base imports fx
            modal = getattr(_base, "CONFIRM_MODAL_TAG", None)
        except Exception as e:  # noqa: BLE001 - only the confirm-card case is lost
            _log("the confirm card's tag is unavailable (%r): Alt+M will not act while a confirm is open" % (e,))
        if modal:
            for seq in SEQUENCES:
                self._bind(modal, seq, self._on_press)
        self.installed = True
        return self

    def uninstall(self):
        """Remove exactly this module's binding lines (tkinter 3.12's unbind(seq, funcid) would drop every other
        binding of that sequence) and delete their Tcl commands. Idempotent, never raises."""
        bound, self._bound = self._bound, []
        self.installed = False
        for tag, seq, funcid in bound:
            try:
                cur = str(self.root.tk.call("bind", tag, seq))
                keep = [ln for ln in cur.split("\n") if funcid not in ln]
                self.root.tk.call("bind", tag, seq, "\n".join(keep).strip("\n"))
            except Exception:  # noqa: BLE001 - the interpreter may already be gone
                pass
            try:
                self.root.deletecommand(funcid)
            except Exception:  # noqa: BLE001
                pass
        self._notice = None
        return None

    # ---------------------------------------------------------------- keys
    def _on_press(self, event=None):
        t = 0
        try:
            t = int(getattr(event, "time", 0) or 0)
        except (TypeError, ValueError):
            t = 0
        if self._held and (not t or not self._last_t or 0 <= t - self._last_t <= HOLD_STALE_MS):
            self._last_t = t or self._last_t
            self.swallowed += 1          # the key's auto-repeat: one toggle per press
            return "break"
        self._held = True
        self._last_t = t
        self.toggle()
        return "break"

    def _on_release(self, _event=None):
        self._held = False
        return None

    # ---------------------------------------------------------------- the act
    def toggle(self):
        """Flip fx.sound.enabled and show the one notice. -> the new value, or None when nothing could be done (fx
        torn down, no store). Never raises."""
        fx = self.fx
        if getattr(fx, "_torn", False) or getattr(fx, "_tearing", False):
            return None
        prefs = getattr(fx, "prefs", None)
        if prefs is None or getattr(prefs, "closed", False):
            return None
        try:
            cur = prefs.get(LEAF)
            new = not (cur if isinstance(cur, bool) else True)
            prefs.set(LEAF, new)                       # prefs FIRST: the engine and the 08 lamp follow at once
        except Exception as e:  # noqa: BLE001
            _log("toggle failed: %r" % (e,))
            return None
        self.toggles += 1
        self._show_notice(new)
        return new

    def _show_notice(self, enabled):
        prev, self._notice = self._notice, None
        dismiss = getattr(prev, "notice_dismiss", None)
        if callable(dismiss):
            try:
                if prev.winfo_exists():
                    dismiss()
            except Exception:  # noqa: BLE001
                pass
        try:
            from ..screens import base as _base
            theme = self.fx.theme
            if enabled:
                frame = _base.show_toast(self.root, theme, "SOUND ON", kind="success", duration_ms=1800,
                                         sound="load_online")
            else:
                frame = _base.show_toast(self.root, theme, "SOUND OFF · ALT+M TO RESTORE", kind="success",
                                         duration_ms=1800, sound="silent")
            self._notice = frame
        except Exception as e:  # noqa: BLE001 - the toggle stands; only its notice is lost
            _log("notice failed: %r" % (e,))


def install(fx):
    """Bind Alt+M for this FX's window (see the module docstring). -> the QuickMute (also fx.quick_mute), or None."""
    root = getattr(fx, "root", None)
    if root is None:
        return None
    qm = QuickMute(fx)
    try:
        qm.bind()
    except Exception as e:  # noqa: BLE001
        qm.uninstall()
        _log("not installed: %r" % (e,))
        return None
    fx.quick_mute = qm
    unsubs = getattr(fx, "_prefs_unsubs", None)
    if isinstance(unsubs, list):
        unsubs.append(qm.uninstall)
    return qm


def bound(widget):
    """True when Alt+M is bound on widget's window now (its FX is live and installed it). Never raises."""
    try:
        from . import get
        qm = getattr(get(widget), "quick_mute", None)
        return bool(qm is not None and qm.installed)
    except Exception:  # noqa: BLE001
        return False
