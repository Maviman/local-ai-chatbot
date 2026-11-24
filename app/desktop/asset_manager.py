"""Shared list/upload/download/delete panel for chat models and vision
(mmproj) files - the two asset kinds share an (almost) identical API
shape (GET list, POST active, POST upload, POST download, GET
download-progress, DELETE), see app/server.py's model-management and
vision (mmproj) handlers.

B8 look (BUILD_PLAN §4 B8; the approved Settings grammar): the colours are the RUNNING window's theme tokens (ctx.theme,
or the Settings screen's found up the widget tree for a panel built without ctx; the module's own theme only for a
panel with neither), no longer module constants read at import. The file list is the flat Treeview in a kit Well; the
actions are kit buttons shown upper-case (cget("text") is the text given, as the Data pane's): SET ACTIVE / TURN OFF,
DELETE (danger_outline, the stop edge), UPLOAD A .GGUF FILE..., DOWNLOAD; the URL and file name sit in Wells; labels
C11 bright, the status lines C9 value; the download progress is a thin flat bar, bright on unlit. The workers and the
poll / apply functions are unchanged (COORD-min)."""

import os
import queue
import threading
import tkinter as tk
import urllib.parse
from tkinter import filedialog, ttk

from .api_client import ApiError
from .async_call import Latest, run_async
from . import theme as theme_mod
from .model_load_overlay import RESTART_WAIT_SECONDS, ModelLoadOverlay
from .screens.base import show_inline_confirm, show_toast

try:  # B8: the kit is the look; a guarded import, so a broken kit still leaves a working panel (plain Tk below)
    from .kit import C9, C11, px as _px, tokens as _tokens
    from .kit.settings import ActionButton, meter_style
    from .kit.widgets import TextAction, Well
    _KIT = True
except Exception:  # noqa: BLE001
    _KIT = False

# The theme of a panel that has neither ctx nor a Settings screen above it (a bare test root); the toasts below keep
# using it, unchanged. Everything the panel paints comes from _live_theme() instead.
_T = theme_mod.load_theme()


def _live_theme(parent, ctx):
    """The running window's theme: ctx.theme, else the first ctx.theme up the widget tree (the image-model panel is
    built without ctx inside the Settings screen), else the module's _T."""
    theme = getattr(ctx, "theme", None)
    w = parent
    while theme is None and w is not None:
        theme = getattr(getattr(w, "ctx", None), "theme", None)
        w = getattr(w, "master", None)
    return theme or _T


def _plain_tokens(theme):
    return {"bg": theme.bg, "panel": theme.panel, "bright": theme.bright, "value": theme.text, "dim": theme.dim,
            "danger": theme.danger, "unlit": theme.panel}

# Placeholder shown in busy_label while a list request is in flight. Compared
# against verbatim in _poll_refresh_queue to decide whether clearing the label
# would stomp on a more important message - see refresh().
LOADING_TEXT = "Loading list..."
# The one row an EMPTY list shows (never a file: _selected_filename ignores it). A fresh copy of the app carries no
# models\ (Make Clean Copy / the update zip), and the list used to say nothing at all - the first real-PC report.
EMPTY_IID = "__no_files__"


def format_bytes(n):
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{n / 1024 / 1024 / 1024:.2f} GB"


def _row_label(m):
    label = m["filename"] + ("  (active)" if m.get("active") else "")
    # Only the chat-model list reports this; mmproj/image assets have no
    # thinking capability and simply omit the key.
    if m.get("thinking"):
        label += "  [thinks]"
    return label


