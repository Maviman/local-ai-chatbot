"""The document viewer (tests/DOCUMENT_TOOL_SPEC.md P3): an overlay card over the window with the Thoughts card's
geometry pattern (backdrop, the card centred, one 1 px bright border), a ribbon (title + `<filename> · <size>` left;
RENDERED | RAW, NEW WINDOW, SAVE AS, DOWNLOAD, CLOSE right), the body (the app's own markdown renderer, or the raw
source in a read-only Text; both selectable and copyable), and a legend line. NEW WINDOW pops the same viewer into a
tk.Toplevel (own ribbon without NEW WINDOW, same body and legend minus W); several may be open; Esc or CLOSE closes
only that window; they die with the app. The pop-out keeps the phosphor it opened with (no re-tint).

The document is untrusted model output: it is only ever shown as text or through markdown_text (never HTML); links
are shown, not opened. Big documents: the body shows at most BODY_CAP characters with a dim line saying so (SAVE AS /
DOWNLOAD keep the whole document) - the viewer never freezes the window on a megabyte.

Keys while the focus is inside a viewer: R raw/rendered, W new window (overlay only), S save as, D download, Esc close,
Ctrl+C copy, Page Up / Page Down / Home / End scroll. The viewer has no text entry, so the bare letters are safe there.
No timers of its own; no new sound (the notices of a save go through the existing rules)."""
import tkinter as tk
from tkinter import ttk

from .markdown_text import MarkdownText

try:
    from .kit import C9, C9B, C10, display_font as _display_font, px as _kpx, tokens as _tokens
    from .kit.widgets import Segmented as _Segmented, StatusLine as _StatusLine, TextAction as _TextAction
    _KIT = True
except Exception:  # noqa: BLE001
    _KIT = False
    C9, C9B, C10 = ("Consolas", 9), ("Consolas", 9, "bold"), ("Consolas", 10)
    _Segmented = _StatusLine = _TextAction = _display_font = _kpx = _tokens = None

from .document_card import format_size, _tok

BODY_CAP = 256 * 1024                     # characters shown in the body (the renderer's own caps still apply)
CAP_NOTE = "Showing the first 256 KB - SAVE AS or DOWNLOAD keeps the whole document."
OVERLAY_MAX_WIDTH = 900
OVERLAY_SIDE_GAP = 48
OVERLAY_TOP_GAP = 96
RIBBON_WRAP_WIDTH = 800       # below this card width (820x560: the card is 772) the ribbon's actions go to a second row
WINDOW_DEFAULT = (900, 700)
WINDOW_MIN = (480, 360)
LEGEND_OVERLAY = "R raw/rendered · W new window · S save as · D download · ESC close"
LEGEND_WINDOW = "R raw/rendered · S save as · D download · ESC close"
APP_NAME = "Personal Terminal"


def _px(widget, n):
    if _kpx is not None:
        try:
            return _kpx(widget, n)
        except Exception:  # noqa: BLE001
            pass
    return int(n)


def _swallow_tabs(widget):
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


