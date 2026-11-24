"""The image viewer (tests/DOCUMENT_TOOL_SPEC.md P4): a click on an image in the transcript opens a full-window
overlay (the theme ground at full opacity, no card border) with the image centred and scaled to fit (never above
100 % unless zoomed), a ribbon in the document viewer's family (IMAGE + `<w> × <h> · <zoom %>` left; NEW WINDOW,
SAVE AS, DOWNLOAD, CLOSE right), zoom by the wheel and + / - (25 % steps, 10 %-800 %), 0 = fit, 1 = 100 %, drag to
pan when the image is larger than the view, and a legend line. NEW WINDOW is the same viewer in a Toplevel titled
"Image - Personal Terminal", sized to the image plus its chrome and clamped to the work area.

The PhotoImages are owned by the viewer and dropped on close (never left to the gc - the F-1 rule). Scaling uses
Tk's own integer zoom / subsample (zoom(n).subsample(d) for the 25 % steps), and an intermediate zoomed copy is never
allowed past SCALE_CAP pixels on a side, so an 8K image never blocks the Tk thread for long. No timers of its own."""
import tkinter as tk

try:
    from .kit import C9, C9B, display_font as _display_font, px as _kpx
    from .kit.widgets import TextAction as _TextAction
except Exception:  # noqa: BLE001
    C9, C9B = ("Consolas", 9), ("Consolas", 9, "bold")
    _TextAction = _display_font = _kpx = None

from .document_card import _tok

ZOOM_MIN, ZOOM_MAX, ZOOM_STEP = 10, 800, 25
MARGIN = 24
SCALE_CAP = 6000                 # the largest side an intermediate zoomed copy may have
FIT_MAX = 6                      # fit = the best zoom(a).subsample(b) with a, b at most this
LEGEND_OVERLAY = "+/- zoom · 0 fit · 1 actual · W new window · S save as · D download · ESC close"
LEGEND_WINDOW = "+/- zoom · 0 fit · 1 actual · S save as · D download · ESC close"
APP_NAME = "Personal Terminal"
WINDOW_MIN = (480, 360)


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


def scale_factors(percent):
    """(zoom n, subsample d) for a percentage in 25 % steps (and 10 %): 125 -> (5, 4), 50 -> (1, 2), 300 -> (3, 1)."""
    percent = max(ZOOM_MIN, min(ZOOM_MAX, int(percent)))
    if percent % 100 == 0:
        return percent // 100, 1
    if percent % 25 == 0:
        n, d = percent // 25, 4
        while n % 2 == 0 and d % 2 == 0:
            n //= 2
            d //= 2
        return n, d
    return 1, max(1, round(100 / percent))


