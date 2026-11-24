"""Image uplink: the control panel for image generation. Keeps the original
external link/app launcher (a URL or local tool opened via webbrowser/
os.startfile - built and verified in Phase 3) and adds, as of 2026-08-03,
real configuration for a second image-generation backend: an externally-run
ComfyUI instance this app talks to over HTTP (submitting a workflow,
polling for the result) rather than a model it manages itself the way
stable-diffusion.cpp is. Backend choice (local vs comfyui) lives here, not
buried in general Settings, since this screen is meant to be the actual
place you come to wire up image generation.

B6 look (approved r2_image_uplink / r2_image_uplink_comfy): EXTERNAL TOOL
(+ the dim exception tag ACTS NOW: Set link... saves at once), the link in a
read-only well (a one-line kit StatusLine; the whole text in its tip and
cget, OBS-04), the row `01 [LAUNCH]  02 SET LINK...` (digits 1-2; the two
captions kept as one C9 line under it, UPL-02..04), then IMAGE GENERATION
BACKEND as two Segmented words `stable-diffusion.cpp` / `ComfyUI` (D-12
L-07: the stored value is still "local"). The ComfyUI block shows only while
ComfyUI is selected; its values stay in their StringVars while hidden, so
_on_save_backend still sends every one of them. The setup help is folded
behind HOW TO CONNECT COMFYUI... (opens in place); TEST CONNECTION's result
is inked ok / danger. SAVE (secondary) + `Changes apply on save` sit in the
Settings footer position. If the kit cannot be imported, plain Tk stand-ins
(and the old DirectoryScreen row) keep the screen working.
"""

import os
import queue
import threading
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from tkinter import filedialog

from .. import theme as theme_mod
from ..async_call import Latest, run_async
from .base import Screen, make_header, make_scrollable, show_toast
from .directory_screen import DirectoryItem, DirectoryScreen

try:  # B6: the kit is the look; a guarded import, so a broken kit still leaves a working screen
    from ..kit import C9, C9B, C10B, C11, C11B, px as _px, tokens as _tokens
    from ..kit.widgets import KitButton, Segmented, StatusLine, TextAction, Well
    KIT = True
except Exception:  # noqa: BLE001
    KIT = False
    C9 = theme_mod.FONT_CAPTION
    C9B = (theme_mod.FONT_FAMILY, 9, "bold")
    C10B = theme_mod.FONT_LABEL
    C11, C11B = theme_mod.FONT_BODY, theme_mod.FONT_BODY_BOLD

    def _px(_widget, n):
        return int(n)

    def _tokens(theme):
        return {"bg": theme.bg, "panel": theme.panel, "bright": theme.bright, "dim": theme.dim,
                "value": getattr(theme, "text", theme.bright), "danger": theme.danger,
                "ok": getattr(theme, "ok", theme.bright)}

    class Well(tk.Frame):
        """Plain stand-in: a 1 px dim frame around a panel body."""

        def __init__(self, master, theme, ground=None, **kw):
            tk.Frame.__init__(self, master, bg=theme.dim, **kw)
            self.body = tk.Frame(self, bg=theme.panel)
            self.body.pack(fill="both", expand=True, padx=1, pady=1)

        def attach(self, widget):
            return widget

    def KitButton(master, theme, text="", kind="secondary", upper=False, command=None, **kw):  # noqa: N802
        for k in ("ground", "mark", "selected", "raw", "width", "height"):
            kw.pop(k, None)
        filled = kind in ("primary", "danger")
        ink = theme.bg if filled else theme.text
        return tk.Button(master, text=text.upper() if upper else text, command=command, relief="flat", padx=12,
                         bg=theme.bright if filled else theme.panel, fg=ink, activebackground=theme.dim,
                         activeforeground=ink, **kw)

    def TextAction(master, theme, text="", command=None, upper=False, **kw):  # noqa: N802
        for k in ("ground", "ink"):
            kw.pop(k, None)
        return tk.Button(master, text=text.upper() if upper else text, command=command, relief="flat", padx=6,
                         bg=theme.bg, fg=theme.text, activebackground=theme.panel, activeforeground=theme.text, **kw)

    class Segmented(tk.Frame):
        """Plain stand-in: one push-like Radiobutton per option."""

        def __init__(self, master, theme, options=(), variable=None, command=None, ground=None, font=None, **kw):
            tk.Frame.__init__(self, master, bg=theme.bg, **kw)
            for value, label in options:
                tk.Radiobutton(self, text=label, value=value, variable=variable, command=command, indicatoron=0,
                               relief="flat", bg=theme.bg, fg=theme.text, selectcolor=theme.panel,
                               activebackground=theme.panel).pack(side="left", padx=(0, 4))

    def StatusLine(master, theme=None, **kw):  # noqa: N802
        kw.pop("max_chars", None)
        return tk.Label(master, **kw)

