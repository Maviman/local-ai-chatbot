"""Root native window for Local AI ChatBot - the screen router.

A persistent 42px icon rail (Home/Chats/Projects/Settings, Alt+1..4 global
shortcuts) plus a single content area that swaps between full-screen
views. No screen is ever a popup - this replaces the old always-visible
sidebar + tab toggle + Toplevel Settings dialog outright. Per the redesign
plan's foundation decision, this is a fresh build against api_client.py,
not an incremental edit of the previous _build_shell/_switch_left_tab
logic; only field-level logic from the old screens is ported, not this
shell itself.

This phase (3) proves the router + rail + keymap wiring with lightweight
placeholder screens - real content replaces each placeholder one screen at
a time in the phases that follow.
"""

import os
import queue
import threading
import time
import tkinter as tk

from . import fx as fx_mod
from . import keymap as keymap_mod
from . import theme as theme_mod
from . import win_dnd
from .async_call import run_async
from .chat_view import IMAGE_GEN_STAGE_LABELS, ChatView
from .screens.base import apply_ttk_theme, cancel_pending_after_callbacks, show_inline_confirm, show_toast
from .screens.chats_screen import ChatsScreen
from .screens.home_screen import HomeScreen
from .screens.image_generator_screen import ImageGeneratorScreen
from .screens.image_uplink_screen import ImageUplinkScreen
from .screens.project_manage_screen import ProjectManageScreen
from .screens.projects_screen import ProjectsScreen
from .screens.settings_screen import SettingsScreen

try:  # batch 34: the activity pull-down (a guarded import: the window opens without it)
    from .activity import ActivityControl
except Exception:  # noqa: BLE001
    ActivityControl = None

# A1 (bug-fix batch 30): the one health watcher for the whole app - every HEALTH_WATCH_MS while the window shows
HEALTH_WATCH_MS = 10_000

ICON_PNG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "images", "O5_10_ver5.png"
)

RAIL_WIDTH = 42
RAIL_SCREENS = ("home", "chats", "projects", "settings", "images")
# B2: each rail cell carries a glyph, its short display word (D-6) and its A-code (Alt+1..5)
RAIL_LABELS = {"home": "HOME", "chats": "CHATS", "projects": "PROJ", "settings": "CONFIG", "images": "IMG"}
RAIL_GLYPHS = {"home": "home", "chats": "chats", "projects": "proj", "settings": "config", "images": "proj"}   # the Images tab reuses the frame glyph
LEGEND_CHIP_SCREENS = ("home",)   # the r3 Home legend: key chips + a 1 px dim rule; every other screen is plain

# B3h (D-37: the program must work on other PCs, small screens included). The window opens at 1100x800, or
# smaller to fit the work area (the screen minus the taskbar) less a 40 px margin, centred in it. MIN_SIZE is
# the measured floor at 100 % (B3h; see design archive): the rail's power cell, the legend line, the Chat composer with
# Send/Queue and Stop and the Settings SAVE footer stay fully on screen, and Home's grid keeps its first row whole
# with the rest one scroll away (below 820 px its right column is squeezed). On a screen too small even for that
# the floor shrinks to what fits, so the window can always be shown whole.
DEFAULT_SIZE = (1100, 800)
MIN_SIZE = (820, 560)
WORK_AREA_MARGIN = 40
_FALLBACK_TASKBAR = 80   # no work area (not Windows, or the call failed): screen size less this, both ways
_FRAME_FALLBACK = (16, 39)  # a sizable window's frame + title bar at 100 % (only used to centre and keep it inside)


