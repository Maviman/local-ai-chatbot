"""Project management screen: instructions/files/memory editing plus
delete-with-keep-options, routed like every other screen instead of a
tk.Toplevel popup (previously ProjectManageDialog/ProjectDeleteDialog in
projects_panel.py - retired, this replaces both). Field-level logic
(upload worker/queue/poll, listbox rendering) is ported unchanged; only
the container and the delete confirmation changed, from OS popups to the
same in-window patterns (make_header, an inline overlay for the delete
confirm) every other screen already uses.

B6 look (approved r2_projects_manage): the header MANAGE PROJECT, the name
in an input well, the four tabs as Segmented words over panes swapped with
pack / pack_forget (no ttk.Notebook; select_tab() / tab_var drive them),
wells for every editor, list and entry, kit buttons and text actions, and
the footer `Delete project...` (danger_outline, bottom left) | CLOSE (text
action) + SAVE (primary). Every caption, tip and budget line is the app's
own text, verbatim (D-27). The delete confirm is the shared B3 confirm
(title DELETE PROJECT, five lamps, the danger sentence, DELETE / CANCEL,
keys, confirm.shown). If the kit cannot be imported, plain Tk stand-ins
(below) keep the screen working.
"""

import base64
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog

from .. import attachment_grid as ag
from ..api_client import ApiError
from ..async_call import Latest, run_async
from .. import theme as theme_mod
from ..chat_view import IMAGE_EXTENSIONS, MAX_IMAGE_BYTES, UNSUPPORTED_IMAGE_EXTENSIONS
from .base import Screen, make_header, show_inline_confirm, show_toast

try:  # B6: the kit is the look; a guarded import, so a broken kit still leaves a working screen
    from ..kit import C9, C10, C11, px as _px, tokens as _tokens
    from ..kit.widgets import KitButton, Segmented, TextAction, Well
    KIT = True
except Exception:  # noqa: BLE001
    KIT = False
    C9, C10, C11 = theme_mod.FONT_CAPTION, (theme_mod.FONT_FAMILY, 10), theme_mod.FONT_BODY

    def _px(_widget, n):
        return int(n)

    def _tokens(theme):
        return {"bg": theme.bg, "panel": theme.panel, "bright": theme.bright, "dim": theme.dim,
                "value": getattr(theme, "text", theme.bright), "danger": theme.danger}

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
        ink = theme.danger if kind.startswith("danger") else (theme.bg if filled else theme.text)
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

# The tabs, in order: (key, the word shown). select_tab() takes a key or an index.
TABS = (("instructions", "Instructions"), ("files", "Files"), ("images", "Images"), ("memory", "Memory"))
# The delete confirm's keep-options, in order (all unchecked by default), and its danger sentence (D-27 verbatim).
DELETE_KEEP_OPTIONS = (
    ("keep_conversations", "Keep conversations (moved to General, no folder)"),
    ("keep_instructions", "Keep instructions"),
    ("keep_files", "Keep files"),
    ("keep_images", "Keep images"),
    ("keep_memory", "Keep memory notes"),
)
DELETE_QUESTION = "What should happen to this project's data?"
DELETE_WARNING = "Unchecked items are permanently deleted. This cannot be undone."
# approved geometry (r2 mock_projects 'manage'), in pixels at 100 % (scaled with kit.px)
SIDE = 16                # left content gutter: window x 58
RIGHT = 15               # right gutter: the wells and SAVE end at window x 1085 (exclusive), as approved
WELL_H = 28              # the name well (58,68)-(1085,96)
FOOT_H = 28              # footer buttons (738..766)
FOOT_GAP = 14            # editor well bottom (724) -> footer (738)


