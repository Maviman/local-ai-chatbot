"""The GPU controls of the Settings screen (Advanced pane): the GPUs in the order they are used, which are switched on, how a model that
does not fit one GPU is spread, which engine build runs it, and which GPU makes images.

Nothing here knows about any particular GPU: the names, sizes and the reasons a GPU cannot be used all come from the server
(GET /api/gpus), which gets them from the engine itself. The section shows nothing on a computer with one usable GPU. It is built
from the Settings screen's own widget helpers (_label / _check / _caption) and theme colours, so it looks like its neighbours and follows
any restyle of them.

Rule of this app: nothing here touches the backend on the Tk thread - every call goes through async_call.Latest.

How the pieces fit: Settings.Save sends payload() along with everything else; the server decides whether the change needs the model
reloaded (it compares what a launch would do, not the raw values). The "plan" line is a preview of where the model would go under the
values on screen, saved or not (POST /api/gpus/plan, a few seconds the first time for a model that does not fit one GPU, cached
after), and "now running" is what the loaded model was actually started with.

B8 look (BUILD_PLAN §4 B8, the approved Settings grammar): built with the Settings screen's helpers (_label: C11 bright;
_label(dim=True) / _caption: C9 value wrapped to the pane width; _check: a kit lamp) and its kit tokens (screen._t).
Placement / Engine build / Image generation GPU are option rows (label, then the dropdown 230 px into the content, as
KV cache type); each GPU row puts its Use lamp and Up / Down on the right and wraps its text in the rest of the width.
X-9 pins: Up / Down / Refresh stay PLAIN tk.Buttons with exactly those texts, styled by options only as text actions
(value ink, no box at rest, bright on hover, a 1 px bright focus ring, dim when disabled); Use stays a tk.Checkbutton
with the text "Use" (the lamp is its image). The overflow plan line keeps theme.warn (test_gpu_section 3f).
B8d: on_change (set by the screen) hears every edit made here (Use, Up / Down, placement, engine); quiet (set by the
screen) wraps the inventory answer, which is not an edit."""
import contextlib
import tkinter as tk
from tkinter import ttk

from . import theme as theme_mod
from .api_client import ApiError
from .async_call import Latest

try:  # the kit's Consolas set and its pixel scale (a guarded import: the section works without the kit)
    from .kit import C9B, C10, px as _px
except Exception:  # noqa: BLE001
    C9B, C10 = theme_mod.FONT_LABEL, theme_mod.FONT_MONO

    def _px(_widget, n):
        return int(n)

PLACEMENT_LABELS = {
    "fill_first": "Fill one GPU before using the next (recommended)",
    "spread": "Spread across all enabled GPUs",
    "single": "Use the first GPU only",
}
AUTO = "Automatic"
ENGINE_CAPTION = ("More than one llama.cpp build is installed. Automatic picks the one that can run the most of your GPUs "
                  "(the one in bin\\ unless another does better).")
PLAN_DELAY_MS = 600
# B8 geometry (approved Settings, 100 %): option rows put their control 230 px into the content (settings_screen
# OPT_COL), rows sit 18 px apart (ROW_GAP), and a GPU row keeps CONTROLS_W px on its right for Use / Down / Up.
OPT_COL = 230
ROW_GAP = 18
CONTROLS_W = 200


def _gb(mb):
    return f"{mb / 1024:.1f} GB"


