"""Screen base class every router-hosted view conforms to, plus the shared
scrollable-container helper more than one screen needs (Chats' row list,
Projects' grid, Home's directory can all outgrow the window)."""

import tkinter as tk
from tkinter import ttk

from .. import fx as fx_mod
from .. import keymap as keymap_mod
from .. import theme as theme_mod

# Tcl global holding the palette apply_ttk_theme last applied in an
# interpreter. Stored IN the interpreter (not keyed by its address in Python)
# so a later window in the same process - e.g. the verification harness's
# second MainWindow - can never inherit a stale "already applied".
_TTK_SIGNATURE_VAR = "::localai_ttk_signature"
_TTK_RETRY_SUFFIX = "|kitfail"      # stamped after a failed kit install: one more full pass is allowed (BF-18)


class Screen(tk.Frame):
    """Standardizes on_show()/on_hide() and the one correct geometry-manager
    check for "am I currently the visible screen" - winfo_manager() ==
    "pack", not winfo_ismapped(). winfo_ismapped() also depends on the
    top-level window's own mapped state (e.g. goes false if the whole app
    is minimized); winfo_manager() only reports whether this frame is the
    one the router currently has packed into the content area. This is the
    exact fix already learned once from a real Phase 4 ProjectsPanel bug -
    centralizing it here stops it from being independently (mis)implemented
    per screen a second time.
    """

    def __init__(self, parent, ctx):
        super().__init__(parent, bg=ctx.theme.bg)
        self.ctx = ctx
        self._digit_sequences = []
        self._mnemonic_sequences = []

    def is_visible(self):
        return self.winfo_manager() == "pack"

    def focus_search(self):
        """Called by the router's global "/" handler. Default no-op;
        screens with a search field (e.g. ListScreen) override it."""

    def handle_back(self):
        """Called by the router's global Esc handler before it pops screen
        history. Return True if this screen consumed the back action (e.g.
        Projects' detail view returning to its grid); False to let the
        router navigate to the previous screen."""
        return False

    def legend_items(self):
        """The key legend the shell paints on the bottom line while this
        screen is showing (B2): a list of (key, verb) pairs, e.g.
        [("1-9", "open"), ("ESC", "back")]. Every key listed must really be
        bound while the screen is visible (tests/fx/test_legend_bound.py).
        The router reads it right after on_show(); a screen whose keys change
        while it is showing (Projects grid <-> detail) asks for a repaint
        through ctx.refresh_legend(). Default: no legend.
        Digit ranges come from digit_legend() (B2 fix ADV1-03/04): they show
        exactly the digits bound right now, and every bind_digits() /
        clear_digit_bindings() asks the shell to repaint."""
        return []

    def digit_count(self):
        """How many digit keys this screen has bound now (0-10: 1..9, then 0)."""
        return len(self._digit_sequences)

    def digit_legend(self, verb):
        """[(range, verb)] for the digits bound now - "1", "1-6", "0-9" (ten
        rows: 0 is the 10th) - or [] when none is bound, so the legend never
        shows a digit that does nothing."""
        key = digit_range(self.digit_count())
        return [(key, verb)] if key else []

    def _legend_keys_changed(self):
        refresh = getattr(self.ctx, "refresh_legend", None)
        if refresh is not None:
            try:
                refresh()
            except tk.TclError:
                pass

    def handle_file_drop(self, paths):
        """Called by the router when the user drops OS files anywhere on
        the window while this screen is visible (see win_dnd.py). paths is
        a list of absolute filesystem paths. Default no-op; only screens
        that accept file attachments (ChatView) override it."""

    def on_show(self):
        """Called by the router every time it swaps this screen in. Override
        to refetch data (so the screen never shows a stale snapshot) and to
        register local digit/mnemonic bindings via bind_digits()/
        bind_mnemonic() below."""

    def on_hide(self):
        """Called by the router right before swapping this screen out.
        Default deregisters this screen's digit/mnemonic bindings so screen
        A's "3" can never fire while screen B is visible. Override only to
        add behavior on top of this, not to replace it."""
        self.clear_local_bindings()

    def bind_digits(self, callbacks):
        """callbacks: ordered list matching this screen's on-screen 01, 02...
        numbering. Replaces any digit bindings this screen made previously."""
        root = self.winfo_toplevel()
        keymap_mod.unbind_all_sequences(root, self._digit_sequences)
        self._digit_sequences = keymap_mod.bind_digit_quick_select(root, callbacks)
        self._legend_keys_changed()

    def bind_mnemonic(self, letter, callback):
        root = self.winfo_toplevel()
        sequence = keymap_mod.bind_mnemonic(root, letter, callback)
        self._mnemonic_sequences.append(sequence)
        return sequence

    def clear_digit_bindings(self):
        root = self.winfo_toplevel()
        keymap_mod.unbind_all_sequences(root, self._digit_sequences)
        self._digit_sequences = []
        self._legend_keys_changed()

    def clear_local_bindings(self):
        root = self.winfo_toplevel()
        keymap_mod.unbind_all_sequences(root, self._digit_sequences + self._mnemonic_sequences)
        self._digit_sequences = []
        self._mnemonic_sequences = []

    def destroy(self):
        # Screens are normally torn down with the whole window, where
        # MainWindow.destroy() has already swept the tree - but they're also
        # built standalone by the verification harness, so each one cleans up
        # after itself rather than depending on its host to do it.
        cancel_pending_after_callbacks(self)
        super().destroy()


def digit_range(count):
    """The legend key for `count` bound quick-select digits (keymap.bind_digit_quick_select:
    1..9, then 0 for the 10th): "" for none, "1", "1-N", or "0-9" for ten."""
    count = max(0, min(10, int(count or 0)))
    if count == 0:
        return ""
    if count == 1:
        return "1"
    if count == 10:
        return "0-9"
    return "1-%d" % count


def cancel_pending_after_callbacks(widget):
    """Cancel every after() callback still queued against `widget` or any of
    its descendants, so tearing it down can't leave orphaned timers behind.

    Destroying a widget deletes the Tcl commands tkinter registered for its
    after() callbacks, but does NOT cancel the after events still pointing at
    them - each one later fires into nothing and Tk reports an "invalid
    command name <id>_<callback>" background error. That's harmless in the
    real app (destroy means process exit), but Tcl timers are per-THREAD, not
    per-interpreter, so in a multi-window host - e.g. the same-process
    verification harness, which builds a window, destroys it, then builds
    another - the second window's event loop services the first window's
    orphans and the errors surface for real.

    The always-pending loops are id-tracked individually at their call sites
    (MainWindow._gui_events_after_id, HomeScreen._clock_after_id,
    ChatView._spinner_after_id). This is the net for everything else: the
    short-lived worker/queue polls scattered across every screen, which are
    only in flight during an operation and so leak or don't purely by
    teardown timing - one harness run happened to catch _poll_status_queue,
    _poll_quick_resume_queue and _poll_max_parallel_chats_queue that way.
    Id-tracking each of those would mean touching ~25 call sites and would
    still miss the next loop someone adds.

    Cancelling must go through the widget that actually OWNS the callback,
    never through `widget`: after_cancel() deletes the underlying Tcl command
    and prunes it from the calling widget's _tclCommands, so cancelling
    another widget's callback leaves a dangling name in the owner's list and
    the owner's own destroy() then dies with "can't delete Tcl command"
    (confirmed empirically, not theoretical).
    """
    try:
        pending = widget.tk.splitlist(widget.tk.call("after", "info"))
    except tk.TclError:
        return  # interpreter already going away
    if not pending:
        return
    # Tcl script name -> the pending after-id(s) that will run it. tkinter
    # names that script after the callback and records it in the owning
    # widget's _tclCommands, which is what makes the owner findable below.
    scripts = {}
    for after_id in pending:
        try:
            info = widget.tk.splitlist(widget.tk.call("after", "info", after_id))
        except tk.TclError:
            continue  # fired or was cancelled between the two calls
        if info:
            scripts.setdefault(info[0], []).append(after_id)

    stack = [widget]
    while stack:
        current = stack.pop()
        try:
            stack.extend(current.winfo_children())
        except tk.TclError:
            pass
        # Copied because after_cancel() mutates _tclCommands as it goes.
        for name in list(getattr(current, "_tclCommands", None) or ()):
            for after_id in scripts.get(name, ()):
                try:
                    current.after_cancel(after_id)
                except tk.TclError:
                    pass