# The backend choice: (stored value, the word shown). D-12 L-07: "Local (" is dropped; "local" is still stored.
BACKENDS = (("local", "stable-diffusion.cpp"), ("comfyui", "ComfyUI"))
COMFYUI_HOWTO = (
    "How to connect ComfyUI:\n"
    "1. Start ComfyUI (however you normally do) - it needs to already be "
    "running at the address above when you generate an image.\n"
    "2. In ComfyUI's UI, build a normal text-to-image workflow.\n"
    "3. In the positive prompt node's text box, type exactly: __PROMPT__ "
    "(this app substitutes your real prompt in for it before every request).\n"
    "4. Turn on Settings (gear icon) > Enable Dev mode options, then use the "
    "new \"Save (API Format)\" button to export the workflow as a .json file.\n"
    "5. Point \"Workflow file\" above at that .json file, then Save."
)
# approved geometry (r2 mock_image_uplink), in pixels at 100 %, content-column relative (window x - 42)
COL_X = 58               # the content column: window x 100 (index column anchored e at window x 88)
IDX_GAP = 12             # index right edge -> its control
WELL_W, WELL_H = 540, 28  # link / ComfyUI wells (100..640)
LAUNCH_H = 32            # LAUNCH (100,136)-(170,168)
ACTION_GAP = 32          # LAUNCH right edge -> '02'
FOOT_H = 29              # SAVE (992,738)-(1084,766) inclusive: the Settings footer box (cs_local SAVE_BOX)
SIDE = 15                # right gutter: SAVE ends at window x 1085 (exclusive), the Settings footer box


class ActionRow(Screen):
    """The approved `01 [LAUNCH]  02 SET LINK...` row, standing in for the DirectoryScreen Image uplink
    used before B6 with the same interface: set_items([DirectoryItem, ...]) draws the row and binds the
    digits, digit_legend() / on_hide() come from Screen. Item 1 is a KitButton: primary when the item is
    primary (a link is set); otherwise it LOOKS disabled but a click still runs its action, as digit 1
    does (the 'nothing configured' toast, as today). Later items are text actions. Each item's caption is
    kept (D-27), all of them on one C9 line under the row. .buttons / .indexes / .captions read back
    what is drawn."""

    def __init__(self, parent, ctx):
        super().__init__(parent, ctx)
        self._t = _tokens(ctx.theme)
        self._items = []
        self.buttons, self.indexes, self.captions = [], [], []
        self.row = tk.Frame(self, bg=self._t["bg"])
        self.row.pack(anchor="w")
        self.caption_row = tk.Frame(self, bg=self._t["bg"])
        self.caption_row.pack(anchor="w", padx=(_px(self, COL_X), 0), pady=(_px(self, 8), 0))

    def set_items(self, items):
        self._items = list(items)
        self._render()
        self.bind_digits([item.on_activate or (lambda: None) for item in self._items])

    def _render(self):
        t, theme = self._t, self.ctx.theme
        for frame in (self.row, self.caption_row):
            for child in frame.winfo_children():
                child.destroy()
        self.buttons, self.indexes, self.captions = [], [], []
        for i, item in enumerate(self._items):
            number = "%02d" % (i + 1)
            if i == 0:
                # the index column: '01' anchored e 12 px left of the content column (window x 88)
                box = tk.Frame(self.row, bg=t["bg"], width=_px(self, COL_X - IDX_GAP), height=_px(self, LAUNCH_H))
                box.pack(side="left")
                box.pack_propagate(False)
                idx = tk.Label(box, text=number, bg=t["bg"], fg=t["value"], font=C11B, padx=0, pady=0, bd=0)
                idx.pack(side="right")
                btn = KitButton(self.row, theme, text=item.label, kind="primary", upper=True, ground=t["bg"],
                                command=item.on_activate, height=_px(self, LAUNCH_H))
                btn.pack(side="left", padx=(_px(self, IDX_GAP), 0))
                if not item.is_primary:
                    self._look_disabled(btn, item.on_activate)
            else:
                idx = tk.Label(self.row, text=number, bg=t["bg"], fg=t["value"], font=C11B, padx=0, pady=0, bd=0)
                idx.pack(side="left", padx=(_px(self, ACTION_GAP), 0))
                btn = TextAction(self.row, theme, text=item.label, command=item.on_activate, upper=True,
                                 ground=t["bg"])
                btn.pack(side="left", padx=(_px(self, IDX_GAP - 6), 0))      # the word itself 12 px after
            self.indexes.append(idx)
            self.buttons.append(btn)
            if item.caption:
                if self.captions:
                    tk.Label(self.caption_row, text="·", bg=t["bg"], fg=t["dim"], font=C9, padx=0, pady=0,
                             bd=0).pack(side="left", padx=_px(self, 10))
                cap = tk.Label(self.caption_row, text=item.caption, bg=t["bg"], fg=t["value"], font=C9, padx=0,
                               pady=0, bd=0)
                cap.pack(side="left")
                self.captions.append(cap)

    def _look_disabled(self, btn, action):
        """Disabled look (ghost outline, dim text) while a click still runs `action`. B6-H1: through the
        kit's explain= the button stays a Tab stop (a dim focus ring on the ghost face) and Return, space
        and a click inside all run `action` - the kit handles the click, so one click is one notice."""
        btn.configure(state="disabled", explain=action, cursor="hand2")


