"""Images: make a picture from a prompt, outside any chat (2026-10-14; the first, small version).

Left: the model that will be used, prompt, negative prompt, size, steps, guidance, seed, the backend switch (the local image server or ComfyUI), GENERATE and STOP.
Right: the picked picture (OPEN, SAVE AS, DOWNLOAD, REUSE SETTINGS, DELETE) and, under it, the gallery of everything made here (outputs\\generated).
ComfyUI itself is still set up on the Image uplink screen (COMFYUI SETUP...), which is unchanged.

The server does the work (POST /api/images/generate) and shares the chat's image lock and status, so this screen only starts a job, polls
/api/generate-image/status and reloads the gallery. Kept in four small parts so more can be added later without reshaping it: _build_controls,
_build_preview, _build_gallery and the job runner (_on_generate / _poll / _finished). The server ignores request keys it does not know, so a new
control only needs a field here and one line in _request_options().
"""

import base64
import tkinter as tk
from tkinter import filedialog

from .. import file_ops
from ..async_call import Latest, run_async
from ..attachment_grid import PhotoCache
from ..kit import C9, C10B, C11, px as _px, tokens as _tokens
from ..kit.widgets import KitButton, Segmented, TextAction
from .base import Screen, make_header, make_scrollable, show_inline_confirm, show_toast
from .image_uplink_screen import BACKENDS

STAGES = {
    "unloading_chat_model": "Freeing the graphics card (putting the chat model away)...",
    "loading_image_model": "Loading the image model (the first picture can take a few minutes)...",
    "generating": "Drawing the picture...",
    "restoring_chat_model": "Bringing the chat model back...",
}
THUMB = 112                     # gallery thumbnail edge, px at 100 %
PREVIEW = 400                   # preview edge, px at 100 %
COLUMNS = 6
GALLERY_MAX = 60                # thumbnails built (the folder keeps everything)
# per model kind: width, height, steps, guidance. A picture-editing model (Flux-style) wants a bigger picture and distilled guidance.
DEFAULTS = {"text2image": ("512", "512", "20", "7.0"), "edit": ("768", "768", "20", "3.5")}