def apply_ttk_theme(theme):
    """The single place every ttk widget style is configured - the plan's
    own cautionary tale is three independent ttk.Style() calls that quietly
    drifted, so no screen/dialog configures ttk styles on its own. Safe to
    call repeatedly (configure is idempotent); MainWindow calls it once at
    startup and make_scrollable() re-invokes it defensively for standalone
    (test-harness) use - a re-call with the palette already applied returns
    before touching ttk (a full pass costs 25-70 ms with a real widget tree,
    and make_scrollable runs on every Thoughts open)."""
    style = ttk.Style()
    # One UI (D-31): the flat chrome below is part of the look, not an effect, so the
    # kill switch no longer skips it. "kit1" versions the signature.
    signature = "|".join((theme.bright, theme.dim, theme.bg, theme.panel, theme.text, "kit1"))
    stamped = None
    try:
        if style.tk.call("info", "exists", _TTK_SIGNATURE_VAR):
            stamped = str(style.tk.call("set", _TTK_SIGNATURE_VAR))
            if stamped == signature:
                return
    except tk.TclError:
        pass
    if style.theme_use() != "clam":
        # The default Windows ttk theme ("vista") renders scrollbars/
        # notebooks/etc. via native OS chrome and ignores custom style
        # colors entirely; "clam" draws its own chrome so the phosphor
        # palette actually applies. Checked per interpreter, so a second
        # window in the same process gets it too.
        style.theme_use("clam")
    style.configure(
        "Phosphor.Vertical.TScrollbar",
        background=theme.panel, troughcolor=theme.bg, bordercolor=theme.bg,
        arrowcolor=theme.dim, darkcolor=theme.panel, lightcolor=theme.panel,
    )
    style.configure("TNotebook", background=theme.bg, bordercolor=theme.dim)
    style.configure(
        "TNotebook.Tab", background=theme.panel, foreground=theme.dim,
        bordercolor=theme.dim, padding=(10, 4),
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", theme.bright)], foreground=[("selected", theme.bg)],
    )
    style.configure(
        "TCombobox", fieldbackground=theme.panel, background=theme.panel,
        foreground=theme.text, bordercolor=theme.dim, arrowcolor=theme.dim,
        lightcolor=theme.panel, darkcolor=theme.panel,
    )
    # configure() alone isn't enough: "clam" ships a built-in state map for
    # TCombobox that swaps in a light system-default fieldbackground for
    # "readonly" (used by every combobox in this app, e.g. KV cache type),
    # which wins over the base color above since ttk resolves state maps
    # before falling back to configure() - confirmed empirically (readonly
    # field rendered light gray while every other input stayed themed dark).
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", theme.panel), ("disabled", theme.panel)],
        foreground=[("readonly", theme.text), ("disabled", theme.dim)],
        background=[("readonly", theme.panel), ("disabled", theme.panel)],
    )
    style.configure(
        "Horizontal.TScale", background=theme.bg, troughcolor=theme.panel,
        bordercolor=theme.dim, lightcolor=theme.dim, darkcolor=theme.dim,
    )
    style.configure(
        "Horizontal.TProgressbar", background=theme.dim, troughcolor=theme.panel,
        bordercolor=theme.bg, lightcolor=theme.dim, darkcolor=theme.dim,
    )
    style.configure(
        "Phosphor.Treeview", background=theme.panel, fieldbackground=theme.panel,
        foreground=theme.text, bordercolor=theme.dim, font=theme_mod.FONT_MONO,
    )
    style.configure(
        "Phosphor.Treeview.Heading", background=theme.panel, foreground=theme.dim,
        bordercolor=theme.dim,
    )
    style.map("Phosphor.Treeview", background=[("selected", theme.bright)], foreground=[("selected", theme.bg)])
    installed = _apply_fx_ttk_chrome(style, theme)
    # The signature is stamped only once the kit's controls are in place (BF-18: a stamp after a failed install
    # left the old or partial ttk look until restart, since every later call returned early). A failed install
    # stamps `signature|kitfail`, which the next call does not match, so the pass runs again - once: a second
    # failure stamps for good, so a permanent failure does not cost a full pass on every make_scrollable.
    if installed or stamped == signature + _TTK_RETRY_SUFFIX:
        stamp = signature
    else:
        stamp = signature + _TTK_RETRY_SUFFIX
    try:
        style.tk.call("set", _TTK_SIGNATURE_VAR, stamp)
    except tk.TclError:
        pass


def _apply_fx_ttk_chrome(style, theme):
    """The flat ttk chrome (FD-8). Always applied: the kill switch turns off effects
    only, not the look (D-31). Each line removes one of clam's hardcoded light-grey
    pieces the base palette above doesn't reach; kit.ttk_controls then adds the
    approved controls (sliders, thin scrollbar, progress bar on unlit). -> True when the kit's
    controls are in place (or there is no kit to import), False when install_ttk raised: the
    caller then leaves the signature unstamped so the next call installs again (BF-18)."""
    # Notebook: clam bevels tabs and the pane with light/dark highlight
    # colours that default to system greys (66 + 76 px of bevel per tab strip).
    style.configure("TNotebook", lightcolor=theme.bg, darkcolor=theme.bg)
    style.configure("TNotebook.Tab", lightcolor=theme.panel, darkcolor=theme.panel)
    style.map(
        "TNotebook.Tab",
        lightcolor=[("selected", theme.bright), ("!selected", theme.panel)],
        darkcolor=[("selected", theme.bright), ("!selected", theme.panel)],
    )
    # Scrollbar: no arrow buttons (trough + full-length thumb), no grip lines,
    # and no light-grey hover/press thumb. Never arrowsize=0 - that collapses
    # the scrollbar's requested width to 1 px.
    style.layout(
        "Phosphor.Vertical.TScrollbar",
        [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})],
    )
    style.configure("Phosphor.Vertical.TScrollbar", gripcount=0)
    style.map(
        "Phosphor.Vertical.TScrollbar",
        background=[("pressed", theme.dim), ("active", theme.dim)],
        lightcolor=[("pressed", theme.dim), ("active", theme.dim)],
        darkcolor=[("pressed", theme.dim), ("active", theme.dim)],
    )
    style.configure("Phosphor.Treeview", lightcolor=theme.panel, darkcolor=theme.panel)
    style.configure(
        "Phosphor.Treeview.Heading", relief="flat", font=theme_mod.FONT_LABEL,
        lightcolor=theme.panel, darkcolor=theme.panel,
    )
    # Unnamed sliders: a disabled thumb would otherwise turn clam's light grey.
    style.map(
        "Horizontal.TScale",
        background=[("disabled", theme.panel)], troughcolor=[("disabled", theme.bg)],
        bordercolor=[("disabled", theme.dim)], lightcolor=[("disabled", theme.dim)],
        darkcolor=[("disabled", theme.dim)],
    )
    style.configure(
        "Horizontal.TProgressbar", background=theme.bright, troughcolor=theme.panel,
        bordercolor=theme.dim, lightcolor=theme.bright, darkcolor=theme.bright,
    )
    # The approved controls live in the static kit. Guarded twice: a missing or broken
    # kit, or a style detail Tk rejects, leaves the look above in place and never
    # stops the window from opening.
    try:
        from ..kit import ttk_controls
    except Exception as exc:  # noqa: BLE001 - optional layer
        _ttk_kit_failed("import", exc)
        return True             # nothing to retry in this process: the signature is stamped as usual
    try:
        ttk_controls.install_ttk(style, theme)
    except Exception as exc:  # noqa: BLE001 - optional layer
        _ttk_kit_failed("install", exc)
        return False            # not stamped: the next apply_ttk_theme runs the install again (BF-18)
    return True


_ttk_kit_failures = set()


def _ttk_kit_failed(stage, exc):
    """Report a kit.ttk_controls problem once per stage on stderr (the app keeps running)."""
    if stage in _ttk_kit_failures:
        return
    _ttk_kit_failures.add(stage)
    try:
        import sys
        sys.stderr.write("[kit] ttk controls %s failed; the window keeps working, but some controls "
                         "may keep the base chrome: %r\n" % (stage, exc))
    except Exception:  # noqa: BLE001
        pass