def open_external_link(link, parent, theme):
    """Open a user-configured link: a web URL via the default browser, or a
    local file/app path via the OS's own file-association handler. One-shot
    launch of a separate process the user already chose - nothing is
    rendered/embedded by this app."""
    try:
        if link.lower().startswith("http://") or link.lower().startswith("https://"):
            webbrowser.open(link)
        else:
            os.startfile(link)
    except OSError as e:
        show_toast(parent, theme, f"Could not open {link}: {e}", kind="error")


class ImageUplinkScreen(Screen):
    def __init__(self, parent, ctx):
        super().__init__(parent, ctx)
        theme = ctx.theme
        self._link = ""
        # B6-H8: True only once the LAST settings load has landed; until then SAVE refuses to send the form
        # (construction defaults, or an older visit's values) over the stored settings
        self._settings_loaded = False
        # B6-H7: TEST CONNECTION's generation; a result from an older click, or from before a reload, is dropped
        self._test_gen = 0
        # Settings are fetched off the Tk thread (see async_call.py); a result
        # that lands after the user has left this screen is dropped.
        self._loader = Latest(self, deliver_when=self.is_visible)

        # B2: the shared title-only header (kit.header, Canvas text) - the same one make_header builds
        self.header, _ = make_header(self, theme, "Image uplink")
        self._t = t = _tokens(theme)
        col = _px(self, COL_X)

        # SAVE + its stamp (the Settings footer position), packed BEFORE the expanding form so a small
        # window scrolls the form and never squeezes SAVE
        self.footer = tk.Frame(self, bg=t["bg"])
        self.footer.pack(side="bottom", fill="x", padx=(col, _px(self, SIDE)), pady=(_px(self, 8), _px(self, 9)))

        # the link (dominant): EXTERNAL TOOL + the dim exception tag (Set link... saves at once)
        self._section(self, "External tool", tag="Acts now").pack(anchor="w", padx=(col, 0), pady=(_px(self, 17), 0))
        self.link_var = tk.StringVar(value="(none configured)")
        link_box = tk.Frame(self, bg=t["bg"], width=_px(self, WELL_W), height=_px(self, WELL_H), bd=0,
                            highlightthickness=0)
        link_box.pack(anchor="w", padx=(col, _px(self, SIDE)), pady=(_px(self, 7), 0))
        link_box.pack_propagate(False)
        self.link_box = link_box
        self.link_well = Well(link_box, theme, ground=t["bg"])
        self.link_well.pack(fill="both", expand=True)
        # read-only (OBS-04): one line, elided in the middle when long; the whole link in cget / the tip
        self.link_label = StatusLine(self.link_well.body, theme, textvariable=self.link_var, max_chars=4096,
                                     bg=t["panel"], fg=t["bright"], font=C11, padx=_px(self, 6), pady=0, bd=0)
        self.link_label.pack(fill="both", expand=True)
        # FIX1 A4: a failed settings load is also shown whole (wrapped, danger ink) right under the one-line
        # well, which elides it; packed only while the last load failed
        self.load_error_label = tk.Label(self, text="", bg=t["bg"], fg=t["danger"], font=C9, anchor="w",
                                         justify="left", padx=0, pady=0, bd=0)
        self._wrap_to_width(self.load_error_label)

        # Just the two actions for the external link - not a scrolling
        # concern, so this gets its own fixed row rather than sharing the
        # scrollable ComfyUI form below it. ActionRow keeps DirectoryScreen's
        # interface (set_items / digit_legend / on_hide), so _apply_settings
        # and the digits 1-2 are unchanged.
        if KIT:
            self.directory = ActionRow(self, ctx)
            self.directory.pack(fill="x", pady=(_px(self, 14), _px(self, 18)))
        else:
            self.directory = DirectoryScreen(self, ctx, columns=2)
            self.directory.pack(fill="x", padx=0, pady=(0, 8))

        outer, inner = make_scrollable(self, theme)
        outer.pack(fill="both", expand=True)
        self.form_outer = outer
        self._build_backend_form(inner)
        # Tab order follows the stacking order: the footer (packed first, for the squeeze rule) is reached last
        self.footer.lift()

    def _section(self, parent, word, tag=None):
        """A plain C9 value section word (upper-case) and an optional exception tag in dim, 16 px after it."""
        t = self._t
        row = tk.Frame(parent, bg=t["bg"])
        tk.Label(row, text=word.upper(), bg=t["bg"], fg=t["value"], font=C9, padx=0, pady=0, bd=0).pack(side="left")
        if tag:
            row.tag_label = tk.Label(row, text=tag.upper(), bg=t["bg"], fg=t["dim"], font=C9, padx=0, pady=0, bd=0)
            row.tag_label.pack(side="left", padx=(_px(self, 16), 0))
        return row

    # ---- external link (unchanged behavior) ----

    def legend_items(self):
        # Launch / Set link... (the directory's digits, none when settings fail to load) + global Esc
        return self.directory.digit_legend("open") + [("ESC", "back")]

    def on_show(self):
        def applied(settings):
            self._show_load_error("")
            self._apply_settings(settings)
            self._settings_loaded = True

        def failed(error):
            self._settings_loaded = False
            self._link = ""
            self.link_var.set(f"could not load settings: {error}")
            self._show_load_error(self.link_var.get())
            self.directory.set_items([])

        self._settings_loaded = False
        self._loader.start(self.ctx.api.get_settings, applied, failed)

    def _show_load_error(self, text):
        """FIX1 A4: show (text) or hide ("") the whole load error under the link well."""
        lab = self.load_error_label
        try:
            if text:
                lab.configure(text=text)
                if not lab.winfo_manager():
                    lab.pack(anchor="w", fill="x", padx=(_px(self, COL_X), _px(self, SIDE)), pady=(_px(self, 4), 0),
                             after=self.link_box)
            elif lab.winfo_manager():
                lab.pack_forget()
        except tk.TclError:
            pass

    def _apply_settings(self, settings):
        self._link = (settings.get("image_gen_link") or "").strip()
        self.link_var.set(self._link or "(none configured - set one below)")
        launch_caption = "open the configured tool" if self._link else "unavailable - nothing configured"
        self.directory.set_items([
            DirectoryItem("Launch", launch_caption, on_activate=self._on_launch, is_primary=bool(self._link)),
            DirectoryItem(
                "Set link...", "browse for an app/file, or paste a URL",
                on_activate=self._on_browse_link,
            ),
        ])
        self._load_backend_settings(settings)

    def on_hide(self):
        super().on_hide()
        self.directory.on_hide()

    def _on_launch(self):
        if not self._link:
            show_toast(
                self, self.ctx.theme,
                "No image generation tool is configured yet. Set a link or app below first.",
                kind="warning",
            )
            return
        open_external_link(self._link, self, self.ctx.theme)

    def _on_browse_link(self):
        path = filedialog.askopenfilename(title="Choose an app or file to open for image generation")
        if path:
            self._save_settings({"image_gen_link": path}, "Link saved.")

    # ---- backend selection + ComfyUI configuration ----

    def _build_backend_form(self, f):
        theme = self.ctx.theme
        t = self._t
        s = lambda n: _px(self, n)  # noqa: E731
        col = s(COL_X)

        def label(parent, text, ink="bright", **kwargs):
            kwargs.setdefault("bg", t["bg"])
            kwargs.setdefault("fg", t[ink])
            kwargs.setdefault("anchor", "w")
            kwargs.setdefault("justify", "left")
            kwargs.setdefault("font", C11)
            return tk.Label(parent, text=text, padx=0, pady=0, bd=0, **kwargs)

        def caption(parent, text, **kwargs):
            kwargs.setdefault("font", C9)
            return label(parent, text, ink="value", **kwargs)

        def well_entry(parent, var):
            """A 540 x 28 input well holding an Entry; -> (box, entry). The box is what is packed."""
            box = tk.Frame(parent, bg=t["bg"], width=s(WELL_W), height=s(WELL_H), bd=0, highlightthickness=0)
            box.pack_propagate(False)
            well = Well(box, theme, ground=t["bg"])
            well.pack(fill="both", expand=True)
            e = well.attach(tk.Entry(
                well.body, textvariable=var, bg=t["panel"], fg=t["bright"], insertbackground=t["bright"],
                selectbackground=t["bright"], selectforeground=t["bg"], relief="flat", bd=0,
                highlightthickness=0, font=C11,
            ))
            e.pack(fill="both", expand=True, padx=s(7))
            box.well, box.entry = well, e
            return box, e

        def action(parent, text, command):
            return TextAction(parent, theme, text=text, command=command, upper=True, ground=t["bg"])

        # ---- the backend: one choice -> two words + the underbar
        self._section(f, "Image generation backend").pack(anchor="w", padx=(col, 0), pady=(s(2), 0))
        self.backend_var = tk.StringVar(value="local")
        self.backend_choice = Segmented(f, theme, options=BACKENDS, variable=self.backend_var,
                                        command=self._sync_comfyui_block, ground=t["bg"], font=C10B)
        # the first word starts at the content column (cell ring 2 + label padding 10 = 12, approved 13)
        self.backend_choice.pack(anchor="w", padx=(max(0, col - s(13)), 0), pady=(s(5), 0))
        self.backend_caption = caption(
            f, "Which backend \"generate an image of ...\"/\"make it ...\" in chat actually uses.")
        self.backend_caption.pack(anchor="w", fill="x", padx=(col, s(SIDE)), pady=(s(4), 0))
        self._wrap_to_width(self.backend_caption)

        # ---- ComfyUI: shown only while it is selected (its values stay in the vars while hidden)
        block = tk.Frame(f, bg=t["bg"])
        self.comfyui_block = block
        self._section(block, "ComfyUI").pack(anchor="w", padx=(col, 0), pady=(s(26), 0))

        label(block, "ComfyUI address").pack(anchor="w", padx=(col, 0), pady=(s(14), s(5)))
        self.comfyui_url_var = tk.StringVar(value="http://127.0.0.1:8188")
        box, self.comfyui_url_entry = well_entry(block, self.comfyui_url_var)
        box.pack(anchor="w", padx=(col, 0))

        label(block, "Workflow file (exported from ComfyUI as \"API Format\")").pack(
            anchor="w", padx=(col, 0), pady=(s(22), s(5)))
        # FIX1 A1: the row is as wide as it asks (well + 12 + BROWSE) when there is room; when there is not
        # (a 125 % / 150 % text scale, a small window), BROWSE... is packed first so it keeps its width and
        # the well is what shrinks (a fixed-width well packed first used to squeeze BROWSE out)
        workflow_row = tk.Frame(block, bg=t["bg"])
        workflow_row.pack(anchor="w", padx=(col, s(SIDE)))
        self.comfyui_workflow_var = tk.StringVar(value="")
        box, self.comfyui_workflow_entry = well_entry(workflow_row, self.comfyui_workflow_var)
        self.browse_workflow_btn = action(workflow_row, "Browse...", self._on_browse_workflow)
        self.browse_workflow_btn.pack(side="right", padx=(s(12), 0))
        box.pack(side="left")       # (Tab order is creation order, unchanged: the well, then BROWSE...)

        label(block, "Prompt placeholder (only change if you used something different)", ink="value").pack(
            anchor="w", padx=(col, 0), pady=(s(22), s(5)))
        self.comfyui_marker_var = tk.StringVar(value="__PROMPT__")
        box, self.comfyui_marker_entry = well_entry(block, self.comfyui_marker_var)
        box.pack(anchor="w", padx=(col, 0))

        action_row = tk.Frame(block, bg=t["bg"])
        action_row.pack(anchor="w", fill="x", padx=(col - s(6), s(SIDE)), pady=(s(14), 0))
        self.test_connection_btn = action(action_row, "Test connection", self._on_test_connection)
        self.test_connection_btn.pack(side="left")
        self.test_status_var = tk.StringVar(value="")
        self.test_status_label = caption(action_row, "", textvariable=self.test_status_var)
        # the rest of the row, wrapped at its width: a long "Could not reach ComfyUI: <error>" is never cut
        self.test_status_label.pack(side="left", fill="x", expand=True, padx=(s(10), 0))
        self._wrap_to_width(self.test_status_label)
        # the result's ink follows its text: Connected. -> ok, Could not reach ... -> danger, Testing... -> value
        self.test_status_var.trace_add("write", lambda *_a: self._ink_test_status())

        # the setup help, folded behind one text action; it opens in place, right under it (verbatim)
        self.howto_btn = action(block, "How to connect ComfyUI...", self._toggle_howto)
        self.howto_btn.pack(anchor="w", padx=(col - s(6), 0), pady=(s(8), 0))
        self.howto_label = caption(block, COMFYUI_HOWTO)
        self._wrap_to_width(self.howto_label)
        self.howto_open = False

        # ---- SAVE (secondary) + the timing stamp, in the Settings footer position
        self.save_btn = KitButton(self.footer, theme, text="Save", kind="secondary", upper=True,
                                  command=self._on_save_backend, ground=t["bg"], width=s(93), height=s(FOOT_H))
        self.save_btn.pack(side="right")
        self.save_stamp = caption(self.footer, "Changes apply on save")
        self.save_stamp.pack(side="right", padx=(0, s(16)))

        # the block follows the choice, whoever writes it (a click, _load_backend_settings, a test)
        self.backend_var.trace_add("write", lambda *_a: self._sync_comfyui_block())
        self._sync_comfyui_block()

    def _wrap_to_width(self, label):
        """Wrap a caption at its own width (never cut, D-27); geometry only, so it cannot feed back."""
        label.configure(wraplength=_px(self, 600))

        def fit(event):
            width = max(_px(self, 120), event.width)
            if int(float(label.cget("wraplength") or 0)) != width:
                label.configure(wraplength=width)
        label.bind("<Configure>", fit, add="+")
        return label

    def _sync_comfyui_block(self):
        """Show the ComfyUI block only while ComfyUI is selected. Hiding it is pack_forget only: the
        address / workflow / placeholder vars keep their values, and _on_save_backend sends them all."""
        try:
            show = self.backend_var.get() == "comfyui"
            shown = bool(self.comfyui_block.winfo_manager())
            if show and not shown:
                self.comfyui_block.pack(fill="x")
            elif shown and not show:
                self.comfyui_block.pack_forget()
        except tk.TclError:
            pass

    def _toggle_howto(self):
        """HOW TO CONNECT COMFYUI... opens the setup help in place (right under it); again closes it."""
        self.howto_open = not self.howto_open
        if self.howto_open:
            self.howto_label.pack(anchor="w", fill="x", padx=(_px(self, COL_X), _px(self, SIDE)),
                                  pady=(_px(self, 6), _px(self, 4)), after=self.howto_btn)
        else:
            self.howto_label.pack_forget()

    def _ink_test_status(self):
        t = self._t
        text = self.test_status_var.get()
        ink = t["ok"] if text.startswith("Connected") else (t["danger"] if text.startswith("Could not") else
                                                              t["value"])
        try:
            self.test_status_label.configure(fg=ink)
        except tk.TclError:
            pass

    def _load_backend_settings(self, settings):
        self.backend_var.set(settings.get("image_backend") or "local")
        self.comfyui_url_var.set(settings.get("comfyui_url") or "http://127.0.0.1:8188")
        self.comfyui_workflow_var.set(settings.get("comfyui_workflow_path") or "")
        self.comfyui_marker_var.set(settings.get("comfyui_prompt_marker") or "__PROMPT__")
        self._test_gen += 1       # B6-H7: a test still in flight belongs to the form before this reload
        self.test_status_var.set("")

    def _on_browse_workflow(self):
        path = filedialog.askopenfilename(
            title="Choose a ComfyUI workflow (API format JSON)", filetypes=[("JSON", "*.json")]
        )
        if path:
            self.comfyui_workflow_var.set(path)

    def _on_test_connection(self):
        url = self.comfyui_url_var.get().strip().rstrip("/")
        if not url:
            show_toast(self, self.ctx.theme, "Enter a ComfyUI address first.", kind="warning")
            return
        self.test_status_var.set("Testing...")
        # B6-H7: this click's generation. An older click's chain (an unreachable address: 4 s) stops polling
        # and drops its result as soon as a newer click or a reload of the form supersedes it.
        self._test_gen += 1
        q = queue.Queue()
        threading.Thread(target=self._test_connection_worker, args=(url, q), daemon=True).start()
        self.after(50, self._poll_test_connection, q, self._test_gen)

    def _test_connection_worker(self, url, q):
        # Worker thread - only ever q.put(...), never touch Tk directly.
        try:
            with urllib.request.urlopen(url + "/system_stats", timeout=4):
                pass
            q.put(("ok", None))
        except Exception as e:
            q.put(("error", str(e)))

    def _poll_test_connection(self, q, gen):
        if gen != self._test_gen:
            return                                    # B6-H7: superseded - its result would be stale
        try:
            kind, payload = q.get_nowait()
        except queue.Empty:
            self.after(50, self._poll_test_connection, q, gen)
            return
        if kind == "ok":
            self.test_status_var.set("Connected.")
        else:
            self.test_status_var.set(f"Could not reach ComfyUI: {payload}")

    def _on_save_backend(self):
        if not self._settings_loaded:
            # B6-H8: the form holds construction defaults (or an older visit's values), not the stored
            # settings: sending it would erase a working ComfyUI setup. Say so; load again if nothing is.
            if self._loader.busy:
                show_toast(self, self.ctx.theme, "Settings haven't finished loading - saving now could overwrite them "
                           "with defaults. Try again in a moment.", kind="warning")
            else:
                show_toast(self, self.ctx.theme, "Settings didn't load - saving now could overwrite them with "
                           "defaults. Loading them again; then try SAVE.", kind="warning")
                self.on_show()
            return
        backend = self.backend_var.get()
        workflow_path = self.comfyui_workflow_var.get().strip()
        if backend == "comfyui" and workflow_path and not os.path.isfile(workflow_path):
            show_toast(self, self.ctx.theme, f"Workflow file not found: {workflow_path}", kind="error")
            return
        self._save_settings(
            {
                "image_backend": backend,
                "comfyui_url": self.comfyui_url_var.get().strip(),
                "comfyui_workflow_path": workflow_path or None,
                "comfyui_prompt_marker": self.comfyui_marker_var.get().strip() or "__PROMPT__",
            },
            "Saved.",
        )

    def _save_settings(self, body, success_message):
        def saved(_result):
            show_toast(self, self.ctx.theme, success_message, kind="success")
            self.on_show()

        run_async(
            self, lambda: self.ctx.api.save_settings(body), saved,
            lambda e: show_toast(self, self.ctx.theme, f"Could not save: {e}", kind="error"),
        )
