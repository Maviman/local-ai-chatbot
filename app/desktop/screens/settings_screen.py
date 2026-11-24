"""Settings: master-detail instead of tabs - a numbered category column
(General/Model/Vision/Appearance/Advanced/Data) on the left feeding
DirectoryScreen, the selected category's fields on the right. No popup:
this is a routed full-screen view, replacing the old SettingsDialog
Toplevel outright.

The field-level logic is ported from settings_dialog.py nearly unchanged -
load/save bodies, VRAM estimate debounce, the save-worker thread + queue +
restart-health polling - because it was already debugged across the
previous phases; only the container changed. Appearance (phosphor preset +
launch screen) is new and client-side only: it writes
data/desktop_theme.json via theme.save_theme and applies on restart.

B8 look (approved r2_settings_general / _advanced / _data; BUILD_PLAN §4 B8): the category column is
unboxed rows from y 70 (CategoryList: the active one an inverse plate, each row's caption always
shown under it, OI-10; today's wording, OI-15; digits as before), the pane has no frame and the thin
scrollbar, and the footer is the pane's timing stamp `Changes apply on save` left of the SAVE primary;
the transient status (self.status_label: Saved. / Model ready. / errors) takes the stamp's place while it
shows. In the panes: section rows (INSTRUCTIONS / SAMPLING / THINKING / TOOLS; RUNNING + LIVE;
LIMITS & FOLDERS / BACKUP / DESTRUCTIVE), labels C11 bright, captions C9 value wrapped to the pane width
and ALWAYS shown in full (D-27 #1: no caption is shortened or folded), wells for the entries and the
system prompt, lamps (still tk.Checkbutton, text unchanged), the kit slider (still ttk.Scale + its value
tk.Label), Segmented words for the thinking mode / effort, KV cache type, phosphor and launch screen.
The helpers keep their signatures, so gpu_section.py builds with them as before. If the kit cannot be
imported, plain Tk stand-ins keep the screen working.

B8d (D-7, spec ST-13): an unsaved-changes guard (kit/dirty.py). _collect_body() is the body Save sends (moved
out of _on_save unchanged); a DirtyTracker compares it with a snapshot of the last load / save on every variable
write and system-prompt <<Modified>>, synchronously (no timer). The stamp reads UNSAVED · n CHANGES while dirty.
Leaving with edits and coming back keeps them on top of the fresh values (_load: UNSAVED CHANGES KEPT · n +
REVERT); a save makes the screen clean; a settings reset or a restore discards them; a model switch rebases the
restart keys, so it never shows a false UNSAVED.
"""

import contextlib
import json
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from .. import theme as theme_mod
from ..api_client import ApiError
from ..async_call import Latest, run_async
from ..asset_manager import AssetManagerPanel
from ..gpu_section import GpuSection
from ..model_load_overlay import RESTART_WAIT_SECONDS, ModelLoadOverlay
from .base import Screen, make_header, make_scrollable, show_inline_confirm, show_toast
from .directory_screen import DirectoryItem, DirectoryScreen

try:  # B8: the kit is the look; a guarded import, so a broken kit still leaves a working Settings screen
    from ..kit import C9, C10, C10B, C11, px as _px, tokens as _tokens
    from ..kit.settings import (ActionButton, CategoryRow, InkSegmented, KitScale, LampCheck, RecordRow, ResizeGrip,
                                SettingsFooter, WrapGroup, meter_style)
    from ..kit.widgets import SectionRow, Segmented, Well
    from ..kit import settings as kset
    KIT = True
except Exception:  # noqa: BLE001
    KIT = False
    CategoryRow = None
    C9, C11 = theme_mod.FONT_CAPTION, theme_mod.FONT_BODY
    C10, C10B = theme_mod.FONT_MONO, theme_mod.FONT_LABEL

    def _px(_widget, n):
        return int(n)

    def _tokens(theme):
        return {"bg": theme.bg, "panel": theme.panel, "bright": theme.bright, "dim": theme.dim, "value": theme.text,
                "warn_eff": theme.warn, "danger": theme.danger, "ghost": theme.dim}

    def meter_style(_widget, theme=None, bar=6):
        return "Horizontal.TProgressbar"

    class WrapGroup:
        """Plain stand-in: today's fixed 560 px wrap."""

        def __init__(self, width=770, minimum=120):
            self.width = width

        def add(self, label, indent=0):
            label.configure(wraplength=560)
            return label

        def set_width(self, width):
            return False

    class Well(tk.Frame):
        """Plain stand-in: a 1 px dim frame around a panel body."""

        def __init__(self, master, theme, ground=None, **kw):
            tk.Frame.__init__(self, master, bg=theme.dim, **kw)
            self.body = tk.Frame(self, bg=theme.panel)
            self.body.pack(fill="both", expand=True, padx=1, pady=1)

        def attach(self, widget):
            return widget

    def SectionRow(master, theme, title, tag=None, ground=None, upper=True, **kw):  # noqa: N802
        row = tk.Frame(master, bg=theme.bg)
        tk.Label(row, text=title.upper() if upper else title, bg=theme.bg, fg=theme.bright,
                 font=theme_mod.FONT_LABEL).pack(side="left")
        if tag:
            tk.Label(row, text=tag, bg=theme.bg, fg=theme.dim, font=theme_mod.FONT_CAPTION).pack(side="left", padx=(12, 0))
        return row

    def LampCheck(master, theme, text="", variable=None, command=None, ground=None, font=None, **kw):  # noqa: N802
        return tk.Checkbutton(master, text=text, variable=variable, command=command, bg=theme.bg, fg=theme.text,
                              activebackground=theme.bg, activeforeground=theme.text, selectcolor=theme.panel,
                              anchor="w", font=theme_mod.FONT_BODY, **kw)

    def KitScale(master, theme=None, step=None, **kw):  # noqa: N802
        scale = ttk.Scale(master, **kw)
        scale.attach = lambda label=None, readout=None: scale
        return scale

    def ActionButton(master, theme, text="", kind="secondary", command=None, **kw):  # noqa: N802
        for k in ("ground", "upper", "width", "height"):
            kw.pop(k, None)
        filled = kind == "primary"
        danger = kind.startswith("danger")
        return tk.Button(master, text=text, command=command, relief="flat", padx=10,
                         bg=theme.bright if filled else theme.bg,
                         fg=theme.bg if filled else (theme.danger if danger else theme.text),
                         activebackground=theme.panel if danger else theme.dim,
                         activeforeground=theme.danger if danger else theme.bg, **kw)

    class Segmented(tk.Frame):
        """Plain stand-in: one push-like Radiobutton per option; configure(state=) reaches each of them."""

        def __init__(self, master, theme, options=(), variable=None, command=None, state="normal", ground=None,
                     font=None, **kw):
            tk.Frame.__init__(self, master, bg=theme.bg)
            self._buttons = []
            for o in options:
                value, label = (o[0], o[1]) if isinstance(o, (tuple, list)) else (o, o)
                rb = tk.Radiobutton(self, text=label, value=value, variable=variable, command=command, indicatoron=0,
                                    relief="flat", bg=theme.bg, fg=theme.text, selectcolor=theme.panel,
                                    activebackground=theme.panel, padx=8)
                rb.pack(side="left", padx=(0, 4))
                self._buttons.append(rb)

        def configure(self, cnf=None, **kw):
            if "state" in kw:
                st = "disabled" if kw.pop("state") == "disabled" else "normal"
                for b in self._buttons:
                    b.configure(state=st)
            return tk.Frame.configure(self, cnf, **kw) if (cnf or kw) else None

        config = configure

    def InkSegmented(master, theme, options=(), inks=None, **kw):  # noqa: N802
        return Segmented(master, theme, options=options, **kw)

    def ResizeGrip(master, theme, ground=None, **kw):  # noqa: N802
        return tk.Label(master, text="⋰", bg=theme.panel, fg=theme.dim, font=theme_mod.FONT_CAPTION, padx=2, **kw)

    class RecordRow(tk.Frame):
        """Plain stand-in: KEY and value on one line."""

        def __init__(self, master, theme, key, textvariable=None, text="", value_x=400, ground=None, **kw):
            tk.Frame.__init__(self, master, bg=theme.bg)
            self.key_label = tk.Label(self, text=key, bg=theme.bg, fg=theme.dim, font=theme_mod.FONT_CAPTION)
            self.key_label.pack(side="left")
            opts = {"textvariable": textvariable} if textvariable is not None else {"text": text}
            self.value_label = tk.Label(self, bg=theme.bg, fg=theme.bright, font=theme_mod.FONT_CAPTION, **opts)
            self.value_label.pack(side="left", padx=(12, 0))

    class SettingsFooter(tk.Frame):
        """Plain stand-in: today's footer (status left, Save right)."""

        def __init__(self, master, theme, command=None, stamp="", save_text="Save", ground=None, **kw):
            tk.Frame.__init__(self, master, bg=theme.panel)
            self.status = tk.Label(self, text="", bg=theme.panel, fg=theme.dim, font=theme_mod.FONT_CAPTION)
            self.status.pack(side="left", padx=12, pady=8)
            self.save_btn = tk.Button(
                self, text=save_text.upper(), command=command, bg=theme.bright, fg=theme.bg,      # B15: no spacing
                relief="flat", padx=16, font=theme_mod.FONT_LABEL, activebackground=theme.dim,
                activeforeground=theme.bg,
            )
            self.save_btn.pack(side="right", padx=12, pady=8)
            self.stamp_label = tk.Label(self, text=stamp)

        def set_stamp(self, text):
            self.stamp_label.configure(text=text)

try:  # B8d (D-7): the unsaved-changes guard; a guarded import like the kit's (kit/dirty.py)
    from ..kit.dirty import DirtyStamp, DirtyTracker
except Exception:  # noqa: BLE001
    DirtyStamp = DirtyTracker = None

FX_CATEGORY = "fx"          # B10: Settings 08 "Sound & motion"
FX_UNAVAILABLE = "Sound & motion controls are unavailable in this build."
SAVE_STAMP = "Changes apply on save"
try:  # B10: page 08 is fx/sound_motion_page.py (its sections in fx/page08/); a guarded import, so a broken page
    # leaves page 08 showing the one-line notice and never stops Settings (or the app) opening
    from ..fx.sound_motion_page import STAMP as FX_STAMP
    from ..fx.sound_motion_page import install_fx_pane
except Exception:  # noqa: BLE001
    FX_STAMP = "Changes apply at once"

    def install_fx_pane(screen):
        """Stand-in: the page module could not be imported, so page 08 is the notice alone."""
        _fx_pane_unavailable(screen)
        return None
try:  # B13: the legend on 08 lists ALT+M only while Alt+M is really bound (fx/quick_mute.py)
    from ..fx.quick_mute import bound as _alt_m_bound
except Exception:  # noqa: BLE001

    def _alt_m_bound(_widget):
        return False
try:  # B14: Save's sound hook (settings.saved through the fx seam; NullFX, i.e. nothing at all, when fx is off)
    from .. import fx as _fx
except Exception:  # noqa: BLE001
    _fx = None


def _fx_emit(widget, event, **data):
    """B14: one seam event from a Settings Tk callback; never raises; a no-op when fx could not be imported."""
    if _fx is not None:
        _fx.get(widget).emit(widget, event, **data)


class _NoDirty:
    """Stand-in when kit/dirty.py cannot be imported: nothing is tracked, nothing is kept (today's reload)."""

    clean = None
    kept = False

    def user_values(self):
        return {}

    def dirty_keys(self):
        return []

    def current(self):
        return None, frozenset()

    def held(self):
        return contextlib.nullcontext(self)

    quiet = held

    def mark_clean(self, base=None, take=()):
        pass

    def mark_kept(self):          # B8 FIX1 (ADV1-03): the real mark_clean takes base= / take= (the save path)
        pass

    discard = detach = recompute = mark_kept

    def rebase(self, fresh, keys, before=None):
        pass

# B8 geometry (approved r2 Settings at 100 %, screen-local px: the screen starts at window x 42, under the 56 px
# header band; cs_local.settings_chrome / Pane)
CAT_COL_W = 220             # the category column, window x 42..262; the pane starts at window x 262
CAT_TOP = 14                # the first category row at window y 70
PANE_TOP = 6                # the pane's viewport from window y 62
PANE_RIGHT = 22             # the thin scrollbar ends at window x 1078 (its 4 px thumb at 1072..1076)
PANE_SB_W = 8               # that scrollbar's width (kit ttk_controls SB_W)
PANE_PAD_L, PANE_PAD_R = 28, 10   # content x 290..1060 (readouts right-aligned at window x 1060)
PANE_PAD_T, PANE_PAD_B = 16, 24
PANE_CONTENT_W = 770        # the content width at 1100 x 800 (captions wrap to the real width)
FOOT_LEFT, FOOT_RIGHT = 16, 15    # SAVE (992,738)-(1085,767)
FOOT_PADY = (8, 9)
OPT_COL = 230               # option words / dropdowns start 230 px into the content (KV cache type)
RECORD_VALUE_X = 400        # RUNNING values right-aligned at window x 690
LAMP_INDENT = 22            # a lamp's caption sits under the lamp's text (x + 22)
SECTION_GAP = 26            # space above a section row (not the first)
ROW_GAP = 18                # space above a control row (label + slider, lamp, field)

# Must stay in the same order as server.RESPONSE_LENGTH_LEVELS - both sides
# index this by slider position.
RESPONSE_LENGTH_LEVELS = ["concise", "normal", "detailed", "long"]
RESPONSE_LENGTH_NAMES = {
    "concise": "Brief",
    "normal": "Standard",
    "detailed": "Detailed",
    "long": "Long-form",
}
# The Reply limit slider runs 0 (= no limit) to this, in steps of REPLY_LIMIT_STEP tokens.
REPLY_LIMIT_MAX = 32768
REPLY_LIMIT_STEP = 256
KV_CACHE_TYPES = ["f16", "q8_0", "q4_0"]
# Mirrors server.py: "auto" leaves the model's chat template to decide.
THINKING_MODES = ["auto", "on", "off"]
THINKING_EFFORTS = ["low", "medium", "high", "max"]
# Mirrors server.MODEL_LOAD_MODES: the dropdown shows the labels, the server
# stores the keys.
MODEL_LOAD_LABELS = {
    "auto": "Automatic (recommended)",
    "mmap": "Memory-mapped",
    "ram": "Fully into RAM",
}

RESTART_KEYS = (
    "context_size", "max_parallel_chats", "gpu_layers", "gpu_layers_auto", "kv_offload",
    "kv_cache_type", "batch_size", "model_load_mode", "mlock_model",
    # --reasoning-budget is launch-only, and with thinking_budget_auto on it's
    # derived from the reply limit - so "max_reply_tokens" can move it too. The
    # server decides for real (it recomputes the effective budget both ways
    # and compares); these are here so the widgets resync afterwards.
    "thinking_budget_auto", "thinking_budget", "max_reply_tokens",
    # which GPUs and which engine build (the server compares what a launch would do, so a no-op change does not reload)
    "gpu_placement", "gpu_order", "gpu_disabled", "engine",
)