class ProjectManageScreen(Screen):
    def __init__(self, parent, ctx):
        super().__init__(parent, ctx)
        theme = ctx.theme
        self.project_id = None
        self._project = None
        self._images = []
        # image_id -> base64 bytes, fetched lazily via a background thread
        # since the project-images list response deliberately omits the raw
        # blob (same reasoning as project_files omitting content in bulk) -
        # cleared/refetched on every _load().
        self._image_bytes = {}
        # Bumped on every _refresh_images() call so a still-running poll
        # loop from a superseded call recognizes it's stale and stops -
        # see _refresh_images()'s comment for why this matters.
        self._images_refresh_gen = 0
        self._photo_cache = ag.PhotoCache()
        self._hover = ag.HoverPreview()
        self._t = t = _tokens(theme)
        side = _px(self, SIDE)
        self.header, self._refresh_datetime = make_header(self, theme, "Manage project")

        # the footer is packed (side="bottom") BEFORE the expanding panes, so a small window squeezes
        # the editor, never the buttons: Delete project... bottom left; CLOSE + SAVE bottom right (D-27 #4)
        footer = tk.Frame(self, bg=t["bg"])
        footer.pack(side="bottom", fill="x", padx=(side, _px(self, RIGHT)), pady=(_px(self, FOOT_GAP), _px(self, 10)))
        self.footer = footer
        self.delete_btn = KitButton(footer, theme, text="Delete project...", kind="danger_outline", upper=True,
                                    command=self._on_delete, height=_px(self, FOOT_H))
        self.delete_btn.pack(side="left")
        self.save_btn = KitButton(footer, theme, text="Save", kind="primary", upper=True, command=self._on_save,
                                  height=_px(self, FOOT_H))
        self.save_btn.pack(side="right")
        self.close_btn = TextAction(footer, theme, text="Close", command=self._on_close, upper=True)
        self.close_btn.pack(side="right", padx=(0, _px(self, 16)))

        # the project name: the one entry well, full content width (58,68)-(1085,96)
        name_box = tk.Frame(self, bg=t["bg"], height=_px(self, WELL_H), bd=0, highlightthickness=0)
        name_box.pack(fill="x", padx=(side, _px(self, RIGHT)), pady=(_px(self, 12), 0))
        name_box.pack_propagate(False)
        self.name_var = tk.StringVar()
        self.name_well = Well(name_box, theme, ground=t["bg"])
        self.name_well.pack(fill="both", expand=True)
        self.name_entry = self.name_well.attach(tk.Entry(
            self.name_well.body, textvariable=self.name_var, font=C11, bg=t["panel"], fg=t["bright"],
            insertbackground=t["bright"], selectbackground=t["bright"], selectforeground=t["bg"],
            relief="flat", bd=0, highlightthickness=0,
        ))
        self.name_entry.pack(fill="both", expand=True, padx=_px(self, 7))

        # the tabs: Segmented words (Instructions bright + underbar), the panes swapped with pack_forget
        self.tab_var = tk.StringVar(master=self, value=TABS[0][0])
        self.tabs = Segmented(self, theme, options=TABS, variable=self.tab_var, command=self._show_tab,
                              ground=t["bg"], font=C10)
        # the first word starts at the content column (x 58): cell ring 2 + label padding 10
        self.tabs.pack(anchor="w", padx=(max(0, side - _px(self, 12)), 0), pady=(_px(self, 13), 0))
        self._pane_host = tk.Frame(self, bg=t["bg"])
        self._pane_host.pack(fill="both", expand=True, padx=(side, _px(self, RIGHT)), pady=(_px(self, 4), 0))
        self.instructions_tab = tk.Frame(self._pane_host, bg=t["bg"])
        self.files_tab = tk.Frame(self._pane_host, bg=t["bg"])
        self.images_tab = tk.Frame(self._pane_host, bg=t["bg"])
        self.memory_tab = tk.Frame(self._pane_host, bg=t["bg"])
        self._panes = {"instructions": self.instructions_tab, "files": self.files_tab,
                       "images": self.images_tab, "memory": self.memory_tab}

        self._build_instructions_tab()
        self._build_files_tab()
        self._build_images_tab()
        self._build_memory_tab()
        self._show_tab()
        # Tab order follows the stacking order: the footer was packed first (for the squeeze rule) but is
        # reached last, Delete project... -> CLOSE -> SAVE, as before B6 (name, ..., Close, Save). Raised
        # BEFORE the loading cover exists, so the cover still covers it.
        footer.lift()
        self.save_btn.lift()

        # _load() fetches the project off the Tk thread (see async_call.py).
        # While it does, this panel covers the whole screen: nothing from the
        # previously open project is visible, and - since clicks land on the
        # cover, not the widgets under it - nothing can be saved or deleted
        # against content that isn't this project's yet.
        self._load_loader = Latest(self)
        self._saving = False
        # B6-H4: the uploads in flight, ("files" | "images", project id) -> {"text": the status line, "errors":
        # [(name, why)]}. An upload belongs to the project it started on whatever project the screen shows
        # later: the status line and the ADD button follow the shown project (_sync_upload_controls); the
        # files never do.
        self._uploads = {}
        self._load_cover = tk.Frame(self, bg=t["bg"])
        self._load_cover_label = tk.Label(
            self._load_cover, text="Loading project...", bg=t["bg"], fg=t["value"], font=C11,
        )
        self._load_cover_label.place(relx=0.5, rely=0.4, anchor="center")

    # ---- tabs (B6: Segmented words over pack_forget panes) ----

    def _show_tab(self):
        """Show the pane the tab words select (tab_var); every other pane is pack_forget-ed."""
        key = self.tab_var.get()
        if key not in self._panes:
            key = TABS[0][0]
        for name, pane in self._panes.items():
            if name != key and pane.winfo_manager():
                pane.pack_forget()
        pane = self._panes[key]
        if not pane.winfo_manager():
            pane.pack(fill="both", expand=True)
        if key == "images" and self._images:
            # the tile tier depends on the row's width, which a hidden pane never had
            self._hover.hide()
            self._render_images_grid()

    def select_tab(self, which):
        """Select a tab by key ('files') or index (1) - the words' own path (tab_var + _show_tab)."""
        keys = [k for k, _label in TABS]
        key = keys[which] if isinstance(which, int) else which
        if key not in keys:
            raise ValueError("unknown tab %r" % (which,))
        self.tab_var.set(key)
        self._show_tab()

    @property
    def current_tab(self):
        return self.tab_var.get()

    # ---- Screen lifecycle ----

    def on_show(self):
        self._refresh_datetime()

    def legend_items(self):
        return [("ESC", "close")]   # the router's global Esc: back to the screen this was opened from

    def handle_file_drop(self, paths):
        # Routed here by MainWindow whenever this screen is the visible one
        # and the user drops OS files anywhere on the window (see
        # win_dnd.py) - not scoped to whichever Notebook tab happens to be
        # active, matching "drag files into the project" broadly rather
        # than "onto the Files tab specifically". Classification mirrors
        # chat_view.py's composer drop handler: images go to the
        # captioning endpoint, everything else goes to the plain-text
        # project-file endpoint (which itself has no extension allowlist,
        # same as the "+ Add files..." picker today).
        if self.project_id is None:
            return
        if self._upload_busy():
            show_toast(self, self.ctx.theme, "Still uploading - try dropping again once it finishes.", kind="warning")
            return
        image_paths, unsupported_images, file_paths, skipped_dirs = [], [], [], []
        for path in paths:
            if os.path.isdir(path):
                skipped_dirs.append(os.path.basename(path) + "/")
                continue
            ext = os.path.splitext(path)[1].lower()
            if ext in UNSUPPORTED_IMAGE_EXTENSIONS:
                unsupported_images.append(os.path.basename(path))
            elif ext in IMAGE_EXTENSIONS:
                image_paths.append(path)
            else:
                file_paths.append(path)

        if skipped_dirs or unsupported_images:
            parts = []
            if skipped_dirs:
                parts.append(f"skipped folders: {', '.join(skipped_dirs)}")
            if unsupported_images:
                parts.append(
                    f"skipped (image format not supported by the vision model): {', '.join(unsupported_images)}"
                )
            show_toast(self, self.ctx.theme, " — ".join(parts), kind="warning")

        if file_paths:
            self._start_files_upload(file_paths)
        if image_paths:
            self._start_images_upload(image_paths)

    def load_project(self, project_id):
        if project_id != self.project_id:
            # Decoded/subsampled PhotoImage tiles are only ever evicted
            # individually (on image delete) - browsing many different
            # projects in one session otherwise accumulates every image
            # ever viewed in RAM indefinitely. Only clear on an actual
            # project switch, not a reload of the same one, so the cache
            # still pays off while staying on one project.
            self._photo_cache.clear()
        self.project_id = project_id
        self._load()

    # ---- widget builders (ported from the old dialog, theme-driven) ----

    # B6: every editor, list and entry sits in a kit Well (panel, 1 px dim edge, 2 px bright while
    # focused); buttons are KitButtons (secondary) or text actions; captions are C9 value, verbatim.

    def _well(self, parent):
        return Well(parent, self.ctx.theme, ground=self._t["bg"])

    def _make_text(self, parent, **kwargs):
        """A tk.Text inside a Well made in `parent`; pack text.kit_well (the Text fills it)."""
        t = self._t
        well = self._well(parent)
        kwargs.setdefault("bg", t["panel"])
        kwargs.setdefault("fg", t["bright"])
        kwargs.setdefault("insertbackground", t["bright"])
        kwargs.setdefault("selectbackground", t["bright"])
        kwargs.setdefault("selectforeground", t["bg"])
        kwargs.setdefault("relief", "flat")
        kwargs.setdefault("bd", 0)
        kwargs.setdefault("font", C11)
        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("padx", _px(self, 8))
        kwargs.setdefault("pady", _px(self, 8))
        # a small request: the pane decides the size (fill + expand), so a long text never pushes the footer
        kwargs.setdefault("width", 1)
        kwargs.setdefault("height", 1)
        text = well.attach(tk.Text(well.body, **kwargs))
        text.pack(fill="both", expand=True)
        text.kit_well = well
        return text

    def _make_listbox(self, parent, **kwargs):
        """A tk.Listbox inside a Well made in `parent`; pack listbox.kit_well (the list fills it)."""
        t = self._t
        well = self._well(parent)
        kwargs.setdefault("bg", t["panel"])
        kwargs.setdefault("fg", t["bright"])
        kwargs.setdefault("relief", "flat")
        kwargs.setdefault("bd", _px(self, 6))
        kwargs.setdefault("font", C11)
        kwargs.setdefault("selectbackground", t["bright"])
        kwargs.setdefault("selectforeground", t["bg"])
        kwargs.setdefault("activestyle", "none")
        kwargs.setdefault("highlightthickness", 0)
        listbox = well.attach(tk.Listbox(well.body, **kwargs))
        listbox.pack(fill="both", expand=True)
        listbox.kit_well = well
        return listbox

    def _flat_button(self, parent, text, command, kind="secondary", **pack_kwargs):
        """A kit button (kind secondary / primary / ...) or, with kind="text", a text action; packed."""
        theme = self.ctx.theme
        if kind == "text":
            btn = TextAction(parent, theme, text=text, command=command, upper=True, ground=self._t["bg"])
        else:
            btn = KitButton(parent, theme, text=text, kind=kind, upper=True, command=command,
                            ground=self._t["bg"], height=_px(self, FOOT_H))
        btn.pack(**pack_kwargs)
        return btn

    def _caption(self, parent, text="", **kw):
        """A C9 value caption / status / budget line (anchor w)."""
        t = self._t
        kw.setdefault("bg", t["bg"])
        kw.setdefault("fg", t["value"])
        kw.setdefault("font", C9)
        kw.setdefault("anchor", "w")
        kw.setdefault("justify", "left")
        return tk.Label(parent, text=text, padx=0, pady=0, bd=0, **kw)

    def _wrap_to_width(self, label):
        """Wrap a caption at its own width (never cut, D-27); geometry only, so it cannot feed back."""
        def fit(event):
            width = max(_px(self, 120), event.width)
            if int(float(label.cget("wraplength") or 0)) != width:
                label.configure(wraplength=width)
        label.configure(wraplength=_px(self, 600))      # a modest first request; <Configure> widens it
        label.bind("<Configure>", fit, add="+")
        return label

    def _build_instructions_tab(self):
        pane = self.instructions_tab
        # Deliberately not counted toward the project content token budget
        # (see Files/Images/Memory tabs) - short and user-authored, unlike
        # bulk file/image/note content, so it isn't worth budgeting.
        self.instructions_caption = self._wrap_to_width(
            self._caption(pane, "Standing instructions injected into every chat in this project."))
        self.instructions_caption.pack(fill="x", pady=(_px(self, 6), _px(self, 7)))
        self.instructions_text = self._make_text(pane, wrap="word")
        self.instructions_text.kit_well.pack(fill="both", expand=True)

    def _build_files_tab(self):
        pane = self.files_tab
        s = lambda n: _px(self, n)  # noqa: E731
        # the budget and status lines wrap at their width too: big numbers or a long file name are never cut
        self.files_budget_label = self._wrap_to_width(self._caption(pane))
        self.files_budget_label.pack(fill="x", pady=(s(6), s(7)))
        # bottom rows first (side="bottom"), so the list well is what a small window shrinks
        tip = self._wrap_to_width(self._caption(
            pane, "Tip: you can also drag files (and images) from Explorer anywhere onto this window."))
        tip.pack(side="bottom", fill="x", pady=(s(2), 0))
        self.files_status_label = self._wrap_to_width(self._caption(pane))
        self.files_status_label.pack(side="bottom", fill="x")
        btn_row = tk.Frame(pane, bg=self._t["bg"])
        btn_row.pack(side="bottom", fill="x", pady=(s(10), s(6)))
        self.add_files_btn = self._flat_button(btn_row, "+ Add files...", self._on_add_file, side="left")
        self.remove_file_btn = self._flat_button(btn_row, "Remove selected", self._on_remove_file, kind="text",
                                                 side="left", padx=(s(16), 0))
        self.files_list = self._make_listbox(pane, height=3)
        self.files_list.kit_well.pack(fill="both", expand=True)
        # FIX1 A3: Tab follows the stacking order, so raise the bottom rows back to the end: list, Add, Remove
        btn_row.lift()

    def _build_images_tab(self):
        pane = self.images_tab
        s = lambda n: _px(self, n)  # noqa: E731
        self.images_caption = self._wrap_to_width(self._caption(
            pane,
            "Captioned once at upload time, then injected as text into every chat in this "
            "project - no vision model needed to chat here afterward."))
        self.images_caption.pack(fill="x", pady=(s(6), s(4)))
        self.images_budget_label = self._wrap_to_width(self._caption(pane))
        self.images_budget_label.pack(fill="x", pady=(0, s(2)))
        # Fixed-height row so the grid tiering (large/medium/small/cube,
        # same tiers as Chat's staging grid) never jumps the rest of the
        # tab's layout as the tile count/size changes - same reasoning as
        # chat_view.py's staged_row.
        self.images_grid_row = tk.Frame(pane, bg=self._t["bg"], height=ag.STAGE_ROW_HEIGHT)
        self.images_grid_row.pack(fill="x")
        self.images_grid_row.pack_propagate(False)
        btn_row = tk.Frame(pane, bg=self._t["bg"])
        btn_row.pack(fill="x", pady=(s(10), s(6)))
        self.add_images_btn = self._flat_button(btn_row, "+ Add images...", self._on_add_image, side="left")
        self.images_status_label = self._wrap_to_width(self._caption(pane))
        self.images_status_label.pack(fill="x")
        # B6-H3: the rows are packed top-down and none of them gives way, so a pane shorter than their sum
        # (a 125 % text scale at the 820x560 floor) squeezed the wrapped budget line. The grid row is the one
        # that yields: its height follows the room the other rows leave, never below the cube tier (+ 16),
        # and is STAGE_ROW_HEIGHT whenever there is room - the approved look is unchanged then.
        pane.bind("<Configure>", self._fit_images_grid_row, add="+")
        self.images_budget_label.bind("<Configure>", self._fit_images_grid_row, add="+")

    def _fit_images_grid_row(self, _e=None):
        """Size the Images grid row to the room the pane has left after its other rows (B6-H3)."""
        pane, row = self.images_tab, self.images_grid_row
        try:
            avail = pane.winfo_height()
            if avail <= 1:
                return
            others = 0
            for w in pane.pack_slaves():
                if w is row:
                    continue
                pad = w.pack_info().get("pady", 0) or 0            # an int, a (top, bottom) tuple, or a string
                parts = [int(float(str(p))) for p in (pad if isinstance(pad, (tuple, list)) else str(pad).split())]
                others += w.winfo_reqheight() + sum(parts) * (2 if len(parts) == 1 else 1)
            want = max(ag.STAGE_TIERS[-1][1] + 16, min(ag.STAGE_ROW_HEIGHT, avail - others))
            if int(row.cget("height")) != want:
                row.configure(height=want)
        except (tk.TclError, ValueError):
            pass

    def _build_memory_tab(self):
        pane = self.memory_tab
        t = self._t
        s = lambda n: _px(self, n)  # noqa: E731
        self.memory_budget_label = self._wrap_to_width(self._caption(pane))
        self.memory_budget_label.pack(fill="x", pady=(s(6), s(7)))
        # bottom rows first (side="bottom"), so the list well is what a small window shrinks
        self.remove_memory_btn = self._flat_button(pane, "Remove selected", self._on_remove_memory, kind="text",
                                                   side="bottom", anchor="w", padx=(0, 0), pady=(s(6), 0))
        add_row = tk.Frame(pane, bg=t["bg"])
        add_row.pack(side="bottom", fill="x", pady=(s(10), 0))
        self.memory_entry_var = tk.StringVar()
        self.memory_add_btn = self._flat_button(add_row, "Add", self._on_add_memory, side="right",
                                                padx=(s(10), 0))
        box = tk.Frame(add_row, bg=t["bg"], height=s(WELL_H), bd=0, highlightthickness=0)
        box.pack(side="left", fill="x", expand=True)
        box.pack_propagate(False)
        well = self._well(box)
        well.pack(fill="both", expand=True)
        entry = well.attach(tk.Entry(
            well.body, textvariable=self.memory_entry_var, font=C11, bg=t["panel"], fg=t["bright"],
            insertbackground=t["bright"], selectbackground=t["bright"], selectforeground=t["bg"],
            relief="flat", bd=0, highlightthickness=0,
        ))
        entry.pack(fill="both", expand=True, padx=s(7))
        entry.bind("<Return>", lambda e: self._on_add_memory())
        self.memory_entry = entry
        self.memory_list = self._make_listbox(pane, height=3)
        self.memory_list.kit_well.pack(fill="both", expand=True)
        # FIX1 A3: Tab follows the stacking order: the list, the entry, Add, Remove (as before B6)
        self.memory_add_btn.lift()
        add_row.lift()
        self.remove_memory_btn.lift()

    # ---- data loading ----

    def _load(self):
        project_id = self.project_id
        # Blank everything first - the previous project's name, instructions and
        # lists must not be visible (or savable) while this one is on its way.
        self._project = {}
        self.name_var.set("")
        self.instructions_text.delete("1.0", "end")
        self._refresh_files()
        self._refresh_images()
        self._refresh_memory()
        self._sync_upload_controls()
        self._load_cover.place(x=0, y=0, relwidth=1, relheight=1)
        self._load_cover.lift()

        def apply(project):
            if project_id != self.project_id:
                return  # a different project was opened while this one loaded
            self._load_cover.place_forget()
            self._project = project
            self.name_var.set(project["name"])
            self.instructions_text.delete("1.0", "end")
            self.instructions_text.insert("1.0", project.get("instructions") or "")
            self._refresh_files()
            self._refresh_images()
            self._refresh_memory()

        def failed(error):
            self._load_cover.place_forget()
            show_toast(self, self.ctx.theme, f"Could not load project: {error}", kind="error")
            self.ctx.go_back()

        self._load_loader.start(lambda: self.ctx.api.get_project(project_id), apply, failed)

    def _project_budget_summary_text(self):
        used = self._project.get("project_content_tokens_used", 0)
        budget = self._project.get("project_content_token_budget", 1)
        effective = self._project.get("effective_slot_context_tokens", 0)
        pct = self._project.get("project_content_budget_pct", 0.5)
        return (f"{used:,} / {budget:,} combined project-content tokens "
                f"({pct:.0%} of {effective:,} effective tokens per chat)")

    def _refresh_files(self):
        self._files = self._project.get("files", [])
        self.files_list.delete(0, "end")
        for f in self._files:
            tokens = f.get("content_tokens") or 0
            self.files_list.insert("end", f"{f['filename']}  ({tokens:,} tok)")
        self.files_budget_label.config(text=self._project_budget_summary_text())

    # ---- images ----

    def _refresh_images(self):
        self._hover.hide()
        self._images = self._project.get("images", [])
        self._image_bytes = {}
        self.images_budget_label.config(text=self._project_budget_summary_text())
        self._render_images_grid()  # renders placeholders immediately, thumbnails fill in as bytes arrive
        # Bumped on every call so a stale poll loop from a *previous* call
        # (still ticking every 50ms because it hadn't finished yet - e.g.
        # add/remove/tab-switch fired before the last refresh's fetches
        # landed) recognizes it's obsolete and stops, instead of running
        # forever alongside the new one. Confirmed live: without this, each
        # additional refresh before the prior one finished left its polling
        # loop running indefinitely, compounding into serious lag over a
        # session with several add/remove actions.
        self._images_refresh_gen += 1
        if not self._images:
            return
        project_id = self.project_id
        gen = self._images_refresh_gen
        q = queue.Queue()
        threading.Thread(target=self._fetch_image_bytes_worker, args=(project_id, q), daemon=True).start()
        self.after(50, self._poll_image_bytes_queue, q, project_id, gen, len(self._images), 0)

    def _fetch_image_bytes_worker(self, project_id, q):
        # One background thread, all images fetched sequentially - local
        # loopback HTTP calls, but still real I/O that must not block the
        # Tk main thread (same reasoning as every other worker in this
        # screen/ChatView).
        for img in self._images:
            try:
                raw, _ = self.ctx.api.get_project_image_bytes(project_id, img["id"])
                q.put((img["id"], base64.b64encode(raw).decode("ascii")))
            except ApiError:
                q.put((img["id"], None))

    def _poll_image_bytes_queue(self, q, project_id, gen, expected_count, attempts_so_far):
        if project_id != self.project_id or gen != self._images_refresh_gen:
            return  # navigated away, or superseded by a newer _refresh_images() call
        drained = False
        while True:
            try:
                image_id, b64 = q.get_nowait()
            except queue.Empty:
                break
            drained = True
            attempts_so_far += 1
            if b64 is not None:
                self._image_bytes[image_id] = b64
        if drained:
            self._render_images_grid()
        # Track attempts, not successes - a permanently-failing fetch (e.g.
        # a deleted image) must not poll forever, same reasoning as the gen
        # check above. attempts_so_far is a plain call-local value (not a
        # shared self attribute), so it can't leak across generations.
        if attempts_so_far < expected_count:
            self.after(50, self._poll_image_bytes_queue, q, project_id, gen, expected_count, attempts_so_far)

    def _render_images_grid(self):
        # Same dangling-hover-overlay hazard as chat_view.py's staging
        # grid - always clear before destroying children, not just on the
        # next hover.
        self._hover.hide()
        for child in self.images_grid_row.winfo_children():
            child.destroy()
        if not self._images:
            return
        self.images_grid_row.update_idletasks()
        width = self.images_grid_row.winfo_width()
        if width <= 1:
            width = 700
        tier_name, size = ag.pick_tier(len(self._images), width)
        for img in self._images:
            self._build_project_image_tile(img, size, tier_name).pack(side="left", padx=(0, ag.STAGE_GAP), pady=8)

    def _build_project_image_tile(self, img, size, tier_name):
        theme = self.ctx.theme
        tile = ag.make_tile_shell(self.images_grid_row, theme, size)
        cost_text = f"{img['caption_tokens']:,} tok"
        if tier_name == "cube":
            tk.Label(
                tile, text=cost_text, bg=theme.panel, fg=theme.text, font=theme_mod.FONT_CAPTION,
                wraplength=size - 4, justify="center",
            ).pack(expand=True)
        else:
            if size >= ag.STAGE_TIERS[2][1]:
                tk.Label(
                    tile, text=cost_text, bg=theme.panel, fg=theme.dim, font=theme_mod.FONT_CAPTION,
                    wraplength=size - 4,
                ).pack(side="bottom")
            b64 = self._image_bytes.get(img["id"])
            photo = self._photo_cache.get(img["id"], b64, max(8, size - 8)) if b64 else None
            if photo is not None:
                tk.Label(tile, image=photo, bg=theme.panel).pack(expand=True)
            else:
                ext_label = os.path.splitext(img["filename"])[1].lstrip(".").upper() or "IMG"
                tk.Label(
                    tile, text=ext_label, bg=theme.panel_hover, fg=theme.dim, font=theme_mod.FONT_LABEL,
                ).pack(expand=True, fill="both", padx=2, pady=2)
        self._hover.bind(tile, theme, lambda overlay, i=img: self._populate_image_hover_content(overlay, i))
        ag.add_remove_button(tile, theme, lambda i=img["id"]: self._on_remove_image(i))
        return tile

    def _populate_image_hover_content(self, overlay, img):
        theme = self.ctx.theme
        b64 = self._image_bytes.get(img["id"])
        photo = self._photo_cache.get(img["id"], b64, 400) if b64 else None
        if photo is not None:
            tk.Label(overlay, image=photo, bg=theme.panel).pack(padx=10, pady=(10, 4))
        text = f"{img['filename']} ({img['caption_tokens']:,} tok)\n\n{img.get('caption', '')}"
        tk.Label(
            overlay, text=text, bg=theme.panel, fg=theme.text, font=theme_mod.FONT_MONO,
            justify="left", wraplength=480, padx=14, pady=(4, 10),
        ).pack()

    def _upload_busy(self):
        # Captioning is real vision-model inference (tens of seconds per
        # image, confirmed live) and file uploads stream over HTTP - a
        # second upload started while one is already in flight was a real,
        # reproducible way to pile up concurrent requests and make the
        # whole app feel like it had frozen. Guard both entry points (the
        # add-file/add-image buttons and a file drop) against that directly
        # rather than relying on the user not double-clicking/dropping.
        return str(self.add_files_btn["state"]) == "disabled" or str(self.add_images_btn["state"]) == "disabled"

    def _on_add_image(self):
        if self._upload_busy():
            return
        paths = filedialog.askopenfilenames(title="Choose images to add to this project")
        if not paths:
            return
        skipped = [
            os.path.basename(p) for p in paths
            if os.path.splitext(p)[1].lower() not in IMAGE_EXTENSIONS
        ]
        paths = [p for p in paths if os.path.splitext(p)[1].lower() in IMAGE_EXTENSIONS]
        if skipped:
            reason = "not supported by the vision model" if any(
                os.path.splitext(s)[1].lower() in UNSUPPORTED_IMAGE_EXTENSIONS for s in skipped
            ) else "not a supported image type"
            show_toast(self, self.ctx.theme, f"Skipped ({reason}): {', '.join(skipped)}", kind="warning")
        if paths:
            self._start_images_upload(list(paths))

    def _start_images_upload(self, paths):
        project_id = self.project_id                      # B6-H4: the project shown NOW owns this upload
        state = self._upload_started("images", project_id,
                                     "Uploading... captioning can take a minute or more per image, this is normal.")
        q = queue.Queue()
        threading.Thread(target=self._upload_images_worker, args=(project_id, paths, q), daemon=True).start()
        self.after(50, self._poll_upload_images_queue, q, len(paths), project_id, state)

    def _upload_images_worker(self, project_id, paths, q):
        for i, path in enumerate(paths):
            name = os.path.basename(path)
            if os.path.getsize(path) > MAX_IMAGE_BYTES:
                q.put(("error", name, f"too large (max {MAX_IMAGE_BYTES // (1024 * 1024)} MB)"))
                continue
            q.put(("progress", f"Captioning image {i + 1} of {len(paths)}: {name}..."))
            try:
                self.ctx.api.upload_project_image(project_id, path)
                q.put(("ok", name))
            except ApiError as e:
                q.put(("error", name, str(e)))
                if e.status == 404:
                    # B6-H6: the project is gone (deleted mid-upload): every image left would fail the
                    # same way - count them and stop, instead of one request and one notice each
                    for rest in paths[i + 1:]:
                        q.put(("error", os.path.basename(rest), str(e)))
                    break
        q.put(("done", None))

    def _poll_upload_images_queue(self, q, total, project_id, state):
        try:
            item = q.get_nowait()
        except queue.Empty:
            self.after(50, self._poll_upload_images_queue, q, total, project_id, state)
            return
        kind = item[0]
        if kind == "progress":
            self._upload_progress("images", project_id, state, item[1])
        elif kind == "error":
            state["errors"].append((item[1], item[2]))
        if kind == "done":
            self._upload_finished("images", project_id, state, self._refresh_images)
            return
        self.after(50, self._poll_upload_images_queue, q, total, project_id, state)

    # ---- uploads: per-project state (B6-H4), one notice for the failures (B6-H6) ----

    def _upload_started(self, kind, project_id, text):
        """Record an upload starting for (kind, project_id) and show it - the status line, the ADD button
        disabled - while that project is the one shown. -> its state dict."""
        state = {"text": text, "errors": []}
        self._uploads[(kind, project_id)] = state
        self._sync_upload_controls()
        return state

    def _upload_progress(self, kind, project_id, state, text):
        state["text"] = text
        if project_id == self.project_id:
            self._upload_label(kind).config(text=text)

    def _upload_finished(self, kind, project_id, state, refresh):
        """The worker is done: drop its state (the shown project's status line and ADD button follow), say
        what failed in ONE notice and, while that project is still the one shown, redraw it from the server."""
        self._uploads.pop((kind, project_id), None)
        self._sync_upload_controls()
        self._upload_error_notice(kind, state["errors"])
        if project_id == self.project_id and self.is_visible():
            self._mutate(lambda: None, "Could not reload project", refresh)

    def _upload_label(self, kind):
        return self.images_status_label if kind == "images" else self.files_status_label

    def _upload_button(self, kind):
        return self.add_images_btn if kind == "images" else self.add_files_btn

    def _sync_upload_controls(self):
        """The status lines and the ADD buttons show the SHOWN project's uploads (B6-H4): another project's
        upload keeps running into that project, and shows again when that project is reopened."""
        for kind in ("files", "images"):
            state = self._uploads.get((kind, self.project_id))
            try:
                self._upload_label(kind).config(text=state["text"] if state else "")
                self._upload_button(kind).config(state="disabled" if state else "normal")
            except tk.TclError:
                pass

    def _upload_error_notice(self, kind, errors):
        """One notice for an upload's failures (B6-H6). A single failure reads as before ("Could not add
        file: <name>: <why>"); several are counted and their names listed (three, then "+N more"), with one
        shared reason said once."""
        if not errors:
            return
        noun = "image" if kind == "images" else "file"
        if len(errors) == 1:
            message = f"Could not add {noun}: {errors[0][0]}: {errors[0][1]}"
        else:
            more = f" (+{len(errors) - 3} more)" if len(errors) > 3 else ""
            reasons = {why for _name, why in errors}
            if len(reasons) == 1:
                names = ", ".join(name for name, _why in errors[:3])
                message = f"Could not add {len(errors)} {noun}s ({reasons.pop()}): {names}{more}"
            else:
                detail = "; ".join(f"{name}: {why}" for name, why in errors[:3])
                message = f"Could not add {len(errors)} {noun}s: {detail}{more}"
        show_toast(self, self.ctx.theme, message, kind="error")

    def _mutate(self, work, error_prefix, refresh, on_success=None):
        """Runs a project write - and the get_project that always follows it, to
        redraw from what the server now holds - off the Tk thread: both are
        round trips to a backend that can be slow (see async_call.py). On
        success runs on_success (if any), stores the fresh project and calls
        `refresh`; on failure shows "<error_prefix>: <why>". Dropped if a
        different project has been opened in the meantime."""
        project_id = self.project_id

        def job():
            work()
            return self.ctx.api.get_project(project_id)

        def done(project):
            if project_id != self.project_id:
                return
            if on_success is not None:
                on_success()
            self._project = project
            refresh()

        run_async(
            self, job, done,
            lambda e: show_toast(self, self.ctx.theme, f"{error_prefix}: {e}", kind="error"),
        )

    def _on_remove_image(self, image_id):
        def forget_image():
            self._photo_cache.evict(image_id)
            self._image_bytes.pop(image_id, None)

        self._mutate(
            lambda: self.ctx.api.delete_project_image(self.project_id, image_id),
            "Could not remove image", self._refresh_images, on_success=forget_image,
        )

    def _refresh_memory(self):
        self._memory = self._project.get("memory", [])
        self.memory_list.delete(0, "end")
        for m in self._memory:
            tokens = m.get("content_tokens") or 0
            self.memory_list.insert("end", f"{m['content']}  ({tokens:,} tok)")
        self.memory_budget_label.config(text=self._project_budget_summary_text())

    # ---- save / close ----

    def _on_save(self):
        if self._load_loader.busy:
            return  # still loading this project - the fields don't hold its content yet
        name = self.name_var.get().strip()
        if not name:
            show_toast(self, self.ctx.theme, "Project name can't be empty.", kind="warning")
            return
        if self._saving:
            return  # a double-click must not send two saves
        self._saving = True
        project_id = self.project_id
        instructions = self.instructions_text.get("1.0", "end-1c")

        def saved(_result):
            self._saving = False
            # B6-H5: a late save (CLOSE pressed and another screen opened while it was in flight) must not
            # drag the user back to Projects: go back only while Manage still shows this project
            if self.is_visible() and project_id == self.project_id:
                self.ctx.go_back()

        def failed(error):
            self._saving = False
            show_toast(self, self.ctx.theme, f"Could not save: {error}", kind="error")

        run_async(
            self, lambda: self.ctx.api.update_project(project_id, name=name, instructions=instructions),
            saved, failed,
        )

    def _on_close(self):
        self.ctx.go_back()

    # ---- files ----

    def _on_add_file(self):
        if self._upload_busy():
            return
        paths = filedialog.askopenfilenames(title="Choose files to add to this project")
        if paths:
            self._start_files_upload(list(paths))

    def _start_files_upload(self, paths):
        project_id = self.project_id                      # B6-H4: the project shown NOW owns this upload
        state = self._upload_started("files", project_id,
                                     f"Uploading 1 of {len(paths)}..." if len(paths) > 1 else "Uploading...")
        q = queue.Queue()
        threading.Thread(target=self._upload_files_worker, args=(project_id, paths, q), daemon=True).start()
        self.after(50, self._poll_upload_files_queue, q, len(paths), project_id, state)

    def _upload_files_worker(self, project_id, paths, q):
        for i, path in enumerate(paths):
            name = os.path.basename(path)
            q.put(("progress", i, name))
            try:
                self.ctx.api.upload_project_file(project_id, path)
                q.put(("ok", name))
            except ApiError as e:
                q.put(("error", name, str(e)))
                if e.status == 404:
                    # B6-H6: the project is gone (deleted mid-upload): every file left would fail the same
                    # way - count them and stop, instead of one request and one notice each
                    for rest in paths[i + 1:]:
                        q.put(("error", os.path.basename(rest), str(e)))
                    break
        q.put(("done", None))

    def _poll_upload_files_queue(self, q, total, project_id, state):
        try:
            item = q.get_nowait()
        except queue.Empty:
            self.after(50, self._poll_upload_files_queue, q, total, project_id, state)
            return
        kind = item[0]
        if kind == "progress":
            _, i, name = item
            self._upload_progress("files", project_id, state,
                                  f"Uploading {i + 1} of {total}: {name}..." if total > 1 else f"Uploading {name}...")
        elif kind == "error":
            state["errors"].append((item[1], item[2]))
        if kind == "done":
            self._upload_finished("files", project_id, state, self._refresh_files)
            return
        self.after(50, self._poll_upload_files_queue, q, total, project_id, state)

    def _on_remove_file(self):
        sel = self.files_list.curselection()
        if not sel:
            return
        file_id = self._files[sel[0]]["id"]
        self._mutate(
            lambda: self.ctx.api.delete_project_file(self.project_id, file_id),
            "Could not remove file", self._refresh_files,
        )

    # ---- memory ----

    def _on_add_memory(self):
        content = self.memory_entry_var.get().strip()
        if not content:
            return
        self._mutate(
            lambda: self.ctx.api.add_project_memory(self.project_id, content),
            "Could not add memory", self._refresh_memory, on_success=lambda: self.memory_entry_var.set(""),
        )

    def _on_remove_memory(self):
        sel = self.memory_list.curselection()
        if not sel:
            return
        mem_id = self._memory[sel[0]]["id"]
        self._mutate(
            lambda: self.ctx.api.delete_project_memory(self.project_id, mem_id),
            "Could not remove memory", self._refresh_memory,
        )

    # ---- delete (inline overlay with keep-options, replaces the old
    # ProjectDeleteDialog Toplevel - a popup launched from a popup was
    # already the fragile part; a popup launched from an embedded screen
    # would be worse, not better) ----

    def _on_delete(self):
        if self._load_loader.busy:
            return  # still loading - nothing to delete yet
        self._show_delete_confirm()

    def _show_delete_confirm(self):
        """B6: the shared B3 confirm (screens/base.show_inline_confirm), so the overlay has the same
        grammar as every other danger prompt: the title bar DELETE PROJECT, the question, the five
        keep-options as lamps (all unchecked), the danger sentence (added under the lamps, below),
        DELETE (danger_outline) + CANCEL (primary, focused), Tab / Enter / Esc, a backdrop click
        cancels, and one confirm.shown emit (danger=True, label="Delete") for the hazard cue (OBS-01).
        The kept values reach _do_delete_confirmed (unchanged) as plain bools in the old order.
        Returns the backdrop."""
        keys = [key for key, _label in DELETE_KEEP_OPTIONS]

        def confirmed(values):
            values = values or {}
            self._do_delete_confirmed(*(bool(values.get(key, False)) for key in keys))

        backdrop = show_inline_confirm(
            self, self.ctx.theme, DELETE_QUESTION, confirmed, confirm_label="Delete", danger=True,
            checkboxes=[(key, label, False) for key, label in DELETE_KEEP_OPTIONS], title="Delete project",
        )
        self._add_delete_warning(backdrop)
        return backdrop

    def _add_delete_warning(self, backdrop):
        """Put the danger sentence (verbatim, danger ink) right under the keep-options of the card
        show_inline_confirm built (the kit card, or base's plain stand-in: both hold the lamps in one
        Frame). If that Frame cannot be found the sentence goes at the card's end: never dropped."""
        t = self._t
        try:
            cards = [c for c in backdrop.winfo_children() if c.winfo_class() == "Frame"]
            if not cards:
                return None
            card = cards[0]
            lamps_frame = None
            for child in card.winfo_children():
                if any(g.winfo_class() == "Checkbutton" for g in child.winfo_children()):
                    lamps_frame = child
                    break
            ground = card.cget("bg")
            label = tk.Label(card, text=DELETE_WARNING, bg=ground, fg=t["danger"], font=C10, justify="left",
                             anchor="w", padx=0, pady=0, bd=0, wraplength=_px(self, 420))
            if lamps_frame is not None:
                label.pack(fill="x", padx=lamps_frame.pack_info().get("padx", 0), pady=(_px(self, 12), 0),
                           after=lamps_frame)
            else:
                label.pack(fill="x", padx=_px(self, 26), pady=(_px(self, 12), _px(self, 12)))
            backdrop.delete_warning = label
            return label
        except tk.TclError:
            return None

    def _do_delete_confirmed(self, keep_conversations, keep_instructions, keep_files, keep_images, keep_memory):
        project_id = self.project_id

        def deleted(_result):
            if self.ctx.on_project_deleted:
                self.ctx.on_project_deleted(project_id)
            self.ctx.go_to_screen("projects")

        run_async(
            self,
            lambda: self.ctx.api.delete_project(
                project_id, keep_conversations=keep_conversations, keep_instructions=keep_instructions,
                keep_files=keep_files, keep_images=keep_images, keep_memory=keep_memory,
            ),
            deleted,
            lambda e: show_toast(self, self.ctx.theme, f"Could not delete project: {e}", kind="error"),
        )
