"""The document tool in the transcript (tests/DOCUMENT_TOOL_SPEC.md P2): the "CREATING DOCUMENT" row while the
model's create_document call streams in, the document card that replaces it (and that history shows), and the dim
line a failed call leaves. All three are widgets the transcript embeds (window_create, like the image tiles).

Kit only: kit.tokens inks and grounds, kit TextAction / StatusLine, the C9 sizes, a square card with a 1 px rule
border. No timers of its own (the creating row's cursor is fx.motion.WaitCursor, and only at FULL motion). The
widgets never call the server: the ChatView passes callbacks for OPEN / SAVE AS / DOWNLOAD / the context menu."""
import tkinter as tk

try:
    from .kit import C9, C9B, display_font as _display_font, px as _kpx, tokens as _tokens
    from .kit.widgets import StatusLine as _StatusLine, TextAction as _TextAction
    _KIT = True
except Exception:  # noqa: BLE001 - a broken kit still leaves working (plain) widgets
    _KIT = False
    C9, C9B = ("Consolas", 9), ("Consolas", 9, "bold")
    _StatusLine = _TextAction = _display_font = _kpx = _tokens = None

CARD_MAX_WIDTH = 560          # approved px at 100 %: the card never grows past this (less when the transcript is narrower)
CARD_WRAP_WIDTH = 440         # below this width the actions go under the meta line (820x560)
BADGE_WIDTH = 32
PAD_X, PAD_Y = 10, 8


def format_size(n):
    """3.1 KB style: bytes under 1 KB, one decimal above."""
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        n = 0
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _tok(theme):
    if _tokens is not None:
        try:
            return _tokens(theme)
        except Exception:  # noqa: BLE001
            pass
    return {"bg": getattr(theme, "bg", "#000000"), "panel": getattr(theme, "panel", "#111111"),
            "bright": getattr(theme, "bright", "#ffbf00"), "dim": getattr(theme, "dim", "#806000"),
            "value": getattr(theme, "dim", "#806000"), "rule_out": getattr(theme, "dim", "#806000"),
            "danger": getattr(theme, "danger", "#ff4040")}


def _px(widget, n):
    if _kpx is not None:
        try:
            return _kpx(widget, n)
        except Exception:  # noqa: BLE001
            pass
    return int(n)


def badge_text(filename):
    return "TXT" if str(filename or "").lower().endswith(".txt") else "MD"


def meta_text(doc):
    text = f"{doc.get('filename') or 'document.md'} · {format_size(doc.get('bytes'))}"
    try:
        if int(doc.get("version") or 1) > 1:
            text += f" · v{int(doc['version'])}"
    except (TypeError, ValueError):
        pass
    return text


class CreatingRow(tk.Frame):
    """One line at the call's position: `CREATING DOCUMENT` (dim), then ` · <title>` (bright, one line, elided) once
    the title is known, then the wait cursor at FULL motion only."""

    def __init__(self, master, theme, title=None, motion_level="off"):
        self._t = t = _tok(theme)
        tk.Frame.__init__(self, master, bg=t["bg"], bd=0, highlightthickness=0)
        self._motion_full = motion_level == "full"
        self.word = tk.Label(self, text="CREATING DOCUMENT", bg=t["bg"], fg=t["value"], font=C9B, bd=0, padx=0, pady=0)
        self.word.pack(side="left")
        if _StatusLine is not None:
            self.title = _StatusLine(self, theme, text="", bg=t["bg"], fg=t["bright"], font=C9B, bd=0, padx=0, pady=0)
        else:
            self.title = tk.Label(self, text="", bg=t["bg"], fg=t["bright"], font=C9B, bd=0, padx=0, pady=0, anchor="w")
        self.title.pack(side="left", fill="x", expand=True, padx=(_px(self, 6), 0))
        self._cursor_on = False
        self.set_title(title)

    def set_available_width(self, avail):
        """The row is as wide as the transcript lets it be, up to CARD_MAX_WIDTH (scaled), so the title has room to
        show (the kit line requests one character and elides into whatever width it is given)."""
        width = max(_px(self, 200), min(_px(self, CARD_MAX_WIDTH), int(avail)))
        try:
            if int(self.cget("width") or 0) != width:
                self.configure(width=width, height=max(self.word.winfo_reqheight(), self.title.winfo_reqheight()))
                self.pack_propagate(False)
        except tk.TclError:
            pass

    def set_title(self, title):
        text = (" · " + " ".join(str(title).split())) if title else ""
        try:
            self.title.configure(text=text)
        except tk.TclError:
            return
        self._sync_cursor()

    def _sync_cursor(self):
        """The block cursor after the text, FULL motion only (REDUCED / OFF / NullFX: none)."""
        if not self._motion_full or self._cursor_on:
            return
        wc = getattr(self.title, "fx_wait_cursor", None) if _StatusLine is not None else None
        if wc is None:
            return
        try:
            wc.start()
            self._cursor_on = True
        except Exception:  # noqa: BLE001
            pass

    def stop(self):
        wc = getattr(self.title, "_wait", None)
        if wc is not None:
            try:
                wc.stop()
            except Exception:  # noqa: BLE001
                pass
        self._cursor_on = False