def describe_restore_skipped(skipped):
    """One line naming what a restore left out ("" when it left nothing out).
    `skipped` is the server's optional `skipped` field of POST /api/restore: a
    damaged backup is restored as far as it is usable, and the user is told what
    was not, rather than being shown a plain "Backup restored"."""
    parts = []
    keys = skipped.get("settings_keys") or []
    if keys:
        shown = ", ".join(keys[:3]) + (", ..." if len(keys) > 3 else "")
        parts.append(f"{len(keys)} setting{'s' if len(keys) != 1 else ''} with an unusable value ({shown})")
    if skipped.get("theme"):
        parts.append("the color theme (its file in the backup was not usable)")
    count = skipped.get("outputs_entries") or 0
    if count:
        parts.append(f"{count} exported file{'s' if count != 1 else ''} that could not be restored safely")
    return "; ".join(parts)


CATEGORIES = (
    ("general", "General", "prompt, sampling"),
    ("model", "Model", "chat model files"),
    ("vision", "Vision", "mmproj files"),
    ("image", "Image model", "stable-diffusion.cpp files"),
    ("appearance", "Appearance", "phosphor, launch screen"),
    ("advanced", "Advanced", "context, GPU, KV cache"),
    ("data", "Data", "limits, folders, reset"),
    (FX_CATEGORY, "Sound & motion", "cues, motion, CRT"),       # B10 (08: appended, so 1-7 keep their meaning)
)


def _fx_pane_unavailable(screen):
    """B10: page 08 as the one-line notice (fx/sound_motion_page.py could not be imported). Never raises."""
    try:
        body = screen._pane(FX_CATEGORY)
        screen._label(body, FX_UNAVAILABLE, dim=True).pack(fill="x")
    except Exception:  # noqa: BLE001 - Settings must open whatever happens here
        pass


class CategoryList(Screen):
    """The Settings category column (B8; approved cs_local.settings_chrome): unboxed kit CategoryRows from
    window y 70, the active category an inverse plate, each row's caption always shown under it (OI-10).
    It keeps the interface DirectoryScreen gave Settings: set_items([DirectoryItem, ...]) draws the rows and
    binds the digits; digit_legend() / on_hide() come from Screen. The rows are rebuilt only when the
    categories themselves change (B10 adds the 8th); otherwise set_items only moves the selection, so a
    category switch, and every on_show, costs no widget churn. .rows reads back the CategoryRows."""

    def __init__(self, parent, ctx):
        super().__init__(parent, ctx)
        self.rows = []
        self.compact = False
        self.body = tk.Frame(self, bg=ctx.theme.bg)
        self.body.pack(anchor="nw", fill="x", pady=(_px(self, CAT_TOP), 0))
        self.bind("<Configure>", self._fit_rows, add="+")

    def set_items(self, items):
        items = list(items)
        if [(it.label, it.caption) for it in items] != [(r.label, r.caption) for r in self.rows]:
            for row in self.rows:
                row.destroy()
            self.rows = []
            for i, it in enumerate(items):
                row = CategoryRow(self.body, self.ctx.theme, i + 1, it.label, it.caption, command=it.on_activate,
                                  selected=it.is_primary)
                row.pack(anchor="w", pady=(0, _px(self, 6)))
                self.rows.append(row)
        else:
            for row, it in zip(self.rows, items):
                row.command = it.on_activate
                row.set_selected(it.is_primary)
        self.bind_digits([it.on_activate or (lambda: None) for it in items])
        self._fit_rows()

    def _fit_rows(self, _e=None):
        """B10-H9: every row with its caption needs CAT_TOP + n x (plate + caption + its padding + the row gap);
        a column shorter than that (a window below ~575 px high: the 820 x 560 minimum, a 1024 x 600 screen)
        closes the rows up - no gap between rows, no padding around the captions - so the last category (08
        since B10) stays whole and clickable; captions stay shown (OI-10, D-27). Decided from the column's own
        height, which the window sets (the column never propagates), and from fixed sizes, never from the rows'
        current padding, so it cannot feed back into itself."""
        try:
            h = int(self.winfo_height())
            if h <= 1 or not self.rows:
                return
            cap = max(int(r.caption_label.winfo_reqheight()) for r in self.rows)
            natural = _px(self, CAT_TOP) + len(self.rows) * (_px(self, kset.CAT_H) + cap + 4 + _px(self, 6))
            compact = natural > h
            if compact == self.compact:
                return
            self.compact = compact
            for row in self.rows:
                row.pack_configure(pady=(0, 0 if compact else _px(self, 6)))
                row.caption_label.pack_configure(pady=(0, 0) if compact else (1, 3))
        except (tk.TclError, ValueError, AttributeError):
            pass