class ViewerPane:
    """The ribbon + body + legend inside `container` (a card frame or a Toplevel). `actions` is the owner: a ChatView
    (its _save_document_as / _download_document / _open_document_window) - the pane itself never touches the server."""

    def __init__(self, container, theme, doc, content, actions, popout=False, on_close=None, fx_level="off"):
        self.container = container
        self.theme = theme
        self.doc = dict(doc)
        self.content = content if isinstance(content, str) else ""
        self.actions = actions
        self.popout = popout
        self.on_close = on_close
        self._t = t = _tok(theme)
        self.mode = "rendered"
        self._ribbon_wrapped = None
        px = self._px = lambda n: _px(container, n)
        bg = t["bg"]

        self.ribbon = tk.Frame(container, bg=bg, bd=0)
        self.ribbon.pack(fill="x", padx=px(14), pady=(px(10), 0))
        self.ribbon_left = tk.Frame(self.ribbon, bg=bg, bd=0)
        self.ribbon_left.pack(side="left", fill="x", expand=True)
        title_font = _display_font(container, "S") if _display_font is not None else ("Consolas", 11, "bold")
        title_text = " ".join(str(self.doc.get("title") or self.doc.get("filename") or "Document").split())
        if _StatusLine is not None:
            self.title = _StatusLine(self.ribbon_left, theme, text=title_text, bg=bg, fg=t["bright"], font=title_font, bd=0,
                                     padx=0, pady=0)
        else:
            self.title = tk.Label(self.ribbon_left, text=title_text, bg=bg, fg=t["bright"], font=title_font, bd=0, padx=0,
                                  pady=0, anchor="w")
        self.title.pack(fill="x")
        self.meta = tk.Label(self.ribbon_left, text=f"{self.doc.get('filename') or 'document.md'} · {format_size(self.doc.get('bytes'))}",
                             bg=bg, fg=t["value"], font=C9, bd=0, padx=0, pady=0, anchor="w")
        self.meta.pack(fill="x", pady=(px(2), 0))
        self.ribbon_actions = tk.Frame(self.ribbon, bg=bg, bd=0)
        self.ribbon_actions.pack(side="right", padx=(px(8), 0))
        self.mode_var = tk.StringVar(container, value="rendered")
        if _Segmented is not None:
            self.segmented = _Segmented(self.ribbon_actions, theme, options=[("rendered", "RENDERED"), ("raw", "RAW")],
                                        variable=self.mode_var, command=lambda *_a: self.set_mode(self.mode_var.get()), ground=bg)
            self.segmented.pack(side="left", padx=(0, px(10)))
        else:
            self.segmented = None
            for value, word in (("rendered", "RENDERED"), ("raw", "RAW")):
                w = tk.Label(self.ribbon_actions, text=word, bg=bg, fg=t["value"], font=C9B, cursor="hand2")
                w.bind("<Button-1>", lambda e, v=value: (self.set_mode(v), "break")[1])
                w.pack(side="left", padx=(0, px(10)))
        self.words = {}
        items = [] if popout else [("new_window", "New window", self.new_window)]
        items += [("save_as", "Save as", self.save_as), ("download", "Download", self.download), ("close", "Close", self.close)]
        for key, word, fn in items:
            if _TextAction is not None:
                w = _TextAction(self.ribbon_actions, theme, text=word, upper=True, ground=bg, ink=t["value"], command=fn)
            else:
                w = tk.Label(self.ribbon_actions, text=word.upper(), bg=bg, fg=t["value"], font=C9B, cursor="hand2")
                w.bind("<Button-1>", lambda e, f=fn: (f(), "break")[1])
            w.pack(side="left", padx=(0, 0 if key == "close" else px(10)))
            self.words[key] = w
        tk.Frame(container, bg=t["dim"], height=1).pack(fill="x", padx=px(14), pady=(px(8), 0))

        # the legend is packed BEFORE the body (at the bottom), so the body's expand never squeezes it out
        self.legend = tk.Label(container, text=LEGEND_WINDOW if popout else LEGEND_OVERLAY, bg=bg, fg=t["value"], font=C9,
                               bd=0, padx=0, pady=0, anchor="w")
        self.legend.pack(side="bottom", fill="x", padx=px(14), pady=(px(6), px(8)))
        self.body = tk.Frame(container, bg=bg, bd=0)
        self.body.pack(fill="both", expand=True, padx=(px(14), px(6)), pady=(px(6), 0))
        self.capped = len(self.content) > BODY_CAP
        shown = self.content[:BODY_CAP] if self.capped else self.content
        if self.capped:
            tk.Label(self.body, text=CAP_NOTE, bg=bg, fg=t["value"], font=C9, bd=0, padx=0, pady=0, anchor="w",
                     justify="left").pack(fill="x", pady=(0, px(4)))
        self.texts = tk.Frame(self.body, bg=bg, bd=0)
        self.texts.pack(fill="both", expand=True)
        self.scrollbar = ttk.Scrollbar(self.texts, orient="vertical", style="Phosphor.Vertical.TScrollbar")
        self.rendered = MarkdownText(self.texts, highlightthickness=0, padx=px(4), bd=0)
        self.rendered.show_message_actions = False          # a read-only rendering: no [Edit] / [Regenerate] link
        self.rendered.add_static_message("assistant", shown)
        self.raw = tk.Text(self.texts, bg=bg, fg=t["bright"], insertbackground=t["bright"], font=C10, wrap="word",
                           highlightthickness=0, bd=0, padx=px(4), selectbackground=t["bright"], selectforeground=bg)
        self.raw.insert("1.0", shown)
        self.raw.configure(state="disabled")
        self._current = None
        self.set_mode("rendered")
        for w in (self.rendered, self.raw):
            w.yview_moveto(0.0)                                   # a document opens at its top

        for w in (container, self.ribbon, self.body, self.rendered, self.raw, self.legend):
            w.bind("<Key>", self._on_key, add="+")
        for w in (self.rendered, self.raw):
            w.bind("<Home>", lambda e: (self._scroll_to(0.0), "break")[1])
            w.bind("<End>", lambda e: (self._scroll_to(1.0), "break")[1])
        container.bind("<Configure>", self._on_configure, add="+")
        self._fx_level = fx_level

    # ---------------------------------------------------------------- modes / layout
    def set_mode(self, mode):
        mode = "raw" if mode == "raw" else "rendered"
        self.mode = mode
        if self.mode_var.get() != mode:
            self.mode_var.set(mode)
        widget = self.raw if mode == "raw" else self.rendered
        if widget is self._current:
            return
        for w in (self.rendered, self.raw):
            try:
                w.pack_forget()
            except tk.TclError:
                pass
        try:
            self.scrollbar.pack_forget()
        except tk.TclError:
            pass
        widget.configure(yscrollcommand=self._yscroll_set)
        self.scrollbar.configure(command=widget.yview)
        widget.pack(side="left", fill="both", expand=True)
        self._current = widget
        try:
            widget.focus_set()
        except tk.TclError:
            pass

    def _yscroll_set(self, first, last):
        self.scrollbar.set(first, last)
        try:
            if float(first) <= 0.0 and float(last) >= 1.0:
                self.scrollbar.pack_forget()
            elif self.scrollbar.winfo_manager() != "pack":
                self.scrollbar.pack(side="right", fill="y")
        except (tk.TclError, ValueError):
            pass

    def toggle_mode(self):
        self.set_mode("raw" if self.mode == "rendered" else "rendered")

    def _scroll_to(self, fraction):
        if self._current is not None:
            self._current.yview_moveto(fraction)

    def _on_configure(self, event):
        try:
            width = int(event.width)
        except (TypeError, ValueError, AttributeError):
            return
        if event.widget is not self.container:
            return
        try:
            self.legend.configure(wraplength=max(self._px(120), width - 2 * self._px(14)), justify="left")   # never cut: wraps to two lines
        except tk.TclError:
            pass
        wrapped = width < self._px(RIBBON_WRAP_WIDTH)
        if wrapped == self._ribbon_wrapped:
            return
        self._ribbon_wrapped = wrapped
        try:
            self.ribbon_actions.pack_forget()
            if wrapped:
                self.ribbon_left.pack_configure(side="top", fill="x", expand=False)
                self.ribbon_actions.pack(side="top", fill="x", anchor="w", pady=(self._px(6), 0))
            else:
                self.ribbon_left.pack_configure(side="left", fill="x", expand=True)
                self.ribbon_actions.pack(side="right", padx=(self._px(8), 0))
        except tk.TclError:
            pass

    # ---------------------------------------------------------------- keys / actions
    def _on_key(self, event):
        state = int(getattr(event, "state", 0) or 0)
        if state & 0x4 or state & 0x20000:          # Control / Alt chords belong to the Text (Ctrl+C) and the app
            return None
        key = (event.keysym or "").lower()
        if key == "escape":
            self.close()
            return "break"
        if key == "r":
            self.toggle_mode()
            return "break"
        if key == "w" and not self.popout:
            self.new_window()
            return "break"
        if key == "s":
            self.save_as()
            return "break"
        if key == "d":
            self.download()
            return "break"
        return None

    def new_window(self):
        fn = getattr(self.actions, "_open_document_window", None)
        if fn is not None:
            fn(self.doc, self.content)

    def save_as(self):
        fn = getattr(self.actions, "_save_document_as", None)
        if fn is not None:
            fn(self.doc)

    def download(self):
        fn = getattr(self.actions, "_download_document", None)
        if fn is not None:
            fn(self.doc)

    def close(self):
        if self.on_close is not None:
            self.on_close()

    def focus(self):
        try:
            (self._current or self.container).focus_set()
        except tk.TclError:
            pass


