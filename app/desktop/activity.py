"""The activity pull-down (bug-fix batch 34, 2026-10-13; the user: "a menu that shows what's up next and what's being
worked on, a pull-down menu on the top bar"; look and behaviour per tests/ACTIVITY_PANEL_SPEC.md, the data shape per the
bug-fix session's message to the UI session).

ActivityControl: ONE control owned by MainWindow - the word "ACTIVITY n" as the last action of every screen header
(hidden while nothing runs and nothing waits, so idle screens look as before); click / Enter / Space / Alt+Q toggle its
panel: a square kit card under the header's right end with NOW (every running item, oldest first, its stage word as the
chat's status row shows it) and NEXT (both queues in dispatch order, a preview line, CANCEL per row). Event-driven
through MainWindow.add_activity_listener (no polling timer); Esc / Alt+Q / a click on the control / a click outside /
any screen change close it; opened by key the first row takes the focus (Up / Down / Enter / Delete), opened by mouse
the focus stays where it was; on close the focus goes back. No sound of its own, no notice.

The word itself is one kit TextAction PER HEADER, made on first use and kept in step (text, shown / hidden): a header
lays its actions out itself (the Chat header places its words and sizes the slot to them; the others pack), so one
widget packed into every slot in turn would fight those layouts - the Chat header's own actions vanished behind it.
"""
import time
import tkinter as tk

from . import theme as theme_mod
from .keymap import confirm_open

try:
    from .kit import C9, C9B, C11B, geom_ratio as _geom_ratio, tokens as _tokens
    from .kit.widgets import StatusLine, TextAction
    _KIT = True
except Exception:  # noqa: BLE001 - the control must never stop the window from opening
    _KIT = False

PANEL_W = 560               # approved px, scaled by the kit ratio; never wider than the window - 32
PANEL_MAX_FRACTION = 0.6    # of the window height; past it the rows scroll
PREVIEW_CHARS = 80
ELLIPSIS = "…"