class SettingsScreen(Screen):
    def __init__(self, parent, ctx):
        super().__init__(parent, ctx)
        theme = ctx.theme
        self.api = ctx.api
        self._settings = {}
        # _load() fetches off the Tk thread (see async_call.py). Until it has
        # succeeded the widgets only hold defaults, so Save is refused: pressing
        # it early would otherwise write those defaults over the real settings.
        self._load_loader = Latest(self)
        self._settings_loaded = False
        self._n_ctx_train = 131072
        # Filled from /api/model on load and on every model switch - empty
        # until then, which _sync_thinking_controls reads as "can't think"
        # and so shows the controls greyed rather than wrongly enabled.
        self._thinking_cap = {}
        self._thinking_budget_effective = None
        self._vram_after_id = None
        self._vram_request_seq = 0
        self._orig_restart_values = {}
        self._active_category = "general"
        self._live_mem_after_id = None
        self._dirty = _NoDirty()          # B8d: the real tracker is attached at the end of __init__

        _, self._refresh_datetime = make_header(self, theme, "Settings")
        self._t = _tokens(theme)
        # every caption wraps to the pane's real content width (770 px at 1100 x 800), so none is ever cut
        self._wrap = WrapGroup(width=_px(self, PANE_CONTENT_W))
        self._pane_canvas = {}
        self._pane_inner = {}
        self._wheel_acc = 0

        body = tk.Frame(self, bg=theme.bg)
        body.pack(fill="both", expand=True)

        left = tk.Frame(body, bg=theme.bg, width=_px(self, CAT_COL_W))
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        self.category_list = CategoryList(left, ctx) if KIT else DirectoryScreen(left, ctx, columns=1)
        self.category_list.pack(fill="both", expand=True)

        self.detail = tk.Frame(body, bg=theme.bg)
        self.detail.pack(side="left", fill="both", expand=True, padx=(0, _px(self, PANE_RIGHT)),
                         pady=(_px(self, PANE_TOP), 0))
        self.detail.bind("<Configure>", self._on_detail_configure, add="+")

        self._panes = {}
        self._build_general_pane()
        self._build_model_pane()
        self._build_vision_pane()
        self._build_image_pane()
        self._build_appearance_pane()
        self._build_advanced_pane()
        self._build_data_pane()
        self.fx_page = None
        install_fx_pane(self)       # B10: page 08 "Sound & motion" (screen.fx_page); never raises

        # No footer bar: SAVE (primary) with the pane's timing stamp left of it; the transient status
        # (status_label) takes the stamp's place while it shows. Built after the panes (so Tab reaches SAVE last)
        # but packed BEFORE the body, so a small window squeezes the pane, never the footer.
        self.footer = SettingsFooter(self, theme, command=self._on_save, stamp="Changes apply on save")
        self.footer.pack(side="bottom", fill="x", before=body, padx=(_px(self, FOOT_LEFT), _px(self, FOOT_RIGHT)),
                         pady=(_px(self, FOOT_PADY[0]), _px(self, FOOT_PADY[1])))
        self.status_label = self.footer.status
        self.save_btn = self.footer.save_btn
        # the wheel scrolls the shown pane from anywhere inside it (make_scrollable binds only its canvas and
        # inner frame, so a wheel over a caption, a slider or a field did nothing): one command, one bindtag
        self._wheel_tag = "KitSettingsWheel%x" % id(self)
        self.bind_class(self._wheel_tag, "<MouseWheel>", "%s %%D" % self.register(self._on_pane_wheel))

        # B8d (D-7, ST-13): the unsaved-changes guard. A local compare of the body Save would send against a
        # snapshot of the last load / save (kit/dirty.py), recomputed synchronously on every variable write and on
        # the system prompt's <<Modified>>; the footer stamp reads UNSAVED · n CHANGES, or UNSAVED CHANGES KEPT · n
        # + REVERT after a reload that kept the edits (_load). The GPU section reports its own edits (on_change)
        # and its async inventory answer is not one (quiet).
        if DirtyTracker is not None:
            self._dirty = DirtyTracker(self, text_widgets=(self.system_prompt_text,), on_change=self._on_dirty_change)
            self._dirty_stamp = DirtyStamp(self.footer, theme, rest="Changes apply on save",
                                           on_revert=self._revert_unsaved)
            self.gpu_section.on_change = self._dirty.recompute
            self.gpu_section.quiet = self._dirty.quiet
            self._dirty.attach()
        else:
            self._dirty = _NoDirty()
            self._dirty_stamp = None

    def destroy(self):
        try:
            self.unbind_class(self._wheel_tag, "<MouseWheel>")
        except (tk.TclError, AttributeError):
            pass
        dirty = getattr(self, "_dirty", None)
        if dirty is not None:
            dirty.detach()            # B8d: the variable traces go with the screen
        super().destroy()

    def _on_dirty_change(self, tracker):
        """B8d: the footer stamp follows the tracker (the transient status still takes its place while it shows)."""
        if self._dirty_stamp is not None:
            if self._active_category == FX_CATEGORY:     # B10: page 08 applies at once - no UNSAVED / REVERT there
                self._dirty_stamp.show(0)
            else:
                self._dirty_stamp.show(tracker.count(), tracker.kept, bool(tracker.invalid))

    def _revert_unsaved(self):
        """B8d REVERT: the screen goes back to the last loaded / saved values (the snapshot, which a model switch
        rebased), then is clean again."""
        clean = self._dirty.clean
        if clean is None:
            return
        self._populate({**self._settings, **clean})
        self._update_running_config_display(self._settings)
        self._orig_restart_values = {k: self._settings.get(k) for k in RESTART_KEYS}
        self._dirty.mark_clean()
        self._schedule_vram_estimate()

    # ---- widget helpers (phosphor-styled equivalents of the old dialog's) ----
    # B8: the kit look, signatures unchanged (gpu_section.py builds with _label / _caption / _check).

    def _entry(self, parent, textvariable=None, **kwargs):
        """A tk.Entry inside a kit Well (1 px dim edge; 2 px bright inside while focused). The well is packed
        into `parent` here (fill="x"); the caller packs the returned Entry, which lands inside the well
        (.kit_well is the well)."""
        t = self._t
        well = Well(parent, self.ctx.theme, ground=t["bg"])
        well.pack(fill="x")
        pad = tk.Frame(well.body, bg=t["panel"])
        pad.pack(fill="both", expand=True, padx=_px(self, 7))
        kwargs.setdefault("bg", t["panel"])
        kwargs.setdefault("fg", t["bright"])
        kwargs.setdefault("insertbackground", t["bright"])
        kwargs.setdefault("relief", "flat")
        kwargs.setdefault("font", C11)
        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("bd", 0)
        entry = tk.Entry(pad, textvariable=textvariable, **kwargs)
        well.attach(entry)
        entry.kit_well = well
        return entry

    def _label(self, parent, text, dim=False, **kwargs):
        """A label C11 bright, or with dim=True a caption-style C9 value line that wraps to the pane width
        (unless the caller gives its own wraplength)."""
        t = self._t
        kwargs.setdefault("bg", t["bg"])
        kwargs.setdefault("fg", t["value"] if dim else t["bright"])
        kwargs.setdefault("anchor", "w")
        kwargs.setdefault("font", C9 if dim else C11)
        for opt in ("bd", "padx", "pady"):            # the text starts on the content column's edge
            kwargs.setdefault(opt, 0)
        auto_wrap = dim and "wraplength" not in kwargs
        if dim:
            kwargs.setdefault("justify", "left")
        label = tk.Label(parent, text=text, **kwargs)
        if auto_wrap:
            self._wrap.add(label)
        return label

    def _check(self, parent, text, variable, command=None):
        """A kit lamp: still a tk.Checkbutton, its text exactly `text` (a lamp never rewrites its text)."""
        lamp = LampCheck(parent, self.ctx.theme, text=text, variable=variable, command=command, ground=self._t["bg"])
        if KIT:     # B8 FIX1 (ADV1-02, D-27): the lamp's text wraps with the pane like a caption (the image takes ~22 px)
            self._wrap.add(lamp, _px(self, LAMP_INDENT + 2))
        return lamp

    def _section(self, parent, title, tag=None, first=False):
        """A section row (title C10B bright, upper-cased; an optional exception tag in C9 value)."""
        row = SectionRow(parent, self.ctx.theme, title, tag=tag, ground=self._t["bg"])
        row.pack(fill="x", pady=(0 if first else _px(self, SECTION_GAP), 0))
        return row

    def _labeled_scale(self, parent, label, var, lo, hi, display=None, step=None):
        """Label (C11 bright) and its readout (C10B bright, right-aligned) on one row, the kit slider under them
        (still a ttk.Scale; the readout is still a tk.Label). A disabled slider inks its label value and its
        readout dim (KitScale)."""
        t = self._t
        row = tk.Frame(parent, bg=t["bg"])
        row.pack(fill="x", pady=(_px(self, ROW_GAP), 0))
        name_label = tk.Label(row, text=label, bg=t["bg"], fg=t["bright"], font=C11, anchor="w", bd=0, padx=0, pady=0)
        name_label.pack(side="left")
        value_label = tk.Label(row, text="", bg=t["bg"], fg=t["bright"], font=C10B, anchor="e", bd=0, padx=0, pady=0)
        value_label.pack(side="right")

        def update_label(*_):
            v = var.get()
            value_label.config(text=display(v) if display else f"{v:.2f}")

        scale = KitScale(parent, self.ctx.theme, from_=lo, to=hi, orient="horizontal", variable=var, step=step)
        scale.pack(fill="x", pady=(_px(self, 3), 0))
        scale.attach(name_label, value_label)
        # A trace on the variable itself, not ttk.Scale's own command=
        # option - command= only fires on a user drag/click, never when the
        # bound variable is set from code (e.g. _populate()/
        # _sync_restart_widgets() after a load or save), so the number here
        # went stale the instant a value changed for any reason other than
        # physically touching the slider. It was worse for GPU layers
        # specifically: a later .configure(command=...) elsewhere (wiring
        # the VRAM estimate) silently replaced this callback entirely, so
        # that label never updated even on a real drag. A variable trace
        # fires uniformly regardless of what's driving the change (drag,
        # keyboard, or code) and can't be clobbered by a later command=
        # override on the widget, since it isn't attached to the widget at
        # all - found live, screenshot showed the slider thumb sitting at
        # the correct dragged/loaded position while its own label still
        # read "99" (the value at widget-construction time).
        var.trace_add("write", update_label)
        update_label()
        return scale, value_label

    def _caption(self, parent, text, indent=0):
        """A caption: C9 value, the FULL text (D-27 #1: never shortened or folded), wrapped to the pane width.
        indent= (px) puts it under a lamp's text. -> the Label."""
        t = self._t
        label = tk.Label(parent, text=text, bg=t["bg"], fg=t["value"], font=C9, anchor="w", justify="left", bd=0,
                         padx=0, pady=0)
        label.pack(fill="x", padx=(_px(self, indent), 0), pady=(_px(self, 4), 0))
        self._wrap.add(label, _px(self, indent))
        return label

    def _add_resize_grip(self, container, text_widget, min_height=3, max_height=60):
        """Small drag handle in `container`'s bottom-right corner that
        resizes `text_widget`'s height (in text lines) by dragging - Tk has
        no native resizable textarea. `container` must shrink-wrap exactly
        to `text_widget` (pack it alone, no fill/expand siblings) so the
        grip's relx=1.0/rely=1.0 placement lands on the widget's own corner,
        not some unrelated container edge. B8: the grip is the kit's three
        1 px steps (kit.settings.ResizeGrip), dim on the well's panel."""
        grip = ResizeGrip(container, self.ctx.theme, ground=self._t["panel"])
        grip.place(relx=1.0, rely=1.0, anchor="se")
        drag = {"start_y": 0, "start_height": 0}

        def on_press(event):
            drag["start_y"] = event.y_root
            drag["start_height"] = int(text_widget.cget("height"))

        def on_drag(event):
            # font metrics through Tcl: a tkinter.font.Font per motion event created (and deleted) a named font each time
            line_height = max(1, int(text_widget.tk.call("font", "metrics", text_widget.cget("font"), "-linespace")))
            delta_lines = round((event.y_root - drag["start_y"]) / line_height)
            new_height = max(min_height, min(max_height, drag["start_height"] + delta_lines))
            if new_height != int(text_widget.cget("height")):
                text_widget.configure(height=new_height)

        grip.bind("<ButtonPress-1>", on_press)
        grip.bind("<B1-Motion>", on_drag)
        return grip

    def _pane(self, key):
        # Wrapped in make_scrollable rather than a plain Frame - the General
        # pane alone (5 sampling sliders each with a multi-line caption) can
        # outgrow a normal window height, and _show_category's pack/
        # pack_forget below operates on whatever's stored in self._panes, so
        # the scrollable *outer* goes there while callers still build their
        # widgets into the returned frame. B8: no frame, the kit's thin
        # scrollbar; the returned frame is padded to the approved content
        # column (window x 290..1060).
        outer, inner = make_scrollable(self.detail, self.ctx.theme)
        self._panes[key] = outer
        self._pane_inner[key] = inner
        self._pane_canvas[key] = inner.master
        body = tk.Frame(inner, bg=self._t["bg"])
        body._kit_pad_r = _px(self, PANE_PAD_R + PANE_SB_W)      # no scrollbar yet (make_scrollable shows it later)
        body.pack(fill="both", expand=True, padx=(_px(self, PANE_PAD_L), body._kit_pad_r),
                  pady=(_px(self, PANE_PAD_T), _px(self, PANE_PAD_B)))
        sb = next((w for w in outer.winfo_children() if isinstance(w, ttk.Scrollbar)), None)
        if sb is not None:
            inner.master.bind("<Configure>", lambda _e, b=body, s=sb: self._fit_pane_pad(b, s), add="+")
        return body

    def _fit_pane_pad(self, body, scrollbar):
        """Keeps the content's right edge at window x 1060 whether or not the pane shows its scrollbar:
        make_scrollable hides the scrollbar while everything fits, and the canvas then grows by its width. The
        caption wrap already reserves that width (_on_detail_configure), so no caption re-wraps either way."""
        try:
            right = _px(self, PANE_PAD_R + (0 if scrollbar.winfo_manager() else PANE_SB_W))
            if body._kit_pad_r != right:
                body._kit_pad_r = right
                body.pack_configure(padx=(_px(self, PANE_PAD_L), right))
        except tk.TclError:
            pass

    def _on_detail_configure(self, event):
        """Captions follow the pane's real width (the scrollbar's room is kept, so a pane that grows a scrollbar
        never re-wraps)."""
        width = int(event.width) - _px(self, PANE_SB_W + PANE_PAD_L + PANE_PAD_R)
        if width > 40:
            self._wrap.set_width(width)

    def _bind_pane_wheel(self, key):
        """Puts the screen's wheel bindtag on every widget of pane `key` that lacks it (rows added later - GPU
        rows, file lists - get it on the next show). Widgets with a wheel of their own keep theirs only: a Text
        scrolls itself, a Treeview / Listbox scroll their lists, a Combobox changes its value."""
        inner = self._pane_inner.get(key)
        if inner is None:
            return
        tag = self._wheel_tag
        stack = list(inner.winfo_children())
        while stack:
            w = stack.pop()
            try:
                stack.extend(w.winfo_children())
                if w.winfo_class() in ("Text", "Treeview", "TCombobox", "Listbox"):
                    continue
                tags = w.bindtags()
                if tag not in tags:
                    w.bindtags(tags + (tag,))
            except tk.TclError:
                continue

    def _on_pane_wheel(self, delta):
        """Scrolls the shown pane by whole notches (sub-notch deltas from precision touchpads add up)."""
        canvas = self._pane_canvas.get(self._active_category)
        try:
            self._wheel_acc += int(float(delta))
        except (TypeError, ValueError):
            return
        steps = int(self._wheel_acc / 120)
        if not steps or canvas is None:
            return
        self._wheel_acc -= steps * 120
        try:
            canvas.yview_scroll(-steps, "units")
        except tk.TclError:
            pass

    # ---- lifecycle ----

    def on_show(self):
        self._refresh_datetime()
        self._set_category_items()
        self._load()
        # the model lists were built once: a .gguf copied into models\ while the app ran never appeared until a
        # restart (the first real-PC report) - they re-list, off the Tk thread, every time Settings is shown
        for panel in (getattr(self, "models_panel", None), getattr(self, "vision_panel", None),
                      getattr(self, "image_model_panel", None)):
            if panel is not None:
                panel.refresh()
        self._show_category(self._active_category)

    def on_hide(self):
        super().on_hide()
        self.category_list.on_hide()
        self._stop_live_memory_poll()
        self.gpu_section.on_hide()
        if getattr(self, "fx_page", None) is not None:
            self.fx_page.set_shown(False)       # B10: page 08's sections hear that they are no longer shown

    def _set_category_items(self):
        items = [
            DirectoryItem(
                label, caption,
                on_activate=lambda k=key: self._show_category(k),
                is_primary=(key == self._active_category),
            )
            for key, label, caption in CATEGORIES
        ]
        self.category_list.set_items(items)

    def _show_category(self, key):
        was_08 = self._active_category == FX_CATEGORY
        self._active_category = key
        for pane_key, pane in self._panes.items():
            if pane_key == key:
                pane.pack(fill="both", expand=True)
            else:
                pane.pack_forget()
        self._set_category_items()  # moves the inverse plate to the selection (B8: restyled in place, no rebuild)
        self._wheel_acc = 0
        self._bind_pane_wheel(key)
        if key == "advanced":
            if self._live_mem_after_id is None:      # B8-H1: ONE chain; another selection must not stack one
                self._start_live_memory_poll()
        else:
            self._stop_live_memory_poll()
        self._sync_page_08()
        if was_08 != (key == FX_CATEGORY):
            self._legend_keys_changed()     # B13: the legend gains / loses ALT+M mute with page 08

    def _set_document_tool_note(self, model_info):
        """The live note under the document tool's switch: shown only while the ACTIVE model's chat template cannot
        call tools (GET /api/model's tool_support False); names an installed tool-capable model when there is one."""
        note = getattr(self, "document_tool_note", None)
        if note is None:
            return
        info = model_info if isinstance(model_info, dict) else {}
        lacks = info.get("tool_support") is False
        text = ""
        if lacks:
            text = "The active model can't call tools, so no documents will be created with it - switch to a model that supports tools"
            names = [n for n in (info.get("tool_capable_models") or []) if isinstance(n, str) and n]
            if names:
                text += f" (for example {names[0]})"
            text += "."
        self.document_tool_note_var.set(text)
        try:
            shown = bool(note.winfo_manager())
            if lacks and not shown:
                note.pack(fill="x", padx=(_px(self, LAMP_INDENT), 0), pady=(_px(self, 4), 0), after=self._document_tool_caption)
            elif shown and not lacks:
                note.pack_forget()
        except tk.TclError:
            pass

    def _set_fit_note(self, fit):
        """A3: the RUNNING record's one-line note while the model is larger than RAM + VRAM (the same sentence as Home)."""
        note = getattr(self, "fit_note", None)
        if note is None:
            return
        too_large = bool(isinstance(fit, dict) and fit.get("too_large"))
        text = ""
        if too_large:
            text = (f"This model is larger than RAM + VRAM ({float(fit.get('model_gb') or 0):.1f} GB vs "
                    f"{float(fit.get('memory_gb') or 0):.1f} GB) - it may fail to load or run very slowly.")
        self.fit_note_var.set(text)
        try:
            shown = bool(note.winfo_manager())
            if too_large and not shown:
                note.pack(fill="x", pady=(_px(self, 6), 0), after=self._running_note)
            elif shown and not too_large:
                note.pack_forget()
        except tk.TclError:
            pass

    def _sync_page_08(self):
        """B10: page 08 (Sound & motion) applies every change at once, so while it is the active category the footer
        stamp reads 'Changes apply at once' and SAVE is hidden (with no UNSAVED count or REVERT: the unsaved edits of
        pages 01-07 are kept and their stamp returns on those pages); the page hears whether it is shown."""
        at_once = self._active_category == FX_CATEGORY
        footer = getattr(self, "footer", None)
        if footer is not None:
            stamp = getattr(self, "_dirty_stamp", None)
            if stamp is not None:
                stamp.rest = FX_STAMP if at_once else SAVE_STAMP
                self._on_dirty_change(self._dirty)
            elif hasattr(footer, "set_stamp"):
                footer.set_stamp(FX_STAMP if at_once else SAVE_STAMP)
            save = self.save_btn
            try:
                if at_once and save.winfo_manager():
                    try:
                        focus = self.focus_get()
                    except KeyError:        # tkinter cannot name a focus widget it did not make (a popdown)
                        focus = None
                    if focus is not None and (focus is save or focus is getattr(stamp, "revert", None)):
                        self._focus_active_category()       # never leave the keyboard on a hidden SAVE
                if hasattr(footer, "set_save_shown"):
                    footer.set_save_shown(not at_once)      # the kit footer: the stamp then ends where SAVE did
                elif at_once and save.winfo_manager():
                    save.pack_forget()
                elif not at_once and not save.winfo_manager():
                    first = next(iter(footer.pack_slaves()), None)
                    if first is not None:
                        save.pack(side="right", before=first)
                    else:
                        save.pack(side="right")
            except tk.TclError:
                pass
        if getattr(self, "fx_page", None) is not None:
            self.fx_page.set_shown(at_once)

    def _focus_active_category(self):
        rows = getattr(self.category_list, "rows", None) or []
        keys = [c[0] for c in CATEGORIES]
        try:
            idx = keys.index(self._active_category)
            target = rows[idx].plate if idx < len(rows) else self
            target.focus_set()
        except (ValueError, tk.TclError, AttributeError):
            pass

    # ---- loading (ported from SettingsDialog._load) ----

    def _load(self):
        self._settings_loaded = False
        api = self.api

        def fetch():
            load_error = None
            try:
                settings = api.get_settings()
            except ApiError as e:
                settings, load_error = {}, e
            try:
                model_info = api.get_model_info()
            except ApiError:
                model_info = None
            return settings, load_error, model_info

        def apply(result):
            settings, load_error, model_info = result
            if load_error is not None and self._dirty.clean is not None:
                # B8 FIX2 (ADV2-02, D-7): a failed RE-read keeps the screen - the last good values plus the user's
                # edits - instead of defaults; Save stays refused (_settings_loaded False) until a good read keeps them
                self.status_label.config(text=f"could not load settings: {load_error}")
                return
            self._settings = settings
            if load_error is not None:
                self.status_label.config(text=f"could not load settings: {load_error}")
            elif str(self.status_label.cget("text")).startswith("could not load settings"):
                self.status_label.config(text="")      # B8 FIX2 (ADV2-01): a good read clears a stale load error
            if model_info is not None:
                self._n_ctx_train = model_info.get("n_ctx_train") or 131072
                self.model_name_var.set(model_info.get("model") or "(no model loaded)")
                self._set_fit_note(model_info.get("fit"))
                self._set_document_tool_note(model_info)
                self._thinking_cap = model_info.get("thinking") or {}
                self._thinking_budget_effective = model_info.get("thinking_budget_effective")
            if load_error is None:
                # B8d (D-7, the C-3 hunk): unsaved edits survive a reload. The fresh values are populated and become
                # the clean snapshot; the user's edits then go back on top (UNSAVED CHANGES KEPT · n + REVERT), while
                # 'Currently running' and the restart snapshot keep the server's values.
                keep = self._dirty.user_values()              # {} when clean is None or nothing differs
                self._populate(self._settings)
                self._dirty.mark_clean()
                if keep:
                    self._populate({**self._settings, **keep})
                    self._update_running_config_display(self._settings)
                    self._orig_restart_values = {k: self._settings.get(k) for k in RESTART_KEYS}
                    self._dirty.mark_kept()
            else:
                self._dirty.discard()       # defaults on screen after a failed read: nothing to flag or to keep
                self._populate(self._settings)
            self._schedule_vram_estimate()
            # A failed settings read leaves defaults on screen - not something to save.
            self._settings_loaded = load_error is None

        self._load_loader.start(
            fetch, apply,
            lambda e: self.status_label.config(text=f"could not load settings: {e}"),
        )

    def _populate(self, s):
        with self._dirty.held():          # B8d: one unsaved-changes recompute for the whole populate, not one per field
            self._populate_fields(s)

    def _populate_fields(self, s):
        self.system_prompt_text.delete("1.0", "end")
        self.system_prompt_text.insert("1.0", s.get("system_prompt", ""))
        self.temperature_var.set(s.get("temperature", 0.7))
        self.repeat_penalty_enabled_var.set(s.get("repeat_penalty_enabled", False))
        self.repeat_penalty_var.set(s.get("repeat_penalty", 1.1))
        self._on_repeat_penalty_toggle()
        self.dry_multiplier_var.set(s.get("dry_multiplier", 0.8))
        self.presence_penalty_var.set(s.get("presence_penalty", 0.0))
        self.tools_enabled_var.set(s.get("tools_enabled", False))
        self.document_tool_var.set(s.get("document_tool_enabled", True))
        self.min_p_var.set(s.get("min_p", 0.05))
        self.top_p_var.set(s.get("top_p", 0.95))
        self.top_k_var.set(s.get("top_k", 40))
        verbosity = s.get("verbosity", "normal")
        self.verbosity_var.set(
            RESPONSE_LENGTH_LEVELS.index(verbosity) if verbosity in RESPONSE_LENGTH_LEVELS else 1
        )
        limit = s.get("max_reply_tokens", 0)
        self.max_reply_tokens_var.set(limit if isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit > 0 else 0)

        mode = s.get("thinking_mode", "auto")
        self.thinking_mode_var.set(mode if mode in THINKING_MODES else "auto")
        effort = s.get("thinking_effort", "medium")
        self.thinking_effort_var.set(effort if effort in THINKING_EFFORTS else "medium")
        self.thinking_budget_auto_var.set(s.get("thinking_budget_auto", True))
        self.thinking_budget_var.set(s.get("thinking_budget", 1024))
        self._sync_thinking_controls()

        self.context_size_var.set(s.get("context_size", 16384))
        self.context_max_label.config(text=f"max {self._n_ctx_train:,}")
        self.max_parallel_chats_var.set(s.get("max_parallel_chats", 1))
        self.gpu_layers_auto_var.set(s.get("gpu_layers_auto", True))
        self.gpu_layers_var.set(s.get("gpu_layers", 99))
        self._on_gpu_layers_auto_toggle()
        self.kv_offload_var.set(s.get("kv_offload", True))
        self.kv_cache_type_var.set(s.get("kv_cache_type", "q8_0"))
        self.batch_size_var.set(s.get("batch_size", 2048))
        self.gpu_section.load(s)
        self._update_running_config_display(s)
        self.model_load_mode_var.set(MODEL_LOAD_LABELS.get(s.get("model_load_mode", "auto"), MODEL_LOAD_LABELS["auto"]))
        self.mlock_model_var.set(s.get("mlock_model", False))

        self.outputs_dir_var.set(s.get("outputs_dir") or "")
        self.confirm_save_location_var.set(s.get("confirm_save_location", False))
        self.max_upload_mb_var.set(s.get("max_upload_mb", 20))
        self.max_file_tokens_var.set(s.get("max_file_tokens", 25_000))
        self.project_pct_var.set(s.get("project_content_budget_pct", 0.5))
        self._update_project_budget_preview()

        # snapshot for restart-needed detection at save time
        self._orig_restart_values = {k: s.get(k) for k in RESTART_KEYS}

    # ---- General ----

    def _build_general_pane(self):
        theme = self.ctx.theme
        f = self._pane("general")

        t = self._t
        self._section(f, "Instructions", first=True)
        self._label(f, "System prompt").pack(fill="x", pady=(_px(self, 10), _px(self, 4)))
        # the prompt sits in a kit Well (1 px dim edge, 2 px bright inside while focused); the grip rides the
        # well body's corner, which shrink-wraps the Text as _add_resize_grip requires
        self.system_prompt_well = Well(f, theme, ground=t["bg"])
        self.system_prompt_well.pack(fill="x")
        prompt_frame = self.system_prompt_well.body
        self.system_prompt_text = tk.Text(
            prompt_frame, height=5, wrap="word", bg=t["panel"], fg=t["bright"], insertbackground=t["bright"],
            relief="flat", font=theme_mod.FONT_MONO, highlightthickness=0, bd=0, padx=_px(self, 7),
            pady=_px(self, 5),
        )
        self.system_prompt_text.pack(fill="x")
        self.system_prompt_well.attach(self.system_prompt_text)
        self._add_resize_grip(prompt_frame, self.system_prompt_text)

        self._section(f, "Sampling")
        self.temperature_var = tk.DoubleVar(value=0.7)
        self._labeled_scale(f, "Temperature", self.temperature_var, 0.0, 1.5)
        self._caption(
            f, "Controls randomness. Lower is more focused and predictable; higher is more varied and creative. "
            "Safe range: 0.2-1.0 (default 0.7). Above ~1.2, especially with Top P and Top K below both wide "
            "open, the model can ramble in its reasoning and never reach a final answer."
        )

        self.dry_multiplier_var = tk.DoubleVar(value=0.8)
        self._labeled_scale(f, "Anti-repetition (DRY)", self.dry_multiplier_var, 0.0, 2.0)
        self._caption(
            f, "The main guard against a long reply looping or drifting into waffle. It watches for phrases "
            "the model has already used and steers it away from repeating them, while leaving ordinary "
            "wording alone. Safe range: 0.5-1.2 (default 0.8); 0 turns it off. This is the setting to raise "
            "first if long answers start going in circles."
        )

        self.repeat_penalty_enabled_var = tk.BooleanVar(value=False)
        self._check(f, "Also penalise repeated words (legacy)", self.repeat_penalty_enabled_var,
                    command=self._on_repeat_penalty_toggle).pack(anchor="w", pady=(_px(self, ROW_GAP), 0))
        self.repeat_penalty_var = tk.DoubleVar(value=1.1)
        self.repeat_penalty_scale, self.repeat_penalty_label = self._labeled_scale(
            f, "Repeat penalty", self.repeat_penalty_var, 1.0, 1.5
        )
        self._caption(
            f, "The older, blunter version of the setting above: it penalises individual words rather than "
            "phrases, so the longer a reply gets the more it pushes the model away from the ordinary "
            "connecting words good prose needs - which tends to make long answers worse, not better. Off by "
            "default now that DRY is doing this job. Only turn it on if DRY alone isn't holding, and drop "
            "DRY when you do."
        )

        self.presence_penalty_var = tk.DoubleVar(value=0.0)
        self._labeled_scale(f, "Presence penalty", self.presence_penalty_var, 0.0, 2.0)
        self._caption(
            f, "Nudges the model to bring in new ideas instead of circling the same few. Unlike the repeat "
            "penalty it doesn't get harsher as the reply grows, so it's safe on long answers. Default 0 "
            "(off); try 0.5-1.5 if replies feel repetitive in substance rather than wording. Some model "
            "cards ask for a specific value here - check the model's page."
        )

        self.min_p_var = tk.DoubleVar(value=0.05)
        self._labeled_scale(f, "Min P", self.min_p_var, 0.0, 0.5)
        self._caption(
            f, "Drops any token far less likely than the top choice at that step. Higher values narrow the "
            "model to safer, more predictable words; 0 disables this filter. Safe range: 0.02-0.1 (default 0.05)."
        )

        self.top_p_var = tk.DoubleVar(value=0.95)
        self._labeled_scale(f, "Top P", self.top_p_var, 0.0, 1.0)
        self._caption(
            f, "Keeps only the smallest group of top tokens whose combined probability reaches this value "
            "(“nucleus sampling”). Lower values narrow the choices; 1.0 disables this filter. Safe range: "
            "0.85-0.98 (default 0.95). Avoid combining 1.0 here with Top K at 0 — see the Top K note below."
        )

        self.top_k_var = tk.IntVar(value=40)
        self._labeled_scale(f, "Top K", self.top_k_var, 0, 100, display=lambda v: f"{int(round(v))}")
        self._caption(
            f, "Keeps only the K most-likely tokens at each step and discards the rest outright. Lower values "
            "narrow the choices; 0 disables this filter. Safe range: 20-60 (default 40). Confirmed live: "
            "setting this to 0 at the same time as Top P above is 1.0 and Temperature above ~1.2 let the model "
            "wander in its reasoning indefinitely without ever producing a final answer — leave at least one "
            "of Top P or Top K actually restricting tokens."
        )

        self.verbosity_var = tk.IntVar(value=1)
        self._labeled_scale(
            f, "Response length", self.verbosity_var, 0, len(RESPONSE_LENGTH_LEVELS) - 1,
            display=lambda v: RESPONSE_LENGTH_NAMES[RESPONSE_LENGTH_LEVELS[int(round(v))]],
        )
        self._caption(
            f, "Brief: a few sentences. Standard: the model decides. Detailed and Long-form ask for more, "
            "but they don't just say \"be thorough\" - they tell the model to plan its sections first, make "
            "each point once, skip the filler intro and the summary that repeats the body, and stop when "
            "it's done rather than padding to fill space. That structure is what keeps a long answer from "
            "wandering. This only changes what the model is told - it never cuts a reply off."
        )

        self.max_reply_tokens_var = tk.DoubleVar(value=0)
        self.max_reply_tokens_scale, self.max_reply_tokens_label = self._labeled_scale(
            f, "Reply limit", self.max_reply_tokens_var, 0, REPLY_LIMIT_MAX, step=REPLY_LIMIT_STEP,
            display=lambda v: "No limit" if self._reply_limit_value(v) <= 0 else f"{self._reply_limit_value(v):,} tokens",
        )
        self._caption(
            f, "An optional hard stop for ONE reply, thinking included. No limit (the default) lets the model "
            "finish what it is writing; the only thing that can still cut a reply short is the context window "
            "(Advanced > Context size), and then the reply says so. Set a limit only if you want one, for "
            "instance to stop a model that never finishes: at that many tokens the reply is cut off, and "
            "says so. Thinking has its own limit, below."
        )

        # ---- Thinking (reasoning models) ----
        # Every widget here stays visible on a model that can't think and is
        # greyed out with an explanation instead of being hidden, so it's
        # clear the app knows the difference rather than the section having
        # silently vanished.
        self._section(f, "Thinking")
        self.thinking_status_var = tk.StringVar(value="")
        self.thinking_status_label = tk.Label(
            f, textvariable=self.thinking_status_var, bg=t["bg"], fg=t["value"], anchor="w",
            justify="left", font=C9, bd=0, padx=0, pady=0,
        )
        self.thinking_status_label.pack(fill="x", pady=(_px(self, 8), 0))
        self._wrap.add(self.thinking_status_label)

        # Thinking mode and effort as Segmented words (OI-16). The attribute names are kept: they are the
        # controls _sync_thinking_controls enables and disables (configure(state=) works on Segmented).
        mode_row = self._option_row(f)
        self.thinking_mode_label = self._label(mode_row, "Thinking")
        self.thinking_mode_label.grid(row=0, column=0, sticky="w")
        self.thinking_mode_var = tk.StringVar(value="auto")
        self.thinking_mode_combo = Segmented(mode_row, theme, options=THINKING_MODES,
                                             variable=self.thinking_mode_var, ground=t["bg"])
        self.thinking_mode_combo.grid(row=0, column=1, sticky="w")

        effort_row = self._option_row(f)
        self.thinking_effort_label = self._label(effort_row, "Thinking effort")
        self.thinking_effort_label.grid(row=0, column=0, sticky="w")
        self.thinking_effort_var = tk.StringVar(value="medium")
        self.thinking_effort_combo = Segmented(effort_row, theme, options=THINKING_EFFORTS,
                                               variable=self.thinking_effort_var, ground=t["bg"])
        self.thinking_effort_combo.grid(row=0, column=1, sticky="w")

        self.thinking_budget_auto_var = tk.BooleanVar(value=True)
        self.thinking_budget_auto_check = self._check(
            f, "Automatic thinking limit (what the context window can spare)",
            self.thinking_budget_auto_var, command=self._sync_thinking_controls,
        )
        self.thinking_budget_auto_check.pack(anchor="w", pady=(_px(self, ROW_GAP), 0))
        self.thinking_budget_var = tk.DoubleVar(value=1024)
        self.thinking_budget_scale, self.thinking_budget_label = self._labeled_scale(
            f, "Thinking budget", self.thinking_budget_var, 0, 32768, step=256,
            display=self._thinking_budget_display,
        )
        self._caption(
            f, "How many tokens the model may spend thinking before it is forced to start answering. "
            "Automatic lets it use up to 70% of the context window: a model that thinks with no limit at "
            "all can fill the whole window with its thoughts and never reach an answer, because the "
            "window is a hard stop. (With a Reply limit set, thinking also never gets more than half of "
            "it.) Untick Automatic to set a number yourself; 0 means no limit whatsoever - only choose "
            "that if you are happy for a very long thought to use up the window. Changing this restarts "
            "the model server (llama.cpp only accepts it at launch)."
        )
        self._caption(
            f, "Thinking effort (on a model that uses think tags, with Automatic on) is how much of the context "
            "window that Automatic limit may use: low 25%, medium 70% (the default), high 80%, max everything "
            "but room for the answer. To think for really long, raise the Context size under Advanced as well - "
            "the limit is a share of it. Changing the effort restarts the model server."
        )

        self._section(f, "Tools")
        self.tools_enabled_var = tk.BooleanVar(value=False)
        self._check(f, "Enable tool calling (calculator)", self.tools_enabled_var).pack(
            anchor="w", pady=(_px(self, 10), 0))
        self._caption(
            f, "Lets the model call a built-in calculator tool for arithmetic instead of computing it itself. "
            "Off by default: only some models actually support this (tested against this app's own bundled "
            "models), and unsupported models may ignore it or behave oddly. Safe to try; turn off if the model "
            "seems confused.", indent=LAMP_INDENT,
        )
        # Document tool (tests/DOCUMENT_TOOL_SPEC.md): its own switch next to the calculator's, on by default, and a
        # live note under it while the ACTIVE model's template cannot call tools (see _set_document_tool_note).
        self.document_tool_var = tk.BooleanVar(value=True)
        self._check(f, "Enable the document tool (the model can write documents as cards)", self.document_tool_var).pack(
            anchor="w", pady=(_px(self, 10), 0))
        self._document_tool_caption = self._caption(
            f, "Lets the model create Markdown (.md) or text (.txt) documents that appear in the chat as cards you can "
            "open, save and download - ask for a system prompt, a guide, notes or a plan. On by default; the model only "
            "uses it when it supports tool calls.", indent=LAMP_INDENT,
        )
        self.document_tool_note_var = tk.StringVar(value="")
        self.document_tool_note = self._caption(f, "", indent=LAMP_INDENT)
        self.document_tool_note.configure(textvariable=self.document_tool_note_var)
        self.document_tool_note.pack_forget()

        self.model_name_var = tk.StringVar(value="(unknown)")
        self._label(f, "Active model", dim=True).pack(fill="x", pady=(_px(self, SECTION_GAP), 0))
        self.model_name_label = tk.Label(f, textvariable=self.model_name_var, bg=t["bg"], fg=t["bright"], anchor="w",
                                         justify="left", font=C11, bd=0, padx=0, pady=0)
        self.model_name_label.pack(fill="x", pady=(_px(self, 2), 0))
        self._wrap.add(self.model_name_label)

        self._label(
            f, "Image generation (link, ComfyUI, model files) has its own screen now - see Image uplink.",
            dim=True,
        ).pack(fill="x", pady=(_px(self, 14), 0))

    def _option_row(self, parent):
        """A label + option words row: the words (or a dropdown) start OPT_COL px into the content, as the
        approved KV cache type row (r2 mock_settings_advanced). Grid: column 0 the label, column 1 the control."""
        row = tk.Frame(parent, bg=self._t["bg"])
        row.pack(fill="x", pady=(_px(self, ROW_GAP), 0))
        row.columnconfigure(0, minsize=_px(self, OPT_COL))
        return row

    @staticmethod
    def _reply_limit_value(raw):
        """The slider position as a token limit: whole steps of REPLY_LIMIT_STEP, 0 meaning no limit."""
        return int(round(float(raw) / REPLY_LIMIT_STEP)) * REPLY_LIMIT_STEP

    def _on_repeat_penalty_toggle(self):
        state = "normal" if self.repeat_penalty_enabled_var.get() else "disabled"
        self.repeat_penalty_scale.configure(state=state)
        self.repeat_penalty_label.config(
            text="Off" if state == "disabled" else f"{self.repeat_penalty_var.get():.2f}"
        )

    def _thinking_budget_display(self, value):
        # Single writer for this label (see _labeled_scale's trace comment) -
        # the "Automatic"/"No limit" states are folded in here rather than
        # written to the widget from _sync_thinking_controls.
        if getattr(self, "thinking_budget_auto_var", None) is not None \
                and self.thinking_budget_auto_var.get():
            return "Automatic"
        n = int(round(value))
        return "No limit" if n <= 0 else f"{n:,}"

    def _sync_thinking_controls(self):
        """Enables exactly the controls the active model's chat template
        actually honours, and says which ones those are.

        Deliberately driven by the detected capability rather than shown
        unconditionally: a think-tag model takes enable_thinking (on/off), a
        harmony model takes reasoning_effort (low/medium/high) and has no off
        switch at all, and a model with neither takes nothing. Offering the
        wrong knob would just be a control that silently does nothing."""
        cap = getattr(self, "_thinking_cap", None) or {}
        supports = bool(cap.get("supports"))
        control = cap.get("control")

        if not supports:
            self.thinking_status_var.set(
                "This model doesn't do step-by-step thinking, so these settings don't apply. "
                "Models that do are marked in the Model list."
            )
        else:
            interleave = (
                " It can also resume thinking part-way through an answer."
                if cap.get("can_interleave") else ""
            )
            if not cap.get("budget_enforced"):
                # Saying nothing here would be better than a number, but
                # saying why is better still - otherwise the greyed-out
                # budget slider just below looks broken.
                budget_text = (
                    " Thinking length can't be capped on this kind of model - llama.cpp's "
                    "budget only works on models that use think tags. Use Thinking effort "
                    "above instead; it makes a large difference here (max is sent as high: "
                    "this kind of model has three levels)."
                )
            else:
                budget = getattr(self, "_thinking_budget_effective", None)
                budget_text = (
                    f" Currently allowed {budget:,} thinking tokens per reply."
                    if isinstance(budget, int) and budget > 0 else " Thinking length is unlimited."
                )
                if self.thinking_budget_auto_var.get():
                    budget_text += (" Thinking effort sets how much of the context window that is: "
                                    "low 25%, medium 70%, high 80%, max everything but room for the answer.")
            self.thinking_status_var.set(
                f"This model thinks before answering ({cap.get('label')})."
                f"{interleave}{budget_text}"
            )

        mode_on = supports and control == "enable_thinking"
        # effort drives the template on a harmony model, and the automatic thinking budget on a think-tags one
        effort_on = supports and (control == "reasoning_effort"
                                  or (bool(cap.get("budget_enforced")) and bool(self.thinking_budget_auto_var.get())))
        self.thinking_mode_combo.configure(state="readonly" if mode_on else "disabled")
        self.thinking_effort_combo.configure(state="readonly" if effort_on else "disabled")
        t = self._t   # B8: a label beside a disabled control reads value, as a disabled slider's label does
        self.thinking_mode_label.config(fg=t["bright"] if mode_on else t["value"])
        self.thinking_effort_label.config(fg=t["bright"] if effort_on else t["value"])

        # Greyed out on a style llama.cpp can't hold to a budget, since the
        # setting would be accepted and then quietly do nothing.
        budget_on = supports and bool(cap.get("budget_enforced"))
        self.thinking_budget_auto_check.configure(state="normal" if budget_on else "disabled")
        auto = bool(self.thinking_budget_auto_var.get())
        self.thinking_budget_scale.configure(
            state="normal" if (budget_on and not auto) else "disabled"
        )
        # Same single-writer rule as the GPU layers slider: the label is fed
        # by _labeled_scale's variable trace, so nudge the variable rather
        # than writing the label text here.
        self.thinking_budget_var.set(self.thinking_budget_var.get())

    def _on_gpu_layers_auto_toggle(self):
        """Same pattern as _on_repeat_penalty_toggle: the slider stays visible
        (so its value is still there when Auto is turned back off) but greys
        out, because under Auto it isn't passed to llama-server at all."""
        auto = bool(self.gpu_layers_auto_var.get())
        self.gpu_layers_scale.configure(state="disabled" if auto else "normal")
        # Re-fires _labeled_scale's write trace so its display= re-evaluates
        # with the new auto state. Setting the same value still counts as a
        # write in Tk, so this doesn't need to perturb the number.
        self.gpu_layers_var.set(self.gpu_layers_var.get())
        self._schedule_vram_estimate()

    # ---- Model / Vision (delegated to the shared AssetManagerPanel) ----

    def _build_model_pane(self):
        f = self._pane("model")
        self.models_panel = AssetManagerPanel(
            f, self.api, kind="models", file_label="chat model", file_ext=".gguf",
            toggleable=False, on_active_changed=self._on_model_active_changed, ctx=self.ctx,
        )
        self.models_panel.pack(fill="both", expand=True)

    def _build_vision_pane(self):
        f = self._pane("vision")
        self._label(
            f, "A vision file (mmproj) lets the active model see images. It must match "
            "your current model's family.", dim=True,
        ).pack(fill="x", pady=(0, 4))
        self.vision_panel = AssetManagerPanel(
            f, self.api, kind="mmproj", file_label="vision file", file_ext=".gguf",
            toggleable=True, on_active_changed=self._on_model_active_changed, ctx=self.ctx,
        )
        self.vision_panel.pack(fill="both", expand=True)

    def _build_image_pane(self):
        f = self._pane("image")
        self._label(
            f, "\"Set as primary\" just remembers which file to use for "
            "\"generate an image of...\"/\"make it...\" requests in chat - it doesn't load "
            "anything now, only the next time an image is actually generated (if none is "
            "set, the largest file in models/image/ is used automatically). Can't be "
            "changed while an image model is currently loaded for fast follow-up edits - "
            "send a chat message first to release it, or wait a few minutes.", dim=True,
        ).pack(fill="x", pady=(0, 4))
        self.image_model_panel = AssetManagerPanel(
            f, self.api, kind="image-models", file_label="image model", file_ext=".gguf",
            toggleable=False, on_active_changed=None, eager_reload=False, active_label="Set as primary",
        )
        self.image_model_panel.pack(fill="both", expand=True)

    def _on_model_active_changed(self):
        api = self.api

        def fetch():
            try:
                model_info = api.get_model_info()
            except ApiError:
                model_info = None
            # A model/vision switch restarts llama-server the same way a
            # restart-triggering Save does, and start_llama() can silently
            # reclamp gpu_layers to fit the new model (safe_gpu_layers, see
            # server.py) - without re-reading the settings, the Advanced tab's
            # slider (and the snapshot a later Save's needs_restart-adjacent
            # comparison used to rely on) would stay stuck at whatever it
            # showed before the switch, and a completely unrelated later Save
            # would resend that stale value, forcing a second, pointless model
            # reload.
            try:
                fresh = api.get_settings()
            except ApiError:
                fresh = None
            return model_info, fresh

        def apply(result):
            model_info, fresh = result
            if model_info is not None:
                self.model_name_var.set(model_info.get("model") or "(no model loaded)")
                self._set_fit_note(model_info.get("fit"))
                self._set_document_tool_note(model_info)
                self._n_ctx_train = model_info.get("n_ctx_train") or 131072
                self.context_max_label.config(text=f"max {self._n_ctx_train:,}")
                # Thinking capability is a property of the model, so it has to be
                # re-read on every switch - otherwise the Thinking controls keep
                # describing (and offering the knobs of) the previous model.
                self._thinking_cap = model_info.get("thinking") or {}
                self._thinking_budget_effective = model_info.get("thinking_budget_effective")
                self._sync_thinking_controls()
            if fresh is not None:
                self._orig_restart_values = {k: fresh.get(k) for k in RESTART_KEYS}
                before, _invalid = self._dirty.current()      # B8d: what the sync below moves is not an edit
                # B8-H2: an unsaved restart-key edit the server did not itself change stays on screen (a key the
                # server really moved, a clamp, takes its value)
                clean0 = self._dirty.clean or {}
                keep = {k: v for k, v in self._dirty.user_values().items()
                        if k in RESTART_KEYS and fresh.get(k) == clean0.get(k)}
                self._sync_restart_widgets({**fresh, **keep})
                self._update_running_config_display(fresh)
                self._dirty.rebase(fresh, RESTART_KEYS, before=before)
            if self.ctx.on_model_switched:
                self.ctx.on_model_switched()

        # Off the Tk thread like every other call here; the chat reload that
        # on_model_switched triggers waits for the fresh settings, as before.
        run_async(self, fetch, apply, lambda _e: self.ctx.on_model_switched and self.ctx.on_model_switched())

    # ---- Appearance (new; client-side desktop_theme.json) ----

    def _build_appearance_pane(self):
        theme = self.ctx.theme
        f = self._pane("appearance")

        t = self._t
        # B8: the phosphor and the launch screen are Segmented words (each phosphor word in its own preset's
        # bright; the chosen one carries the 2 px underbar). A click saves at once, as the buttons did.
        self._label(f, "Phosphor color").pack(fill="x")
        self._label(f, "Applies after restarting the app.", dim=True).pack(fill="x", pady=(_px(self, 4), _px(self, 8)))
        chosen = getattr(theme, "saved_phosphor", theme.phosphor_name)       # BF-28: what is saved (applies after a restart)
        self.phosphor_var = tk.StringVar(value=chosen)
        self.phosphor_picker = InkSegmented(
            f, theme, options=[(name, name.capitalize()) for name in theme_mod.PHOSPHOR_PRESETS],
            inks={name: preset["bright"] for name, preset in theme_mod.PHOSPHOR_PRESETS.items()},
            variable=self.phosphor_var, command=lambda: self._set_phosphor(self.phosphor_var.get()), ground=t["bg"],
        )
        self.phosphor_picker.pack(anchor="w")
        self._highlight_phosphor(chosen)

        self._label(f, "Open on launch").pack(fill="x", pady=(_px(self, SECTION_GAP), _px(self, 8)))
        self.launch_screen_var = tk.StringVar(value=theme.launch_screen)
        self.launch_picker = Segmented(
            f, theme, options=(("home", "Home"), ("last_chat", "Last chat")), variable=self.launch_screen_var,
            command=lambda: self._set_launch_screen(self.launch_screen_var.get()), ground=t["bg"],
        )
        self.launch_picker.pack(anchor="w")
        self._highlight_launch(theme.launch_screen)

        self.appearance_status = self._label(f, "", dim=True)
        self.appearance_status.pack(fill="x", pady=(_px(self, 14), 0))

    def _set_phosphor(self, name):
        if name not in theme_mod.PHOSPHOR_PRESETS:
            return
        # BF-28: the choice is SAVED and applies after a restart; the running window keeps its palette (set_phosphor
        # used to recolour the live Theme, so some widgets switched and the rest did not)
        self.ctx.theme.saved_phosphor = name
        theme_mod.save_theme(self.ctx.theme)
        self.phosphor_var.set(name)
        self._highlight_phosphor(name)
        self.appearance_status.config(text=f"saved - {name} phosphor applies after restart")

    def _highlight_phosphor(self, active_name):
        """The picker follows phosphor_var (Segmented repaints on every write), so this only makes sure the
        variable names `active_name`."""
        if self.phosphor_var.get() != active_name:
            self.phosphor_var.set(active_name)

    def _set_launch_screen(self, value):
        self.ctx.theme.set_launch_screen(value)
        theme_mod.save_theme(self.ctx.theme)
        self.launch_screen_var.set(value)
        self._highlight_launch(value)
        self.appearance_status.config(text=f"saved - app will open to {value.replace('_', ' ')}")

    def _highlight_launch(self, active_value):
        """As _highlight_phosphor: the launch picker follows launch_screen_var."""
        if self.launch_screen_var.get() != active_value:
            self.launch_screen_var.set(active_value)

    # ---- Advanced (ported) ----

    def _build_advanced_pane(self):
        theme = self.ctx.theme
        f = self._pane("advanced")

        # Ground truth, sourced only from confirmed server responses (initial
        # load, or after a save/restart actually completes) - never from
        # whatever the editable controls below currently show. gpu_layers in
        # particular can be silently clamped down by the server to fit free
        # VRAM (see safe_gpu_layers in server.py), so "what I'm about to
        # request" and "what's actually running" are genuinely two different
        # facts - this makes the second one impossible to miss instead of
        # trusting the slider to always agree with reality.
        # B8: the RUNNING record (tagged LIVE: the pane's default is 'Changes apply on save') replaces the
        # 'Currently running:' box and keeps every fact it held (OI-17): MODEL (the active model, the same
        # model_name_var as General's Active model), CONTEXT and GPU LAYERS. running_config_var still holds
        # today's one-line form, written by the same _update_running_config_display.
        t = self._t
        self.running_config_var = tk.StringVar(value="")
        self.running_context_var = tk.StringVar(value="")
        self.running_layers_var = tk.StringVar(value="")
        self._section(f, "Running", tag="LIVE", first=True)
        record = tk.Frame(f, bg=t["bg"])
        record.pack(anchor="w", pady=(_px(self, 10), 0))     # MODEL at y 113, then a 20 px pitch (approved)
        self.running_rows = []
        for key, var in (("MODEL", self.model_name_var), ("CONTEXT", self.running_context_var),
                         ("GPU LAYERS", self.running_layers_var)):
            row = RecordRow(record, theme, key, textvariable=var, value_x=RECORD_VALUE_X, ground=t["bg"])
            row.pack(anchor="w")
            self.running_rows.append(row)
        self._running_note = self._label(
            f, "The controls below are your next request, not necessarily what's running now - "
            "a value can get clamped down to fit available VRAM. Check the box above after saving.",
            dim=True,
        )
        self._running_note.pack(fill="x", pady=(_px(self, 6), 0))
        # A3 (bug-fix batch 30): one line under the record, packed only while the model is larger than RAM + VRAM
        # (_set_fit_note) - present when the model server is not running too, since the reading is kept after a crash
        self.fit_note_var = tk.StringVar(value="")
        self.fit_note = self._label(f, "", dim=True)
        self.fit_note.configure(textvariable=self.fit_note_var)
        self.fit_note.bind("<Configure>", lambda e: self.fit_note.configure(wraplength=max(_px(self, 120), e.width - _px(self, 4))), add="+")

        # Real, measured numbers (nvidia-smi + this process's own working
        # set) - not the estimate the bar further down uses. Only polls
        # while this pane is actually visible (_show_category/on_hide start
        # and stop it) so it doesn't spend a background HTTP round-trip
        # every few seconds on screens that don't show it.
        # B8: label and a short 160 px meter on one row (approved r2 Advanced), the measured readout in full
        # under it.
        mem_row = tk.Frame(f, bg=t["bg"])
        mem_row.pack(fill="x", pady=(_px(self, SECTION_GAP), 0))
        self._label(mem_row, "Live memory usage").pack(side="left")
        self.live_mem_bar = ttk.Progressbar(mem_row, orient="horizontal", mode="determinate", maximum=100,
                                            length=_px(self, 160), style=meter_style(self, self.ctx.theme))
        self.live_mem_bar.pack(side="left", padx=(_px(self, 24), 0))
        self.live_mem_var = tk.StringVar(value="")
        self.live_mem_label = tk.Label(
            f, textvariable=self.live_mem_var, bg=t["bg"], fg=t["value"], anchor="w",
            justify="left", font=C9, bd=0, padx=0, pady=0,
        )
        self.live_mem_label.pack(fill="x", pady=(_px(self, 6), 0))
        self._wrap.add(self.live_mem_label)

        # Separate label, in warn colour, for the two "and here's *why* offload
        # came out small" cases (another program holding the card / model bigger
        # than the card). Kept apart from live_mem_var above so it can carry its
        # own colour and collapse to nothing when neither applies - folding it
        # into the dim readout buried the one line the user actually needs.
        # BF-29: it is never packed here; _set_vram_warning packs it BEFORE _vram_warning_anchor, i.e. right
        # under the live meter's readout (a plain re-pack used to put it at the bottom of the pane).
        self.vram_warning_var = tk.StringVar(value="")
        self.vram_warning_label = tk.Label(
            f, textvariable=self.vram_warning_var, bg=t["bg"], fg=t["warn_eff"], anchor="w",
            justify="left", font=C9, bd=0, padx=0, pady=0,
        )
        self._wrap.add(self.vram_warning_label)
        self._vram_warning_anchor = tk.Frame(f, bg=t["bg"], height=_px(self, 4))
        self._vram_warning_anchor.pack(fill="x")

        ctx_row = tk.Frame(f, bg=t["bg"])
        ctx_row.pack(fill="x", pady=(_px(self, SECTION_GAP), _px(self, 4)))
        self._label(ctx_row, "Context size (tokens)").pack(side="left")
        self.context_max_label = tk.Label(ctx_row, text="", bg=t["bg"], fg=t["value"], font=C10, anchor="e", bd=0,
                                          padx=0, pady=0)
        self.context_max_label.pack(side="right")
        self.context_size_var = tk.IntVar(value=16384)
        ctx_entry = self._entry(f, textvariable=self.context_size_var)
        ctx_entry.pack(fill="x", ipady=3)
        ctx_entry.bind("<KeyRelease>", lambda e: self._schedule_vram_estimate())

        self.max_parallel_chats_var = tk.DoubleVar(value=1)
        parallel_scale, _ = self._labeled_scale(f, "Max parallel chats", self.max_parallel_chats_var, 1, 8,
                                                 display=lambda v: f"{int(round(v))}")
        parallel_scale.configure(command=lambda v: (self._schedule_vram_estimate(),))
        self._caption(
            f, "How many chats can generate replies at once. Each one always gets the full context size "
            "above, regardless of this value - raising it doesn't shrink any chat's budget, it costs "
            "proportionally more VRAM instead (context size below is now used N times over)."
        )

        self.gpu_layers_auto_var = tk.BooleanVar(value=True)
        self._check(f, "Fit GPU layers automatically (recommended)", self.gpu_layers_auto_var,
                    command=self._on_gpu_layers_auto_toggle).pack(anchor="w", pady=(_px(self, ROW_GAP), 0))
        self._caption(
            f, "Lets llama.cpp size the offload itself against your card at load time, instead of this app "
            "guessing beforehand. The guess had to stay conservative to avoid overfilling the card, and it "
            "left a lot on the table: measured on this machine at a 16,384 context, Qwen3.6-27B-IQ2_XXS ran "
            "4.98 tokens/sec under the old guess and 43.92 letting llama.cpp choose, with 3.2 GB of VRAM "
            "sitting unused in the first case. Turn this off only to force a specific layer count for "
            "troubleshooting.", indent=LAMP_INDENT,
        )
        self.gpu_layers_var = tk.DoubleVar(value=99)
        # "Auto" is folded into display= rather than written to the label
        # directly from _on_gpu_layers_auto_toggle. _labeled_scale drives this
        # label from a trace on the variable, so a second writer would be
        # silently overwritten the next time gpu_layers_var changed for any
        # reason - the same label-desync this file already hit once (see the
        # comment in _labeled_scale). One writer, and the toggle just re-fires
        # the trace.
        self.gpu_layers_scale, self.gpu_layers_label = self._labeled_scale(
            f, "GPU layers", self.gpu_layers_var, 0, 99,
            display=lambda v: "Auto" if self.gpu_layers_auto_var.get() else f"{int(round(v))}",
        )
        self.gpu_layers_scale.configure(command=lambda v: (self._schedule_vram_estimate(),))

        # Which GPUs, in what order, how a model too big for one is spread (shown only with more than one usable GPU).
        self.gpu_section = GpuSection(f, self, self._gpu_size_overrides)
        self.gpu_section.pack(fill="x")

        self.kv_offload_var = tk.BooleanVar(value=True)
        self._check(f, "Keep KV cache on GPU", self.kv_offload_var,
                    command=self._schedule_vram_estimate).pack(anchor="w", pady=(_px(self, ROW_GAP), 0))

        # KV cache type as Segmented words (f16 q8_0 q4_0); a choice re-estimates VRAM through the same
        # debounced _schedule_vram_estimate the dropdown's <<ComboboxSelected>> used (KEY-19)
        kv_row = self._option_row(f)
        self._label(kv_row, "KV cache type").grid(row=0, column=0, sticky="w")
        self.kv_cache_type_var = tk.StringVar(value="q8_0")
        self.kv_cache_seg = Segmented(kv_row, theme, options=KV_CACHE_TYPES, variable=self.kv_cache_type_var,
                                      command=self._schedule_vram_estimate, ground=t["bg"])
        self.kv_cache_seg.grid(row=0, column=1, sticky="w")
        self._caption(
            f, "How much the conversation history is compressed in VRAM. f16 = full precision, no quality "
            "loss. q8_0 = about half the VRAM, effectively lossless. q4_0 = about a quarter the VRAM, slight "
            "quality risk. Lower precision leaves more room for a bigger context size or model."
        )

        self.batch_size_var = tk.DoubleVar(value=2048)
        self._labeled_scale(f, "Prompt batch size", self.batch_size_var, 32, 8192, step=32,
                            display=lambda v: f"{int(round(v)):,}")
        self._caption(
            f, "Below 512: lower = smaller GPU compute buffer, less VRAM (slower on long "
            "prompts/documents). Above 512: no VRAM difference - start_llama() caps the actual "
            "compute buffer at 512 regardless of this slider, so raising it further only queues "
            "more tokens per pass (throughput), which is also why the VRAM meter below doesn't "
            "move for most of this range - that's expected, not a bug."
        )

        # Model loading stays a dropdown (styled): model_load_mode_var holds the LABELS (test_settings_manage)
        load_row = self._option_row(f)
        self._label(load_row, "Model loading").grid(row=0, column=0, sticky="w")
        self.model_load_mode_var = tk.StringVar(value=MODEL_LOAD_LABELS["auto"])
        self.model_load_combo = ttk.Combobox(
            load_row, textvariable=self.model_load_mode_var, values=list(MODEL_LOAD_LABELS.values()),
            state="readonly", width=24, font=C10,
        )
        self.model_load_combo.grid(row=0, column=1, sticky="w")
        self._caption(
            f, "How the model file is read at launch. Fully into RAM reads it once, in order - on a hard disk "
            "this keeps the rest of the computer (and this app) responsive while it loads. Memory-mapped lets "
            "the OS page it in on demand: instant when the file is already cached and on an SSD, but on a hard "
            "disk it floods the drive with thousands of simultaneous reads that can freeze everything until the "
            "load finishes. Automatic picks RAM for a hard disk when the model fits comfortably in free RAM, "
            "memory-mapped otherwise."
        )
        self.mlock_model_var = tk.BooleanVar(value=False)
        self._check(f, "Lock model in RAM (prevent swapping to disk)", self.mlock_model_var).pack(
            anchor="w", pady=(_px(self, ROW_GAP), 0)
        )

        self._label(
            f, "Context size, GPU layers, KV cache, batch size, and RAM settings all restart the model server.",
            dim=True,
        ).pack(fill="x", pady=(_px(self, ROW_GAP), 0))

        self._label(f, "Estimated VRAM usage").pack(fill="x", pady=(_px(self, SECTION_GAP), _px(self, 6)))
        self.vram_bar = ttk.Progressbar(f, orient="horizontal", mode="determinate", maximum=100,
                                        style=meter_style(self, self.ctx.theme))
        self.vram_bar.pack(fill="x")
        self.vram_label = tk.Label(
            f, text="", bg=t["bg"], fg=t["value"], anchor="w", justify="left", font=C9, bd=0, padx=0, pady=0,
        )
        self.vram_label.pack(fill="x", pady=(_px(self, 6), 0))
        self._wrap.add(self.vram_label)

    def _gpu_size_overrides(self):
        """The settings that decide how much memory the model needs, as currently on screen - what the GPU placement preview plans for."""
        return {
            "context_size": int(self.context_size_var.get()),
            "kv_cache_type": self.kv_cache_type_var.get(),
            "kv_offload": bool(self.kv_offload_var.get()),
            "batch_size": int(round(float(self.batch_size_var.get()))),
            "max_parallel_chats": int(round(float(self.max_parallel_chats_var.get()))),
        }

    def _schedule_vram_estimate(self):
        self.gpu_section.schedule_plan()      # the same inputs decide where the model goes
        if self._vram_after_id is not None:
            self.after_cancel(self._vram_after_id)
        self._vram_after_id = self.after(250, self._start_vram_estimate)

    def _start_vram_estimate(self):
        # Runs the HTTP round-trip off the main thread (same worker/queue/poll
        # shape as _on_save below) - this used to call self.api.estimate_vram()
        # directly here, which blocked the whole GUI for the request's duration
        # every time a slider tick fired.
        self._vram_after_id = None
        try:
            context_size = int(self.context_size_var.get())
            gpu_layers = int(self.gpu_layers_var.get())
            parallel = int(self.max_parallel_chats_var.get())
        except (tk.TclError, ValueError):
            return
        kv_cache_type = self.kv_cache_type_var.get()
        kv_offload = bool(self.kv_offload_var.get())
        gpu_auto = bool(self.gpu_layers_auto_var.get())
        self._vram_request_seq += 1
        seq = self._vram_request_seq
        q = queue.Queue()
        threading.Thread(
            target=self._vram_estimate_worker,
            args=(gpu_layers, context_size, kv_cache_type, kv_offload, parallel, gpu_auto, q),
            daemon=True,
        ).start()
        self.after(50, lambda: self._poll_vram_queue(seq, q))

    def _vram_estimate_worker(self, gpu_layers, context_size, kv_cache_type, kv_offload, parallel,
                              gpu_auto, q):
        try:
            result = self.api.estimate_vram(
                gpu_layers, context_size, kv_cache_type, kv_offload, max_parallel_chats=parallel,
                gpu_layers_auto=gpu_auto,
            )
            q.put(("ok", result))
        except ApiError as e:
            q.put(("error", str(e)))

    def _poll_vram_queue(self, seq, q):
        try:
            kind, payload = q.get_nowait()
        except queue.Empty:
            self.after(50, lambda: self._poll_vram_queue(seq, q))
            return
        if seq != self._vram_request_seq:
            # A newer slider tick has since superseded this request - drop it
            # rather than overwriting the meter with a stale estimate.
            return
        if kind == "error":
            self.vram_label.config(text="Estimate unavailable.")
            return
        result = payload
        low, high = result.get("estimated_mb_low"), result.get("estimated_mb_high")
        if high is None:
            self.vram_label.config(text="Estimate unavailable for this model.")
            self.vram_bar["value"] = 0
            return
        if not result.get("vram_available"):
            self.vram_label.config(text=f"~{high:,.0f} MB estimated (couldn't detect GPU memory to compare against).")
            self.vram_bar["value"] = 0
            return
        total = result.get("total_mb") or 1
        free = result.get("free_mb")
        pct_low = min(100, low / total * 100)
        range_text = f"~{low:,.0f} MB" if low == high else f"~{low:,.0f}-{high:,.0f} MB"
        note = " (real usage trends toward the low end)" if result.get("uses_sliding_window") else ""
        text = (
            f"{range_text} of {total:,} MB total VRAM{note}. {free:,} MB free right now."
            if free is not None else f"{range_text} of {total:,} MB total VRAM{note}."
        )
        if result.get("gpu_layers_auto"):
            # Not a prediction of what will actually be used - llama.cpp
            # decides that at load time. Say so, or the number reads as a
            # forecast that will often be wrong (it deliberately isn't one).
            text += (
                " This is the cost of putting the whole model on the GPU; with Auto on, llama.cpp "
                "fits as much as your card holds and runs the rest on the CPU."
            )
        self.vram_label.config(text=text)
        self.vram_bar["value"] = pct_low

    # ---- live (measured, not estimated) memory meter ----

    def _start_live_memory_poll(self):
        self._fetch_live_memory()
        self._live_mem_after_id = self.after(3000, self._start_live_memory_poll)

    def _stop_live_memory_poll(self):
        if self._live_mem_after_id is not None:
            self.after_cancel(self._live_mem_after_id)
            self._live_mem_after_id = None

    def _fetch_live_memory(self):
        q = queue.Queue()
        threading.Thread(target=self._live_memory_worker, args=(q,), daemon=True).start()
        self.after(50, lambda: self._poll_live_memory_queue(q))

    def _live_memory_worker(self, q):
        try:
            result = self.api.get_vram_status()
            q.put(("ok", result))
        except ApiError as e:
            q.put(("error", str(e)))

    def _poll_live_memory_queue(self, q):
        try:
            kind, payload = q.get_nowait()
        except queue.Empty:
            self.after(50, lambda: self._poll_live_memory_queue(q))
            return
        if kind == "error" or not payload.get("available"):
            self.live_mem_var.set("Live memory readout unavailable (no NVIDIA GPU detected, or nvidia-smi not on PATH).")
            self.live_mem_bar["value"] = 0
            self._set_vram_warning("")
            return
        total = payload.get("total_mb") or 0
        free = payload.get("free_mb") or 0
        used = max(0, total - free)
        self.live_mem_bar["value"] = min(100, used / total * 100) if total else 0
        if not payload.get("process_running"):
            self.live_mem_var.set(
                f"VRAM: {used:,.0f} / {total:,.0f} MB used card-wide ({free:,.0f} MB free). "
                "No model currently running."
            )
            self._set_vram_warning("")
            return
        self._set_vram_warning(self._vram_warning_text(payload, total))
        ram_mb = payload.get("process_ram_mb")
        ram_text = f"{ram_mb:,.0f} MB" if ram_mb is not None else "unknown"
        gpu_layers = payload.get("gpu_layers")
        if gpu_layers is not None:
            layers_text = f", currently {int(gpu_layers)} layers on GPU"
        elif payload.get("gpu_layers_auto"):
            layers_text = ", with the split chosen automatically by llama.cpp"
        else:
            layers_text = ""
        kv_text = "on GPU" if payload.get("kv_offload") else "on system RAM"
        # What "Automatic" model loading actually decided for the running model.
        load_text = " The model file was read fully into RAM at launch." if payload.get("model_loaded_into_ram") else ""
        self.live_mem_var.set(
            f"VRAM: {used:,.0f} / {total:,.0f} MB used card-wide ({free:,.0f} MB free - this "
            f"is the whole card, not isolated to this app; nvidia-smi can't attribute VRAM "
            f"per-process on this GPU). System RAM held by llama-server: {ram_text} "
            f"(includes the model's weight layers not offloaded to GPU{layers_text}, "
            f"plus the KV cache, which is {kv_text}).{load_text}"
        )

    def _set_vram_warning(self, text):
        """Shows/hides the warn-coloured line under the live meter. Packing and
        unpacking (rather than setting an empty string) keeps the pane from
        carrying a blank gap on the common case where nothing is wrong.
        BF-29: it is packed BEFORE _vram_warning_anchor, so it lands right under
        the live meter's readout (a bare pack() appended it to the END of the
        pane), and the manager - not winfo_ismapped(), which is False whenever
        the pane is hidden - decides whether it is packed, so calls made while
        the pane is hidden neither re-pack it nor move it."""
        if text:
            self.vram_warning_var.set(text)
            if not self.vram_warning_label.winfo_manager():
                self.vram_warning_label.pack(fill="x", pady=(_px(self, 6), 0), before=self._vram_warning_anchor)
        else:
            self.vram_warning_var.set("")
            if self.vram_warning_label.winfo_manager():
                self.vram_warning_label.pack_forget()

    @staticmethod
    def _vram_warning_text(payload, total):
        """The 'why is offload small' line, or "" when there's nothing to say.

        Two distinct causes, and it matters which one the user is looking at:
        another program holding the card is fixable in seconds (close it and
        reload), while a model larger than the card is not fixable at all
        without a smaller quantization. Both produce identical symptoms - slow
        replies, idle-looking GPU - so naming the actual one is the whole point.
        Both can be true at once, hence two independent checks rather than an
        elif."""
        parts = []
        foreign = payload.get("launch_foreign_used_mb")
        threshold = payload.get("foreign_vram_warn_mb")
        launch_free = payload.get("launch_free_mb")
        if foreign is not None and threshold is not None and foreign >= threshold:
            parts.append(
                f"Another program was using {foreign:,.0f} MB of your {total:,.0f} MB GPU when this "
                f"model loaded, leaving it only {launch_free:,.0f} MB. That is why fewer layers are "
                f"on the GPU than your card could otherwise hold. Close the other program and "
                f"reload the model to reclaim it."
            )
        model_mb = payload.get("model_mb")
        if model_mb and total and model_mb > total:
            parts.append(
                f"This model is {model_mb:,.0f} MB - larger than the {total:,.0f} MB your GPU has in "
                f"total - so part of it always runs on the CPU no matter how these settings are "
                f"tuned. Only a smaller quantization changes that."
            )
        return "  ".join(parts)

    # ---- Data (ported fields regrouped from General/Advanced) ----

    def _build_data_pane(self):
        theme = self.ctx.theme
        f = self._pane("data")
        t = self._t

        self._section(f, "Limits & folders", first=True)
        self._label(f, "Outputs folder (blank = default)").pack(fill="x", pady=(_px(self, 10), _px(self, 4)))
        self.outputs_dir_var = tk.StringVar(value="")
        self._entry(f, textvariable=self.outputs_dir_var).pack(fill="x", ipady=3)

        self.confirm_save_location_var = tk.BooleanVar(value=False)
        self._check(
            f, "Ask where to save exports (instead of saving automatically)",
            self.confirm_save_location_var,
        ).pack(anchor="w", pady=(_px(self, ROW_GAP), 0))

        self.max_upload_mb_var = tk.DoubleVar(value=20)
        self._labeled_scale(f, "Max upload size (MB)", self.max_upload_mb_var, 1, 500,
                            display=lambda v: f"{v:,.0f}")

        self.max_file_tokens_var = tk.DoubleVar(value=25_000)
        self._labeled_scale(f, "Max tokens per file", self.max_file_tokens_var, 250, 500_000,
                            display=lambda v: f"{v:,.0f}")

        self.project_pct_var = tk.DoubleVar(value=0.5)
        self._labeled_scale(f, "Project content budget", self.project_pct_var, 0.05, 0.95,
                            display=lambda v: f"{v:.0%}")
        self.project_budget_preview = self._label(f, "", dim=True)
        self.project_budget_preview.pack(fill="x", pady=(_px(self, 6), 0))
        # context_size_var is built in _build_advanced_pane, which runs before
        # this pane in __init__ - same Tk variable instance on self, so a
        # trace here reacts to edits made on that other pane too.
        self.context_size_var.trace_add("write", self._update_project_budget_preview)
        self.project_pct_var.trace_add("write", self._update_project_budget_preview)

        # BACKUP acts at once (the exception to the pane's 'Changes apply on save'), so its section says so.
        # The buttons are kit buttons shown upper-case; cget("text") is still the text given (ActionButton), so
        # their busy texts ("Backing up..." / "Restoring..." / "Resetting...") and the code reading them back work.
        self._section(f, "Backup", tag="ACTS NOW")
        self.backup_include_outputs_var = tk.BooleanVar(value=False)
        self._check(
            f, "Include exported outputs in backup", self.backup_include_outputs_var,
        ).pack(anchor="w", pady=(_px(self, 10), 0))

        backup_row = tk.Frame(f, bg=t["bg"])
        backup_row.pack(anchor="w", pady=(_px(self, 10), 0))
        self.backup_btn = ActionButton(backup_row, theme, text="Back up now...", kind="secondary",
                                       command=self._on_backup, ground=t["bg"], height=_px(self, 28))
        self.backup_btn.pack(side="left")

        # DESTRUCTIVE: both buttons overwrite or delete data and confirm first (danger_outline + the stop edge).
        # Restore moved here from beside Back up (same screen, one click, D-27).
        self._section(f, "Destructive", tag="ACTS NOW - CONFIRMS FIRST")
        danger_row = tk.Frame(f, bg=t["bg"])
        danger_row.pack(anchor="w", pady=(_px(self, 10), 0))
        self.restore_btn = ActionButton(danger_row, theme, text="Restore from backup...", kind="danger_outline",
                                        command=self._on_restore, ground=t["bg"], height=_px(self, 28))
        self.restore_btn.pack(side="left")

        self.reset_btn = ActionButton(danger_row, theme, text="Reset app data...", kind="danger_outline",
                                      command=self._on_reset_data, ground=t["bg"], height=_px(self, 28))
        self.reset_btn.pack(side="left", padx=(_px(self, 12), 0))

    def _update_project_budget_preview(self, *_):
        # Every chat always gets the full context_size (see max_parallel_chats()
        # in server.py) - no division by parallel-chat count anymore, so this
        # preview no longer depends on that setting at all.
        try:
            ctx = int(self.context_size_var.get())
        except (tk.TclError, ValueError):
            return
        budget = int(ctx * self.project_pct_var.get())
        self.project_budget_preview.config(
            text=f"= {budget:,} tokens ({self.project_pct_var.get():.0%} of your {ctx:,} context size)"
        )

    def _on_reset_data(self):
        show_inline_confirm(
            self, self.ctx.theme,
            "Choose what to permanently delete — this cannot be undone. Downloaded models "
            "are kept either way.",
            on_confirm=self._do_reset_data, confirm_label="Reset", danger=True,
            checkboxes=[
                ("conversations", "Conversations & folders", True),
                ("projects", "Projects", True),
                ("settings", "Settings & preferences (back to defaults)", True),
                ("outputs", "Exported outputs (saved images/conversations on disk)", False),
            ],
            title="Reset app data",
        )

    def _do_reset_data(self, selected):
        if not any(selected.values()):
            show_toast(self, self.ctx.theme, "Nothing selected — nothing was reset.", kind="warning")
            return
        body = {
            "wipe_conversations": selected.get("conversations", False),
            "wipe_projects": selected.get("projects", False),
            "reset_settings": selected.get("settings", False),
            "wipe_outputs": selected.get("outputs", False),
        }
        # Off the Tk thread (async_call): the reset can take a while on a hard disk, and the window used to show "Not Responding"
        # for all of it. The button says what is going on and is held down until the answer is back.
        self.reset_btn.config(state="disabled", text="Resetting...")

        def restore_button():
            try:
                self.reset_btn.config(state="normal", text="Reset app data...")
            except tk.TclError:
                pass

        def done(result):
            restore_button()
            if isinstance(result, dict) and result.get("outputs_skipped"):
                show_toast(
                    self, self.ctx.theme,
                    "Everything else was reset. The outputs folder was redirected to a custom "
                    "location in Settings, so it was left untouched.", kind="warning",
                )
            else:
                show_toast(self, self.ctx.theme, "App data has been reset.", kind="success")
            if body["reset_settings"]:
                self._dirty.discard()      # B8d: the settings were reset - unsaved edits are not put back on top
            self._load()

        def failed(e):
            restore_button()
            show_toast(self, self.ctx.theme, f"Could not reset app data: {e}", kind="error")

        run_async(self, lambda: self.api._request("POST", "/api/reset", body=body), done, failed)

    # ---- backup / restore (same worker/queue/self.after(50,...) shape as
    # home_screen.py's _on_export_all - the HTTP call runs off the Tk main
    # thread, the poll loop back on it does any UI/dialog work) ----

    def _on_backup(self):
        self.backup_btn.configure(state="disabled", text="Backing up...")
        include_outputs = bool(self.backup_include_outputs_var.get())
        q = queue.Queue()
        threading.Thread(target=self._backup_worker, args=(q, include_outputs), daemon=True).start()
        self.after(50, self._poll_backup_queue, q)

    def _backup_worker(self, q, include_outputs):
        try:
            data, filename = self.api.backup(include_outputs=include_outputs)
        except ApiError as e:
            q.put(("error", str(e)))
            return
        q.put(("done", data, filename))

    def _poll_backup_queue(self, q):
        try:
            result = q.get_nowait()
        except queue.Empty:
            self.after(50, self._poll_backup_queue, q)
            return
        self.backup_btn.configure(state="normal", text="Back up now...")
        if result[0] == "error":
            show_toast(self, self.ctx.theme, f"Could not create backup: {result[1]}", kind="error")
            return
        _, data, filename = result
        # filedialog is a native modal dialog - only safe to call here, back
        # on the Tk main thread via this poll loop, never from the worker.
        path = filedialog.asksaveasfilename(
            title="Save backup as", initialfile=filename or "personal-terminal-backup.zip",
            defaultextension=".zip", filetypes=[("Zip archive", "*.zip")],
        )
        if not path:
            show_toast(self, self.ctx.theme, "Backup cancelled.", kind="warning")
            return
        try:
            with open(path, "wb") as f:
                f.write(data)
        except OSError as e:
            show_toast(self, self.ctx.theme, f"Could not save backup file: {e}", kind="error")
            return
        show_toast(self, self.ctx.theme, "Backup saved.", kind="success")

    def _on_restore(self):
        path = filedialog.askopenfilename(
            title="Choose a backup file", filetypes=[("Zip archive", "*.zip")],
        )
        if not path:
            return
        show_inline_confirm(
            self, self.ctx.theme,
            "Restoring overwrites your current conversations, projects, and settings with what's "
            "in this backup. This cannot be undone.",
            on_confirm=lambda: self._do_restore(path), confirm_label="Restore", danger=True,
            title="Restore from backup",
        )

    def _do_restore(self, path):
        self.restore_btn.configure(state="disabled", text="Restoring...")
        q = queue.Queue()
        threading.Thread(target=self._restore_worker, args=(q, path), daemon=True).start()
        self.after(50, self._poll_restore_queue, q)

    def _restore_worker(self, q, path):
        try:
            result = self.api.restore(path)
        except ApiError as e:
            q.put(("error", str(e)))
            return
        # The restored theme file, read here (off the Tk thread) for the fx preference store to adopt (see
        # _poll_restore_queue): None when the backup carried none, or it cannot be read as a JSON object.
        theme_raw = None
        try:
            if isinstance(result, dict) and (result.get("restored") or {}).get("theme"):
                with open(theme_mod.THEME_PATH, "rb") as f:
                    loaded = json.loads(f.read().decode("utf-8-sig"))
                theme_raw = loaded if isinstance(loaded, dict) else None
        except (OSError, ValueError, UnicodeDecodeError):
            theme_raw = None
        q.put(("done", result, theme_raw))

    def _poll_restore_queue(self, q):
        try:
            result = q.get_nowait()
        except queue.Empty:
            self.after(50, self._poll_restore_queue, q)
            return
        self.restore_btn.configure(state="normal", text="Restore from backup...")
        if result[0] == "error":
            show_toast(self, self.ctx.theme, f"Could not restore backup: {result[1]}", kind="error")
            return
        payload = result[1]
        theme_raw = result[2] if len(result) > 2 else None
        if theme_raw is not None:
            # The fx preference store (sound, motion, face, ink) adopts the restored file now - on the Tk thread, as its reload()
            # requires. Without this its in-memory values stayed the old ones until a restart, and a later change (or its own
            # defaults rewrite) would write those old values over the restored file. The colour theme itself still needs the
            # restart the confirm below asks for.
            prefs = getattr(getattr(self.ctx, "fx", None), "prefs", None)
            if prefs is not None and not getattr(prefs, "closed", False):
                try:
                    prefs.reload(theme_raw)
                except Exception as e:  # noqa: BLE001 - a preference problem must not break the restore's own reporting
                    print(f"WARNING: fx preferences could not adopt the restored theme file: {e!r}")
        if (payload.get("restored") or {}).get("settings", True):
            self._dirty.discard()          # B8d: the backup's settings replace the screen's - unsaved edits are not kept
        self._load()
        left_out = describe_restore_skipped(payload.get("skipped") or {})
        if payload.get("restart_needed_for_theme"):
            # chats.db/settings.json both take effect immediately (the server
            # retires its pooled database connections and reopens them on the
            # restored file, and settings are re-read from disk every load -
            # see swap_db_file/handle_restore in server.py) but the theme is
            # only ever loaded once at window construction, so that piece
            # genuinely needs a restart.
            show_inline_confirm(
                self, self.ctx.theme,
                "Backup restored" + (f", except {left_out}" if left_out else "") + ". The color theme in "
                "this backup will only apply after restarting the app - shut down now?",
                on_confirm=self.ctx.shutdown, confirm_label="Shut down now", title="Shut down now",
            )
        elif left_out:
            show_toast(self, self.ctx.theme, f"Backup restored, except {left_out}.", kind="warning")
        else:
            show_toast(self, self.ctx.theme, "Backup restored.", kind="success")

    # ---- save (ported: worker thread + queue poll + restart health poll) ----

    def _on_save(self):
        if not self._settings_loaded:
            show_toast(
                self, self.ctx.theme,
                "Settings haven't finished loading - saving now could overwrite them with defaults. "
                "Try again in a moment.", kind="warning",
            )
            return
        try:
            body = self._collect_body()
        except (tk.TclError, ValueError):
            show_toast(self, self.ctx.theme, "One or more numeric fields aren't valid numbers.", kind="error")
            return

        self._saving_body = body          # B8d: what this save stores (the clean snapshot once it succeeds)
        self._load_overlay = None
        self.save_btn.configure(state="disabled")
        self.status_label.config(text="Saving...")
        # Same worker/queue/poll shape as everywhere else in this app: the
        # HTTP call runs off the main thread (save_settings has a 20s timeout
        # to cover a possible llama-server restart), the worker only ever
        # puts onto the queue, and this main-thread poll loop does the UI.
        self._save_queue = queue.Queue()
        threading.Thread(target=self._save_worker, args=(body, self._save_queue), daemon=True).start()
        self.after(50, self._poll_save_queue)

    def _collect_body(self):
        """The body Save sends (B8d: moved out of _on_save unchanged, so saves are byte-identical; the unsaved-
        changes guard compares it with its snapshot). Raises tk.TclError / ValueError on a field that is not a
        valid number, which _on_save turns into its error toast as before."""
        context_size = int(self.context_size_var.get())
        max_parallel_chats = int(self.max_parallel_chats_var.get())
        max_upload_mb = float(self.max_upload_mb_var.get())
        max_file_tokens = float(self.max_file_tokens_var.get())
        project_content_budget_pct = round(float(self.project_pct_var.get()), 2)
        # int(round(...)), not float(): gpu_layers_var is the ttk.Scale's
        # DoubleVar, so saving it raw persisted meaningless fractional layer
        # counts ("gpu_layers": 22.0, and a log line reading "clamped from
        # 22.856375838926176 to 10"). Worse, the slider *displayed*
        # int(round(v)) while safe_gpu_layers consumed int(v), so 22.86 showed
        # as "23" and ran as 22. Every sibling integer control here already
        # uses int(round(...)) - this was the one that didn't.
        gpu_layers = int(round(self.gpu_layers_var.get()))
        batch_size = int(self.batch_size_var.get())

        body = {
            "system_prompt": self.system_prompt_text.get("1.0", "end-1c"),
            "temperature": round(float(self.temperature_var.get()), 2),
            "repeat_penalty_enabled": bool(self.repeat_penalty_enabled_var.get()),
            "repeat_penalty": round(float(self.repeat_penalty_var.get()), 2),
            "dry_multiplier": round(float(self.dry_multiplier_var.get()), 2),
            "presence_penalty": round(float(self.presence_penalty_var.get()), 2),
            "min_p": round(float(self.min_p_var.get()), 2),
            "top_p": round(float(self.top_p_var.get()), 2),
            "top_k": int(round(self.top_k_var.get())),
            "verbosity": RESPONSE_LENGTH_LEVELS[int(round(self.verbosity_var.get()))],
            "max_reply_tokens": self._reply_limit_value(self.max_reply_tokens_var.get()),
            "thinking_mode": self.thinking_mode_var.get(),
            "thinking_effort": self.thinking_effort_var.get(),
            "thinking_budget_auto": bool(self.thinking_budget_auto_var.get()),
            "thinking_budget": int(round(self.thinking_budget_var.get())),
            "tools_enabled": bool(self.tools_enabled_var.get()),
            "document_tool_enabled": bool(self.document_tool_var.get()),
            "outputs_dir": self.outputs_dir_var.get().strip() or None,
            "confirm_save_location": bool(self.confirm_save_location_var.get()),
            "context_size": context_size,
            "max_parallel_chats": max_parallel_chats,
            "max_upload_mb": max_upload_mb,
            "max_file_tokens": max_file_tokens,
            "project_content_budget_pct": project_content_budget_pct,
            "gpu_layers_auto": bool(self.gpu_layers_auto_var.get()),
            "gpu_layers": gpu_layers,
            "kv_offload": bool(self.kv_offload_var.get()),
            "kv_cache_type": self.kv_cache_type_var.get(),
            "batch_size": batch_size,
            "model_load_mode": next(
                (k for k, v in MODEL_LOAD_LABELS.items() if v == self.model_load_mode_var.get()), "auto"
            ),
            "mlock_model": bool(self.mlock_model_var.get()),
        }
        body.update(self.gpu_section.payload())
        return body

    def _finish_load_overlay(self):
        if self._load_overlay is not None:
            self._load_overlay.finish()
            self._load_overlay = None

    def _edits_since_send(self, sent, answer):
        """B8-H7: ({restart key: value}, {gpu key: value}) the user changed AFTER `sent` went to the server: what the
        screen holds now differs from what was sent, and (for a restart key) the answer did not itself move the key."""
        pending, pending_gpu = {}, {}
        try:
            body, invalid = self._dirty.current()
            if body is None:
                return pending, pending_gpu
            gpu_keys = set(self.gpu_section.payload())
            for k, v in body.items():
                if k in invalid or k not in sent or sent[k] == v:
                    continue
                if k in gpu_keys:                       # (some GPU keys are restart keys too: the GPU section owns them)
                    pending_gpu[k] = v
                elif k in RESTART_KEYS and answer.get(k) == sent.get(k):
                    pending[k] = v
        except Exception:  # noqa: BLE001 - a broken read must never break the save answer
            return {}, {}
        return pending, pending_gpu

    def _sync_restart_widgets(self, payload):
        """Re-populates the Advanced-tab widgets backing RESTART_KEYS from a
        server response/settings snapshot. Only gpu_layers (safe_gpu_layers)
        and context_size (model-ceiling clamp) can currently diverge from
        what was requested, but this syncs all of them defensively - the
        alternative (only fixing gpu_layers specifically) is one more
        "which fields can drift" fact to keep remembering correctly. Without
        this, the WIDGET stays stuck at a stale value even after
        _orig_restart_values itself is corrected - and since _on_save()
        reads gpu_layers fresh from the widget every time, a stale widget
        keeps resending the wrong value and re-triggering a real, pointless
        restart on every later save, not just the one that caused the
        original clamp."""
        if isinstance(payload.get("gpu_layers"), (int, float)):
            # Guarded on type, not presence: the server sends gpu_layers=None
            # (or omits it) whenever Auto is on, and a numeric Tk var raises
            # TclError on None.
            self.gpu_layers_var.set(payload["gpu_layers"])
        if "gpu_layers_auto" in payload and payload["gpu_layers_auto"] is not None:
            self.gpu_layers_auto_var.set(bool(payload["gpu_layers_auto"]))
            self._on_gpu_layers_auto_toggle()
        if "context_size" in payload:
            self.context_size_var.set(payload["context_size"])
        if "max_parallel_chats" in payload:
            self.max_parallel_chats_var.set(payload["max_parallel_chats"])
        if "kv_offload" in payload:
            self.kv_offload_var.set(payload["kv_offload"])
        if "kv_cache_type" in payload:
            self.kv_cache_type_var.set(payload["kv_cache_type"])
        if "batch_size" in payload:
            self.batch_size_var.set(payload["batch_size"])
        if "thinking_budget_auto" in payload:
            self.thinking_budget_auto_var.set(bool(payload["thinking_budget_auto"]))
        if "thinking_budget" in payload:
            self.thinking_budget_var.set(payload["thinking_budget"])
        if "max_reply_tokens" in payload:
            self.max_reply_tokens_var.set(payload["max_reply_tokens"] or 0)
        if "model_load_mode" in payload:
            self.model_load_mode_var.set(
                MODEL_LOAD_LABELS.get(payload["model_load_mode"], MODEL_LOAD_LABELS["auto"])
            )
        if "mlock_model" in payload:
            self.mlock_model_var.set(payload["mlock_model"])
        self._update_running_config_display(payload)

    def _update_running_config_display(self, s):
        """B8: fills the RUNNING record's CONTEXT and GPU LAYERS rows (MODEL is model_name_var) with the facts
        the 'Currently running:' line carried, and keeps that line in running_config_var. Unknown -> all empty,
        as before."""
        gpu_layers = s.get("gpu_layers")
        context_size = s.get("context_size")
        if context_size is None:
            self._set_running("", "", "")
            return
        if s.get("gpu_layers_auto", False):
            layers_text = "GPU layers fitted automatically"
            layers_value = "fitted automatically"
        elif gpu_layers is None:
            self._set_running("", "", "")
            return
        else:
            layers_text = f"{int(gpu_layers)} of 99 GPU layers"
            layers_value = f"{int(gpu_layers)} of 99"
        self._set_running(
            f"Currently running: {layers_text}, {int(context_size):,} context",
            f"{int(context_size):,} tokens", layers_value,
        )

    def _set_running(self, line, context, layers):
        self.running_config_var.set(line)
        self.running_context_var.set(context)
        self.running_layers_var.set(layers)

    def _save_worker(self, body, q):
        try:
            result = self.api.save_settings(body)
            q.put(("ok", result))
        except ApiError as e:
            q.put(("error", str(e)))

    def _poll_save_queue(self):
        try:
            kind, payload = self._save_queue.get_nowait()
        except queue.Empty:
            self.after(50, self._poll_save_queue)
            return
        if kind == "error":
            self.save_btn.configure(state="normal")
            self.status_label.config(text="")
            show_toast(self, self.ctx.theme, f"Could not save settings: {payload}", kind="error")
            return
        self._settings = payload
        self._orig_restart_values = {k: payload.get(k) for k in RESTART_KEYS}
        sent, self._saving_body = getattr(self, "_saving_body", None), None
        # B8-H7: a restart-key / GPU edit typed WHILE the save was in flight is the user's: it is put back after the
        # answer's sync (unless the server itself moved that key - a clamp - which takes the server's value)
        pending, pending_gpu = self._edits_since_send(sent or {}, payload)
        self._sync_restart_widgets({**payload, **pending})
        self._update_running_config_display(payload)
        self.gpu_section.saved(payload, keep=pending_gpu or None)
        # B8d: what was sent is now stored; the keys this answer rewrote on screen (clamped restart keys, the GPU
        # section) are read back, and an edit typed while the save was in flight stays UNSAVED
        take = tuple(k for k in RESTART_KEYS + tuple(self.gpu_section.payload()) if k not in pending and k not in pending_gpu)
        self._dirty.mark_clean(base=sent, take=take)
        # The server is the sole authority on whether it actually restarted
        # (via the transient _restart_triggered response field) - NOT a
        # client-side guess computed from a possibly-stale snapshot of when
        # Settings was last opened. That guess used to disagree with reality
        # whenever something else (e.g. safe_gpu_layers's own auto-clamp
        # during an unrelated model switch) changed a restart-key
        # server-side without this screen ever finding out - the client
        # would show "Saved." and stop, unaware the server was mid-restart
        # in the background. .get(..., False) degrades gracefully against
        # an older server that doesn't send this field.
        if payload.get("_restart_triggered", False):
            model_name = self.model_name_var.get() or "the model"
            self._load_overlay = ModelLoadOverlay(self, self.ctx, f"Reloading {model_name} with updated settings...")
            self.status_label.config(text="Restarting model...")
            self._poll_restart_health()
        else:
            self.save_btn.configure(state="normal")
            self.status_label.config(text="Saved.")
            _fx_emit(self, "settings.saved", restart=False)      # B14: the affirm cue
            self._clear_status_later("Saved.")

    def _clear_status_later(self, shown, ms=2500):
        """Wipes the status line after `ms` - but only if it still says what it said when this was scheduled. An older message's timer
        must never erase a newer one: a "Saved." timer used to wipe the error of a settings load that failed a moment later (and made
        the gate flaky whenever a test happened to line the two up)."""
        def clear():
            try:
                if self.status_label.cget("text") == shown:
                    self.status_label.config(text="")
            except tk.TclError:
                pass                      # the screen is already gone
        self.after(ms, clear)

    def _poll_restart_health(self, attempt=0):
        # self.api.health() is a real HTTP round trip (up to its own 2s
        # timeout, longer still if llama-server is busy loading and slow to
        # answer) - calling it directly here on the main thread the way this
        # used to would freeze the whole GUI for that duration on every poll
        # while the model restarts, same class of bug _vram_estimate_worker
        # above was already fixed for. Confirmed live: fast scrolling in this
        # same scrollable pane during a restart would queue up input that
        # only got processed in a burst once one of these blocking calls
        # finally returned, which read as a freeze.
        q = queue.Queue()
        threading.Thread(target=self._restart_health_worker, args=(q,), daemon=True).start()
        self.after(50, lambda: self._poll_restart_health_result(q, attempt))

    def _restart_health_worker(self, q):
        try:
            q.put(self.api.health())
        except ApiError:
            q.put({"status": "starting"})

    def _poll_restart_health_result(self, q, attempt):
        try:
            health = q.get_nowait()
        except queue.Empty:
            self.after(50, lambda: self._poll_restart_health_result(q, attempt))
            return
        status = health.get("status")
        if status == "ok":
            self.save_btn.configure(state="normal")
            self.status_label.config(text="Model ready.")
            _fx_emit(self, "settings.saved", restart=True)       # B14: silent (the model-load cues own a restart)
            self._finish_load_overlay()
            if self.ctx.on_model_switched:
                self.ctx.on_model_switched()
            self._clear_status_later("Model ready.")
            return
        if status == "error":
            self.status_label.config(text="")
            self.save_btn.configure(state="normal")
            self._finish_load_overlay()
            show_toast(
                self, self.ctx.theme, f"The model server failed to start: {health.get('error', '')}", kind="error",
            )
            return
        if attempt > RESTART_WAIT_SECONDS:
            self.status_label.config(text="")
            self.save_btn.configure(state="normal")
            self._finish_load_overlay()
            show_toast(
                self, self.ctx.theme, "The model is taking a long time to restart. Check the app once it's ready.",
                kind="warning",
            )
            return
        self.after(1000, lambda: self._poll_restart_health(attempt + 1))

    # ---- B2 key legend (kept at the end of the class, so no existing line moves) ----

    def legend_items(self):
        # the category column's digits (DirectoryScreen.bind_digits, one per CATEGORIES entry) + global Esc
        items = self.category_list.digit_legend("category") + [("ESC", "back")]
        if self._active_category == FX_CATEGORY and _alt_m_bound(self):
            items.append(("ALT+M", "mute"))     # B13 (approved r2_settings_sound): on 08, while Alt+M is bound
        return items
