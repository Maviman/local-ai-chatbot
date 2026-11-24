"""Global + per-screen keyboard action registry.

Two-tier model (confirmed design decision, numbered quick-select primary):

1. Global, always active (bind_all on root) - wired once by main_window.py:
   Alt+1..4 switches the top-level screen via the rail regardless of what's
   focused, Ctrl+N creates a new session and jumps to Chat, Esc goes back /
   cancels, "/" focuses the nearest search field.
2. Local, per-screen quick-select - bare digit keys (no modifier) activate
   the Nth visible numbered item on the *current* screen, immediately (not
   just focus-move). Screens register/deregister these in on_show()/
   on_hide() (see screens/base.py) so screen A's "3" can never fire while
   screen B is visible. Standalone actions without a number get a single
   mnemonic letter instead (N, G, ...) via bind_mnemonic().

Every digit/mnemonic binding must go through is_text_entry_focused() first -
if focus is inside an Entry/Text (search box, a Settings field, the
composer), the key must be allowed to type normally, not hijacked as a
shortcut. This is a shared helper, not something each screen reimplements.
"""

import sys
import tkinter as tk

DIGIT_KEYS = [str(i) for i in range(1, 10)] + ["0"]

_TEXT_ENTRY_CLASSES = ("Entry", "TEntry", "TCombobox", "Text", "TSpinbox", "Spinbox")


def is_text_entry_focused(widget):
    """True if the currently focused widget accepts free-form text input, in
    which case bare-digit/mnemonic quick-select must be suppressed."""
    try:
        focused = widget.focus_get()
    except (KeyError, tk.TclError):
        # Can raise if focus is on a widget in a different toplevel.
        return False
    if focused is None:
        return False
    try:
        return focused.winfo_class() in _TEXT_ENTRY_CLASSES
    except tk.TclError:
        return False


# Set on the root window by screens/base.py while a kit confirm card is open (that module imports this one, so the name is
# repeated here rather than imported): the card's backdrop, or None.
_OPEN_CONFIRM_ATTR = "_kit_open_confirm"


def confirm_open(root):
    """True while a confirm card is open on this window. No global key may then act on the screen under the card: the card's own
    bindtag only covers the card's widgets, so a key arriving while focus sits on a widget under the backdrop would otherwise
    navigate back (leaving the card orphaned over another screen), switch screens, or quick-select an item."""
    card = getattr(root, _OPEN_CONFIRM_ATTR, None)
    if card is None:
        return False
    try:
        return bool(card.winfo_exists())
    except (tk.TclError, AttributeError):
        return False


def button_invoke_pending(root):
    """True while Tk's own `tk::ButtonInvoke` - what a Space on a focused button runs - still has its 100 ms timer pending
    (`after 100 {tk::ButtonInvokeEnd ...}`), i.e. the button's command has not run yet. Read from `after info`, a read-only
    query: no timer of our own. False on any error."""
    try:
        for aid in root.tk.splitlist(root.tk.call("after", "info")):
            try:
                script = root.tk.call("after", "info", aid)
            except tk.TclError:
                continue
            if "ButtonInvokeEnd" in str(script):
                return True
    except tk.TclError:
        pass
    return False


# bind_all() registers a Tcl command (and keeps the Python closure behind it alive)
# on every call, and unbind_all() removes the binding but NOT that command - so every
# screen show/hide used to leave one behind per digit/mnemonic key it had bound, ~10
# a visit, for the life of the window (found by tests/diagnostics/soak_ui.py). The
# command's id is kept here so unbinding can delete it along with the binding.
_commands = {}   # (id(root), sequence) -> the command bind_all registered for it


def _free_command(root, sequence):
    funcid = _commands.pop((id(root), sequence), None)
    if funcid:
        try:
            root.deletecommand(funcid)
        except tk.TclError:
            pass


# B13-H3: Tk matches a bare <Key-m> binding for Alt+m or Ctrl+m too when no more specific binding exists, so under the
# effects kill switch (no Alt+M quick-mute binding) Alt+M on Projects opened Manage project. A guarded bare-key binding
# fires only on the bare key: a chord passes through to its own binding, or to nobody. Alt is 0x20000 on Windows
# (0x8, Mod1, elsewhere); Control is 0x4. Shift / CapsLock / NumLock are not chords.
CHORD_MASK = (0x20000 if sys.platform == "win32" else 0x8) | 0x4