class GpuSection(tk.Frame):
    def __init__(self, parent, screen, size_overrides):
        """`screen`: the Settings screen (its helpers and theme). `size_overrides()`: the settings that decide how much memory the model
        needs - context_size, kv_cache_type, kv_offload, batch_size, max_parallel_chats - as currently on screen, for the plan preview."""
        super().__init__(parent, bg=self._tokens_of(screen)["bg"])
        self.screen = screen
        self.api = screen.api
        self._size_overrides = size_overrides
        self.on_change = None                # B8d: the screen's unsaved-changes recompute (set by the screen)
        self.quiet = contextlib.nullcontext  # B8d: the screen's tracker.quiet (an inventory answer is not an edit)
        self._ready = False                  # load() has run: only then does payload() have anything honest to send
        self._saved = {}
        self._disabled = set()               # keys switched off, including GPUs that are not installed right now (they stay remembered)
        self._order_touched = False
        self._devices = []                   # in the order shown (the user's, or by memory), as the server reported them
        self._engines = []
        self._image_ok = {}
        self._plan_after = None
        self._inventory_loader = Latest(self)
        self._plan_loader = Latest(self)
        self.placement_var = tk.StringVar(value=PLACEMENT_LABELS["fill_first"])
        self.engine_var = tk.StringVar(value=AUTO)
        self.image_var = tk.StringVar(value=AUTO)
        self.plan_var = tk.StringVar(value="")
        self.running_var = tk.StringVar(value="")
        self._build()

    # ---- look (B8: the Settings screen's kit tokens; plain theme colours when the screen has none) ----

    @staticmethod
    def _tokens_of(screen):
        t = getattr(screen, "_t", None)
        if isinstance(t, dict) and "value" in t:
            return t
        th = screen.ctx.theme
        return {"bg": th.bg, "panel": th.panel, "bright": getattr(th, "bright", th.text), "value": th.text,
                "dim": th.dim, "ghost": th.dim}

    @property
    def _t(self):
        return self._tokens_of(self.screen)

    def _flat_button(self, parent, text, command, state="normal"):
        """Up / Down / Refresh: PLAIN tk.Buttons (X-9: test_gpu_section finds them by class and exact text), styled by
        options only as the approved text action: C9B value on the ground, no box at rest, bright while hovered, a
        1 px bright ring while focused, dim when disabled."""
        t = self._t
        return tk.Button(
            parent, text=text, command=command, bg=t["bg"], fg=t["value"], relief="flat", bd=0, overrelief="flat",
            padx=_px(self, 6), pady=_px(self, 2), font=C9B, activebackground=t["bg"], activeforeground=t["bright"],
            disabledforeground=t["dim"], highlightthickness=1, highlightbackground=t["bg"], highlightcolor=t["bright"],
            cursor="hand2", takefocus=1, state=state,
        )

    def _option_row(self, parent, label, pack=True):
        """A label (C11 bright) and its dropdown 230 px into the content (as KV cache type / Model loading)."""
        row = tk.Frame(parent, bg=self._t["bg"])
        row.columnconfigure(0, minsize=_px(self, OPT_COL))
        row.columnconfigure(1, weight=1)       # a narrow window shrinks the dropdown, never pushes it past the pane
        self.screen._label(row, label).grid(row=0, column=0, sticky="w")
        if pack:
            row.pack(fill="x", pady=(_px(self, ROW_GAP), 0))
        return row

    def _place_combo(self, row, combo):
        """B8-H5: the dropdown sits 230 px into its row (beside the label) while the row is wide enough for it WHOLE;
        a narrower row (the 820 px floor) puts it on its own line under the label instead of cutting its value
        (D-27 #1: never cut)."""
        def fit(_e=None):
            try:
                beside = row.winfo_width() <= 1 or row.winfo_width() >= _px(self, OPT_COL) + combo.winfo_reqwidth()
                want = (0, 1, 1) if beside else (1, 0, 2)
                info = combo.grid_info()
                if (int(info.get("row", -1)), int(info.get("column", -1))) != want[:2]:
                    combo.grid(row=want[0], column=want[1], columnspan=want[2], sticky="w")
            except tk.TclError:
                pass
        combo.grid(row=0, column=1, sticky="w")
        row.bind("<Configure>", fit, add="+")

    def _wrapped(self, label, reserve=0):
        """Let `label` wrap to the pane's width less `reserve` px and follow it (the screen's WrapGroup; else 560)."""
        wrap = getattr(self.screen, "_wrap", None)
        if wrap is not None and hasattr(wrap, "add"):
            wrap.add(label, _px(self, reserve) if reserve else 0)
        else:
            label.configure(wraplength=max(120, 560 - reserve))
        return label

    # ---- building ----

    def _build(self):
        s, t = self.screen, self._t
        self.content = tk.Frame(self, bg=t["bg"])           # packed only when there is something to show
        head = tk.Frame(self.content, bg=t["bg"])
        head.pack(fill="x", pady=(_px(self, ROW_GAP), 0))
        s._label(head, "GPUs").pack(side="left")
        self.refresh_btn = self._flat_button(head, "Refresh", self.refresh)
        self.refresh_btn.pack(side="right")
        s._caption(
            self.content,
            "The GPUs in this computer, in the order they are used: the first is filled before the next is touched, "
            "and by default the one with the most memory comes first. Switch a GPU off to keep the model off it, or "
            "use Up and Down to change the order. A GPU the installed engine build cannot run is skipped, with the reason.",
        )
        self.rows_frame = tk.Frame(self.content, bg=t["bg"])
        self.rows_frame.pack(fill="x", pady=(_px(self, 4), 0))

        place_row = self._option_row(self.content, "Placement")
        # wide enough for the longest choice, so no label is cut in the field (D-27)
        self.placement_combo = ttk.Combobox(
            place_row, textvariable=self.placement_var, values=list(PLACEMENT_LABELS.values()), state="readonly",
            width=max(46, max(len(v) for v in PLACEMENT_LABELS.values()) + 2), font=C10,
        )
        self._place_combo(place_row, self.placement_combo)
        self.placement_combo.bind("<<ComboboxSelected>>", lambda e: self._changed())
        self.plan_label = s._label(self.content, "", dim=True, textvariable=self.plan_var)
        self.plan_label.pack(fill="x", pady=(_px(self, 6), 0))
        self.running_label = s._label(self.content, "", dim=True, textvariable=self.running_var)
        self.running_label.pack(fill="x", pady=(_px(self, 2), 0))

        # only when more than one engine build is installed (bin\engines\<name>)
        self.engine_row = self._option_row(self.content, "Engine build", pack=False)
        self.engine_combo = ttk.Combobox(self.engine_row, textvariable=self.engine_var, values=[AUTO], state="readonly", width=24,
                                         font=C10)
        self._place_combo(self.engine_row, self.engine_combo)
        self.engine_combo.bind("<<ComboboxSelected>>", lambda e: self._changed())
        self.engine_caption = s._label(self.content, ENGINE_CAPTION, dim=True)

        self.image_row = self._option_row(self.content, "Image generation GPU", pack=False)
        self.image_combo = ttk.Combobox(self.image_row, textvariable=self.image_var, values=[AUTO], state="readonly", width=46,
                                        font=C10)
        self._place_combo(self.image_row, self.image_combo)
        self.image_combo.bind("<<ComboboxSelected>>", lambda e: None)
        self.image_caption = s._label(
            self.content,
            "Applies the next time the image engine starts. Automatic uses the GPU with the most memory (or the first in your order, if you "
            "set one) that the image engine can run on - the image engine is a separate build, so that can be a GPU marked as not used above.",
            dim=True,
        )

    # ---- the state the Settings screen drives ----

    def load(self, settings):
        """Called whenever the screen (re)loads its settings: resets this section's state to the saved values, then asks the server which
        GPUs there are."""
        self._saved = {
            "gpu_placement": settings.get("gpu_placement") if settings.get("gpu_placement") in PLACEMENT_LABELS else "fill_first",
            "gpu_order": [k for k in (settings.get("gpu_order") or []) if isinstance(k, str)],
            "gpu_disabled": [k for k in (settings.get("gpu_disabled") or []) if isinstance(k, str)],
            "engine": settings.get("engine") if isinstance(settings.get("engine"), str) and settings.get("engine") else "auto",
            "image_gpu": settings.get("image_gpu") if isinstance(settings.get("image_gpu"), str) and settings.get("image_gpu") else "auto",
        }
        self._disabled = set(self._saved["gpu_disabled"])
        self._order_touched = False
        self._devices = []                                    # (until the server answers: the image GPU then comes from the stored value)
        self.placement_var.set(PLACEMENT_LABELS[self._saved["gpu_placement"]])
        self.engine_var.set(AUTO if self._saved["engine"] == "auto" else self._saved["engine"])
        self.image_var.set(AUTO)                              # resolved to a name once the GPUs are known
        self._ready = True
        self.refresh()

    def saved(self, settings, keep=None):
        """The server accepted a save: what is on screen is now what is stored. keep= (B8-H7): GPU keys the user
        changed AFTER the save was sent - put back on screen, so they stay unsaved edits."""
        self.load(settings)
        if keep:
            self.reapply(keep)

    def reapply(self, keep):
        """B8-H7: show the user's own gpu_order / gpu_disabled / gpu_placement / engine again (a payload subset)."""
        if "gpu_order" in keep:
            self._saved["gpu_order"] = list(keep["gpu_order"])
        if "gpu_disabled" in keep:
            self._disabled = set(keep["gpu_disabled"])
        if keep.get("gpu_placement") in PLACEMENT_LABELS:
            self.placement_var.set(PLACEMENT_LABELS[keep["gpu_placement"]])
        if "engine" in keep:
            self.engine_var.set(AUTO if keep["engine"] == "auto" else keep["engine"])
        self.refresh()

    def payload(self):
        """The GPU keys for POST /api/settings ({} before load(): sending nothing is safer than sending defaults)."""
        if not self._ready:
            return {}
        shown = [d["key"] for d in self._devices]
        order = shown + [k for k in self._saved["gpu_order"] if k not in shown] if self._order_touched else list(self._saved["gpu_order"])
        return {
            "gpu_placement": self._placement_value(),
            "gpu_order": order,
            "gpu_disabled": sorted(self._disabled),
            "engine": self._engine_value(),
            "image_gpu": self._image_value(),
        }

    def _placement_value(self):
        return next((k for k, v in PLACEMENT_LABELS.items() if v == self.placement_var.get()), "fill_first")

    def _engine_value(self):
        return "auto" if self.engine_var.get() == AUTO else self.engine_var.get()

    def _image_value(self):
        """The image GPU as a stored value ("auto" or a key). Until the GPUs are known, what is stored."""
        if not self._devices:
            return self._saved.get("image_gpu", "auto")
        choice = self.image_var.get()
        if choice == AUTO:
            return "auto"
        return next((d["key"] for d in self._devices if self._image_label(d) == choice), self._saved.get("image_gpu", "auto"))

    # ---- fetching ----

    def refresh(self):
        self._inventory_loader.start(lambda: self.api.get_gpus(refresh=True), self._apply_inventory_quietly,
                                     lambda e: self._hide_quietly())

    # B8d: the inventory answer (or its failure) is the program refreshing what it shows, not the user's edit: inside
    # the screen's quiet(), a value it moves (the image GPU resolving to Automatic) does not raise an UNSAVED stamp
    def _apply_inventory_quietly(self, result):
        with self.quiet():
            self._apply_inventory(result)

    def _hide_quietly(self):
        with self.quiet():
            self._hide()

    def _apply_inventory(self, result):
        devices = result.get("devices") or []
        if not result.get("available") or not (len(devices) >= 2 or any(not d.get("supported", True) for d in devices)):
            self._hide()
            return
        keep_image = self._image_value()                      # the selection as it is now (or as stored, on a first load)
        self._devices = list(devices)
        # B8 FIX1 (ADV1-01): the rows follow the order this section holds (a KEPT unsaved order, B8d), not only the stored
        # one the server lists; a no-op on a normal load (the server already lists the stored order, unusable GPUs last)
        rank = {k: i for i, k in enumerate(self._saved.get("gpu_order") or [])}
        if rank:
            self._devices.sort(key=lambda d: (not d.get("supported", True), rank.get(d.get("key"), len(rank))))
        self._engines = result.get("engines") or []
        self._image_ok = {d["key"]: d.get("image_ok", True) for d in devices}
        running = result.get("plan") if result.get("running") else None
        self.running_var.set(f"Now running: {running['reason']}" if running and running.get("reason") else "")
        self._show()
        self._render_rows()
        self._render_engine_and_image(keep_image)
        self._schedule_plan()

    # (winfo_manager, not winfo_ismapped: the pane this lives in is not mapped while another Settings category is showing)
    def _show(self):
        if self.content.winfo_manager() != "pack":
            self.content.pack(fill="x")

    def _hide(self):
        if self.content.winfo_manager() == "pack":
            self.content.pack_forget()
        self._devices = []

    # ---- the list of GPUs ----

    def _render_rows(self):
        t = self._t
        for child in self.rows_frame.winfo_children():
            child.destroy()
        for i, d in enumerate(self._devices):
            row = tk.Frame(self.rows_frame, bg=t["bg"])
            row.pack(fill="x", pady=(_px(self, 10), 0))
            usable = d.get("supported", True)
            detail = f"{_gb(d['total_mb'])}, {_gb(d['free_mb'])} free"
            if not usable:
                detail += f" - not used: {d.get('reason') or 'this engine build cannot run it'}"
            elif d.get("jit"):
                detail += " - this engine build has no native code for it: the first load compiles its kernels (this can take a few minutes; the driver keeps the result)"
            # B8: the controls are packed first (they always keep their room); the text takes the rest of the row and
            # wraps inside it (the pane width less CONTROLS_W), so a long reason is never cut
            text_col = tk.Frame(row, bg=t["bg"])
            self._wrapped(self.screen._label(text_col, f"GPU {i + 1}   {d['name']}", dim=not usable, wraplength=1,
                                             justify="left"), CONTROLS_W).pack(fill="x")
            self._wrapped(self.screen._label(text_col, detail, dim=True, justify="left", wraplength=1),
                          CONTROLS_W).pack(fill="x", pady=(_px(self, 2), 0))
            # A GPU the engine cannot run sits at the end, outside the order: nothing to move, and nothing to move past it
            last_usable = max((j for j, x in enumerate(self._devices) if x.get("supported", True)), default=-1)
            for text, delta in (("Down", 1), ("Up", -1)):
                at_end = (not usable) or (delta < 0 and i == 0) or (delta > 0 and i >= last_usable)
                self._flat_button(row, text, lambda idx=i, dl=delta: self._move(idx, dl),
                                  state="disabled" if at_end else "normal").pack(side="right", anchor="n",
                                                                                 padx=(_px(self, 4), 0))
            var = tk.BooleanVar(value=usable and d["key"] not in self._disabled)
            check = self.screen._check(row, "Use", var, command=lambda key=d["key"], v=var: self._toggle(key, v))
            if not usable:
                check.configure(state="disabled")
            check.pack(side="right", anchor="n", padx=(0, _px(self, 8)))
            text_col.pack(side="left", fill="x", expand=True, anchor="n")

    def _toggle(self, key, var):
        if var.get():
            self._disabled.discard(key)
        else:
            self._disabled.add(key)
        self._changed()

    def _move(self, index, delta):
        j = index + delta
        if 0 <= j < len(self._devices):
            keep_image = self._image_value()                  # by key: the labels renumber when the order changes
            self._devices[index], self._devices[j] = self._devices[j], self._devices[index]
            self._order_touched = True
            self._render_rows()
            self._render_engine_and_image(keep_image)
            self._changed()

    def _changed(self):
        self._schedule_plan()
        if self.on_change is not None:       # B8d: the Settings screen's unsaved-changes stamp
            self.on_change()

    # ---- engine build and image GPU ----

    def _image_label(self, d):
        return f"GPU {self._devices.index(d) + 1}   {d['name']}"

    def _render_engine_and_image(self, keep_image="auto"):
        """Refills the engine and image-GPU dropdowns. `keep_image`: the image GPU to show selected, as a stored value ("auto" or a key)."""
        names = [e["name"] for e in self._engines]
        current = self._engine_value()
        values = [AUTO] + names + ([current] if current != "auto" and current not in names else [])
        self.engine_combo.configure(values=values)
        if len(names) > 1:
            # a build that is installed but useless says why (the usual cause: its CUDA runtime DLLs are not in its folder)
            hints = [f"'{e['name']}' cannot load its GPU support: missing {', '.join(e['missing'])} - put them next to its llama-server.exe." for e in self._engines if e.get("missing")]
            hints += [f"'{e['name']}' lists no GPU: its CUDA runtime DLLs (cudart, cublas) may be missing from its folder." for e in self._engines
                      if e["name"] != "default" and e.get("sees") == 0 and not e.get("missing")]
            hints += [f"'{e['name']}' could not be started." for e in self._engines if e["name"] != "default" and "sees" in e and e["sees"] is None]
            self.engine_caption.configure(text=ENGINE_CAPTION + ("  " + " ".join(hints) if hints else ""))
            # B8: always above the image GPU row (a row packed again later would otherwise land at the end)
            ahead = {"before": self.image_row} if self.image_row.winfo_manager() == "pack" else {}
            self.engine_row.pack(fill="x", pady=(_px(self, ROW_GAP), 0), **ahead)
            self.engine_caption.pack(fill="x", pady=(_px(self, 4), 0), **ahead)
        else:
            self.engine_row.pack_forget()
            self.engine_caption.pack_forget()
        usable_for_images = [d for d in self._devices if self._image_ok.get(d["key"], True)]
        image_values = [AUTO] + [self._image_label(d) for d in usable_for_images]
        # B8: the field is as wide as the longest GPU name (capped at what the pane holds), so none is cut
        self.image_combo.configure(values=image_values, width=min(72, max(46, max(len(v) for v in image_values) + 2)))
        self.engine_combo.configure(width=min(72, max(24, max(len(str(v)) for v in values) + 2)))
        pick = next((d for d in usable_for_images if d["key"] == keep_image), None)
        self.image_var.set(self._image_label(pick) if pick else AUTO)
        self.image_row.pack(fill="x", pady=(_px(self, ROW_GAP), 0))
        self.image_caption.pack(fill="x", pady=(_px(self, 4), 0))

    # ---- the plan preview ----

    def schedule_plan(self):
        """Called by the Settings screen when something that changes the memory the model needs (context size, KV cache...) moved."""
        self._schedule_plan()

    def _schedule_plan(self):
        if not self._devices:
            return
        if self._plan_after is not None:
            self.after_cancel(self._plan_after)
        self._plan_after = self.after(PLAN_DELAY_MS, self._start_plan)

    def _start_plan(self):
        self._plan_after = None
        if not self._devices:
            return
        try:
            overrides = dict(self._size_overrides())
        except Exception:  # noqa: BLE001  (a half-typed number on screen: no preview this time)
            return
        shown = [d["key"] for d in self._devices]
        overrides.update({
            "gpu_placement": self._placement_value(),
            "gpu_order": shown + [k for k in self._saved.get("gpu_order", []) if k not in shown],
            "gpu_disabled": sorted(self._disabled),
            "engine": self._engine_value(),
        })
        self.plan_label.configure(fg=self._t["value"])
        self.plan_var.set("Working out where the model will go...")
        self._plan_loader.start(lambda: self.api.plan_gpus(overrides), self._apply_plan, self._plan_failed)

    def _apply_plan(self, result):
        plan = result.get("plan")
        if not plan:
            self.plan_var.set("")
            return
        text = f"Next load: {plan.get('reason', '')}"
        sizes = [f"{d['label']}: ~{_gb(d['need_mb'])} of {_gb(d['cap_mb'])}" for d in plan.get("devices", []) if d.get("need_mb") is not None]
        if sizes:
            text += "  (" + "; ".join(sizes) + ")"
        if plan.get("estimated"):
            text += "  This is an estimate: the engine's own memory tool could not be used."
        theme = self.screen.ctx.theme
        # B8: the plan is a caption (C9 value); a model that does not fit keeps theme.warn (pinned: test_gpu_section 3f)
        self.plan_label.configure(fg=theme.warn if plan.get("overflow") else self._t["value"])
        self.plan_var.set(text)

    def _plan_failed(self, error):
        self.plan_label.configure(fg=self._t["value"])
        self.plan_var.set("No model is selected, so there is nothing to plan yet." if isinstance(error, ApiError) and getattr(error, "status", None) == 404
                          else "The placement preview is unavailable right now.")

    # ---- lifecycle ----

    def on_hide(self):
        if self._plan_after is not None:
            try:
                self.after_cancel(self._plan_after)
            except tk.TclError:
                pass
            self._plan_after = None
        self._inventory_loader.invalidate()
        self._plan_loader.invalidate()

    def destroy(self):
        self.on_hide()
        super().destroy()