class DocumentViewer:
    """The overlay: backdrop over the root window (a click on it closes), the card centred, width min(900, window -
    48), height window - 96 (scaled), one 1 px bright border; the keyboard stays inside (Tab swallowed); closing
    gives the focus back to the opener (the card, when opened by key) or the composer."""

    def __init__(self, chat, doc, content, opener=None):
        self.chat = chat
        self.doc = dict(doc)
        theme = chat.ctx.theme
        root = chat.winfo_toplevel()
        self.root = root
        self._t = t = _tok(theme)
        self.opener = opener
        self.backdrop = tk.Frame(root, bg=t["panel"])
        self.backdrop.place(x=0, y=0, relwidth=1, relheight=1)
        self.backdrop.tkraise()
        self.backdrop.bind("<Button-1>", lambda e: (self.close(), "break")[1])
        self.card = tk.Frame(self.backdrop, bg=t["bg"], highlightthickness=1, highlightbackground=t["bright"],
                             highlightcolor=t["bright"], bd=0)
        self.card.bind("<Button-1>", lambda e: "break")
        self._place()
        self.backdrop.bind("<Configure>", lambda e: self._place(), add="+")
        level = "off"
        try:
            level = chat.ctx.fx.level()
        except Exception:  # noqa: BLE001
            pass
        self.pane = ViewerPane(self.card, theme, self.doc, content, chat, popout=False, on_close=self.close, fx_level=level)
        self.backdrop.bind("<Key>", self.pane._on_key, add="+")
        _swallow_tabs(self.backdrop)
        self.pane.focus()

    def _place(self):
        try:
            w, h = self.root.winfo_width(), self.root.winfo_height()
        except tk.TclError:
            return
        px = lambda n: _px(self.root, n)
        width = max(px(320), min(px(OVERLAY_MAX_WIDTH), w - px(OVERLAY_SIDE_GAP)))
        height = max(px(240), h - px(OVERLAY_TOP_GAP))
        self.card.place(relx=0.5, rely=0.5, anchor="center", width=width, height=height)

    @property
    def open(self):
        try:
            return bool(self.backdrop.winfo_exists())
        except tk.TclError:
            return False

    def close(self):
        if getattr(self.chat, "_document_viewer", None) is self:
            self.chat._document_viewer = None
        try:
            self.backdrop.destroy()
        except tk.TclError:
            pass
        target = self.opener
        try:
            if target is None or not target.winfo_exists() or not target.winfo_viewable():
                target = self.chat.input if self.chat.is_visible() else None
        except (tk.TclError, AttributeError):
            target = None
        if target is not None:
            try:
                target.focus_set()
            except tk.TclError:
                pass