def make_header(parent, theme, title, compact=False, actions=None):
    """The screen header shared by every screen except Home (B2): the title
    only, drawn as Canvas text by kit.header (display XL, or the compact
    display L "CHAT" form with compact=True), packed full width at the top of
    `parent`. The title sits 16 px into the content column, so the header
    needs no padding of its own. header.actions is the right-hand slot for
    text actions; `actions` (widgets) are packed into it.

    D-12 L-01: the "System: Local AI ChatBot >" crumb is gone; the title is
    kept. D-32: the date/time stamp the right end used to carry is dropped.

    Returns (header, refresh_callable). The callable is kept because the
    screens call it from on_show(); it now has nothing to refresh and does
    nothing. If the kit cannot be imported, a plain Label title stands in, so
    the screen still opens."""
    screen = parent if isinstance(parent, Screen) else None
    try:
        from ..kit import header as kit_header
        maker = kit_header.compact_header if compact else kit_header.full_header
        header = maker(parent, theme, title, actions=actions, screen=screen)
    except Exception as exc:  # noqa: BLE001 - the header must never stop a screen being built
        _kit_header_failed(exc)
        header = tk.Frame(parent, bg=theme.bg)
        tk.Label(
            header, text=str(title).upper(), bg=theme.bg, fg=theme.bright, font=theme_mod.FONT_HEADER,
        ).pack(side="left", padx=16, pady=(12, 8))
        header.actions = tk.Frame(header, bg=theme.bg)
        header.actions.pack(side="right", padx=16)
        title_label = header.winfo_children()[0]
        # B2 fix ADV1-07: the same set_title as the kit header, so Projects still shows the
        # project's name (D-27) when the kit is unavailable
        header.set_title = lambda text, lbl=title_label: lbl.configure(text=str(text or "").upper())
    header.pack(fill="x")

    def refresh_datetime():
        """Kept for the screens' on_show() calls; the stamp is gone (D-32)."""

    return header, refresh_datetime


def _kit_header_failed(exc):
    if "header" in _ttk_kit_failures:
        return
    _ttk_kit_failures.add("header")
    try:
        import sys
        sys.stderr.write("[kit] header failed; a plain title stands in: %r\n" % (exc,))
    except Exception:  # noqa: BLE001
        pass


def make_prompt_slot(parent, theme):
    """An empty, zero-height-until-populated Frame a screen packs once in
    its layout (right where an inline form should appear) and passes to
    show_inline_prompt(). Keeping the slot's position fixed at construction
    time avoids fighting Tk's pack ordering when the form needs to appear
    and disappear later."""
    slot = tk.Frame(parent, bg=theme.bg)
    slot.pack(fill="x")
    return slot


def clear_prompt_slot(slot):
    """Dismisses whatever inline prompt (if any) is currently showing in
    `slot`. Screens that switch between internal modes (e.g. Projects'
    grid/detail) or that can be navigated away from mid-prompt must call
    this from their mode-switch points and on_hide() - otherwise a prompt
    opened in one context (e.g. "New project" naming) keeps showing after
    the user has moved on to something unrelated, since nothing else ever
    destroys it besides its own submit/cancel."""
    for child in slot.winfo_children():
        child.destroy()


def show_inline_prompt(slot, theme, label, on_submit, initial=""):
    """Reveals a one-line Entry form inside `slot` (from make_prompt_slot())
    instead of a native OS dialog - this app's navigation model is zero
    popups, and a modal simpledialog.askstring() for something as small as
    "New project name?" broke that just as much as a Toplevel screen would
    have. Submits on Enter/OK with non-empty text, cancels on Escape/Cancel
    or empty text; either way the form removes itself.

    B3 look (kit): the label in Consolas 9 `value`, the Entry inside a kit
    Well (2 px bright ring while focused), OK as the primary KitButton and
    CANCEL as the secondary one. Enter / Escape on the Entry are bound
    exactly as before (KEY-15). If the kit cannot build the form, today's
    form stands in, so the prompt always opens."""
    clear_prompt_slot(slot)
    try:
        return _kit_inline_prompt(slot, theme, label, on_submit, initial)
    except Exception as exc:  # noqa: BLE001 - the prompt must always open
        _kit_overlay_failed("prompt", exc)
        clear_prompt_slot(slot)
        return _legacy_inline_prompt(slot, theme, label, on_submit, initial)


def _kit_inline_prompt(slot, theme, label, on_submit, initial):
    from ..kit import C9, C11, tokens
    from ..kit.widgets import KitButton, Well

    t = tokens(theme)
    bg = t["bg"]
    frame = tk.Frame(slot, bg=bg)
    try:
        frame.pack(fill="x", pady=(0, 10))
        tk.Label(frame, text=label, bg=bg, fg=t["value"], font=C9, padx=0, pady=0).pack(side="left", padx=(0, 8))
        # held only by submit / cancel (kept alive by their Tcl commands), never by a widget: a widget
        # attribute would tie it to the kit buttons' reference cycles (Variable.__del__ at GC time)
        var = tk.StringVar(master=frame, value=initial)
        well = Well(frame, theme, ground=bg)
        well.pack(side="left", fill="x", expand=True, padx=(0, 8))
        entry = tk.Entry(
            well.body, textvariable=var, bg=t["panel"], fg=t["bright"], insertbackground=t["bright"],
            selectbackground=t["bright"], selectforeground=t["bg"], relief="flat", bd=0, highlightthickness=0,
            font=C11,
        )
        entry.pack(fill="x", expand=True, padx=6, ipady=3)
        well.attach(entry)

        def submit(_event=None):
            value = var.get().strip()
            frame.destroy()
            if value:
                on_submit(value)

        def cancel(_event=None):
            frame.destroy()
            return "break"      # BF-20: Esc closes the prompt only - the next Esc goes back (the user's yes, 2026-10-12)

        KitButton(frame, theme, text="OK", kind="primary", upper=True, ground=bg,
                  command=submit).pack(side="left", padx=(0, 6))
        KitButton(frame, theme, text="Cancel", kind="secondary", upper=True, ground=bg,
                  command=cancel).pack(side="left")
        entry.bind("<Return>", submit)
        entry.bind("<Escape>", cancel)
        entry.focus_set()
    except Exception:
        try:
            frame.destroy()
        except tk.TclError:
            pass
        raise
    return frame


def _legacy_inline_prompt(slot, theme, label, on_submit, initial):
    """Today's (pre-B3) form: the fallback when the kit cannot build the new one."""
    frame = tk.Frame(slot, bg=theme.panel, highlightthickness=1, highlightbackground=theme.bright)
    frame.pack(fill="x", pady=(0, 10))
    tk.Label(
        frame, text=label, bg=theme.panel, fg=theme.dim, font=theme_mod.FONT_CAPTION,
    ).pack(side="left", padx=(10, 6), pady=8)
    var = tk.StringVar(value=initial)
    entry = tk.Entry(
        frame, textvariable=var, bg=theme.bg, fg=theme.text, insertbackground=theme.bright,
        relief="flat", font=theme_mod.FONT_BODY, highlightthickness=1,
        highlightbackground=theme.dim, highlightcolor=theme.bright,
    )
    entry.pack(side="left", fill="x", expand=True, padx=(0, 6), ipady=4)

    def submit(_event=None):
        value = var.get().strip()
        frame.destroy()
        if value:
            on_submit(value)

    def cancel(_event=None):
        frame.destroy()
        return "break"          # BF-20, as above

    ok_btn = tk.Label(
        frame, text="OK", bg=theme.bright, fg=theme.bg, font=theme_mod.FONT_CAPTION,
        padx=10, pady=4, cursor="hand2",
    )
    ok_btn.pack(side="left", padx=(0, 4), pady=6)
    ok_btn.bind("<Button-1>", submit)
    cancel_btn = tk.Label(
        frame, text="Cancel", bg=theme.panel, fg=theme.dim, font=theme_mod.FONT_CAPTION,
        padx=10, pady=4, cursor="hand2", highlightthickness=1, highlightbackground=theme.dim,
    )
    cancel_btn.pack(side="left", padx=(0, 10), pady=6)
    cancel_btn.bind("<Button-1>", cancel)
    entry.bind("<Return>", submit)
    entry.bind("<Escape>", cancel)
    entry.focus_set()
    return frame


def _kit_overlay_failed(stage, exc):
    """Report a kit notice / confirm / prompt failure once per stage on stderr (today's
    widget stands in, so the app keeps working)."""
    key = "overlay-" + stage
    if key in _ttk_kit_failures:
        return
    _ttk_kit_failures.add(key)
    try:
        import sys
        sys.stderr.write("[kit] %s failed; the previous look stands in: %r\n" % (stage, exc))
    except Exception:  # noqa: BLE001
        pass