class ImagePane:
    """The ribbon + canvas + legend inside `container`. `actions` is the owner (a ChatView: _save_image_as,
    _download_image, _open_image_window). `photo` is the full-size tk.PhotoImage; `ref` the (message_id, image_id)
    the actions work on; `meta` {"filename", "bytes"}."""

    def __init__(self, container, theme, photo, ref, meta, actions, popout=False, on_close=None):
        self.container = container
        self.theme = theme
        self.photo = photo
        self.ref = ref
        self.meta = dict(meta or {})
        self.actions = actions
        self.popout = popout
        self.on_close = on_close
        self._t = t = _tok(theme)
        px = self._px = lambda n: _px(container, n)
        bg = t["bg"]
        self.full_w, self.full_h = (photo.width(), photo.height()) if photo is not None else (0, 0)
        self.percent = 100
        self._scaled = None
        self._scaled_percent = None
        self._fit_mode = True
        self._drag = None

        self.ribbon = tk.Frame(container, bg=bg, bd=0)
        self.ribbon.pack(fill="x", padx=px(14), pady=(px(10), 0))
        self.ribbon_left = tk.Frame(self.ribbon, bg=bg, bd=0)
        self.ribbon_left.pack(side="left", fill="x", expand=True)
        title_font = _display_font(container, "S") if _display_font is not None else ("Consolas", 11, "bold")
        self.title = tk.Label(self.ribbon_left, text="IMAGE", bg=bg, fg=t["bright"], font=title_font, bd=0, padx=0, pady=0, anchor="w")
        self.title.pack(fill="x")
        self.meta_label = tk.Label(self.ribbon_left, text="", bg=bg, fg=t["value"], font=C9, bd=0, padx=0, pady=0, anchor="w")
        self.meta_label.pack(fill="x", pady=(px(2), 0))
        self.ribbon_actions = tk.Frame(self.ribbon, bg=bg, bd=0)
        self.ribbon_actions.pack(side="right", padx=(px(8), 0))
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
        self.legend = tk.Label(container, text=LEGEND_WINDOW if popout else LEGEND_OVERLAY, bg=bg, fg=t["value"], font=C9,
                               bd=0, padx=0, pady=0, anchor="w")
        self.legend.pack(side="bottom", fill="x", padx=px(14), pady=(px(6), px(8)))
        self.canvas = tk.Canvas(container, bg=bg, highlightthickness=0, bd=0, takefocus=1)
        self.canvas.pack(fill="both", expand=True, padx=px(MARGIN), pady=px(MARGIN) // 2)
        self.item = None
        if photo is None:
            self.canvas.create_text(10, 10, anchor="nw", fill=t["value"], font=C9,
                                    text="No preview for this image format - SAVE AS or DOWNLOAD keeps the file.")
        self.canvas.bind("<Configure>", self._on_configure, add="+")
        self.canvas.bind("<MouseWheel>", self._on_wheel, add="+")
        self.canvas.bind("<ButtonPress-1>", self._on_press, add="+")
        self.canvas.bind("<B1-Motion>", self._on_drag, add="+")
        for w in (container, self.canvas, self.ribbon, self.legend):
            w.bind("<Key>", self._on_key, add="+")
        container.bind("<Configure>", self._on_container_configure, add="+")
        self._sync_meta()

    def _on_container_configure(self, event):
        """The legend line wraps to the width it has (two lines in a narrow window at 150 %), never cut."""
        if event.widget is not self.container:
            return
        try:
            self.legend.configure(wraplength=max(self._px(120), int(event.width) - 2 * self._px(14)), justify="left")
        except (tk.TclError, TypeError, ValueError):
            pass

    # ---------------------------------------------------------------- scaling
    def _view_size(self):
        try:
            w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        except tk.TclError:
            return 1, 1
        return max(1, w), max(1, h)

    def fit_scale(self):
        """(zoom a, subsample b), a and b at most FIT_MAX, the largest a/b (never above 1) at which the whole image fits
        the view both ways - 3/5, 2/3, 3/4 ... instead of only 1/n (which left most of the ground empty); a plain 1/n
        when a zoomed intermediate would pass SCALE_CAP. (1, 1) when the image already fits."""
        if not self.full_w or not self.full_h:
            return 1, 1
        vw, vh = self._view_size()
        if self.full_w <= vw and self.full_h <= vh:
            return 1, 1
        side = max(self.full_w, self.full_h)
        best, best_ratio = None, 0.0
        for a in range(1, FIT_MAX + 1):
            if a > 1 and side * a > SCALE_CAP:
                break
            for b in range(a, FIT_MAX + 1):
                ratio = a / b
                if ratio > 1 or ratio <= best_ratio:
                    continue
                if -(-self.full_w * a // b) <= vw and -(-self.full_h * a // b) <= vh:
                    best, best_ratio = (a, b), ratio
        if best is None:
            k = max(-(-self.full_w // vw), -(-self.full_h // vh))
            best = (1, max(1, k))
        return best

    def fit_percent(self):
        a, b = self.fit_scale()
        return max(ZOOM_MIN, round(100 * a / b))

    def _scaled_photo(self, a, b):
        if self.photo is None:
            return None
        if (a, b) == (1, 1):
            return self.photo
        if self._scaled is not None and self._scaled_percent == (a, b):
            return self._scaled
        n, d = a, b
        while n > 1 and max(self.full_w, self.full_h) * n > SCALE_CAP:
            n -= 1                                                  # never an intermediate past SCALE_CAP a side
        img = self.photo
        if n > 1:
            img = img.zoom(n, n)
        if d > 1:
            img = img.subsample(d, d)
        self._scaled, self._scaled_percent = img, (a, b)
        return img

    def set_percent(self, percent, fit=False):
        percent = max(ZOOM_MIN, min(ZOOM_MAX, int(percent)))
        a, b = scale_factors(percent)
        self.set_scale(a, b, fit=fit, percent=percent)

    def set_scale(self, a, b, fit=False, percent=None):
        self.percent = max(ZOOM_MIN, min(ZOOM_MAX, int(percent if percent is not None else round(100 * a / b))))
        self._fit_mode = fit
        img = self._scaled_photo(a, b)
        self.canvas.delete("image")
        self.item = None
        if img is not None:
            vw, vh = self._view_size()
            iw, ih = img.width(), img.height()
            x, y = max(0, (vw - iw) // 2), max(0, (vh - ih) // 2)
            self.item = self.canvas.create_image(x, y, anchor="nw", image=img, tags=("image",))
            self.canvas.configure(scrollregion=(0, 0, max(vw, iw), max(vh, ih)))
            self.canvas.xview_moveto(0)
            self.canvas.yview_moveto(0)
            self._shown = img                                       # a live reference while it is on the canvas
        self._sync_meta()

    def fit(self):
        a, b = self.fit_scale()
        self.set_scale(a, b, fit=True)

    def zoom_step(self, direction):
        cur = self.percent
        if cur % ZOOM_STEP:
            cur = (cur // ZOOM_STEP) * ZOOM_STEP + (ZOOM_STEP if direction > 0 else 0)
        else:
            cur += ZOOM_STEP * (1 if direction > 0 else -1)
        self.set_percent(cur)

    def _sync_meta(self):
        try:
            self.meta_label.configure(text=f"{self.full_w} × {self.full_h} · {self.percent} %")
        except tk.TclError:
            pass

    def _on_configure(self, _event=None):
        if self._fit_mode:
            self.fit()

    # ---------------------------------------------------------------- input
    def _on_wheel(self, event):
        self.zoom_step(1 if event.delta > 0 else -1)
        return "break"

    def _on_press(self, event):
        self._drag = (event.x, event.y)
        self.canvas.scan_mark(event.x, event.y)
        try:
            self.canvas.focus_set()
        except tk.TclError:
            pass

    def _on_drag(self, event):
        if self._drag is not None:
            self.canvas.scan_dragto(event.x, event.y, gain=1)

    def _on_key(self, event):
        state = int(getattr(event, "state", 0) or 0)
        if state & 0x4 or state & 0x20000:
            return None
        key = (event.keysym or "").lower()
        char = event.char or ""
        if key == "escape":
            self.close()
            return "break"
        if key in ("plus", "equal", "kp_add") or char == "+":
            self.zoom_step(1)
            return "break"
        if key in ("minus", "kp_subtract") or char == "-":
            self.zoom_step(-1)
            return "break"
        if key in ("0", "kp_0"):
            self.fit()
            return "break"
        if key in ("1", "kp_1"):
            self.set_percent(100)
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
        fn = getattr(self.actions, "_open_image_window", None)
        if fn is not None:
            fn(self.ref, self.meta, self.photo)

    def save_as(self):
        fn = getattr(self.actions, "_save_image_as", None)
        if fn is not None:
            fn(self.ref, self.meta)

    def download(self):
        fn = getattr(self.actions, "_download_image", None)
        if fn is not None:
            fn(self.ref, self.meta)

    def close(self):
        if self.on_close is not None:
            self.on_close()

    def release(self):
        """Drops the PhotoImages the pane made (the scaled copies); the full one belongs to the opener."""
        self._scaled = None
        self._shown = None
        try:
            self.canvas.delete("all")
        except tk.TclError:
            pass

    def focus(self):
        try:
            self.canvas.focus_set()
        except tk.TclError:
            pass


class ImageViewer:
    """The full-window overlay: the theme ground over the whole window (a click outside the image closes nothing - the
    ribbon's CLOSE / Esc do), the pane filling it."""

    def __init__(self, chat, photo, ref, meta, opener=None):
        self.chat = chat
        self.ref = ref
        theme = chat.ctx.theme
        root = chat.winfo_toplevel()
        self._t = t = _tok(theme)
        self.opener = opener
        self.photo = photo
        self.backdrop = tk.Frame(root, bg=t["bg"], highlightthickness=0)
        self.backdrop.place(x=0, y=0, relwidth=1, relheight=1)
        self.backdrop.tkraise()
        self.pane = ImagePane(self.backdrop, theme, photo, ref, meta, chat, popout=False, on_close=self.close)
        self.backdrop.bind("<Key>", self.pane._on_key, add="+")
        _swallow_tabs(self.backdrop)
        self.backdrop.update_idletasks()
        self.pane.fit()
        self.pane.focus()

    @property
    def open(self):
        try:
            return bool(self.backdrop.winfo_exists())
        except tk.TclError:
            return False

    def close(self):
        if getattr(self.chat, "_image_viewer", None) is self:
            self.chat._image_viewer = None
        self.pane.release()
        try:
            self.backdrop.destroy()
        except tk.TclError:
            pass
        self.photo = None
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


def open_image_window(chat, photo, ref, meta):
    """The pop-out: a Toplevel "Image - Personal Terminal", the image's size plus chrome, clamped to the work area."""
    root = chat.winfo_toplevel()
    theme = chat.ctx.theme
    t = _tok(theme)
    win = tk.Toplevel(root, bg=t["bg"])
    win.title(f"Image - {APP_NAME}")
    try:
        icons = getattr(root, "_icon_images", None)
        if icons:
            win.iconphoto(False, *icons)
    except tk.TclError:
        pass
    px = lambda n: _px(root, n)
    iw, ih = (photo.width(), photo.height()) if photo is not None else (640, 480)
    width, height = iw + px(2 * MARGIN) + px(28), ih + px(2 * MARGIN) + px(110)
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
    win.photo = photo                                             # owned by the window, dropped with it

    def close():
        windows = getattr(chat, "_image_windows", None)
        if windows is not None and win in windows:
            windows.remove(win)
        try:
            win.pane.release()
        except Exception:  # noqa: BLE001
            pass
        win.photo = None
        try:
            win.destroy()
        except tk.TclError:
            pass
        try:
            root.focus_force()                  # the keyboard goes back to the app (the overlay, or the chat)
        except tk.TclError:
            pass

    win.pane = ImagePane(win, theme, photo, ref, meta, chat, popout=True, on_close=close)
    win.protocol("WM_DELETE_WINDOW", close)
    win.bind("<Key>", win.pane._on_key, add="+")
    windows = getattr(chat, "_image_windows", None)
    if windows is None:
        windows = chat._image_windows = []
    windows.append(win)
    win.update_idletasks()
    win.pane.fit()
    win.pane.focus()
    return win