class FailedLine(tk.Label):
    """The dim line a failed call leaves in place of the creating row."""

    def __init__(self, master, theme, reason):
        t = _tok(theme)
        self.text = failed_text(reason)
        tk.Label.__init__(self, master, text=self.text, bg=t["bg"], fg=t["value"], font=C9, bd=0, padx=0, pady=0,
                          anchor="w", justify="left")

    def set_available_width(self, avail):
        """The line wraps to the transcript's width (never cut at the edge)."""
        try:
            self.configure(wraplength=max(_px(self, 120), int(avail)))
        except tk.TclError:
            pass


def failed_text(reason):
    reason = " ".join(str(reason or "").split()) or "the model's call could not be used"
    return f"Could not create the document - {reason}"


class DocumentCard(tk.Frame):
    """A square card: raised ground, 1 px rule border, up to CARD_MAX_WIDTH. Left a two-line text badge (MD / TXT),
    middle the title (display S, bright, one line, elided; full title in the tip) over `<filename> · <size>[ · v2]`,
    right the text actions OPEN · SAVE AS · DOWNLOAD (dim at rest, bright on hover / focus). A click anywhere but the
    actions opens the viewer; Tab reaches the card (focus ring on the border), Return opens, the menu key / Shift+F10
    opens its context menu (P5). Below CARD_WRAP_WIDTH the actions go under the meta line."""

    def __init__(self, master, theme, doc, on_open=None, on_save_as=None, on_download=None, on_menu=None, on_focus=None):
        self._t = t = _tok(theme)
        self.doc = dict(doc)
        self.on_open, self.on_save_as, self.on_download, self.on_menu = on_open, on_save_as, on_download, on_menu
        self.on_focus = on_focus
        tk.Frame.__init__(self, master, bg=t["panel"], bd=0, highlightthickness=1, highlightbackground=t["rule_out"],
                          highlightcolor=t["bright"], takefocus=1, cursor="hand2")
        self._wrapped = None
        px = self._px = lambda n: _px(self, n)
        self.inner = tk.Frame(self, bg=t["panel"], bd=0)
        self.inner.pack(fill="both", expand=True, padx=px(PAD_X), pady=px(PAD_Y))
        self.top = tk.Frame(self.inner, bg=t["panel"], bd=0)
        self.top.pack(fill="x")
        badge = tk.Frame(self.top, bg=t["panel"], width=px(BADGE_WIDTH), bd=0)
        badge.pack(side="left", fill="y")
        badge.pack_propagate(False)
        self.badge = tk.Label(badge, text=badge_text(self.doc.get("filename")), bg=t["panel"], fg=t["value"], font=C9B,
                              bd=0, padx=0, pady=0)
        self.badge.pack(expand=True)
        self.middle = tk.Frame(self.top, bg=t["panel"], bd=0)
        self.middle.pack(side="left", fill="x", expand=True, padx=(px(8), px(8)))
        title_font = _display_font(self, "S") if _display_font is not None else ("Consolas", 11, "bold")
        title_text = " ".join(str(self.doc.get("title") or self.doc.get("filename") or "Document").split())
        if _StatusLine is not None:
            self.title = _StatusLine(self.middle, theme, text=title_text, bg=t["panel"], fg=t["bright"], font=title_font,
                                     bd=0, padx=0, pady=0)
        else:
            self.title = tk.Label(self.middle, text=title_text, bg=t["panel"], fg=t["bright"], font=title_font, bd=0,
                                  padx=0, pady=0, anchor="w")
        self.title.pack(fill="x")
        self.meta = tk.Label(self.middle, text=meta_text(self.doc), bg=t["panel"], fg=t["value"], font=C9, bd=0, padx=0,
                             pady=0, anchor="w")
        self.meta.pack(fill="x", pady=(px(2), 0))
        # a child of `inner` (the common ancestor), so it can be packed in the top row or under the meta line
        self.actions = tk.Frame(self.inner, bg=t["panel"], bd=0)
        self.actions.pack(in_=self.top, side="right", padx=(px(4), 0))
        self.action_words = {}
        for key, word, fn in (("open", "Open", self._open), ("save_as", "Save as", self._save_as),
                              ("download", "Download", self._download)):
            if _TextAction is not None:
                w = _TextAction(self.actions, theme, text=word, upper=True, ground=t["panel"], ink=t["value"], command=fn)
            else:
                w = tk.Label(self.actions, text=word.upper(), bg=t["panel"], fg=t["value"], font=C9B, cursor="hand2")
                w.bind("<Button-1>", lambda e, f=fn: (f(), "break")[1])
            w.pack(side="left", padx=(0, px(10)) if key != "download" else 0)
            self.action_words[key] = w
        for w in (self, self.inner, self.top, badge, self.badge, self.middle, self.title, self.meta):
            w.bind("<Button-1>", self._on_click, add="+")
        self.bind("<Return>", lambda e: (self._open(), "break")[1])
        self.bind("<space>", lambda e: (self._open(), "break")[1])
        for seq in ("<App>", "<Shift-F10>"):
            try:
                self.bind(seq, self._on_menu_key)
            except tk.TclError:
                pass
        self.bind("<Button-3>", self._on_menu_key, add="+")
        self.bind("<Configure>", self._on_configure, add="+")
        self.bind("<FocusIn>", lambda e: self._focus_changed(True), add="+")
        self.bind("<FocusOut>", lambda e: self._focus_changed(False), add="+")

    def _focus_changed(self, focused):
        if self.on_focus is not None:
            try:
                self.on_focus(focused)
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- layout
    def set_available_width(self, avail):
        """The card is as wide as the transcript lets it be, up to CARD_MAX_WIDTH (scaled); its height follows its
        content (pack_propagate is off for the width, so the height is set from the inner frame's request)."""
        width = max(self._px(200), min(self._px(CARD_MAX_WIDTH), int(avail)))
        try:
            if int(self.cget("width") or 0) != width:
                self.configure(width=width)
                self.pack_propagate(False)
                self.after_idle(self._sync_height)
        except tk.TclError:
            pass

    def _sync_height(self):
        try:
            if not self.winfo_exists():
                return
            self.update_idletasks()
            h = self.inner.winfo_reqheight() + 2 * self._px(PAD_Y) + 2
            if int(self.cget("height") or 0) != h:
                self.configure(height=h)
        except tk.TclError:
            pass

    def _on_configure(self, event):
        try:
            width = int(event.width)
        except (TypeError, ValueError, AttributeError):
            return
        wrapped = width < self._px(CARD_WRAP_WIDTH)
        if wrapped == self._wrapped:
            return
        self._wrapped = wrapped
        try:
            self.actions.pack_forget()
            if wrapped:
                self.actions.pack(in_=self.inner, fill="x", anchor="w", pady=(self._px(6), 0))
            else:
                self.actions.pack(in_=self.top, side="right", padx=(self._px(4), 0))
            self.after_idle(self._sync_height)
        except tk.TclError:
            pass

    # ---------------------------------------------------------------- actions
    def _on_click(self, _event=None):
        try:
            self.focus_set()
        except tk.TclError:
            pass
        self._open()
        return "break"

    def _open(self):
        if self.on_open is not None:
            self.on_open(self.doc, self)

    def _save_as(self):
        if self.on_save_as is not None:
            self.on_save_as(self.doc, self)

    def _download(self):
        if self.on_download is not None:
            self.on_download(self.doc, self)

    def _on_menu_key(self, event=None):
        if self.on_menu is not None:
            self.on_menu(self.doc, self, event)
            return "break"
        return None

    def update_doc(self, doc):
        self.doc = dict(doc)
        try:
            self.title.configure(text=" ".join(str(self.doc.get("title") or self.doc.get("filename") or "Document").split()))
            self.meta.configure(text=meta_text(self.doc))
            self.badge.configure(text=badge_text(self.doc.get("filename")))
        except tk.TclError:
            pass