def open_document_window(chat, doc, content):
    """The pop-out: a Toplevel titled `<title> - Personal Terminal` with the app's icon, 900x700 clamped to the work area,
    at least 480x360, resizable; Esc / CLOSE closes only it. -> the Toplevel."""
    root = chat.winfo_toplevel()
    theme = chat.ctx.theme
    t = _tok(theme)
    win = tk.Toplevel(root, bg=t["bg"])
    title = " ".join(str(doc.get("title") or doc.get("filename") or "Document").split())
    win.title(f"{title} - {APP_NAME}")
    try:
        icons = getattr(root, "_icon_images", None)
        if icons:
            win.iconphoto(False, *icons)
    except tk.TclError:
        pass
    px = lambda n: _px(root, n)
    width, height = px(WINDOW_DEFAULT[0]), px(WINDOW_DEFAULT[1])
    area = None
    try:
        from .main_window import _work_area
        area = _work_area()
    except Exception:  # noqa: BLE001
        area = None
    if area:
        left, top, right, bottom = area
    else:
        left, top, right, bottom = 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()
    width = max(px(WINDOW_MIN[0]), min(width, right - left))
    height = max(px(WINDOW_MIN[1]), min(height, bottom - top))
    x = left + max(0, (right - left - width) // 2)
    y = top + max(0, (bottom - top - height) // 2)
    win.geometry(f"{width}x{height}+{x}+{y}")
    win.minsize(px(WINDOW_MIN[0]), px(WINDOW_MIN[1]))
    level = "off"
    try:
        level = chat.ctx.fx.level()
    except Exception:  # noqa: BLE001
        pass
    holder = {}

    def close():
        windows = getattr(chat, "_document_windows", None)
        if windows is not None and win in windows:
            windows.remove(win)
        try:
            win.destroy()
        except tk.TclError:
            pass
        try:
            root.focus_force()                  # the keyboard goes back to the app (the overlay, or the chat)
        except tk.TclError:
            pass

    holder["pane"] = ViewerPane(win, theme, doc, content, chat, popout=True, on_close=close, fx_level=level)
    win.pane = holder["pane"]
    win.protocol("WM_DELETE_WINDOW", close)
    win.bind("<Key>", holder["pane"]._on_key, add="+")
    windows = getattr(chat, "_document_windows", None)
    if windows is None:
        windows = chat._document_windows = []
    windows.append(win)
    holder["pane"].focus()
    return win