def _work_area():
    """(left, top, right, bottom) of the primary monitor's work area, or None. The app is not DPI-aware, so
    Windows reports this in the same (virtualised) pixels Tk uses; initial_geometry clamps it to Tk's screen
    size anyway, so another coordinate space could never make the window bigger than the screen."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32")
        spi = user32.SystemParametersInfoW
        spi.argtypes = (wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT)
        spi.restype = wintypes.BOOL
        rect = wintypes.RECT()
        if not spi(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
            return None
        return rect.left, rect.top, rect.right, rect.bottom
    except Exception:  # noqa: BLE001 - not Windows / no user32: the fallback applies
        return None


def _frame_size():
    """(extra width, extra height) the window frame and title bar add around the client area."""
    try:
        import ctypes
        metric = ctypes.WinDLL("user32").GetSystemMetrics
        border = metric(32) + metric(92)  # SM_CXSIZEFRAME + SM_CXPADDEDBORDER, each side
        caption = metric(4)               # SM_CYCAPTION
        if 0 < border < 64 and 0 < caption < 128:
            return 2 * border, caption + 2 * border
    except Exception:  # noqa: BLE001
        pass
    return _FRAME_FALLBACK


def initial_geometry(screen_w, screen_h, work=None, frame=_FRAME_FALLBACK):
    """("WxH+X+Y", (min_w, min_h)) for the first show. `work` is the work area (left, top, right, bottom) or
    None. The client size is DEFAULT_SIZE, or the work area less WORK_AREA_MARGIN when that is smaller; the whole
    window (frame included) is centred in the work area and never placed above or left of it."""
    sw, sh = max(1, int(screen_w)), max(1, int(screen_h))
    left = top = 0
    right, bottom = sw - _FALLBACK_TASKBAR, sh - _FALLBACK_TASKBAR
    if work is not None:
        l, t, r, b = (int(v) for v in work)
        l, t, r, b = max(0, l), max(0, t), min(r, sw), min(b, sh)  # never larger than the screen Tk sees
        if r - l > 0 and b - t > 0:
            left, top, right, bottom = l, t, r, b
    work_w, work_h = max(1, right - left), max(1, bottom - top)
    w = max(1, min(DEFAULT_SIZE[0], work_w - WORK_AREA_MARGIN))
    h = max(1, min(DEFAULT_SIZE[1], work_h - WORK_AREA_MARGIN))
    fw, fh = frame
    x = left + max(0, (work_w - (w + fw)) // 2)
    y = top + max(0, (work_h - (h + fh)) // 2)
    return "%dx%d+%d+%d" % (w, h, x, y), (min(MIN_SIZE[0], w), min(MIN_SIZE[1], h))


class _PlainLegend(tk.Label):
    """The legend row when kit.legend cannot be imported (B2 fix ADV1-07): one plain Label with the
    same set_items(items, chips=None, rule=None, dim=None) / .items surface as kit.legend.LegendLine,
    so the keys the screens moved there are still shown."""

    def __init__(self, master, theme):
        self._theme = theme
        self._items = []
        super().__init__(master, text="", bg=theme.bg, fg=theme.value, font=("Consolas", 9), anchor="w",
                         padx=16, pady=4)

    @property
    def items(self):
        return list(self._items)

    def set_items(self, items, chips=None, rule=None, dim=None):
        self._items = [tuple(it) if isinstance(it, (tuple, list)) else (str(it), "") for it in (items or ())]
        text = "   ".join(" ".join(str(p) for p in it[:2] if p).strip() for it in self._items)
        self.configure(text=text, fg=self._theme.dim if dim else self._theme.value)


def _legacy_bell_silenced():
    """B12-H2: LOCALAI_SFX_SILENT (fx/sound/_win.is_silent's parse) covers the one audible path outside fx/sound."""
    try:
        from .fx.sound._win import is_silent
        return bool(is_silent())
    except Exception:
        return False


class ScreenContext:
    """Shared handles constructed once by the router and passed to every
    screen. Screens reach cross-screen navigation only through here (e.g.
    ctx.go_to_chat(conv_id)) - never by importing another screen directly,
    so screens stay swappable/independent."""

    def __init__(self, api, theme):
        self.api = api
        self.theme = theme
        self.go_to_screen = None
        self.go_to_chat = None
        self.go_to_project_manage = None
        self.go_back = None
        self.new_session = None
        self.on_model_switched = None
        self.on_project_deleted = None
        self.on_conversation_deleted = None     # B14-H7: the Chats / Projects screens tell Chat a chat is gone
        self.shutdown = None
        self.lock_navigation = None
        self.unlock_navigation = None
        self.fx = fx_mod.NULL  # set by MainWindow._build_shell (fx.install)


class MainWindow(tk.Tk):
    # The aesthetic layer (desktop/fx). Replaced by the real one at the top of
    # _build_shell; until then - and whenever fx is switched off or failed to
    # install - every hook below is a no-op against fx.NULL.
    fx = fx_mod.NULL

    def __init__(self, server_module, api_client):
        super().__init__()
        self.server = server_module
        self.api = api_client
        self.theme = theme_mod.load_theme()
        self._normalise_fonts()

        self.title("Personal Terminal")
        self._apply_initial_geometry()
        self.configure(bg=self.theme.bg)
        apply_ttk_theme(self.theme)
        self._set_icon()

        self.ctx = ScreenContext(api_client, self.theme)
        self.ctx.go_to_screen = self.show_screen
        self.ctx.go_to_chat = self._go_to_chat
        self.ctx.go_to_project_manage = self._go_to_project_manage
        self.ctx.go_back = self._on_back
        self.ctx.new_session = self._new_session
        self.ctx.on_model_switched = self._on_model_switched
        self.ctx.on_project_deleted = self._on_project_deleted
        self.ctx.on_conversation_deleted = self._on_conversation_deleted
        self.ctx.shutdown = self._on_shutdown_clicked
        self.ctx.lock_navigation = self._lock_navigation
        self.ctx.unlock_navigation = self._unlock_navigation

        self.keymap = keymap_mod.Keymap(self)
        self._screens = {}
        self._rail_buttons = {}
        self._current_screen_name = None
        self._history = []
        self._navigation_locked = False
        self._nav_lock_depth = 0      # B9-H3: how many model-load cards hold the lock (the bool is what readers use)
        # True while a "new chat" request is on its way to the backend, so a
        # double-click / held Ctrl+N can't create (and abandon) several chats.
        self._new_session_pending = False
        # A1: the health watcher (see _health_watch_tick): was the model server ok at some point this session?
        self._health_was_ok = False
        self._health_watch_busy = False
        self._health_watch_after_id = None
        # batch 34: the activity snapshot's listeners (the pull-down), coalesced per idle pass; the watcher's last word;
        # when the load card went up
        self._activity_listeners = []
        self._activity_pending = False
        self._health_last = {}
        self._load_since = None
        self.activity = None

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_shell()
        self._register_screens()
        self._wire_global_keymap()
        chat = self._screens.get("chat")
        if chat is not None:
            chat.on_activity_changed = self._notify_activity
        if ActivityControl is not None:
            try:
                self.activity = ActivityControl(self)
            except Exception as exc:  # noqa: BLE001 - never keep the window from opening
                print(f"activity pull-down not built ({exc!r})")
                self.activity = None
        win_dnd.install(self, self._on_files_dropped)

        self.show_screen(self._initial_screen())
        self.after(300, self._poll_gui_events)
        self._health_watch_after_id = self.after(HEALTH_WATCH_MS, self._health_watch_tick)

    def _apply_initial_geometry(self):
        """B3h (D-37): size and centre the window for this PC's work area, and set the minimum size."""
        try:
            geometry, minsize = initial_geometry(self.winfo_screenwidth(), self.winfo_screenheight(),
                                                 _work_area(), _frame_size())
            self.geometry(geometry)
            self.minsize(*minsize)
        except Exception as exc:  # noqa: BLE001 - never keep the window from opening over its size
            import sys
            sys.stderr.write("[window] fitted size skipped: %r\n" % (exc,))
            self.geometry("%dx%d" % DEFAULT_SIZE)

    def _normalise_fonts(self):
        """Fonts are part of the look, not an effect (D-31): normalise the named
        defaults and create the display faces before the first widget, kill switch
        or not. fx.install repeats both calls later; they are idempotent."""
        try:
            from .fx import fonts as fonts_mod
            from .fx import prefs as prefs_mod
            raw = theme_mod.last_loaded_raw()
            fxraw = raw.get("fx") if isinstance(raw, dict) else None
            face = fxraw.get("display_face") if isinstance(fxraw, dict) else None
            if not (isinstance(face, str) and face in prefs_mod.DISPLAY_FACES):
                face = fonts_mod.DEFAULT_FACE
            fonts_mod.normalise_default_fonts(self)
            fonts_mod.ensure(self, face)
        except Exception as exc:  # noqa: BLE001 - never block the window over fonts
            import sys
            sys.stderr.write("[fonts] early normalisation skipped: %r\n" % (exc,))

    def _set_icon(self):
        try:
            icon = tk.PhotoImage(file=ICON_PNG)
            # keep a reference or Tk will garbage-collect the image
            self._icon_images = [icon, icon.subsample(4), icon.subsample(8), icon.subsample(16)]
            self.iconphoto(True, *self._icon_images)
        except tk.TclError:
            pass

    # ---- shell: rail + content area ----

    def _build_shell(self):
        self.fx = self.ctx.fx = fx_mod.install(self, self.theme)
        self.ctx.refresh_legend = self.refresh_legend
        body = tk.Frame(self, bg=self.theme.bg)
        body.pack(fill="both", expand=True)

        # B2: the approved rail (r2_rail_states) - panel fill, only a 1 px dim right edge, four 40x60
        # cells (glyph, short word, A-code) from y 12 at a 64 px pitch, and the 40x34 power cell 46 px
        # above the bottom. Sizes follow the display ratio like the kit's (1.0 at 100 %).
        try:
            from .kit import geom_ratio
            from .kit import rail as kit_rail
            ratio = geom_ratio(self)
        except Exception as exc:  # noqa: BLE001 - the window must open even without the kit
            kit_rail, ratio = None, 1.0
            self._kit_failed("rail", exc)

        def px(n):
            return int(round(n * ratio))

        self.rail = tk.Frame(body, bg=self.theme.panel, width=px(RAIL_WIDTH), highlightthickness=0, bd=0)
        self.rail.pack(side="left", fill="y")
        self.rail.pack_propagate(False)
        self.rail_edge = tk.Frame(self.rail, bg=self.theme.dim, highlightthickness=0, bd=0)
        self.rail_edge.place(relx=1.0, x=0, y=0, anchor="ne", width=px(1), relheight=1.0)

        for i, name in enumerate(RAIL_SCREENS, start=1):
            command = (lambda n=name: self.show_screen(n))
            if kit_rail is not None:
                cell = kit_rail.RailCell(self.rail, self.theme, RAIL_GLYPHS[name], RAIL_LABELS[name], "A%d" % i,
                                         command=command)
                x, y = kit_rail.cell_xy(i - 1, ratio)
                cell.place(x=x, y=y)
            else:
                cell = tk.Label(self.rail, text=RAIL_LABELS[name], bg=self.theme.panel, fg=self.theme.value,
                                font=("Consolas", 7, "bold"), cursor="hand2", pady=10)
                cell.pack(fill="x", padx=(0, 1), pady=(12 if i == 1 else 4, 4))
                cell.bind("<Button-1>", lambda e, c=command: c())
            self._rail_buttons[name] = cell

        if kit_rail is not None:
            self.power_btn = kit_rail.PowerCell(self.rail, self.theme, command=self._on_shutdown_clicked)
            self.power_btn.place(x=px(kit_rail.RAIL_X0), rely=1.0, y=-px(kit_rail.POWER_BOTTOM))
            # B2 fix ADV1-08: on a short window the power cell must never ride up over the CONFIG cell
            # (stacked above it, it would take CONFIG's clicks and ask the shut-down confirm). It sits
            # 46 px above the bottom, but never above the last cell's bottom + 4 px: the rail clips it.
            self._power_min_y = kit_rail.cell_xy(len(RAIL_SCREENS) - 1, ratio)[1] + px(kit_rail.CELL_H + 4)
            self._power_bottom = px(kit_rail.POWER_BOTTOM)
            self.rail.bind("<Configure>", self._place_power_cell, add="+")
        else:
            self.power_btn = tk.Label(
                self.rail, text="⏻", bg=self.theme.panel, fg=self.theme.danger, cursor="hand2", pady=10,
            )
            self.power_btn.pack(side="bottom", pady=(4, 12))
            self.power_btn.bind("<Button-1>", lambda e: self._on_shutdown_clicked())

        self.content = tk.Frame(body, bg=self.theme.bg)
        self.content.pack(side="left", fill="both", expand=True)
        # B2: the key legend line - packed at the content column's bottom BEFORE any screen is packed,
        # so a short window squeezes the screen, never this row. Effects (notices, B3) keep clear of it
        # through fx_bottom_reserve.
        try:
            from .kit.legend import LegendLine
            self.legend = LegendLine(self.content, self.theme)
        except Exception as exc:  # noqa: BLE001 - the window must open even without the kit
            self._kit_failed("legend", exc)
            # B2 fix ADV1-07: the fallback still shows the keys (D-27: the moved hints never vanish)
            self.legend = _PlainLegend(self.content, self.theme)
        self.legend.pack(side="bottom", fill="x")
        self.fx_bottom_reserve = int(self.legend.winfo_reqheight())

    def _place_power_cell(self, event=None):
        """46 px above the rail's bottom, but never over the last screen cell (ADV1-08)."""
        try:
            h = event.height if event is not None else self.rail.winfo_height()
            self.power_btn.place_configure(rely=0, y=max(self._power_min_y, h - self._power_bottom))
        except (tk.TclError, AttributeError):
            pass

    @staticmethod
    def _kit_failed(what, exc):
        import sys
        sys.stderr.write("[kit] %s failed; the window keeps a plain fallback: %r\n" % (what, exc))

    def _register_screens(self):
        self._screens["home"] = HomeScreen(self.content, self.ctx)
        self._screens["chats"] = ChatsScreen(self.content, self.ctx)
        self._screens["projects"] = ProjectsScreen(self.content, self.ctx)
        self._screens["settings"] = SettingsScreen(self.content, self.ctx)
        self._screens["image_uplink"] = ImageUplinkScreen(self.content, self.ctx)
        self._screens["images"] = ImageGeneratorScreen(self.content, self.ctx)
        self._screens["chat"] = ChatView(self.content, self.ctx)
        self._screens["project_manage"] = ProjectManageScreen(self.content, self.ctx)

    # ---- A1: the one health watcher ----
    def _health_watch_tick(self):
        """Every HEALTH_WATCH_MS while the window shows: one /api/health off the Tk thread (async_call), the reading
        to Home's chip, and ONE notice when a server that was ok has stopped or hangs - nothing else noticed a death
        mid-session (Home's own poll stops at ok, so its chip kept "Ready"). Paused while the window is hidden and
        while a load overlay holds the navigation (it polls health itself)."""
        self._health_watch_after_id = self.after(HEALTH_WATCH_MS, self._health_watch_tick)
        if self._health_watch_busy or self._navigation_locked:
            return
        try:
            if not self.winfo_viewable():
                return
        except tk.TclError:
            return
        self._health_watch_busy = True
        run_async(self, self.api.health, self._on_health_watch, lambda e: self._on_health_watch(None))

    def _on_health_watch(self, health):
        self._health_watch_busy = False
        if not isinstance(health, dict):
            return                      # the backend itself did not answer: Home says "unreachable"
        status = health.get("status")
        home = self._screens.get("home")
        if home is not None and hasattr(home, "apply_watch_health"):
            home.apply_watch_health(health)
        # batch 34: the pull-down's MODEL row carries the watcher's word when it is not ok
        if status == "error":
            word = "Stopped" if health.get("reason") == "stopped" else "Error"
        else:
            word = {"ok": "Ready", "starting": "Loading...", "hung": "Not responding"}.get(status, "")
        if word != self._health_last.get("word"):
            self._health_last = {"status": status, "word": word, "since": time.time()}
            self._notify_activity()
        if status == "ok":
            self._health_was_ok = True
            return
        if not self._health_was_ok:
            return                      # never ok this session (no model yet, a start error): Home's notice says why
        if status == "hung":
            self._health_was_ok = False
            show_toast(self, self.theme, "The model server is not responding - press RESTART on Home or in Settings > Model.",
                       kind="warning")
        elif status == "error" and health.get("reason") == "stopped":
            self._health_was_ok = False
            show_toast(self, self.theme, "The model server stopped - press RESTART on Home or in Settings > Model.",
                       kind="warning")

    # ---- batch 34: the activity snapshot (the pull-down's data) ----
    def add_activity_listener(self, callback):
        if callback not in self._activity_listeners:
            self._activity_listeners.append(callback)

    def remove_activity_listener(self, callback):
        try:
            self._activity_listeners.remove(callback)
        except ValueError:
            pass

    def _notify_activity(self):
        """Something the snapshot shows changed: the listeners run once per Tk idle pass (coalesced). Tk thread only."""
        if self._activity_pending or not self._activity_listeners:
            return
        self._activity_pending = True
        try:
            self.after_idle(self._fire_activity)
        except tk.TclError:
            self._activity_pending = False

    def _fire_activity(self):
        self._activity_pending = False
        for cb in list(self._activity_listeners):
            try:
                cb()
            except Exception:  # noqa: BLE001 - one listener never stops the others
                pass

    def activity_snapshot(self):
        """{"now": [...], "next": [...], "health": {...}} from state the app already holds - no HTTP, never raises.
        now: every running item oldest first (reply / thinking / regenerate per stream, an image job, the load card);
        next: every queued entry in dispatch order (each chat's head in the global queue, then its own backlog;
        then the backlogs of chats whose own reply still streams)."""
        now, nxt = [], []
        chat = self._screens.get("chat")
        try:
            if chat is not None:
                titles = {c.get("id"): (c.get("title") or "New chat")
                          for c in (getattr(chat, "_chat_list_conversations", None) or [])}

                def title_of(cid):
                    return titles.get(cid) or "New chat"

                for cid, st in list(getattr(chat, "_streams", {}).items()):
                    if getattr(st, "regenerating_message_id", None) is not None:
                        kind = "regenerate"
                    elif getattr(st, "thinking_started", False) and not getattr(st, "thinking_finished", False):
                        kind = "thinking"
                    else:
                        kind = "reply"
                    stage = (getattr(st, "status_text", "") or "").split("\n")[0].strip().rstrip(".")
                    now.append({"kind": kind, "conv_id": cid, "title": title_of(cid), "stage": stage,
                                "since": getattr(st, "started_at", None)})
                    for doc in getattr(st, "documents", None) or []:        # document tool P2: a document being created
                        if doc.get("state") == "creating":
                            now.append({"kind": "document", "conv_id": cid, "title": title_of(cid),
                                        "stage": "DOCUMENT \u00b7 CREATING", "since": doc.get("since")})
                ig = getattr(chat, "_image_gen", None)
                if ig:
                    label = IMAGE_GEN_STAGE_LABELS.get(ig.get("stage"), "IMAGE GEN: WORKING...")
                    now.append({"kind": "image", "conv_id": ig.get("conv_id"), "title": title_of(ig.get("conv_id")),
                                "stage": label.rstrip("."), "since": ig.get("started_at")})
                if self._nav_lock_depth > 0:
                    now.append({"kind": "load", "conv_id": None, "title": "MODEL", "stage": "LOADING", "since": self._load_since})
                queue_entries = list(getattr(chat, "_global_send_queue", []))
                order = []
                for e in queue_entries:
                    if e["conv_id"] not in order:
                        order.append(e["conv_id"])
                for cid in list(getattr(chat, "_streams", {})) + list(getattr(chat, "_queued_messages", {})):
                    if cid not in order:
                        order.append(cid)
                backlog = getattr(chat, "_backlog", None)
                for cid in order:
                    entries = [("global", e) for e in queue_entries if e["conv_id"] == cid]
                    entries += [("chat", e) for e in (backlog(cid) if backlog is not None else [])]
                    for i, (q, e) in enumerate(entries):
                        kind = "regenerate" if e.get("kind") == "regenerate" else "message"
                        flat = " ".join(str(e.get("text") or "").split())
                        preview = "" if kind == "regenerate" else (flat if len(flat) <= 60 else flat[:59].rstrip() + "\u2026")
                        nxt.append({"kind": kind, "conv_id": cid, "title": title_of(cid), "preview": preview, "queue": q, "index": i})
        except Exception:  # noqa: BLE001 - a snapshot is best effort, never an error
            pass
        return {"now": now, "next": nxt, "health": dict(self._health_last)}

    def open_chat(self, conv_id):
        """The pull-down's row click: the normal navigation path."""
        try:
            self._go_to_chat(conv_id)
        except Exception:  # noqa: BLE001
            pass

    def cancel_queued(self, conv_id, index):
        """Drops one queued entry (index as the snapshot numbers that chat's entries): a message's text goes back to
        that chat's composer when it is on screen, else into its draft mirror (shown when the chat is opened); a
        regenerate is simply dropped. -> True when something was removed."""
        chat = self._screens.get("chat")
        if chat is None:
            return False
        try:
            heads = [e for e in chat._global_send_queue if e["conv_id"] == conv_id]
            entries = [("global", e) for e in heads] + [("chat", e) for e in chat._backlog(conv_id)]
            if not 0 <= index < len(entries):
                return False
            q, entry = entries[index]
            if q == "global":
                chat._global_send_queue.remove(entry)
            else:
                chat._backlog(conv_id).remove(entry)
                if not chat._backlog(conv_id):
                    chat._queued_messages.pop(conv_id, None)
            if entry.get("kind") != "regenerate":
                text = entry.get("text") or ""
                if conv_id == chat.conv_id:
                    chat.input.delete("1.0", "end")
                    chat.input.insert("1.0", text)
                    chat._staged_images = entry.get("images") or []
                    chat._staged_docs = entry.get("docs") or {}
                    chat._render_staging_grid()
                else:
                    chat._drafts[conv_id] = text
                    chat._queue_draft(conv_id, text)
            chat._sync_queued_indicator()
            chat._sync_rail_for_current_conv()
            self._notify_activity()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _on_model_switched(self):
        # A model switch means any in-flight streaming reply from the old
        # model is gone - refresh the visible conversation so the transcript
        # reflects whatever actually made it into the database.
        chat = self._screens.get("chat")
        if chat is not None and getattr(chat, "conv_id", None) is not None:
            chat.load_conversation(chat.conv_id)

    def _on_conversation_deleted(self, conv_id):
        """B14-H7: a chat deleted from the Chats / Projects screens - its queued message (if any) is dropped."""
        chat = self._screens.get("chat")
        if chat is not None:
            chat.forget_conversation(conv_id)

    def _on_project_deleted(self, project_id):
        # ProjectsScreen may be holding this project as its "current detail
        # view" - clear that stale reference so navigating back to it shows
        # the grid instead of re-fetching a project that's now a 404.
        projects = self._screens.get("projects")
        if projects is not None:
            projects.forget_project(project_id)

    def _on_files_dropped(self, paths):
        # win_dnd's hook covers the whole window regardless of which screen
        # is showing - route to whichever one is actually visible right
        # now. Every other screen just inherits Screen.handle_file_drop()'s
        # no-op default.
        screen = self._screens.get(self._current_screen_name)
        if screen is not None:
            screen.handle_file_drop(paths)

    def _initial_screen(self):
        # Always paints Home first. "Open to the last chat" is applied as soon
        # as the conversation list arrives - fetched off the Tk thread, because
        # this runs at the moment the backend is busiest (a cold model load has
        # just begun) and a blocking call here would hold up the window's very
        # first paint for as long as the backend took to answer.
        if self.theme.launch_screen == "last_chat":
            run_async(self, self.api.list_conversations, self._open_last_chat_if_still_home)
        return "home"

    def _open_last_chat_if_still_home(self, conversations):
        if self._current_screen_name != "home" or self._navigation_locked or not conversations:
            return  # the user already went somewhere else, or there's nothing to open
        # list_conversations is sorted by updated_at DESC (see sidebar.py's
        # prior use of this same ordering assumption).
        self._go_to_chat(conversations[0]["id"])

    # ---- router ----

    def show_screen(self, name, record_history=True):
        if self._navigation_locked:
            # A model load is in progress (see model_load_overlay.py) - this
            # is the single chokepoint every navigation path funnels through
            # (rail clicks, Alt+1..4, Ctrl+N, digit quick-select that opens
            # a different router screen), so blocking here blocks all of them.
            self.fx.emit(self, "nav.denied", reason="locked")
            return
        if name not in self._screens or name == self._current_screen_name:
            return
        # Samples who/what caused this navigation now, before on_hide() can
        # block; the matching end() below announces it only once committed.
        token = self.fx.transitions.begin(self._current_screen_name, name, back=not record_history)
        current = None
        if self._current_screen_name is not None:
            current = self._screens[self._current_screen_name]
            current.on_hide()
            current.pack_forget()
            if record_history:
                self._history.append(self._current_screen_name)
        self._current_screen_name = name
        screen = self._screens[name]
        screen.pack(fill="both", expand=True)
        # B7B-H11: the keyboard never stays in the hidden screen (Tk delivers keys to its focus window, mapped or
        # not: digits typed on Home landed in Chat's composer and its draft); on_show may focus its own widget next
        if current is not None and self._focus_inside(current):
            screen.focus_set()
        screen.on_show()
        if self.activity is not None:
            self.activity.attach(getattr(screen, "header", None))    # batch 34: the control moves with the header
        self._update_rail_highlight(name)
        self.refresh_legend()
        self.fx.transitions.end(token, name)

    def _focus_inside(self, widget):
        """True while the keyboard focus is `widget` or a widget inside it (B7B-H11)."""
        try:
            f = self.focus_get()
        except (KeyError, tk.TclError):
            return False
        return f is not None and (f is widget or str(f).startswith(str(widget) + "."))

    def _update_rail_highlight(self, name):
        """The current screen's cell gets the bright plate; every other cell is idle (value ink, a
        1 px bright outline while hovered). Screens off the rail (chat, manage, uplink) light none."""
        for screen_name, cell in self._rail_buttons.items():
            active = screen_name == name
            if hasattr(cell, "set_active"):
                cell.set_active(active)
            else:  # plain-Label fallback (kit unavailable)
                cell.configure(bg=self.theme.bright if active else self.theme.panel,
                               fg=self.theme.panel if active else self.theme.value)

    def fx_active_rail_cell(self):
        """The rail cell of the screen showing now, or None (chat, manage, uplink are off the rail).
        For the B10 rail latch."""
        return self._rail_buttons.get(self._current_screen_name)

    def refresh_legend(self):
        """Repaint the bottom key legend from the current screen's legend_items(). The router calls it
        after every screen change; a screen whose keys change while it shows calls ctx.refresh_legend()."""
        legend = getattr(self, "legend", None)
        if legend is None or not hasattr(legend, "set_items"):
            return
        name = self._current_screen_name
        screen = self._screens.get(name)
        items = []
        if screen is not None:
            try:
                items = list(screen.legend_items() or [])
            except Exception as exc:  # noqa: BLE001 - a legend must never break navigation
                import sys
                sys.stderr.write("[legend] %s.legend_items() failed: %r\n" % (type(screen).__name__, exc))
        chips = name in LEGEND_CHIP_SCREENS
        try:
            # B2 fix ADV1-05: while a model load locks navigation the router refuses these keys,
            # so the legend dims with the rail (the words stay readable; nothing is removed)
            legend.set_items(items, chips=chips, rule=chips, dim=self._navigation_locked)
        except tk.TclError:
            pass

    def _on_back(self):
        if self.activity is not None and self.activity.is_open:
            self.activity.close()               # batch 34: Esc closes the activity panel first
            return "break"
        if self._navigation_locked:
            self.fx.emit(self, "nav.denied", reason="locked")
            return
        current = self._screens.get(self._current_screen_name)
        if current is not None and current.handle_back():
            return  # screen-local back (e.g. Projects detail -> grid)
        if not self._history:
            return
        previous = self._history.pop()
        self.show_screen(previous, record_history=False)

    def _new_session(self, project_id=None, folder_id=None):
        """New session (Home, Ctrl+N, the Chat header; the Chats / Projects New chat with their folder / project):
        the Chat screen with a DRAFT new chat - no conversations row until its first message (the user's report:
        an empty row per New session and per deleted chat, so the list could never be emptied). No database write
        here, so nothing to wait for and nothing that can fail."""
        if self._navigation_locked:
            return                      # a model load is running under its card (show_screen would refuse too)
        chat = self._screens["chat"]
        chat.skip_next_show_reload()
        try:
            self.show_screen("chat")
        finally:
            chat.clear_skip_show_reload()
        chat.start_draft(project_id=project_id, folder_id=folder_id)

    def _go_to_chat(self, conv_id):
        if self._navigation_locked:
            # a model load is in progress: show_screen would refuse below, but load_conversation still ran into
            # the hidden chat view, so the shown screen and the chat's state disagreed afterwards (BF-15)
            self.fx.emit(self, "nav.denied", reason="locked")
            return
        chat = self._screens["chat"]
        # Showing the chat screen makes it reload whichever conversation it had
        # open before; that's wasted work (and a blocking round trip) when the
        # one we're about to load is a different conversation anyway.
        chat.skip_next_show_reload()
        try:
            self.show_screen("chat")
        finally:
            chat.clear_skip_show_reload()  # show_screen() returns early if chat is already showing
        chat.load_conversation(conv_id)

    def _go_to_project_manage(self, project_id):
        if self._navigation_locked:
            self.fx.emit(self, "nav.denied", reason="locked")      # the same defect as _go_to_chat (BF-15)
            return
        self.show_screen("project_manage")
        self._screens["project_manage"].load_project(project_id)

    # ---- keyboard system ----

    def _wire_global_keymap(self):
        def switch(i):
            if 1 <= i <= len(RAIL_SCREENS):
                self.show_screen(RAIL_SCREENS[i - 1])

        def focus_search():
            screen = self._screens.get(self._current_screen_name)
            if screen is not None:
                screen.focus_search()

        self.keymap.bind_global(
            on_switch_screen=switch,
            on_new_session=self._new_session,
            on_back=self._on_back,
            on_focus_search=focus_search,
        )
        # batch 34: Alt+Q toggles the activity pull-down (a chord: bind_guarded passes chords, so bound here)
        self.bind_all("<Alt-Key-q>", lambda e: self.activity.on_alt_q() if self.activity is not None else None)
        self.bind_all("<Alt-Key-Q>", lambda e: self.activity.on_alt_q() if self.activity is not None else None)

    # ---- navigation lock (model loading - see model_load_overlay.py) ----

    def _lock_navigation(self):
        # B9-H3: a count, not a flag - two model-load cards at once (a save that restarts the server during a
        # switch) keep the lock until the LAST one finishes
        self._nav_lock_depth += 1
        if self._nav_lock_depth > 1:
            return
        self._navigation_locked = True
        self._load_since = time.time()
        self._notify_activity()                 # batch 34: the load card is a NOW item
        # Locked: every cell dims; the current screen's cell keeps its box, in ghost (r2_rail_states).
        for cell in list(self._rail_buttons.values()) + [self.power_btn]:
            if hasattr(cell, "set_locked"):
                cell.set_locked(True)
            else:  # plain-Label fallback
                cell.configure(bg=self.theme.panel, fg=self.theme.dim, cursor="")
        self.refresh_legend()
        self.fx.emit(self, "load.begin")

    def _unlock_navigation(self):
        self._nav_lock_depth = max(0, self._nav_lock_depth - 1)
        if self._nav_lock_depth:
            return                    # B9-H3: another card is still up
        self._navigation_locked = False
        self._load_since = None
        self._notify_activity()
        for cell in list(self._rail_buttons.values()) + [self.power_btn]:
            if hasattr(cell, "set_locked"):
                cell.set_locked(False)
            else:  # plain-Label fallback
                cell.configure(cursor="hand2")
        if not hasattr(self.power_btn, "set_locked"):
            self.power_btn.configure(fg=self.theme.danger)
        self._update_rail_highlight(self._current_screen_name)
        self.refresh_legend()
        self.fx.emit(self, "load.end")

    # ---- close / shutdown semantics (unchanged from the prior shell) ----

    def _flush_chat_draft(self):
        """Persists the composer's unsent text before the window goes away or
        the backend stops. Best-effort: the chat screen may not exist yet, and
        a failure here must never block closing."""
        try:
            chat = self._screens.get("chat")
            if chat is not None:
                chat.flush_draft()
        except Exception:
            pass

    def _on_close(self):
        # Closing the window does not stop the backend/model - it keeps
        # running so a relaunch reattaches instantly. But while a model load
        # is locked, disappearing entirely could read as "it crashed" and
        # invite a Task Manager force-kill mid-restart - exactly what the
        # lock exists to prevent - so keep the window (and its loading
        # overlay) visible instead of withdrawing.
        if self._navigation_locked:
            self.lift()
            self.focus_force()
            self.fx.emit(self, "nav.denied", reason="close_locked", insist=True)
            if not self.fx.sound.enabled_for("denied") and not _legacy_bell_silenced():
                self.bell()  # the system bell only when the fx refusal cue won't play instead (never when silenced)
            return
        # The backend stays up here, so the debounce would eventually land -
        # but the user may shut down from the tray/another launch afterwards,
        # and a withdrawn window's after() timers are easy to lose.
        self._flush_chat_draft()
        self.withdraw()

    def _on_shutdown_clicked(self):
        if self._navigation_locked:
            show_toast(
                self, self.theme, "Can't shut down while a model is loading - wait for it to finish.",
                kind="warning",
            )
            return
        show_inline_confirm(
            self, self.theme, "Fully shut down Personal Terminal? The model will be unloaded.",
            on_confirm=self._do_shutdown, confirm_label="Shut down", danger=True, title="SHUT DOWN",
        )

    def _do_shutdown(self):
        # Must complete BEFORE /api/shutdown is issued - the draft is written
        # through that same backend, so a save racing the shutdown would be
        # cut off mid-request and the text lost. flush_draft blocks (briefly,
        # bounded) for exactly this reason.
        self._flush_chat_draft()
        threading.Thread(target=self._shutdown_backend, daemon=True).start()
        self.fx.emit(self, "app.shutdown", grace_ms=800)
        self.after(800, self.destroy)

    def _shutdown_backend(self):
        try:
            self.api.shutdown()
        except Exception:
            pass

    # ---- re-show when a second launch attempt pings us ----

    def _poll_gui_events(self):
        try:
            while True:
                self.server.gui_events.get_nowait()
                self.deiconify()
                self.lift()
                self.focus_force()
        except queue.Empty:
            pass
        except tk.TclError:
            return  # window destroyed - stop rescheduling
        self._gui_events_after_id = self.after(300, self._poll_gui_events)

    def destroy(self):
        # Cancel the self-rescheduling poll loop so a destroyed window can't
        # leave a stale after-callback behind (harmless in the real app,
        # where destroy means process exit, but it breaks any multi-window
        # host - e.g. the verification harness - with "invalid command
        # name" Tcl background errors).
        if getattr(self, "_gui_events_after_id", None) is not None:
            try:
                self.after_cancel(self._gui_events_after_id)
            except tk.TclError:
                pass
        # Same hazard for every other timer in the tree - the screens' own
        # worker/queue polls are pending or not purely by teardown timing, so
        # they're swept generically here rather than id-tracked one call site
        # at a time. Must run before super(), which is what deletes the Tcl
        # commands the pending events still point at.
        # The fx layer goes first: it runs its last one-shots, persists any
        # pending preference and stops its clock, so nothing it owns can re-arm
        # after the sweep below.
        fx_mod.teardown(self)
        cancel_pending_after_callbacks(self)
        win_dnd.uninstall(self)
        super().destroy()