def _one_line(text, limit):
    """The first line of `text`, newlines as spaces, at most `limit` characters (elided with an ellipsis)."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + ELLIPSIS


class ActivityControl:
    """ActivityControl(win): win is the MainWindow (activity_snapshot / open_chat / cancel_queued / add_activity_listener)."""

    def __init__(self, win):
        self.win = win
        self.theme = win.theme
        self._t = _tokens(self.theme) if _KIT else None
        self._ratio = _geom_ratio(win) if _KIT else 1.0
        self.header = None
        self.panel = None
        self._rows = []
        self._focus_before = None
        self._count = 0
        self._by_key = False
        self._snapshot = {"now": [], "next": [], "health": {}}
        self._words = {}            # header -> its word (a kit TextAction, or a plain Label without the kit)
        win.bind_all("<Button-1>", self._on_click_anywhere, add="+")
        win.bind("<Unmap>", lambda e: self.close() if e.widget is win else None, add="+")
        win.add_activity_listener(self.refresh)
        self.refresh()

    # ---------------------------------------------------------------- the word, per header
    def _s(self, n):
        return int(round(n * self._ratio))

    @property
    def word(self):
        """The current header's word (None before the first attach)."""
        return self._words.get(self.header)

    def _word_for(self, header):
        w = self._words.get(header)
        if w is not None:
            try:
                if w.winfo_exists():
                    return w
            except tk.TclError:
                pass
        actions = getattr(header, "actions", None)
        if actions is None:
            return None
        text = "Activity %d" % self._count
        if _KIT and hasattr(header, "add_action"):
            # the Chat header: its own action API (it places and wraps its words itself); the word joins its list
            w = header.add_action(text, self.toggle)
            w._activity_in_list = True
        elif _KIT:
            w = TextAction(actions, self.theme, text=text, upper=True, ground=self._t["bg"], command=self.toggle)
            w._activity_in_list = False
        else:
            w = tk.Label(actions, text=text.upper(), bg=self.theme.bg, fg=self.theme.dim, font=theme_mod.FONT_CAPTION, cursor="hand2")
            w.bind("<Button-1>", lambda e: self.toggle())
            w._activity_in_list = False
        w.bind("<Return>", lambda e: (self.toggle(by_key=True), "break")[1], add="+")
        w.bind("<space>", lambda e: (self.toggle(by_key=True), "break")[1], add="+")
        self._words[header] = w
        return w

    def _set_shown(self, header, w, shown):
        """Shows / hides one header's word the way that header lays its actions out."""
        try:
            if getattr(w, "_activity_in_list", False):
                words = header._words
                present = w in words
                if shown and not present:
                    words.append(w)
                elif not shown and present:
                    words.remove(w)
                    w.place_forget()
                if shown != present:
                    header._layout_key = None
                    header._layout_actions()
            else:
                if shown and not w.winfo_manager():
                    w.pack(in_=header.actions, side="left", padx=(16, 0))
                    w.lift()
                elif not shown and w.winfo_manager():
                    w.pack_forget()
        except (tk.TclError, AttributeError):
            pass

    def attach(self, header):
        """Called by MainWindow.show_screen: the control moves to the new screen's header (its actions slot, last)."""
        self.close()
        self.header = header
        if header is None:
            return
        w = self._word_for(header)
        if w is not None:
            self._set_shown(header, w, self._count > 0)

    # ---------------------------------------------------------------- data -> look
    def refresh(self):
        """The listener: recount, show / hide the word, re-render the open panel. Never raises."""
        try:
            snap = self.win.activity_snapshot()
        except Exception:  # noqa: BLE001
            snap = {"now": [], "next": [], "health": {}}
        # the MODEL row: the watcher's word while the model server is not ok (STOPPED / NOT RESPONDING / ERROR /
        # LOADING...), unless a load card already stands for the model
        health = snap.get("health") or {}
        word = str(health.get("word") or "")
        now = list(snap.get("now") or [])
        if word and word != "Ready" and not any(i.get("kind") == "load" for i in now):
            now.append({"kind": "model", "conv_id": None, "title": "MODEL", "stage": word, "since": health.get("since")})
            snap = dict(snap, now=now)
        self._snapshot = snap
        count = len(now) + len(snap.get("next") or [])
        self._count = count
        text = "Activity %d" % count
        for header, w in list(self._words.items()):
            try:
                if not w.winfo_exists():
                    self._words.pop(header, None)
                    continue
                w.configure(text=text if _KIT else text.upper())
            except tk.TclError:
                self._words.pop(header, None)
        if self.header is not None:
            w = self._word_for(self.header)
            if w is not None:
                self._set_shown(self.header, w, count > 0)
        if count <= 0:
            self.close()
        elif self.panel is not None:
            self._render_rows()

    # ---------------------------------------------------------------- the panel
    @property
    def is_open(self):
        return self.panel is not None

    def toggle(self, by_key=False):
        if self.panel is not None:
            self.close()
        else:
            self.open(by_key=by_key)

    def on_alt_q(self):
        """Alt+Q anywhere: toggle - unless a confirm card or the model-load card is up."""
        try:
            if confirm_open(self.win) or getattr(self.win, "_navigation_locked", False):
                return "break"
        except Exception:  # noqa: BLE001
            pass
        self.toggle(by_key=True)
        return "break"

    def open(self, by_key=False):
        if self.panel is not None or self._count <= 0:
            return
        win = self.win
        t = self._t
        theme = self.theme
        bg = t["bg"] if t else theme.bg
        bright = t["bright"] if t else theme.bright
        try:
            self._focus_before = win.focus_get()
        except (tk.TclError, KeyError):
            self._focus_before = None
        self._by_key = by_key
        panel = tk.Frame(win, bg=bg, highlightthickness=1, highlightbackground=bright, highlightcolor=bright, bd=0)
        self.panel = panel
        s = self._s
        title = tk.Frame(panel, bg=bg)
        title.pack(fill="x", padx=s(12), pady=(s(8), s(4)))
        tk.Label(title, text="ACTIVITY", bg=bg, fg=bright, font=C11B if _KIT else theme_mod.FONT_LABEL, bd=0, padx=0, pady=0).pack(side="left")
        tk.Label(title, text="ALT+Q · ESC close", bg=bg, fg=t["value"] if t else theme.dim, font=C9 if _KIT else theme_mod.FONT_CAPTION,
                 bd=0, padx=0, pady=0).pack(side="right")
        body = tk.Frame(panel, bg=bg)
        body.pack(fill="both", expand=True, padx=s(12), pady=(0, s(10)))
        self._body = body
        self._rows = []
        panel.bind("<Escape>", lambda e: (self.close(), "break")[1])
        panel.bind("<Button-1>", lambda e: "break")
        self._render_rows()
        self._place()
        panel.lift()
        if by_key and self._rows:
            try:
                self._rows[0].focus_set()
            except tk.TclError:
                pass

    def _place(self):
        panel, win, header = self.panel, self.win, self.header
        if panel is None:
            return
        try:
            win.update_idletasks()
            ww, wh = max(1, win.winfo_width()), max(1, win.winfo_height())
            width = min(self._s(PANEL_W), ww - 32)
            right = ww - 16
            top = 16
            if header is not None and header.winfo_ismapped():
                right = header.winfo_rootx() + header.winfo_width() - win.winfo_rootx() - self._s(16)
                top = header.winfo_rooty() + header.winfo_height() - win.winfo_rooty() + 4
            need = panel.winfo_reqheight()
            cap = int(wh * PANEL_MAX_FRACTION)
            if need > cap:
                self._scroll_body(cap - self._s(44))
                need = cap
            panel.place(x=right, y=top, anchor="ne", width=width, height=need)
        except tk.TclError:
            pass

    def _scroll_body(self, height):
        """Past 60 % of the window the rows live in a scrolling frame (the kit's make_scrollable: a thin bar)."""
        body = self._body
        try:
            from .screens.base import make_scrollable
            outer, inner = make_scrollable(body.master, self.theme)
        except Exception:  # noqa: BLE001
            return
        body.pack_forget()
        outer.pack(fill="both", expand=True, padx=self._s(12), pady=(0, self._s(10)))
        outer.configure(height=height)
        try:
            outer.pack_propagate(False)
        except tk.TclError:
            pass
        self._body = inner
        self._rows = []
        self._render_rows()

    def _render_rows(self):
        body = getattr(self, "_body", None)
        if body is None or self.panel is None:
            return
        focused_key = None
        try:
            f = self.win.focus_get()
            if f is not None and getattr(f, "_activity_key", None) is not None:
                focused_key = f._activity_key
        except (tk.TclError, KeyError):
            pass
        for child in list(body.winfo_children()):
            try:
                child.destroy()
            except tk.TclError:
                pass
        self._rows = []
        snap = self._snapshot
        self._section(body, "NOW", "Nothing is running.", snap.get("now") or [], now=True)
        self._section(body, "NEXT", "Nothing is queued.", snap.get("next") or [], now=False)
        if focused_key is not None:
            for row in self._rows:
                if getattr(row, "_activity_key", None) == focused_key:
                    try:
                        row.focus_set()
                    except tk.TclError:
                        pass
                    break
        if self.panel is not None and self.panel.winfo_manager():
            self._place()

    def _section(self, body, label, empty_text, items, now):
        t = self._t
        theme = self.theme
        bg = t["bg"] if t else theme.bg
        value = t["value"] if t else theme.dim
        dim = t["dim"] if t else theme.dim
        bright = t["bright"] if t else theme.bright
        s = self._s
        f9 = C9 if _KIT else theme_mod.FONT_CAPTION
        f9b = C9B if _KIT else theme_mod.FONT_CAPTION
        tk.Label(body, text=label, bg=bg, fg=value, font=f9b, bd=0, padx=0, pady=0, anchor="w").pack(fill="x", pady=(s(6), s(2)))
        tk.Frame(body, bg=dim, height=1).pack(fill="x")
        if not items:
            tk.Label(body, text=empty_text, bg=bg, fg=value, font=f9, bd=0, padx=0, pady=0, anchor="w").pack(fill="x", pady=(s(4), 0))
            return
        for n, item in enumerate(items, start=1):
            row = tk.Frame(body, bg=bg, highlightthickness=1, highlightbackground=bg, highlightcolor=bright, takefocus=1)
            row.pack(fill="x", pady=(s(4), 0))
            row._activity_key = (label, item.get("kind"), item.get("conv_id"), item.get("index"), n)
            row._activity_item = item
            row._activity_now = now
            row.bind("<FocusIn>", lambda e, r=row: r.configure(highlightbackground=bright))
            row.bind("<FocusOut>", lambda e, r=row: r.configure(highlightbackground=bg))
            row.bind("<Up>", lambda e: (self._move_focus(-1), "break")[1])
            row.bind("<Down>", lambda e: (self._move_focus(1), "break")[1])
            row.bind("<Escape>", lambda e: (self.close(), "break")[1])
            row.bind("<Return>", lambda e, r=row: (self._activate(r), "break")[1])
            if not now:
                row.bind("<Delete>", lambda e, r=row: (self._cancel(r), "break")[1])
            top = tk.Frame(row, bg=bg)
            top.pack(fill="x")
            clickable = item.get("kind") not in ("load", "model") and item.get("conv_id") is not None
            if not now:
                tk.Label(top, text="%d." % n, bg=bg, fg=value, font=f9, bd=0, padx=0, pady=0).pack(side="left", padx=(0, s(6)))
            title_text = str(item.get("title") or ("MODEL" if item.get("kind") in ("load", "model") else "New chat"))
            if _KIT:
                title = StatusLine(top, self.theme, text=title_text, bg=bg, fg=bright, font=f9b, bd=0, padx=0, pady=0)
            else:
                title = tk.Label(top, text=title_text, bg=bg, fg=bright, font=f9b, bd=0, padx=0, pady=0, anchor="w")
            title.pack(side="left", fill="x", expand=True)
            if now:
                stage = str(item.get("stage") or "").upper()
                if item.get("kind") == "image":
                    stage = stage if stage.startswith("IMAGE") else ("IMAGE · " + stage)
                tk.Label(top, text=stage, bg=bg, fg=value, font=f9, bd=0, padx=0, pady=0).pack(side="right", padx=(s(8), 0))
            else:
                if _KIT:
                    cancel = TextAction(top, self.theme, text="Cancel", upper=True, ground=bg, ink=value,
                                        command=lambda r=row: self._cancel(r))
                    danger = t["danger"]
                    cancel.bind("<Enter>", lambda e, c=cancel, d=danger: c.configure(fg=d), add="+")
                    cancel.bind("<Leave>", lambda e, c=cancel, v=value: c.configure(fg=v), add="+")
                else:
                    cancel = tk.Label(top, text="CANCEL", bg=bg, fg=value, font=f9, cursor="hand2")
                    cancel.bind("<Button-1>", lambda e, r=row: self._cancel(r))
                cancel.pack(side="right", padx=(s(8), 0))
                preview = "REGENERATE" if item.get("kind") == "regenerate" else _one_line(item.get("preview"), PREVIEW_CHARS)
                tk.Label(row, text=preview, bg=bg, fg=value, font=f9, bd=0, padx=0, pady=0, anchor="w",
                         justify="left").pack(fill="x", padx=(s(18), 0))
            if clickable:
                for w in (row, top, title):
                    w.bind("<Button-1>", lambda e, r=row: (self._activate(r), "break")[1], add="+")
                    try:
                        w.configure(cursor="hand2")
                    except tk.TclError:
                        pass
            self._rows.append(row)

    # ---------------------------------------------------------------- actions
    def _move_focus(self, step):
        rows = [r for r in self._rows if r.winfo_exists()]
        if not rows:
            return
        try:
            cur = self.win.focus_get()
        except (tk.TclError, KeyError):
            cur = None
        i = rows.index(cur) if cur in rows else (0 if step > 0 else len(rows) - 1)
        if cur in rows:
            i = max(0, min(len(rows) - 1, i + step))
        rows[i].focus_set()

    def _activate(self, row):
        item = getattr(row, "_activity_item", None) or {}
        conv_id = item.get("conv_id")
        if item.get("kind") in ("load", "model") or conv_id is None:
            return
        self.close()
        try:
            self.win.open_chat(conv_id)
        except Exception:  # noqa: BLE001
            pass

    def _cancel(self, row):
        item = getattr(row, "_activity_item", None) or {}
        if getattr(row, "_activity_now", False):
            return
        try:
            self.win.cancel_queued(item.get("conv_id"), item.get("index", 0))
        except Exception:  # noqa: BLE001
            pass
        self.refresh()

    def _on_click_anywhere(self, event):
        """A click outside the panel (and not on the control's word) closes it; the click itself goes on to its target."""
        if self.panel is None:
            return None
        w = event.widget
        words = set(self._words.values())
        try:
            while w is not None:
                if w is self.panel or w in words:
                    return None
                w = w.master
        except (tk.TclError, AttributeError):
            return None
        self.close()
        return None

    def close(self):
        panel, self.panel = self.panel, None
        if panel is None:
            return
        try:
            panel.destroy()
        except tk.TclError:
            pass
        self._rows = []
        before, self._focus_before = self._focus_before, None
        if before is not None:
            try:
                if before.winfo_exists():
                    before.focus_set()
            except (tk.TclError, AttributeError):
                pass

    def destroy(self):
        self.close()
        try:
            self.win.remove_activity_listener(self.refresh)
        except Exception:  # noqa: BLE001
            pass