def _alive(widget):
    try:
        return bool(widget.winfo_exists())
    except Exception:  # noqa: BLE001
        return False


# ---- notices (B3: D-26 / D-28, X-13)

NOTICE_DEFAULT_MS = 4500
NOTICE_MIN_MS = 1000     # the shortest notice: a shorter one is gone before it can be read (BF-21)
NOTICE_MAX_MS = 2 ** 31 - 1
NOTICE_DISMISS_SLOT = "notice.dismiss"


def _notice_duration(duration_ms):
    """duration_ms as a usable timer length. None, a non-number, 0 or a negative value give the
    default 4500 ms: a notice that vanished at once would be a lost message (D-27). A positive value
    is at least NOTICE_MIN_MS (1000 ms: 2.9 became a 2 ms notice and 1 stayed a 1 ms one, both gone
    unseen, B3 ADV2 Q7 / BF-21) and at most Tcl's largest timer. A bool is not a duration (int(True)
    == 1 would be a 1 ms notice, B3 ADV1-06): it gives the default too."""
    if isinstance(duration_ms, bool):
        return NOTICE_DEFAULT_MS
    try:
        ms = int(duration_ms)
    except (TypeError, ValueError, OverflowError):
        return NOTICE_DEFAULT_MS
    if ms <= 0:
        return NOTICE_DEFAULT_MS
    return min(max(ms, NOTICE_MIN_MS), NOTICE_MAX_MS)


def show_toast(anchor, theme, message, kind="error", duration_ms=4500, *, sound=None):
    """Non-blocking inline notification floated over `anchor` via .place()
    instead of a native messagebox popup - this app's navigation model is
    zero popups, and an OS dialog for "could not delete: ..." broke that
    exactly like a Toplevel screen would have. `anchor` just needs to be a
    widget whose top-level window this floats over; .place() doesn't
    reserve layout space, so this works from any screen with no per-screen
    wiring. Auto-dismisses after duration_ms or on its [ x ].
    kind: "error" / "warning" / "success" / anything else (a note). sound:
    optional override of the sound cue the fx layer would pick for this
    toast (e.g. "silent"); ignored when fx is off.

    B3 (D-26 / D-28): the ASCII notice box (kit.notice.build_notice, the
    lab's asciiBox) at the bottom centre of the content column, its bottom
    edge 16 px above the legend row (MainWindow.fx_bottom_reserve). Tag and
    ink by kind: ERROR danger, WARNING warn_eff, OK ok, NOTE bright. The
    message is wrapped at 72 columns (fewer when the column is narrower) and
    never truncated or re-cased. Up to three notices stack, newest at the
    bottom; a fourth evicts the one with the lowest (severity, remaining
    time) - kit.notice.NoticeStack - so a long error outlives a burst. The
    same rule evicts while the stack is taller than the content column
    above the legend row (never the newest; a lone notice taller than the
    column is kept and clipped at the top - accepted, B3 FIX1 ADV1-05).
    When the content column is resized, the shown notices are re-wrapped
    at its new width and re-fitted the same way (B3 FIX2 ADV2-04).
    Auto-dismiss: fx.clock.later(frame, ...) while fx is live, else
    frame.after(...) - the one documented after() outside fx/clock.py - kept
    by id and cancelled through `frame`, the widget that owns it, whenever
    the notice closes early ([ x ], eviction, destroy). duration_ms that is
    None, 0 or negative means the default 4500 ms; a positive one is at least
    NOTICE_MIN_MS (1000 ms).
    Returns the notice frame (None only when `anchor` is already destroyed:
    there is no window to show it on). Today's toast body stands in on any
    exception from the kit path. Emits toast.shown exactly once."""
    ms = _notice_duration(duration_ms)
    try:
        frame = _kit_notice(anchor, theme, message, kind, ms)
    except Exception as exc:  # noqa: BLE001 - a notice must never be lost to a look problem
        if not _alive(anchor):
            return None
        _kit_overlay_failed("notice", exc)
        frame = _legacy_toast(anchor, theme, message, kind, ms)
    fx_mod.get(anchor).emit(frame, "toast.shown", kind=kind, text=message, sound=sound)
    return frame


def _notice_host(root):
    host = getattr(root, "content", None)
    return host if isinstance(host, tk.Misc) and _alive(host) else root


def _notice_reserve(root):
    reserve = getattr(root, "fx_bottom_reserve", 0)
    return reserve if isinstance(reserve, int) and not isinstance(reserve, bool) and reserve > 0 else 0