def bind_guarded(root, sequence, callback):
    """Bind `sequence` on the "all" bindtag, but only invoke `callback` (and
    swallow the key with "break") when focus isn't in a text entry - see
    is_text_entry_focused(). A chord (Alt / Control held) is not the bare key:
    it passes (B13-H3). Returns `sequence` so callers can unbind later."""

    def handler(event):
        try:
            if int(getattr(event, "state", 0) or 0) & CHORD_MASK:
                return None
        except (TypeError, ValueError):
            pass
        if confirm_open(root):
            return "break"          # a confirm card is open: nothing under it may be quick-selected
        if getattr(root, "_navigation_locked", False):
            return "break"          # B9-H1: a model load is running under its card: nothing under it either
        if is_text_entry_focused(root):
            return None
        callback()
        return "break"

    _free_command(root, sequence)   # binding the same sequence again replaces the old binding - and strands its command
    _commands[(id(root), sequence)] = root.bind_all(sequence, handler)
    return sequence


def unbind_all_sequences(root, sequences):
    for sequence in sequences:
        root.unbind_all(sequence)
        _free_command(root, sequence)   # safe even from inside that very key's handler (tkinter's own after() does the same)


def bind_digit_quick_select(root, callbacks):
    """callbacks: ordered list matching the on-screen 01, 02, ... numbering -
    index 0 activates on "1", ... index 8 on "9", index 9 (the 10th, "10")
    on "0". Only the first 10 are bindable (single bare keypress). Returns
    the bound sequences for unbind_all_sequences() to clear on_hide()."""
    return [bind_guarded(root, f"<Key-{DIGIT_KEYS[i]}>", cb) for i, cb in enumerate(callbacks[:10])]


def bind_mnemonic(root, letter, callback):
    """Single-letter mnemonic for a standalone action that isn't numbered
    (e.g. N = New project, G = New group). Returns the bound sequence."""
    return bind_guarded(root, f"<Key-{letter.lower()}>", callback)


class Keymap:
    """Action registry (name -> callback) so a menu box's click handler and
    its keyboard shortcut invoke the exact same function, plus the global
    bindings every screen shares."""

    def __init__(self, root):
        self.root = root
        self._actions = {}
        self._global_bound = False

    def register(self, name, callback):
        self._actions[name] = callback

    def invoke(self, name):
        action = self._actions.get(name)
        if action is not None:
            action()

    def bind_global(self, on_switch_screen, on_new_session, on_back, on_focus_search):
        """Wire the always-active bindings exactly once, at startup."""
        if self._global_bound:
            return
        self._global_bound = True
        root = self.root

        def unless_confirm(action):
            """The global action - unless a confirm card is open, when the key is swallowed instead (confirm_open). The card's own
            Escape/Enter/Tab are widget bindings on the card and run before these, as before."""
            def handler(event):
                if confirm_open(root):
                    return "break"
                return action()
            return handler

        for i in range(1, 6):
            root.bind_all(f"<Alt-Key-{i}>", unless_confirm(lambda i=i: on_switch_screen(i)))
        root.bind_all("<Control-n>", unless_confirm(on_new_session))
        root.bind_all("<Control-N>", unless_confirm(on_new_session))

        def escape_handler(event):
            if confirm_open(root):
                return "break"
            if button_invoke_pending(root):
                # A Space on a button is still being invoked (Tk waits 100 ms before running the command): going back now
                # would run FIRST, and a confirm card that button opens (SHUT DOWN) would then land over the wrong screen
                # (B3 ADV2 P2). The Esc is simply not a back step in that window; the next one is.
                return None
            return on_back()

        root.bind_all("<Escape>", escape_handler)

        def slash_handler(event):
            if confirm_open(root):
                return "break"
            if is_text_entry_focused(root):
                return None  # let '/' type normally in a text field
            on_focus_search()
            return "break"

        root.bind_all("<Key-slash>", slash_handler)