class ImageGeneratorScreen(Screen):
    def __init__(self, parent, ctx):
        super().__init__(parent, ctx)
        self._t = _tokens(ctx.theme)
        self._photos = PhotoCache()
        self._loader = Latest(self, deliver_when=self.is_visible)
        self._images = []                   # the gallery list, newest first
        self._selected = None               # the picked file name
        self._running = False
        self._bytes = {}                    # name -> png bytes (a few, for OPEN / SAVE / DOWNLOAD / the preview)
        self._tiles = {}
        self.scrub_widgets = {}             # word -> (name label, entry): drag the label / wheel the entry changes the number
        self._defaults_for = None          # the model kind the size/steps/guidance boxes were last filled for
        self._touched = False               # the person changed one of those boxes
        self.header, _ = make_header(self, ctx.theme, "Images")
        body = tk.Frame(self, bg=self._t["bg"])
        body.pack(fill="both", expand=True, padx=(_px(self, 16), _px(self, 16)), pady=(_px(self, 8), _px(self, 8)))
        self.controls = tk.Frame(body, bg=self._t["bg"])
        self.controls.pack(side="left", fill="y", anchor="n")
        right = tk.Frame(body, bg=self._t["bg"])
        right.pack(side="left", fill="both", expand=True, padx=(_px(self, 24), 0))
        self._build_controls(self.controls)
        self._build_preview(right)
        self._build_gallery(right)

    # ---------------------------------------------------------------- small builders

    def _label(self, parent, text, ink="value", font=C9, **kw):
        return tk.Label(parent, text=text, bg=self._t["bg"], fg=self._t[ink], font=font, anchor="w", justify="left", padx=0, pady=0, bd=0, **kw)

    def _entry(self, parent, var, width):
        t = self._t
        e = tk.Entry(parent, textvariable=var, width=width, bg=t["panel"], fg=t["bright"], insertbackground=t["bright"], selectbackground=t["bright"],
                     selectforeground=t["bg"], relief="flat", bd=0, highlightthickness=1, highlightbackground=t["dim"], highlightcolor=t["bright"], font=C11)
        e.bind("<KeyRelease>", lambda _e: setattr(self, "_touched", True), add="+")
        return e

    # ---------------------------------------------------------------- drag / wheel to change a number
    # word -> (step, pixels of drag per step, lowest, highest, default when the box is empty, decimals). Ranges match the server's limits.
    SCRUB = {"WIDTH": (64, 14, 256, 1536, 512, 0), "HEIGHT": (64, 14, 256, 1536, 512, 0), "STEPS": (1, 7, 1, 100, 20, 0),
             "GUIDANCE": (0.1, 7, 0.0, 30.0, 7.0, 1), "SEED": (1, 3, 0, 999999999, 0, 0)}

    def _make_scrubbable(self, word, var, label, entry):
        step, per_px, lo, hi, default, dec = self.SCRUB[word]
        state = {}

        def current():
            try:
                return float(var.get())
            except ValueError:
                return float(default)

        def put(value):
            value = max(lo, min(hi, value))
            var.set(f"{value:.{dec}f}" if dec else str(int(round(value))))
            self._touched = True

        def press(e):
            state["x"], state["v"] = e.x_root, current()

        def drag(e):
            if "x" in state:
                put(round((state["v"] + (e.x_root - state["x"]) / per_px * step) / step) * step)

        def wheel(e):
            put(round((current() + (step if e.delta > 0 else -step)) / step) * step)
            return "break"
        label.configure(cursor="sb_h_double_arrow")
        label.bind("<ButtonPress-1>", press)
        label.bind("<B1-Motion>", drag)
        entry.bind("<MouseWheel>", wheel)

    # ---------------------------------------------------------------- controls

    def _build_controls(self, f):
        t = self._t
        theme = self.ctx.theme
        s = lambda n: _px(self, n)  # noqa: E731
        self._label(f, "MODEL").pack(anchor="w")
        self.model_var = tk.StringVar(value="...")
        self.model_label = self._label(f, "", ink="bright", font=C11, textvariable=self.model_var, wraplength=s(380))
        self.model_label.pack(anchor="w", pady=(s(3), 0))
        self._label(f, "Change it in CONFIG > image model.").pack(anchor="w")

        self._label(f, "PROMPT").pack(anchor="w", pady=(s(12), 0))
        self.prompt = tk.Text(f, width=44, height=4, wrap="word", bg=t["panel"], fg=t["bright"], insertbackground=t["bright"], selectbackground=t["bright"],
                              selectforeground=t["bg"], relief="flat", bd=0, highlightthickness=1, highlightbackground=t["dim"], highlightcolor=t["bright"],
                              font=C11, undo=True)
        self.prompt.pack(anchor="w", pady=(s(3), 0))
        self.prompt.bind("<Control-Return>", lambda _e: (self._on_generate(), "break")[1])

        self._label(f, "NOT WANTED (optional)").pack(anchor="w", pady=(s(10), 0))
        self.negative_var = tk.StringVar()
        self._entry(f, self.negative_var, 46).pack(anchor="w", pady=(s(3), 0))

        grid = tk.Frame(f, bg=t["bg"])
        grid.pack(anchor="w", pady=(s(12), 0))
        self.width_var, self.height_var = tk.StringVar(value="512"), tk.StringVar(value="512")
        self.steps_var, self.cfg_var, self.seed_var = tk.StringVar(value="20"), tk.StringVar(value="7.0"), tk.StringVar(value="")
        for col, (word, var, w) in enumerate((("WIDTH", self.width_var, 6), ("HEIGHT", self.height_var, 6), ("STEPS", self.steps_var, 5),
                                              ("GUIDANCE", self.cfg_var, 6), ("SEED", self.seed_var, 10))):
            lab = self._label(grid, word)
            lab.grid(row=0, column=col, sticky="w", padx=(0, s(10)))
            ent = self._entry(grid, var, w)
            ent.grid(row=1, column=col, sticky="w", padx=(0, s(10)), pady=(s(3), 0))
            self._make_scrubbable(word, var, lab, ent)
            self.scrub_widgets[word] = (lab, ent)
        self._label(f, "Drag a name (WIDTH, STEPS...) left or right to change its number, or roll the mouse wheel over a box. Seed empty = random.", wraplength=s(400)).pack(
            anchor="w", pady=(s(3), 0))

        self._label(f, "BACKEND").pack(anchor="w", pady=(s(12), 0))
        self.backend_var = tk.StringVar(value="local")
        self.backend_choice = Segmented(f, theme, options=BACKENDS, variable=self.backend_var, command=self._on_backend, ground=t["bg"], font=C10B)
        self.backend_choice.pack(anchor="w", pady=(s(3), 0))

        row = tk.Frame(f, bg=t["bg"])
        row.pack(anchor="w", pady=(s(14), 0))
        self.generate_btn = KitButton(row, theme, text="Generate", kind="primary", upper=True, ground=t["bg"], command=self._on_generate)
        self.generate_btn.pack(side="left")
        self.stop_btn = KitButton(row, theme, text="Stop", kind="danger_outline", upper=True, ground=t["bg"], command=self._on_stop)
        self.stop_btn.pack(side="left", padx=(s(10), 0))
        self.stop_btn.configure(state="disabled")
        self.comfyui_btn = TextAction(row, theme, text="ComfyUI setup...", command=lambda: self.ctx.go_to_screen("image_uplink"), upper=True,
                                      ground=t["bg"])
        self.comfyui_btn.pack(side="left", padx=(s(18), 0))

        self.status_var = tk.StringVar(value="")
        self.status_label = tk.Label(f, textvariable=self.status_var, bg=t["bg"], fg=t["value"], font=C9, anchor="w", justify="left", padx=0, pady=0, bd=0,
                                     wraplength=s(400))
        self.status_label.pack(anchor="w", pady=(s(10), 0))

    # ---------------------------------------------------------------- preview

    def _build_preview(self, f):
        t = self._t
        theme = self.ctx.theme
        s = lambda n: _px(self, n)  # noqa: E731
        box = tk.Frame(f, bg=t["panel"], width=s(PREVIEW), height=s(PREVIEW), highlightthickness=1, highlightbackground=t["dim"])
        box.pack(anchor="w")
        box.pack_propagate(False)
        self.preview = tk.Label(box, bg=t["panel"], fg=t["dim"], font=C11, text="Nothing picked yet.\nMake a picture, or click one below.")
        self.preview.pack(fill="both", expand=True)
        self.caption_var = tk.StringVar(value="")
        tk.Label(f, textvariable=self.caption_var, bg=t["bg"], fg=t["value"], font=C9, anchor="w", justify="left", wraplength=s(560), padx=0, pady=0,
                 bd=0).pack(anchor="w", pady=(s(6), 0))
        rows = (tk.Frame(f, bg=t["bg"]), tk.Frame(f, bg=t["bg"]))        # two rows: five buttons in one would not fit a small window
        rows[0].pack(anchor="w", pady=(s(6), 0))
        rows[1].pack(anchor="w", pady=(s(6), 0))
        self.action_btns = {}
        for key, word, command, line in (("open", "Open", self._on_open, 0), ("save", "Save as...", self._on_save_as, 0),
                                          ("download", "Download", self._on_download, 0), ("reuse", "Reuse settings", self._on_reuse, 1),
                                          ("delete", "Delete", self._on_delete, 1)):
            kind = "danger_outline" if key == "delete" else "secondary"
            b = KitButton(rows[line], theme, text=word, kind=kind, upper=True, ground=t["bg"], command=command)
            b.pack(side="left", padx=(0, s(8)))
            b.configure(state="disabled")
            self.action_btns[key] = b

    # ---------------------------------------------------------------- gallery

    def _build_gallery(self, f):
        t = self._t
        s = lambda n: _px(self, n)  # noqa: E731
        self._label(f, "GALLERY").pack(anchor="w", pady=(s(10), 0))
        self.gallery_note = self._label(f, "")
        self.gallery_note.pack(anchor="w")
        outer, inner = make_scrollable(f, self.ctx.theme)
        outer.pack(fill="both", expand=True, pady=(s(4), 0))
        self.gallery = inner

    def _fill_gallery(self):
        for w in self.gallery.winfo_children():
            w.destroy()
        self._tiles = {}
        t = self._t
        s = lambda n: _px(self, n)  # noqa: E731
        self.gallery_note.configure(text="" if self._images else "Nothing here yet.")
        for i, meta in enumerate(self._images[:GALLERY_MAX]):
            name = meta["name"]
            tile = tk.Label(self.gallery, bg=t["panel"], fg=t["dim"], width=THUMB // 8, height=THUMB // 16, text="...", font=C9, bd=0,
                            highlightthickness=2, highlightbackground=t["bg"], cursor="hand2")
            tile.grid(row=i // COLUMNS, column=i % COLUMNS, padx=(0, s(6)), pady=(0, s(6)))
            tile.bind("<Button-1>", lambda _e, n=name: self._select(n))
            self._tiles[name] = tile
            self._load_thumb(name)
        self._mark_selected()

    def _load_thumb(self, name):
        def done(data):
            self._bytes_put(name, data)
            tile = self._tiles.get(name)
            if tile is None:
                return
            photo = self._photos.get("t:" + name, base64.b64encode(data), _px(self, THUMB))
            if photo is not None:
                try:
                    tile.configure(image=photo, text="", width=photo.width(), height=photo.height())
                    tile.image = photo
                except tk.TclError:
                    pass
        run_async(self, lambda: self.ctx.api.get_gallery_image_bytes(name)[0], done, lambda _e: None)

    def _mark_selected(self):
        for name, tile in self._tiles.items():
            try:
                tile.configure(highlightbackground=self._t["bright"] if name == self._selected else self._t["bg"])
            except tk.TclError:
                pass

    def _bytes_put(self, name, data):
        self._bytes[name] = data
        while len(self._bytes) > 14:
            self._bytes.pop(next(iter(self._bytes)))

    # ---------------------------------------------------------------- showing / loading

    def legend_items(self):
        return [("ESC", "back")]

    def on_show(self):
        def settings_in(settings):
            backend = "comfyui" if settings.get("image_backend") == "comfyui" else "local"
            self.backend_var.set(backend)
        run_async(self, self.ctx.api.get_settings, settings_in, lambda _e: None)
        run_async(self, lambda: self.ctx.api.list_assets("image-models"), self._models_in, lambda e: self.model_var.set(f"could not read the models: {e}"))
        self._reload_gallery()
        run_async(self, self.ctx.api.image_gen_status, self._resume, lambda _e: None)

    def _resume(self, status):
        if status.get("active") and not self._running:
            self._running = True
            self._busy_buttons(True)
            self._poll()

    def _models_in(self, body):
        models = (body or {}).get("models") or []
        use = next((m for m in models if m.get("active")), None)
        if use is None:
            self.model_var.set("No usable image model. Add or choose one in CONFIG > image model.")
            return
        note = {"text2image": "makes pictures from text", "edit": "an editing model (slow for plain pictures)"}.get(use.get("kind"), "")
        self.model_var.set(use["filename"] + (f"  -  {note}" if note else ""))
        kind = use.get("kind") if use.get("kind") in DEFAULTS else "text2image"
        if self._defaults_for != kind and not self._touched:
            for var, val in zip((self.width_var, self.height_var, self.steps_var, self.cfg_var), DEFAULTS[kind]):
                var.set(val)
        self._defaults_for = kind

    def _reload_gallery(self, select_first=False):
        def done(body):
            self._images = list((body or {}).get("images") or [])
            if select_first and self._images:
                self._selected = self._images[0]["name"]
            if self._selected not in {m["name"] for m in self._images}:
                self._selected = None
            self._fill_gallery()
            self._show_selected()
        self._loader.start(self.ctx.api.gallery_list, done, lambda e: self.gallery_note.configure(text=f"could not read the gallery: {e}"))

    # ---------------------------------------------------------------- the picked picture

    def _meta(self, name=None):
        name = name or self._selected
        return next((m for m in self._images if m["name"] == name), None)

    def _select(self, name):
        self._selected = name
        self._mark_selected()
        self._show_selected()

    def _show_selected(self):
        meta = self._meta()
        for b in self.action_btns.values():
            b.configure(state="normal" if meta else "disabled")
        if meta is None:
            self.preview.configure(image="", text="Nothing picked yet.\nMake a picture, or click one below.")
            self.preview.image = None
            self.caption_var.set("")
            return
        bits = [meta.get("model"), f"{meta.get('width', '?')}x{meta.get('height', '?')}" if meta.get("width") else None,
                f"{int(meta['steps'])} steps" if meta.get("steps") else None, f"guidance {meta['cfg_scale']:g}" if meta.get("cfg_scale") is not None else None,
                f"seed {meta['seed']}" if meta.get("seed", -1) >= 0 else None, meta.get("created")]
        self.caption_var.set((meta.get("prompt") or "") + "\n" + "  |  ".join(str(b) for b in bits if b))
        name = meta["name"]

        def shown(data):
            if name != self._selected:
                return
            self._bytes_put(name, data)
            photo = self._photos.get("p:" + name, base64.b64encode(data), _px(self, PREVIEW) - 4)
            if photo is None:
                self.preview.configure(image="", text="This picture cannot be shown here - use OPEN.")
                return
            self.preview.configure(image=photo, text="")
            self.preview.image = photo
        if name in self._bytes:
            shown(self._bytes[name])
        else:
            run_async(self, lambda: self.ctx.api.get_gallery_image_bytes(name)[0], shown, lambda e: self.caption_var.set(f"could not load it: {e}"))

    def _with_bytes(self, then):
        meta = self._meta()
        if meta is None:
            return
        name = meta["name"]
        if name in self._bytes:
            then(name, self._bytes[name])
            return
        run_async(self, lambda: self.ctx.api.get_gallery_image_bytes(name)[0], lambda data: then(name, data),
                  lambda e: show_toast(self, self.ctx.theme, f"Could not read the picture: {e}", kind="error"))

    def _on_open(self):
        def go(name, data):
            try:
                file_ops.open_with_default(file_ops.write_temp_copy(name, data))
            except file_ops.SaveError as e:
                show_toast(self, self.ctx.theme, f"Could not open it: {e}", kind="error")
        self._with_bytes(go)

    def _on_save_as(self):
        def go(name, data):
            path = filedialog.asksaveasfilename(parent=self.winfo_toplevel(), title="Save picture", initialfile=name, defaultextension=".png",
                                                filetypes=file_ops.filetypes_for(name))
            if not path:
                return
            try:
                file_ops.atomic_write_bytes(path, data)
            except (file_ops.SaveError, OSError) as e:
                show_toast(self, self.ctx.theme, f"Could not save it: {e}", kind="error")
                return
            show_toast(self, self.ctx.theme, f"Saved {path}", kind="success")
        self._with_bytes(go)

    def _on_download(self):
        def go(name, data):
            try:
                path = file_ops.download_bytes(name, data)
            except file_ops.SaveError as e:
                show_toast(self, self.ctx.theme, f"Could not download it: {e}", kind="error")
                return
            show_toast(self, self.ctx.theme, f"Saved to {path}", kind="success")
        self._with_bytes(go)

    def _on_reuse(self):
        meta = self._meta()
        if meta is None:
            return
        self.prompt.delete("1.0", "end")
        self.prompt.insert("1.0", meta.get("prompt") or "")
        self.negative_var.set(meta.get("negative_prompt") or "")
        for var, key in ((self.width_var, "width"), (self.height_var, "height"), (self.steps_var, "steps"), (self.cfg_var, "cfg_scale")):
            if meta.get(key) is not None:
                var.set(f"{meta[key]:g}" if isinstance(meta[key], float) else str(meta[key]))
        self.seed_var.set(str(meta["seed"]) if meta.get("seed", -1) >= 0 else "")
        self._touched = True
        self.status_var.set("Settings copied back into the boxes.")

    def _on_delete(self):
        meta = self._meta()
        if meta is None:
            return
        name = meta["name"]

        def go():
            def done(_r):
                self._bytes.pop(name, None)
                self._photos.evict("t:" + name)
                self._photos.evict("p:" + name)
                self._selected = None
                self._reload_gallery()
            run_async(self, lambda: self.ctx.api.delete_gallery_image(name), done,
                      lambda e: show_toast(self, self.ctx.theme, f"Could not delete it: {e}", kind="error"))
        show_inline_confirm(self, self.ctx.theme, f"Delete {name}? The picture and its settings file are removed from the outputs folder.", go,
                            confirm_label="Delete", danger=True)

    # ---------------------------------------------------------------- the job

    def _request_options(self):
        """What the server gets besides the prompt; blank boxes are left out so the model's own defaults apply."""
        opts = {"backend": self.backend_var.get()}
        for key, var in (("negative_prompt", self.negative_var), ("width", self.width_var), ("height", self.height_var), ("steps", self.steps_var),
                         ("cfg_scale", self.cfg_var), ("seed", self.seed_var)):
            val = var.get().strip()
            if val:
                opts[key] = val
        return opts

    def _say(self, text, ink="value"):
        self.status_var.set(text)
        self.status_label.configure(fg=self._t[ink])

    def _busy_buttons(self, busy):
        self.generate_btn.configure(state="disabled" if busy else "normal")
        self.stop_btn.configure(state="normal" if busy else "disabled")

    def _on_backend(self):
        backend = self.backend_var.get()
        run_async(self, lambda: self.ctx.api.save_settings({"image_backend": backend}),
                  lambda _r: self._say("Chat image requests use " + ("ComfyUI." if backend == "comfyui" else "the local image server.")),
                  lambda e: self._say(f"could not save the backend: {e}", "danger"))

    def _on_generate(self):
        if self._running:
            return
        prompt = self.prompt.get("1.0", "end").strip()
        if not prompt:
            self._say("Write a prompt first.", "danger")
            return
        opts = self._request_options()
        self._running = True
        self._busy_buttons(True)
        self._say("Starting...")

        def started(_r):
            self._poll()

        def failed(e):
            self._running = False
            self._busy_buttons(False)
            self._say(str(e), "danger")
        run_async(self, lambda: self.ctx.api.gallery_generate(prompt, **opts), started, failed)

    def _poll(self):
        def got(status):
            if status.get("active"):
                self._say(STAGES.get(status.get("stage"), "Working..."))
                self.after(1000, self._poll)
            else:
                self._finished(status.get("error"))

        def lost(e):
            self._say(f"lost the connection to the app: {e}", "danger")
            self.after(3000, self._poll)
        run_async(self, self.ctx.api.image_gen_status, got, lost)

    def _finished(self, error):
        self._running = False
        self._busy_buttons(False)
        if error == "cancelled":
            self._say("Stopped.")
        elif error:
            self._say(error, "danger")
        else:
            self._say("Done.", "ok")
            self._reload_gallery(select_first=True)

    def _on_stop(self):
        run_async(self, self.ctx.api.cancel_image_gen, lambda _r: self._say("Stopping..."), lambda e: self._say(str(e), "danger"))