def _notice_cols(host, font, max_cols):
    """Wrap width in columns: 72, or fewer when the content column is too narrow for a 72-column
    box (wrapping more is never truncating)."""
    try:
        w = int(host.winfo_width())
        if w <= 1:
            return max_cols
        cw = max(1, int(host.tk.call("font", "measure", font, "0")))
        return max(24, min(max_cols, (w - 32) // cw - 4))
    except Exception:  # noqa: BLE001
        return max_cols


def _notice_room(host, reserve, bottom_gap):
    """Height in px the notice stack may use: the content column above the legend row and the gap.
    None while the column has no size yet (no fit is attempted then)."""
    try:
        h = int(host.winfo_height())
    except (tk.TclError, ValueError):
        return None
    if h <= 1:
        return None
    return max(0, h - reserve - bottom_gap)


def _layout_notices(stack, host, reserve, bottom_gap):
    base = reserve + bottom_gap
    for frame, offset in stack.offsets():
        try:
            frame.place(in_=host, relx=0.5, rely=1.0, anchor="s", y=-(base + offset))
        except tk.TclError:
            pass  # the window is being torn down


_NOTICE_WATCH_ATTR = "_kit_notice_watch"


def _refit_notices(root, host):
    """The content column changed size: re-wrap every shown notice at the column's width (_notice_cols),
    evict to fit its height (the show-time rule, never the newest) and lay the stack out again. Without
    it a box kept the wrap it was shown with and ran off a narrower window (B3 ADV2-04)."""
    from ..kit import notice as kit_notice

    stack = kit_notice.stack_for(root)
    if not len(stack):
        return
    cols = _notice_cols(host, kit_notice.NOTICE_FONT, kit_notice.MAX_COLS)
    for frame in stack.frames:
        if _alive(frame) and kit_notice.rewrap_notice(frame, cols):
            stack.set_height(frame, frame.notice_height)
    reserve = _notice_reserve(root)
    for evicted in stack.fit(_notice_room(host, reserve, kit_notice.BOTTOM_GAP)):
        try:
            evicted.close()     # cancels its timer through its own frame
        except Exception:  # noqa: BLE001
            pass
    _layout_notices(stack, host, reserve, kit_notice.BOTTOM_GAP)


def _watch_notice_host(root, host):
    """Once per content column: a <Configure> binding (add='+', registered once, so no Tcl command per
    notice or per resize) that re-fits the shown notices when the column is resized (_refit_notices)."""
    if getattr(host, _NOTICE_WATCH_ATTR, False):
        return

    def on_configure(event):
        if str(event.widget) != str(host):      # host may be the root, whose tag every child carries
            return
        try:
            _refit_notices(root, host)
        except Exception as exc:  # noqa: BLE001 - a resize must never raise out of a notice
            _kit_overlay_failed("notice-refit", exc)

    host.bind("<Configure>", on_configure, add="+")
    setattr(host, _NOTICE_WATCH_ATTR, True)


def _kit_notice(anchor, theme, message, kind, ms):
    from ..kit import notice as kit_notice

    root = anchor.winfo_toplevel()
    host = _notice_host(root)
    reserve = _notice_reserve(root)
    cols = _notice_cols(host, kit_notice.NOTICE_FONT, kit_notice.MAX_COLS)
    frame = kit_notice.build_notice(root, theme, message, kind, max_cols=cols)
    stack = kit_notice.stack_for(root)
    state = {"closed": False, "after": None, "clock": None}

    def relayout():
        _layout_notices(stack, host, reserve, kit_notice.BOTTOM_GAP)

    def stop_timer():
        after_id, state["after"] = state["after"], None
        if after_id is not None:
            try:
                frame.after_cancel(after_id)  # through the OWNING widget (cancel_pending_after_callbacks)
            except (tk.TclError, ValueError):
                pass
        clock, state["clock"] = state["clock"], None
        if clock is not None:
            try:
                clock.cancel(frame, NOTICE_DISMISS_SLOT)
            except Exception:  # noqa: BLE001
                pass

    def forget():
        if state["closed"]:
            return False
        state["closed"] = True
        stop_timer()
        stack.remove(frame)
        return True

    def close(_event=None):
        if not forget():
            return
        try:
            frame.destroy()
        except tk.TclError:
            pass
        relayout()

    def on_destroy(event):
        # destroyed from outside (window teardown, an overlay sweep): drop the timer and the slot
        if str(event.widget) != str(frame):
            return
        if forget():
            relayout()

    try:
        frame.notice_close.bind("<Button-1>", close)
        frame.bind("<Destroy>", on_destroy, add="+")
        frame.notice_dismiss = close
        _watch_notice_host(root, host)      # a later resize re-wraps and re-fits the stack (ADV2-04)
        evicted_all = stack.add(frame, kind, ms, frame.notice_height, close)
        # B3 ADV1-05: a stack taller than the room above the legend row would push the oldest box off the
        # top of the window; evict (lowest severity / remaining first, never the newest) until it fits
        evicted_all += stack.fit(_notice_room(host, reserve, kit_notice.BOTTOM_GAP))
        for evicted in evicted_all:
            try:
                evicted.close()
            except Exception:  # noqa: BLE001
                pass
        fx = fx_mod.get(anchor)
        clock = getattr(fx, "clock", None) if getattr(fx, "enabled", False) else None
        entry = None
        if clock is not None:
            try:
                entry = clock.later(frame, NOTICE_DISMISS_SLOT, ms, close)
            except Exception:  # noqa: BLE001
                entry = None
        if entry is not None:
            state["clock"] = clock
        else:
            # kill switch (or no live clock): the one documented after() outside fx/clock.py,
            # owned by `frame` and cancelled through it in stop_timer()
            state["after"] = frame.after(ms, close)
        relayout()
        frame.lift()
    except Exception:
        forget()
        try:
            frame.destroy()
        except tk.TclError:
            pass
        relayout()
        raise
    return frame


def _legacy_toast(anchor, theme, message, kind, ms):
    """Today's (pre-B3) toast body: the fallback when the kit path raises."""
    root = anchor.winfo_toplevel()
    color = {"error": theme.danger, "success": theme.bright, "warning": theme.warn}.get(kind, theme.bright)
    frame = tk.Frame(root, bg=theme.panel, highlightthickness=2, highlightbackground=color)
    tk.Label(
        frame, text=message, bg=theme.panel, fg=color, font=theme_mod.FONT_CAPTION,
        wraplength=420, justify="left", padx=14, pady=10,
    ).pack(side="left")
    close = tk.Label(
        frame, text="×", bg=theme.panel, fg=theme.dim, font=theme_mod.FONT_CAPTION, cursor="hand2", padx=10,
    )
    close.pack(side="left")
    # B2 fix ADV1-10: clear of the shell's legend row (MainWindow.fx_bottom_reserve), never on it
    reserve = _notice_reserve(root)
    frame.place(relx=0.5, rely=1.0, anchor="s", y=-(20 + reserve))
    auto_dismiss = []

    def dismiss(event=None):
        if event is not None and auto_dismiss:
            # Closed early via the x: cancel the pending auto-dismiss through
            # `frame`, the widget that owns it, or it later fires into the
            # destroyed frame as an "invalid command name ...dismiss" error.
            try:
                frame.after_cancel(auto_dismiss[0])
            except tk.TclError:
                pass
        try:
            frame.destroy()
        except tk.TclError:
            pass

    close.bind("<Button-1>", dismiss)
    auto_dismiss.append(frame.after(ms, dismiss))
    return frame


# ---- confirm (B3: the approved card, OI-2 keys)

CONFIRM_CARD_W = 476          # outer width, border included (approved r2 confirm)
CONFIRM_BAR_H = 22            # the inverse title bar, flush inside the border
CONFIRM_PAD = 28              # (476 - 420) / 2: message, buttons and legend share this margin
CONFIRM_WRAP = 420
CONFIRM_LAMP_W = 24           # the lamp image (14 + 8 px gap) plus the 1 px focus ring each side
CONFIRM_LEGEND = (("TAB", "select"), ("ENTER", "activate"), ("ESC", "cancel"))
_OPEN_CONFIRM_ATTR = "_kit_open_confirm"
CONFIRM_MODAL_TAG = "KitConfirmModal"   # bindtag whose <Key> is `break`: no global key acts under a card
_KEY_SINK_ATTR = "_kit_key_sink"        # the held-key sink still waiting for its release (one per window)
_KEY_SINK_WATCH_ATTR = "_kit_key_sink_watch"
_MODIFIER_KEYSYMS = frozenset((
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Meta_L", "Meta_R",
    "Super_L", "Super_R", "Win_L", "Win_R", "Caps_Lock", "Num_Lock", "Scroll_Lock",
))
# The keys that decide a card. Return and KP_Enter are one Windows virtual key (VK_RETURN), so they are held and
# released together (B3 ADV2-02).
_CARD_KEY_GROUPS = (("Return", "KP_Enter"), ("space",), ("Escape",))
_CARD_KEY_VK = {"Return": 0x0D, "space": 0x20, "Escape": 0x1B}
_GET_KEY_STATE = []      # filled once: [user32.GetKeyState] on Windows, [None] elsewhere


def _key_group(keysym):
    for group in _CARD_KEY_GROUPS:
        if keysym in group:
            return group
    return (keysym,)


def _card_keys_down():
    """The card keys (Return / KP_Enter, space, Escape) that are DOWN for the input being handled now, as
    keysyms. Windows GetKeyState: the calling thread's key state as of the input message it is processing,
    so inside the press (or the Button's 100 ms Space invoke) that opens a card, a key still held reads
    down and a key whose release was already handled reads up. Called on the Tk thread only. An empty set
    off Windows or on any failure: the card then takes every key, as before (B3 ADV2-02)."""
    if not _GET_KEY_STATE:
        try:
            import ctypes
            _GET_KEY_STATE.append(ctypes.windll.user32.GetKeyState)
        except Exception:  # noqa: BLE001 - not Windows
            _GET_KEY_STATE.append(None)
    get_key_state = _GET_KEY_STATE[0]
    down = set()
    if get_key_state is None:
        return down
    for group in _CARD_KEY_GROUPS:
        try:
            if int(get_key_state(_CARD_KEY_VK[group[0]])) & 0x8000:     # high bit of the SHORT: down
                down.update(group)
        except Exception:  # noqa: BLE001
            pass
    return down


_GET_ASYNC_KEY_STATE = []    # filled once: [user32.GetAsyncKeyState] on Windows, [None] elsewhere
_ROOT_ONLY_TAG = "KitRootOnly"   # a bindtag only the toplevel carries: its <Activate> handler runs once per activation


def _card_keys_physically_down():
    """The card keys that are physically down NOW, as keysyms - GetAsyncKeyState, the real-time state, where
    _card_keys_down reads the thread's message-time state. For the window coming back to the front: a key held
    when a card opened and released while another window was active sent its release there, so the card went
    on ignoring that key (BF-19). None off Windows or on any failure: nothing is pruned then."""
    if not _GET_ASYNC_KEY_STATE:
        try:
            import ctypes
            fn = ctypes.windll.user32.GetAsyncKeyState
            fn.argtypes = [ctypes.c_int]
            fn.restype = ctypes.c_short
            _GET_ASYNC_KEY_STATE.append(fn)
        except Exception:  # noqa: BLE001 - not Windows
            _GET_ASYNC_KEY_STATE.append(None)
    fn = _GET_ASYNC_KEY_STATE[0]
    if fn is None:
        return None
    down = set()
    try:
        for group in _CARD_KEY_GROUPS:
            if int(fn(_CARD_KEY_VK[group[0]])) & 0x8000:
                down.update(group)
    except Exception:  # noqa: BLE001
        return None
    return down


def _prune_released_keys(root):
    """The window is the active one again (<Activate>): a card key that is no longer physically down is taken
    off the open card's held keys and off the pending sink's (its release went to another window, BF-19), and
    a sink whose own key is up is ended. Nothing happens when the key state cannot be read."""
    down = _card_keys_physically_down()
    if down is None:
        return
    card = getattr(root, _OPEN_CONFIRM_ATTR, None)
    held = getattr(card, "_kit_held", None) if card is not None else None
    if isinstance(held, set):
        held.intersection_update(down)
    entry = _active_key_sink(root)
    if entry is not None:
        if isinstance(entry.get("held"), set):
            entry["held"].intersection_update(down)
        if not set(_key_group(entry["keysym"])) & down:
            entry["finish"](True)


def _note_key_release(root, keysym):
    """A key went up somewhere in the window: end the held-key sink waiting for it, and let the open card
    take that key again (its press was being ignored as the auto-repeat of a key held when the card
    opened, or held through a chained card's hand-over: ADV2-02 / ADV2-03)."""
    entry = _active_key_sink(root)
    if entry is not None and keysym == entry["keysym"]:
        entry["finish"](True)
    card = getattr(root, _OPEN_CONFIRM_ATTR, None)
    held = getattr(card, "_kit_held", None) if card is not None else None
    if isinstance(held, set):
        held.difference_update(_key_group(keysym))


def _focus_of(root):
    try:
        return root.focus_get()
    except (KeyError, tk.TclError):
        return None


def _restore_focus(widget):
    if widget is not None and _alive(widget):
        try:
            widget.focus_set()
        except tk.TclError:
            pass


def _make_key_sink(root):
    """The 1 px focus sink of _swallow_key_repeat, placed just off the window. Made when the card
    OPENS, so it is mapped by the time a key closes the card: Tk defers a focus change to an
    unmapped window until the next idle pass, and a key release already queued then went to the
    toplevel instead, leaving the focus stranded on the sink (B3 ADV1-01)."""
    sink = tk.Frame(root, width=1, height=1, bd=0, highlightthickness=0, takefocus=0)
    sink.place(x=-4, y=-4, width=1, height=1)
    return sink


def _active_key_sink(root):
    """The sink still waiting for its key's release, or None."""
    entry = getattr(root, _KEY_SINK_ATTR, None)
    if isinstance(entry, dict) and not entry.get("done") and _alive(entry.get("sink")):
        return entry
    return None


def _watch_key_releases(root):
    """Once per window: a <KeyRelease> watcher on the toplevel's own bindtag ("."), which every
    widget of the window carries. It ends the active sink when its key goes up wherever the release
    lands - the backstop for a release that reaches the toplevel before the sink has the focus
    (B3 ADV1-01) - and clears that key from the open card's held keys (_note_key_release). Registered once
    and never removed, so no Tcl command is created per key."""
    if getattr(root, _KEY_SINK_WATCH_ATTR, False):
        return

    def on_release(event):
        _note_key_release(root, event.keysym)

    def on_activate(event):
        _prune_released_keys(event.widget)

    root.bind("<KeyRelease>", on_release, add="+")
    # <Activate> is sent to every window of the toplevel; a bindtag only the toplevel carries runs the pruning
    # once per activation (and serves every toplevel of the interpreter: the handler reads the event's window)
    try:
        tags = tuple(root.bindtags())
        if _ROOT_ONLY_TAG not in tags:
            root.bindtags((tags[0], _ROOT_ONLY_TAG) + tags[1:])
        root.bind_class(_ROOT_ONLY_TAG, "<Activate>", on_activate)
    except tk.TclError:
        pass
    setattr(root, _KEY_SINK_WATCH_ATTR, True)


def _swallow_key_repeat(root, keysym, prev_focus, sink=None):
    """After a key closed a confirm, hold the focus on a 1 px sink until that key is released:
    its auto-repeat (Esc or Enter held down) then never reaches the widget underneath (a second
    Esc would navigate back, a held Enter would send the composer's text). Ends on:
    - that key's release, on the sink or anywhere in the window (_watch_key_releases);
    - any other key press (the focus goes back at once);
    - anything else taking the focus.
    When it ends while the focus is still on the sink (or on nothing / the bare toplevel), the
    focus goes to entry["restore_to"] - set by a confirm opened while the key is still down
    (chained confirms, ADV1-03) - else to prev_focus. `sink` is the premapped sink the card made
    when it opened (_make_key_sink); a new one is made only when it is gone."""
    old = _active_key_sink(root)
    if old is not None:
        old["finish"](False)
    if sink is None or not _alive(sink):
        sink = _make_key_sink(root)
    entry = {"sink": sink, "keysym": keysym, "prev_focus": prev_focus, "restore_to": None, "held": None,
             "done": False}

    def target():
        for w in (entry["restore_to"], prev_focus):
            if w is not None and _alive(w):
                return w
        return None

    def finish(restore):
        if entry["done"]:
            return
        entry["done"] = True
        if getattr(root, _KEY_SINK_ATTR, None) is entry:
            setattr(root, _KEY_SINK_ATTR, None)
        cur = _focus_of(root)
        if cur is None:     # the window is not the active one: what it will focus when it is
            try:
                cur = root.focus_lastfor()
            except (KeyError, tk.TclError):
                cur = None
        stranded = cur is None or cur is sink or cur is root
        try:
            sink.destroy()
        except tk.TclError:
            pass
        if restore and stranded:
            _restore_focus(target())

    def other_key(event):
        if event.keysym == keysym or event.keysym in _MODIFIER_KEYSYMS:
            return "break"
        # another key ends the hold at once (the focus goes back). While a chained card waits for the
        # release, that key is kept from the global shortcuts: the card is modal (ADV1-02)
        waiting = entry["restore_to"] is not None and _alive(entry["restore_to"])
        if waiting and isinstance(entry["held"], set):
            # the original key is still down: the chained card ignores its auto-repeat until its release
            # (ADV2-03: else a held Enter's repeats activated the card's CANCEL right after the hand-over)
            entry["held"].update(_key_group(keysym))
        finish(True)
        return "break" if waiting else None

    def released(_e):
        finish(True)
        _note_key_release(root, keysym)     # this binding breaks before the "." watcher sees the release
        return "break"

    entry["finish"] = finish
    try:
        sink.bind("<KeyPress-%s>" % keysym, lambda _e: "break")
        sink.bind("<KeyRelease-%s>" % keysym, released)
        sink.bind("<KeyPress>", other_key)
        sink.bind("<FocusOut>", lambda _e: finish(True))
        setattr(root, _KEY_SINK_ATTR, entry)
        _watch_key_releases(root)
        sink.focus_set()
    except tk.TclError:
        finish(False)
        _restore_focus(prev_focus)
    return sink


def _apply_modal_tag(root, top):
    """Put CONFIRM_MODAL_TAG on `top` and every widget in it, after the widget and class tags and
    before the toplevel and `all` tags. Its <Key> binding is a bare Tcl `break`, so while the focus
    is in the card no bind_all shortcut ('/', a screen's digits, Alt+1..4, Ctrl+N, Esc) acts on the
    screen under the backdrop or pulls the focus out of the card (B3 ADV1-02). The card's own
    widget bindings and the class bindings (Space on a lamp or a button) still run first."""
    try:
        if not str(root.tk.call("bind", CONFIRM_MODAL_TAG, "<Key>")).strip():
            root.tk.call("bind", CONFIRM_MODAL_TAG, "<Key>", "break")
    except tk.TclError:
        pass
    stack = [top]
    while stack:
        w = stack.pop()
        try:
            stack.extend(w.winfo_children())
            tags = list(w.bindtags())
            if CONFIRM_MODAL_TAG in tags:
                continue
            top_tag = str(w.winfo_toplevel())
            if top_tag in tags[1:]:
                i = tags.index(top_tag, 1)
            elif "all" in tags:
                i = tags.index("all")
            else:
                i = len(tags)
            tags.insert(i, CONFIRM_MODAL_TAG)
            w.bindtags(tuple(tags))
        except tk.TclError:
            pass


def show_inline_confirm(
    anchor, theme, message, on_confirm, confirm_label="Confirm", danger=False, checkboxes=None, *, title=None,
):
    """Full-window overlay (covers every other element, card centered on the
    app window) replacing messagebox.askyesno() for the same zero-popups
    reason as show_toast() - but unlike show_toast(), a destructive
    confirmation needs to be impossible to miss or click around, so this
    covers the whole window rather than floating in a corner. tkraise()
    explicitly forces it above all sibling content instead of relying on
    widget-creation-order stacking. Clicking the dimmed backdrop (outside
    the card) cancels, same as clicking Cancel.

    checkboxes, if given, is a list of (key, label, default_checked) tuples
    rendered between the message and the button row. When present,
    on_confirm is called with a single {key: bool} dict of the checked
    states instead of no arguments - existing zero-arg callers are
    unaffected since they simply don't pass checkboxes.

    B3 look (approved r2 confirm): a 476 px card, 1 px bright (plain) or
    2 px danger (danger) border, an inverse 22 px title bar flush inside it
    (title=, default confirm_label.upper()), the message verbatim in
    Consolas 11 wrapped at 420 px, the checkboxes as kit lamps (still
    tk.Checkbutton), [act] [CANCEL] right-aligned and the in-card legend
    TAB select / ENTER activate / ESC cancel. Plain: the act is the primary
    and has the focus. Danger: the act is danger_outline and CANCEL is the
    primary with the focus, so Enter never does the destructive act by
    default. The filled primary follows the Tab selection.
    Keys (OI-2): Tab / Shift+Tab move the selection inside the card, Enter
    activates the selected button (or toggles a selected lamp; Space also
    toggles it), Esc cancels. Each returns 'break', so the global Esc does
    not also navigate back, and a held Esc / Enter does not repeat into the
    window underneath. The card is modal for keys: its widgets carry
    CONFIRM_MODAL_TAG, so no bind_all shortcut ('/', digits, Alt+1..4,
    Ctrl+N) acts under it. A confirm opened from another's on_confirm while
    the closing key is still held takes the focus on that key's release and
    inherits the first card's saved focus. The card is mapped before it takes the focus, so a
    key typed ahead while the app was busy reaches the card, never the widget under it. A card
    key (Enter, Space, Esc) that is still held as the card opens - the press that opened it -
    is ignored by the card until its release, so its auto-repeat is never taken as a decision.
    Opening a second confirm while one is open cancels
    the first. Today's card stands in if the kit cannot build the new one.
    Returns the backdrop; emits confirm.shown once."""
    root = anchor.winfo_toplevel()
    try:
        backdrop, card = _kit_confirm(root, theme, message, on_confirm, confirm_label, danger, checkboxes, title)
    except Exception as exc:  # noqa: BLE001 - the confirm must always open
        _kit_overlay_failed("confirm", exc)
        backdrop, card = _legacy_confirm(root, theme, message, on_confirm, confirm_label, danger, checkboxes)
    fx_mod.get(anchor).emit(card, "confirm.shown", danger=danger, label=confirm_label)
    return backdrop


def _kit_confirm(root, theme, message, on_confirm, confirm_label, danger, checkboxes, title):
    from ..kit import C9B, C11, px, tokens
    from ..kit.legend import LegendLine
    from ..kit.widgets import KitButton, lamp_check

    t = tokens(theme)
    bg = t["bg"]
    danger = bool(danger)
    accent = t["danger"] if danger else t["bright"]
    bw = 2 if danger else 1
    # a key that closed a card is still down (a confirm opened from another's on_confirm, ADV1-03):
    # this card inherits the focus that card had saved, and takes the focus only on that key's release
    pending = _active_key_sink(root)
    prev_focus = pending["prev_focus"] if pending is not None else _focus_of(root)
    # a card key still DOWN as the card opens (the press that opened it, held): its auto-repeat is not a
    # decision, so the card ignores that key until its release (ADV2-02; _note_key_release clears it)
    held = _card_keys_down()
    old = getattr(root, _OPEN_CONFIRM_ATTR, None)
    if old is not None and _alive(old):
        prev_focus = getattr(old, "_kit_prev_focus", prev_focus)
        cancel_old = getattr(old, "_kit_cancel", None)
        if callable(cancel_old):
            cancel_old()
        else:
            old.destroy()

    sink = _make_key_sink(root)      # premapped now, so a closing key's focus move is immediate (ADV1-01)
    backdrop = tk.Frame(root, bg=t["panel"])
    backdrop._kit_prev_focus = prev_focus
    backdrop._kit_held = held
    state = {"done": False, "sink_given": False}
    checkbox_vars = {}

    def drop_sink():
        if not state["sink_given"]:
            state["sink_given"] = True
            try:
                sink.destroy()
            except tk.TclError:
                pass

    def finish(restore=True, keysym=None):
        if state["done"]:
            return False
        state["done"] = True
        if getattr(root, _OPEN_CONFIRM_ATTR, None) is backdrop:
            setattr(root, _OPEN_CONFIRM_ATTR, None)
        if keysym:
            # the sink takes the focus before the card goes, so the focus never falls to the toplevel
            state["sink_given"] = True
            _swallow_key_repeat(root, keysym, prev_focus, sink)
        try:
            backdrop.destroy()
        except tk.TclError:
            pass
        # the card's BooleanVars and the backdrop <-> finish cycle are cut here, so the Variables die on this
        # thread at once instead of as cyclic garbage the gc could finalise on a worker thread (the F-1 freeze
        # shape, BF-01-FOLLOWUP; do_confirm copied the values out before calling finish)
        checkbox_vars.clear()
        backdrop._kit_cancel = None
        if not keysym:
            drop_sink()
            if restore:
                _restore_focus(prev_focus)
        return True

    def do_confirm(keysym=None):
        values = {key: var.get() for key, var in checkbox_vars.items()} if checkboxes else None
        if not finish(keysym=keysym):
            return
        if checkboxes:
            on_confirm(values)
        else:
            on_confirm()

    def do_cancel(keysym=None):
        finish(keysym=keysym)

    try:
        backdrop._kit_cancel = lambda: finish(restore=False)
        backdrop.place(x=0, y=0, relwidth=1, relheight=1)
        backdrop.tkraise()
        backdrop.bind("<Button-1>", lambda _e: do_cancel())
        # destroyed from outside (window teardown): the premapped sink goes with it
        backdrop.bind("<Destroy>", lambda e: drop_sink() if str(e.widget) == str(backdrop) else None, add="+")

        card = tk.Frame(backdrop, bg=bg, bd=0, highlightthickness=bw, highlightbackground=accent,
                        highlightcolor=accent, takefocus=0)
        card.place(relx=0.5, rely=0.5, anchor="center", width=px(root, CONFIRM_CARD_W))
        # Swallow clicks on the card itself so they don't fall through to the
        # backdrop's click-outside-to-cancel binding.
        card.bind("<Button-1>", lambda _e: "break")

        bar = tk.Frame(card, bg=accent, height=px(root, CONFIRM_BAR_H), bd=0, highlightthickness=0)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        bar_text = str(confirm_label).upper() if title is None else str(title).upper()
        tk.Label(bar, text=bar_text, bg=accent, fg=bg, font=C9B, padx=0, pady=0, bd=0).pack(
            side="left", padx=px(root, 10))

        side = max(0, px(root, CONFIRM_PAD) - bw)
        tk.Label(
            card, text=message, bg=bg, fg=t["bright"], font=C11, wraplength=px(root, CONFIRM_WRAP),
            justify="left", anchor="w", padx=0, pady=0, bd=0,
        ).pack(fill="x", padx=side, pady=(px(root, 18), 0))

        lamps = []
        if checkboxes:
            check_frame = tk.Frame(card, bg=bg)
            check_frame.pack(fill="x", padx=side, pady=(px(root, 14), 0))
            for key, label, default_checked in checkboxes:
                var = tk.BooleanVar(master=root, value=default_checked)
                checkbox_vars[key] = var
                # wrapped, never cut (D-27): the text column is the message width minus the lamp image
                lamp = lamp_check(check_frame, theme, text=label, variable=var, ground=bg, justify="left",
                                  wraplength=max(40, px(root, CONFIRM_WRAP - CONFIRM_LAMP_W)))
                lamp.pack(fill="x", anchor="w")
                lamps.append(lamp)

        row = tk.Frame(card, bg=bg)
        row.pack(fill="x", padx=side, pady=(px(root, 20), 0))
        cancel_btn = KitButton(row, theme, text="Cancel", kind="primary" if danger else "secondary", upper=True,
                               ground=bg, command=lambda: do_cancel())
        cancel_btn.pack(side="right")
        act_kind = "danger_outline" if danger else "primary"
        act_btn = KitButton(row, theme, text=confirm_label, kind=act_kind, upper=True, ground=bg,
                            command=lambda: do_confirm())
        act_btn.pack(side="right", padx=(0, px(root, 10)))

        legend = LegendLine(card, theme, items=CONFIRM_LEGEND, ground=bg, x0=0, right_pad=0)
        legend.pack(fill="x", padx=side, pady=(px(root, 10), px(root, 10)))

        def select(widget):
            """The filled primary follows the Enter target (approved tabbed frame)."""
            if widget is act_btn:
                kinds = (act_kind, "secondary")
            else:
                kinds = ("danger_outline" if danger else "secondary", "primary")
            for btn, kind in zip((act_btn, cancel_btn), kinds):
                if btn.cget("kind") != kind:
                    btn.configure(kind=kind)

        act_btn.bind("<FocusIn>", lambda _e: select(act_btn), add="+")
        cancel_btn.bind("<FocusIn>", lambda _e: select(cancel_btn), add="+")

        order = lamps + [act_btn, cancel_btn]

        def move(widget, step):
            i = order.index(widget) if widget in order else 0
            order[(i + step) % len(order)].focus_set()
            return "break"

        def activate(widget, keysym):
            if keysym in held:
                return "break"           # the auto-repeat of a key held since before the card took it
            if widget in lamps:
                widget.invoke()          # Enter on a lamp toggles it, like Space
            elif widget is act_btn:
                do_confirm(keysym)
            else:
                do_cancel(keysym)
            return "break"

        def escape(_e=None):
            if "Escape" not in held:
                do_cancel("Escape")
            return "break"

        for w in order:
            w.bind("<Tab>", lambda _e, w=w: move(w, 1))
            w.bind("<Shift-Tab>", lambda _e, w=w: move(w, -1))
            w.bind("<Escape>", escape)
            # replaces KitButton's own Return binding, so the key path knows which key closed the card
            w.bind("<Return>", lambda _e, w=w: activate(w, "Return"))
            w.bind("<KP_Enter>", lambda _e, w=w: activate(w, "KP_Enter"))
            if w not in lamps:
                w.bind("<space>", lambda _e, w=w: activate(w, "space"))
            else:
                # the lamp's class binding toggles it, except for a Space held since before the card took it
                w.bind("<space>", lambda _e: "break" if "space" in held else None)
        default = cancel_btn if danger else act_btn
        for w in (backdrop, card):
            w.bind("<Escape>", escape)
            w.bind("<Tab>", lambda _e: (default.focus_set(), "break")[1])
        _apply_modal_tag(root, backdrop)
        _watch_key_releases(root)               # clears `held` on each release, wherever it lands
        setattr(root, _OPEN_CONFIRM_ATTR, backdrop)
        select(default)
        # map the card (and its premapped sink) NOW: Tk defers a focus_set on an unmapped window to the next
        # idle pass, and a key already queued then went to the widget UNDER the card - an Enter sent the
        # composer's draft, an Esc navigated back with the card left open (B3 ADV2-01)
        root.update_idletasks()
        if not _alive(default):                  # torn down during that pass (the window closing)
            return backdrop, card
        if pending is not None and _active_key_sink(root) is pending:
            pending["restore_to"] = default      # the held key's release hands the focus to the card
            pending["held"] = held               # ...and a key pressed meanwhile leaves that key held (ADV2-03)
        else:
            default.focus_set()
    except Exception:
        state["done"] = True
        if getattr(root, _OPEN_CONFIRM_ATTR, None) is backdrop:
            setattr(root, _OPEN_CONFIRM_ATTR, None)
        try:
            backdrop.destroy()
        except tk.TclError:
            pass
        drop_sink()
        raise
    return backdrop, card


def _legacy_confirm(root, theme, message, on_confirm, confirm_label, danger, checkboxes):
    """Today's (pre-B3) card: the fallback when the kit cannot build the new one."""
    accent = theme.danger if danger else theme.bright

    backdrop = tk.Frame(root, bg=theme.panel)
    backdrop.place(x=0, y=0, relwidth=1, relheight=1)
    backdrop.tkraise()

    def cleanup():
        try:
            backdrop.destroy()
        except tk.TclError:
            pass

    def confirm(_event=None):
        cleanup()
        if checkboxes:
            on_confirm({key: var.get() for key, var in checkbox_vars.items()})
        else:
            on_confirm()

    def cancel(_event=None):
        cleanup()

    backdrop.bind("<Button-1>", cancel)

    card = tk.Frame(backdrop, bg=theme.bg, highlightthickness=2, highlightbackground=accent)
    card.place(relx=0.5, rely=0.5, anchor="center")
    card.bind("<Button-1>", lambda e: "break")

    tk.Label(
        card, text=message, bg=theme.bg, fg=theme.text, font=theme_mod.FONT_BODY,
        wraplength=420, justify="left", padx=20, pady=8,
    ).pack(pady=(20, 12))

    checkbox_vars = {}
    if checkboxes:
        check_frame = tk.Frame(card, bg=theme.bg)
        check_frame.pack(fill="x", padx=20, pady=(0, 12))
        for key, label, default_checked in checkboxes:
            var = tk.BooleanVar(value=default_checked)
            checkbox_vars[key] = var
            tk.Checkbutton(
                check_frame, text=label, variable=var,
                bg=theme.bg, fg=theme.text, activebackground=theme.bg, activeforeground=theme.text,
                selectcolor=theme.panel, anchor="w", font=theme_mod.FONT_BODY,
            ).pack(fill="x", anchor="w")

    btn_row = tk.Frame(card, bg=theme.bg)
    btn_row.pack(fill="x", padx=20, pady=(0, 20))

    cancel_btn = tk.Label(
        btn_row, text="Cancel", bg=theme.panel, fg=theme.dim, font=theme_mod.FONT_CAPTION,
        padx=14, pady=6, cursor="hand2", highlightthickness=1, highlightbackground=theme.dim,
    )
    cancel_btn.pack(side="right")
    cancel_btn.bind("<Button-1>", cancel)
    confirm_btn = tk.Label(
        btn_row, text=confirm_label, bg=accent, fg=theme.bg, font=theme_mod.FONT_CAPTION, padx=14, pady=6,
        cursor="hand2",
    )
    confirm_btn.pack(side="right", padx=(0, 10))
    confirm_btn.bind("<Button-1>", confirm)
    return backdrop, card


def make_scrollable(parent, theme):
    """A canvas+inner-frame+scrollbar combo for screens whose content can
    outgrow the window. Returns (outer, inner) - pack `outer` into the
    screen's layout and pack content into `inner`."""
    apply_ttk_theme(theme)
    bg = theme.bg
    outer = tk.Frame(parent, bg=bg)
    # Small requested size: the canvas's parcel must never dominate the pack
    # cavity, or the later-packed scrollbar gets squeezed to 1x1 in narrow
    # windows (a tk.Canvas's default requested width is ~378px - confirmed
    # empirically in a 300px window). fill/expand still grow it to fit.
    canvas = tk.Canvas(outer, bg=bg, highlightthickness=0, width=100, height=100)
    scrollbar = ttk.Scrollbar(outer, orient="vertical", style="Phosphor.Vertical.TScrollbar", command=canvas.yview)
    inner = tk.Frame(canvas, bg=bg)

    def on_inner_configure(_event=None):
        canvas.configure(scrollregion=canvas.bbox("all"))

    inner.bind("<Configure>", on_inner_configure)
    window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))

    def yscroll_set(first, last):
        # Auto-hide when content fits (first<=0 and last>=1, i.e. nothing to
        # scroll) - this is also the fix for a real ttk "clam" quirk: a
        # scrollbar whose thumb spans the entire trough renders a hardcoded
        # gray/seam appearance that ignores the configured style colors
        # (confirmed empirically), so a non-actionable full-track scrollbar
        # was both useless and visually broken. Hiding it dodges both.
        if float(first) <= 0.0 and float(last) >= 1.0:
            scrollbar.pack_forget()
        elif scrollbar.winfo_manager() != "pack":
            # before=canvas puts the scrollbar ahead of the canvas in pack
            # order, so its 15px is allocated before the canvas expands.
            scrollbar.pack(side="right", fill="y", before=canvas)
        scrollbar.set(first, last)

    canvas.configure(yscrollcommand=yscroll_set)
    canvas.pack(side="left", fill="both", expand=True)

    def on_mousewheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    canvas.bind("<MouseWheel>", on_mousewheel)
    inner.bind("<MouseWheel>", on_mousewheel)
    return outer, inner