class AssetManagerPanel(tk.Frame):
    def __init__(
        self, parent, api, kind, file_label, file_ext, toggleable=False, on_active_changed=None,
        eager_reload=True, active_label="Set active", ctx=None,
    ):
        self._theme = _live_theme(parent, ctx)
        t = self._t = _tokens(self._theme) if _KIT else _plain_tokens(self._theme)
        super().__init__(parent, bg=t["bg"])
        self.api = api
        self.kind = kind
        self.file_label = file_label
        self.file_ext = file_ext
        self.toggleable = toggleable
        self.on_active_changed = on_active_changed
        self.active_label = active_label
        # ctx (optional - only the models/mmproj panels pass it) is used
        # purely to show the blocking model-load overlay for eager_reload
        # switches - see _on_set_active(). Kept optional rather than
        # required so this class doesn't need a router context just to list/
        # upload/delete files, only to lock navigation during a real restart.
        self.ctx = ctx
        # True for chat models/mmproj: "Set active" actually restarts
        # llama-server with the new file right now, so it's real to show
        # "Loading model..." and poll /api/health() until it comes back.
        # False for image models: POST .../active just persists which file
        # to use *next time* image generation is actually triggered (see
        # find_image_model()'s lazy fallback) - nothing loads immediately,
        # so pretending otherwise would show a meaningless "loading" wait
        # that resolves instantly since the (unrelated) chat model's health
        # was never affected in the first place.
        self.eager_reload = eager_reload
        self._active_filename = None
        self._download_poll_id = None
        # One progress reading in flight at a time, off the Tk thread (see _poll_download_progress); invalidated in destroy().
        self._download_progress = Latest(self)
        self._load_overlay = None
        # Bumped by every refresh() so an older in-flight list response can't
        # overwrite a newer one - refresh() is fired from six different places
        # (construction, delete, upload, download, both set-active paths) and
        # now that it's asynchronous they can genuinely overlap.
        self._refresh_seq = 0

        # B8: the file list is the flat Treeview (kit Phosphor.Treeview) in a Well (panel, 1 px dim edge, 2 px bright
        # while the list has the focus), on the content column of the pane (no extra side padding)
        list_well = self._well(self)
        list_well.pack(fill="both", expand=True, pady=(0, self._px(10)))
        # the empty-list guidance: its own line under the list, as wide as the panel (wraps to it at the 820 px
        # floor: never cut), packed only while the list is empty
        self.empty_label = self._status(self, wraplength=self._px(560))
        self.bind("<Configure>", lambda e: self.empty_label.configure(wraplength=max(self._px(120), e.width - self._px(4))),
                  add="+")
        self.tree = ttk.Treeview(
            list_well.body, columns=("size",), show="tree headings", height=6, selectmode="browse",
            style=self._tree_style(),
        )
        self.tree.heading("#0", text="File")
        self.tree.heading("size", text="Size")
        self.tree.column("size", width=90, anchor="e")
        self.tree.pack(fill="both", expand=True)
        list_well.attach(self.tree)
        self.tree.kit_well = list_well
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_action_buttons())

        btn_row = self._btn_row = tk.Frame(self, bg=t["bg"])
        btn_row.pack(fill="x")
        self.active_btn = self._button(btn_row, self.active_label, self._on_set_active)
        self.active_btn.pack(side="left")
        self.delete_btn = self._button(btn_row, "Delete", self._on_delete, kind="danger_outline")
        self.delete_btn.pack(side="left", padx=(self._px(12), 0))
        self.restart_btn = None
        if self.kind == "models" and self.ctx is not None and _KIT:
            # A2 (bug-fix batch 30): RESTART next to Set active - the chat model server started again as it is
            self.restart_btn = TextAction(btn_row, self._theme, text="Restart", upper=True, ground=t["bg"], command=self._on_restart)
            self.restart_btn.pack(side="left", padx=(self._px(12), 0))
        self.busy_label = self._status(btn_row, wraplength=self._px(420), justify="right", anchor="e")
        self.busy_label.pack(side="right")

        upload_row = tk.Frame(self, bg=t["bg"])
        upload_row.pack(fill="x", pady=(self._px(10), 0))
        self.upload_btn = self._button(upload_row, f"Upload a {file_ext} file...", self._on_upload)
        self.upload_btn.pack(side="left")
        self.upload_progress_label = self._status(upload_row, wraplength=self._px(480))
        self.upload_progress_label.pack(side="left", padx=(self._px(12), 0))

        self.url_label = self._field_label(self, "Download from a URL")
        self.url_label.pack(fill="x", pady=(self._px(26), self._px(4)))
        self.url_var = tk.StringVar()
        self.url_entry = url_entry = self._entry(self, self.url_var)
        url_entry.kit_well.pack(fill="x")
        url_entry.bind("<FocusOut>", lambda e: self._maybe_autofill_filename(), add="+")

        filename_row = tk.Frame(self, bg=t["bg"])
        filename_row.pack(fill="x", pady=(self._px(10), 0))
        self.filename_label = self._field_label(filename_row, f"Save as (must end in {file_ext})")
        self.filename_label.pack(side="left")
        self.filename_var = tk.StringVar()
        self.filename_entry = self._entry(filename_row, self.filename_var, width=30)
        self.filename_entry.kit_well.pack(side="right")

        self.download_btn = self._button(self, "Download", self._on_download)
        self.download_btn.pack(anchor="w", pady=(self._px(10), self._px(6)))

        # a thin flat bar, bright on unlit (the kit meter style in bright ink: clam's own bar draws no fill when thin)
        bar_style = meter_style(self, self._theme, ink="bright") if _KIT else "Horizontal.TProgressbar"
        self.progress_bar = ttk.Progressbar(self, orient="horizontal", mode="determinate", maximum=100, style=bar_style)
        self.progress_label = self._status(self, wraplength=self._px(560))
        # progress widgets are packed/unpacked on demand (hidden until a download starts)

        self.refresh()

    # ---- look (B8): the kit pieces, or plain Tk when the kit cannot be imported ----

    def _px(self, n):
        return _px(self, n) if _KIT else int(n)

    def _well(self, parent):
        if _KIT:
            return Well(parent, self._theme, ground=self._t["bg"])
        well = tk.Frame(parent, bg=self._t["dim"])
        well.body = tk.Frame(well, bg=self._t["panel"])
        well.body.pack(fill="both", expand=True, padx=1, pady=1)
        well.attach = lambda w: w
        return well

    def _tree_style(self):
        """The flat file list inside its Well: the kit's Phosphor.Treeview (rows, heading and selection inherited),
        without a field border of its own - the Well is the edge (clam drew a second 1 px dim box inside it)."""
        t = self._t
        try:
            ttk.Style(self).configure("Well.Phosphor.Treeview", bordercolor=t["panel"], lightcolor=t["panel"],
                                      darkcolor=t["panel"], borderwidth=0)
            return "Well.Phosphor.Treeview"
        except tk.TclError:
            return "Phosphor.Treeview"

    def _button(self, parent, text, command, kind="secondary"):
        """A kit button shown upper-case whose cget("text") is the text given (ActionButton, as the Data pane's)."""
        t = self._t
        if _KIT:
            return ActionButton(parent, self._theme, text=text, kind=kind, command=command, ground=t["bg"],
                                height=self._px(28))
        danger = kind.startswith("danger")
        return tk.Button(parent, text=text, command=command, relief="flat", padx=10, bg=t["bg"],
                         fg=t["danger"] if danger else t["value"], activebackground=t["panel"],
                         activeforeground=t["danger"] if danger else t["bright"])

    def _status(self, parent, **kw):
        """A status line (C9 value): Loading list... / Uploading... / the download progress."""
        t = self._t
        kw.setdefault("anchor", "w")
        kw.setdefault("justify", "left")
        return tk.Label(parent, text="", bg=t["bg"], fg=t["value"], font=C9 if _KIT else theme_mod.FONT_CAPTION,
                        bd=0, padx=0, pady=0, **kw)

    def _field_label(self, parent, text):
        """A field label (C11 bright, as the Settings screen's _label)."""
        t = self._t
        return tk.Label(parent, text=text, bg=t["bg"], fg=t["bright"], anchor="w", bd=0, padx=0, pady=0,
                        font=C11 if _KIT else theme_mod.FONT_BODY)

    def _entry(self, parent, textvariable, **kwargs):
        """A tk.Entry (C11 bright on panel) inside a Well made in `parent`; the caller packs entry.kit_well."""
        t = self._t
        well = self._well(parent)
        pad = tk.Frame(well.body, bg=t["panel"])
        pad.pack(fill="both", expand=True, padx=self._px(7), pady=self._px(3))
        entry = tk.Entry(pad, textvariable=textvariable, bg=t["panel"], fg=t["bright"], insertbackground=t["bright"],
                         relief="flat", bd=0, highlightthickness=0, font=C11 if _KIT else theme_mod.FONT_BODY,
                         **kwargs)
        entry.pack(fill="x")
        well.attach(entry)
        entry.kit_well = well
        return entry

    # ---- data ----

    def refresh(self):
        """Reload the file list off the main thread.

        This used to call self.api.list_assets() synchronously, right here,
        and __init__ calls it - so a settings panel could only be built as
        fast as the backend answered. That broke the app outright twice (see
        the tracebacks in data/desktop.log): at launch, with llama-server
        still coming up, the request timed out, MainWindow.__init__ ->
        _register_screens -> SettingsScreen -> here raised, and the window
        never opened at all. api_client no longer leaks a bare TimeoutError
        past ApiError, which fixes the crash; this fixes the reason a
        construction-time network call was dangerous in the first place, by
        using the same worker-thread + queue + after() poll every other
        network call in this codebase uses (see home_screen._refresh_status
        and chat_view._refresh_max_parallel_chats_cache, which spell out that
        blocking the Tk main thread is a real hazard). A slow or dead backend
        now degrades to an empty list plus a status line instead of taking
        startup down with it.
        """
        self._refresh_seq += 1
        seq = self._refresh_seq
        if not self.busy_label.cget("text"):
            # busy_label is shared with the model-switch flow, which posts
            # messages that matter more than this one ("Loading model...").
            # Only claim it when it's idle, and below, only clear it if this
            # placeholder is still the thing showing.
            self.busy_label.config(text=LOADING_TEXT)
        q = queue.Queue()
        threading.Thread(target=self._refresh_worker, args=(self.kind, q), daemon=True).start()
        self.after(50, self._poll_refresh_queue, q, seq)

    def _refresh_worker(self, kind, q):
        # Worker thread: only ever q.put(...), never touch Tk directly (see
        # the note on _on_set_active for why). `kind` is passed in rather
        # than read off self so this can't race a future kind reassignment.
        # Catches Exception, not just ApiError, so the queue is guaranteed
        # exactly one item - anything else (a malformed body reaching
        # json.loads, say) would otherwise leave the poll below rescheduling
        # itself every 50ms forever against a queue nothing will ever fill.
        try:
            q.put(("ok", self.api.list_assets(kind)))
        except ApiError as e:
            q.put(("error", str(e)))
        except Exception as e:
            q.put(("error", str(e)))

    def _poll_refresh_queue(self, q, seq):
        try:
            status, payload = q.get_nowait()
        except queue.Empty:
            self.after(50, self._poll_refresh_queue, q, seq)
            return
        if seq != self._refresh_seq:
            return  # a newer refresh() started while this one was in flight
        if status == "error":
            self.busy_label.config(text=f"Could not load list: {payload}")
            return
        try:
            rows = [(m["filename"], _row_label(m), format_bytes(m["size_bytes"]))
                    for m in payload.get("models", [])]
        except (KeyError, TypeError, AttributeError) as e:
            # Every row is built before a single widget is touched, so a
            # payload in an unexpected shape leaves whatever is already
            # listed on screen alone and says so. Our own server.py always
            # sends filename/size_bytes for all three asset kinds, so this
            # is belt-and-braces - but the failure mode it replaces (found
            # by feeding this panel a malformed body) was the tree getting
            # cleared and then a KeyError aborting the loop half way, i.e. a
            # silently empty list with no explanation anywhere.
            self.busy_label.config(text=f"Could not load list: unexpected response ({e})")
            return
        if self.busy_label.cget("text") == LOADING_TEXT:
            self.busy_label.config(text="")
        self._active_filename = payload.get("active")
        self.tree.delete(*self.tree.get_children())
        for filename, label, size in rows:
            self.tree.insert("", "end", iid=filename, text=label, values=(size,))
        if rows:
            self.empty_label.pack_forget()
        else:
            # nothing to list: say so IN the list, and say where a file goes (the server names its folder) - a blank
            # box was the first thing a freshly copied PC showed, with no way to know why
            folder = payload.get("dir") or "the models folder next to the app"
            # "vision file" + " files" read "No vision file files found" on the fresh-copy run: one noun, pluralised
            noun = self.file_label[:-5] + " files" if self.file_label.endswith(" file") else self.file_label + " files"
            self.tree.insert("", "end", iid=EMPTY_IID, text=f"No {noun} found", values=("",))
            self.empty_label.config(text=f"Copy a {self.file_ext} file into {folder}, or use Upload / Download below. "
                                         "The list refreshes each time Settings opens.")
            if not self.empty_label.winfo_manager():
                self.empty_label.pack(fill="x", pady=(0, self._px(10)), before=self._btn_row)
        self._update_action_buttons()

    def _selected_filename(self):
        sel = self.tree.selection()
        return sel[0] if sel and sel[0] != EMPTY_IID else None

    def _update_action_buttons(self):
        filename = self._selected_filename()
        is_active = filename is not None and filename == self._active_filename
        if self.toggleable and is_active:
            self.active_btn.config(text="Turn off")
        else:
            self.active_btn.config(text=self.active_label)

    # ---- actions ----

    def _on_set_active(self):
        # Called from the main thread (button command=), so scheduling
        # self.after(...) here is safe. The worker thread below must never
        # touch any Tk widget/method directly - it only puts onto a queue,
        # which the main-thread poll loop below drains. Calling self.after()
        # from a background thread is a real tkinter hazard (undefined
        # behavior / RuntimeError depending on interpreter state), not just
        # a style preference - found by testing this exact path.
        filename = self._selected_filename()
        turning_off = self.toggleable and filename == self._active_filename
        target = "" if turning_off else filename
        if target is None:
            return
        self.busy_label.config(text="Loading model..." if self.eager_reload else "Setting as primary...")
        self.active_btn.config(state="disabled")
        if self.eager_reload and self.ctx is not None:
            # Locks the whole app until llama-server finishes restarting -
            # see model_load_overlay.py for why (navigating away or closing
            # the app mid-load has hung the whole machine, not just the GUI).
            label = f"Turning off {self.file_label}..." if turning_off else f"Loading {self.file_label}: {target}"
            self._load_overlay = ModelLoadOverlay(self, self.ctx, label)
        self._switch_queue = queue.Queue()
        threading.Thread(target=self._set_active_worker, args=(target, self._switch_queue), daemon=True).start()
        self.after(50, self._poll_switch_queue)

    def _on_restart(self):
        """A2: the shared restart flow (model_server_control): the server's refusal as a notice, else the load overlay
        and the health poll; the list and the status line follow."""
        from .model_server_control import restart_model_server   # noqa: PLC0415 - a late import: no cycle at import time
        self.busy_label.config(text="Restarting the model server...")
        self.active_btn.config(state="disabled")

        def done(ok):
            try:
                self.busy_label.config(text="")
                self.active_btn.config(state="normal")
                self.refresh()
            except tk.TclError:
                return
            if ok and self.on_active_changed:
                self.on_active_changed()

        restart_model_server(self, self.ctx, self.api, on_done=done)

    def _finish_load_overlay(self):
        if self._load_overlay is not None:
            self._load_overlay.finish()
            self._load_overlay = None

    def _set_active_worker(self, filename, q):
        try:
            result = self.api.set_active_asset(self.kind, filename)
            q.put(("ok", result))
        except ApiError as e:
            q.put(("error", str(e)))

    def _poll_switch_queue(self):
        try:
            kind, payload = self._switch_queue.get_nowait()
        except queue.Empty:
            self.after(50, self._poll_switch_queue)
            return
        self.active_btn.config(state="normal")
        if kind == "error":
            self.busy_label.config(text="")
            self._finish_load_overlay()
            show_toast(self, _T, f"Could not switch: {payload}", kind="error")
            return
        if not self.eager_reload:
            # Nothing actually loads until this file is next used - see
            # eager_reload's own comment - so there's nothing to wait for.
            self.busy_label.config(text="")
            self.refresh()
            if self.on_active_changed:
                self.on_active_changed()
            return
        self.busy_label.config(text="Waiting for the model to finish loading...")
        self._poll_switch_health()
        if payload and payload.get("mmproj_cleared"):
            show_toast(self, _T, "Vision file turned off — it didn't match the new model.", kind="warning")

    def _poll_switch_health(self, attempt=0):
        # self.api.health() is a real HTTP round trip (up to its own 2s
        # timeout, and slower still if llama-server is busy loading a large
        # model and slow to answer) - calling it directly here on the main
        # thread the way this used to would freeze the whole GUI for that
        # duration on every single poll while a model loads, same class of
        # bug _vram_estimate_worker below was already fixed for. Confirmed
        # live: fast scrolling elsewhere in the app during a model load would
        # queue up input that only got processed in a burst once one of
        # these blocking calls finally returned, which read as a freeze.
        q = queue.Queue()
        threading.Thread(target=self._health_worker, args=(q,), daemon=True).start()
        self.after(50, lambda: self._poll_switch_health_result(q, attempt))

    def _health_worker(self, q):
        try:
            q.put(self.api.health())
        except ApiError:
            q.put({"status": "starting"})

    def _poll_switch_health_result(self, q, attempt):
        try:
            health = q.get_nowait()
        except queue.Empty:
            self.after(50, lambda: self._poll_switch_health_result(q, attempt))
            return
        status = health.get("status")
        if status == "ok":
            self.busy_label.config(text="")
            self._finish_load_overlay()
            self.refresh()
            if self.on_active_changed:
                self.on_active_changed()
            return
        if status == "error":
            self.busy_label.config(text="")
            self._finish_load_overlay()
            show_toast(self, _T, "The model server failed to start: " + health.get("error", ""), kind="error")
            self.refresh()
            return
        if attempt > RESTART_WAIT_SECONDS:
            self.busy_label.config(text="")
            self._finish_load_overlay()
            show_toast(self, _T, "Still waiting for the model to load.", kind="warning")
            return
        self.after(1000, lambda: self._poll_switch_health(attempt + 1))

    def _on_delete(self):
        filename = self._selected_filename()
        if not filename:
            return
        show_inline_confirm(
            self, _T, f'Delete {self.file_label} file "{filename}"?',
            on_confirm=lambda: self._do_delete(filename), confirm_label="Delete", danger=True,
            title=f"Delete {self.file_label} file",      # B8 (B3 grammar): the title says what is deleted
        )

    def _do_delete(self, filename):
        # Off the Tk thread (async_call): this used to call the backend right here, and a delete while the backend was busy (a
        # model loading from a hard disk) froze the whole window until it answered. The button is held down meanwhile.
        self.delete_btn.config(state="disabled")

        def done(_result):
            self.delete_btn.config(state="normal")
            self.refresh()

        def failed(e):
            self.delete_btn.config(state="normal")
            show_toast(self, _T, f"Could not delete: {e}", kind="error")

        run_async(self, lambda: self.api.delete_asset(self.kind, filename), done, failed)

    def _on_upload(self):
        path = filedialog.askopenfilename(
            title=f"Choose a {self.file_ext} file", filetypes=[(self.file_ext.upper(), f"*{self.file_ext}")]
        )
        if not path:
            return
        self.upload_progress_label.config(text="Starting upload...")
        self._upload_queue = queue.Queue()
        threading.Thread(target=self._upload_worker, args=(path, self._upload_queue), daemon=True).start()
        self.after(50, self._poll_upload_queue)

    def _upload_worker(self, path, q):
        # Worker thread: only ever q.put(...), never touch Tk directly (see
        # the note on _on_set_active for why).
        def progress(sent, total):
            q.put(("progress", (sent, total)))
        try:
            self.api.upload_asset(self.kind, path, on_progress=progress)
            q.put(("done", None))
        except ApiError as e:
            q.put(("error", str(e)))

    def _poll_upload_queue(self):
        try:
            while True:
                kind, payload = self._upload_queue.get_nowait()
                if kind == "progress":
                    sent, total = payload
                    self.upload_progress_label.config(
                        text=f"Uploading... {format_bytes(sent)} / {format_bytes(total)}"
                    )
                elif kind == "error":
                    self.upload_progress_label.config(text=f"Upload failed: {payload}")
                    return
                elif kind == "done":
                    self.upload_progress_label.config(text="Upload complete.")
                    self.refresh()
                    return
        except queue.Empty:
            pass
        self.after(50, self._poll_upload_queue)

    def _maybe_autofill_filename(self):
        if self.filename_var.get().strip():
            return
        url = self.url_var.get().strip()
        if not url:
            return
        name = os.path.basename(urllib.parse.urlsplit(url).path) or "download"
        if not name.lower().endswith(self.file_ext):
            name += self.file_ext
        self.filename_var.set(name)

    def _on_download(self):
        url = self.url_var.get().strip()
        filename = self.filename_var.get().strip()
        if not url:
            show_toast(self, _T, "Enter a URL first.", kind="warning")
            return
        if not filename:
            show_toast(self, _T, "Enter a filename to save as.", kind="warning")
            return
        if not filename.lower().endswith(self.file_ext):
            filename += self.file_ext
            self.filename_var.set(filename)
        # The start request and every progress reading run off the Tk thread (async_call): they used to be made right here and in
        # the 1 s poll, so the window hitched once a second for the whole download and froze when the backend was busy.
        self.download_btn.config(state="disabled")

        def started(_result):
            self.progress_bar.pack(fill="x", padx=12, pady=(0, 2))
            self.progress_label.pack(fill="x", padx=12, pady=(0, 8))
            self.progress_bar["value"] = 0
            self.progress_label.config(text="Starting...")
            self._poll_download_progress(filename)

        def failed(e):
            self.download_btn.config(state="normal")
            show_toast(self, _T, f"Could not start download: {e}", kind="error")

        run_async(self, lambda: self.api.download_asset(self.kind, url, filename), started, failed)

    def _poll_download_progress(self, filename):
        """One progress reading off the Tk thread - a Latest slot, so at most one request is in flight and a stale answer is dropped -
        then _apply_download_progress, which schedules the next reading a second later. A slow backend therefore means fewer
        readings, never a frozen window."""
        self._download_poll_id = None
        self._download_progress.start(
            lambda: self.api.download_progress(self.kind, filename),
            lambda progress: self._apply_download_progress(filename, progress),
            self._download_failed,
        )

    def _download_failed(self, e):
        # ApiError or anything else (a TimeoutError from a backend that stopped answering): the text, and the button back.
        self.progress_label.config(text=f"Error: {e}")
        self.download_btn.config(state="normal")

    def _apply_download_progress(self, filename, progress):
        error = progress.get("error") if isinstance(progress, dict) else "unexpected answer"
        if error:
            self._download_failed(error)
            return
        downloaded, total = progress.get("downloaded", 0), progress.get("total", 0)
        pct = (downloaded / total * 100) if total else 0
        self.progress_bar["value"] = pct
        if progress.get("done"):
            self.progress_label.config(text=f"{format_bytes(downloaded)} — done")
            self.download_btn.config(state="normal")
            self.refresh()
            return
        if total:
            self.progress_label.config(text=f"{format_bytes(downloaded)} / {format_bytes(total)} ({pct:.0f}%)")
        else:
            self.progress_label.config(text=f"{format_bytes(downloaded)} downloaded...")
        self._download_poll_id = self.after(1000, lambda: self._poll_download_progress(filename))

    def destroy(self):
        # Leaving Settings in the middle of a download: the progress poll used to go on, making Tk-thread HTTP calls against a
        # panel that was gone. The pending tick is cancelled and a reading still in flight is dropped on arrival.
        if self._download_poll_id is not None:
            try:
                self.after_cancel(self._download_poll_id)
            except tk.TclError:
                pass
            self._download_poll_id = None
        self._download_progress.invalidate()
        super().destroy()
