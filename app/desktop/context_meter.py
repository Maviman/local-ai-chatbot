"""Live context-window usage meter + Compact action, on the Chat status row.

B7b (approved r2_chat / r2_chat_meter, shared_glyphs.context_meter): the CTX key, 20 flat segments 8 x 12
(2 px apart; unlit = solid `unlit`), the 75 % / 90 % threshold ticks (1 px, 3 px past the segments), the
caption `{used:,} / {max:,} tokens ({pct}%)` (+ the compacted note) and COMPACT, a kit text action. Lit
segments are `ok` below 75 %, `warn_eff` from 75 %, `danger` from 90 %, where the CTX key also becomes a
danger-filled 30 x 16 box. The colours come from the live theme (theme= or the toplevel's .theme), never from
an import-time load_theme(). The group is right-aligned in the meter's own width; fit_to(width) wraps the
caption to a width (the status row asks for it on a narrow window: BF-10, never cut)."""

import queue
import threading
import tkinter as tk

from .api_client import ApiError
from . import theme as theme_mod
from .screens.base import show_toast

try:  # B7b: the kit look (guarded: a broken kit still leaves a working meter)
    from .kit import C9, C9B, geom_ratio as _geom_ratio, tokens as _kit_tokens
    from .kit.painters import segment_rects as _segment_rects, segments_filled as _segments_filled, tick_x as _tick_x
    from .kit.widgets import TA_PADX as _TA_PADX, TextAction as _TextAction
except Exception:  # noqa: BLE001
    C9, C9B = ("Consolas", 9), ("Consolas", 9, "bold")
    _kit_tokens = _TextAction = None
    _TA_PADX = 6

    def _geom_ratio(_w):
        return 1.0

    def _segments_filled(pct, n):
        return max(0, min(int(n), int(max(0.0, min(100.0, float(pct))) * n // 100)))

    def _segment_rects(x0, y0, n, seg_w, seg_h, gap):
        return [(x0 + i * (seg_w + gap), y0, x0 + i * (seg_w + gap) + seg_w, y0 + seg_h) for i in range(int(n))]

    def _tick_x(pct, x0, width):
        return int(x0 + width * pct // 100)

try:  # B14 H20: context.level goes through the fx seam (NullFX, i.e. nothing at all, when fx is off or absent)
    from . import fx as _fx
except Exception:  # noqa: BLE001
    _fx = None

WARN_THRESHOLD = 75
DANGER_THRESHOLD = 90

# approved pixels at 100 % (scaled by the display ratio)
SEGMENTS = 20
SEGMENT_W = 8
SEGMENT_GAP = 2
SEGMENT_H = 12
TICK_OVER = 3            # the threshold ticks stand 3 px past the segments, top and bottom
KEY_W, KEY_H = 30, 16    # the CTX key's cell; at >= DANGER_THRESHOLD a danger-filled box of that size
KEY_GAP = 4              # the key cell -> the segments (the word itself ends about 8 px before them)
CAPTION_GAP = 12         # the segments -> the caption
COMPACT_GAP = 16         # the caption -> the COMPACT word
MIN_CAPTION_CHARS = 10   # a wrapped caption keeps at least this many cells per line
COMPACTED_NOTE = " (earlier turns already compacted)"


def _meter_tokens(theme):
    """The kit tokens, or the theme's own colours when the kit is unavailable."""
    if _kit_tokens is not None:
        return _kit_tokens(theme)
    g = lambda k, d: getattr(theme, k, None) or d  # noqa: E731
    bright = g("bright", "#FFB000")
    return {"bg": g("bg", "#0B0A08"), "panel": g("panel", "#151209"), "bright": bright, "dim": g("dim", "#8A5A00"),
            "value": bright, "unlit": g("panel", "#151209"), "ok": theme_mod.OK, "warn_eff": theme_mod.WARN,
            "danger": theme_mod.DANGER}


class ContextMeter(tk.Frame):
    def __init__(self, parent, api_client, on_compacted=None, theme=None):
        if theme is None:
            theme = getattr(parent.winfo_toplevel(), "theme", None)
        if theme is None:
            theme = theme_mod.Theme()          # the default look; never a disk read here
        self._theme = theme
        t = self._t = _meter_tokens(theme)
        super().__init__(parent, bg=t["bg"])
        self.api = api_client
        self.on_compacted = on_compacted
        self.conv_id = None
        self._refresh_queue = queue.Queue()
        self._compact_queue = queue.Queue()
        self._compacting = False          # B7B-H6: a compaction is running: no reading re-enables COMPACT meanwhile
        self.ratio = _geom_ratio(self)
        s = self._s
        # the status row's re-layout hook (kit.composer.StatusRow sets it): called after each new reading
        self.on_layout = None
        self._fit_width = None
        self._wrap = 0
        self._danger_key = None

        # Packed right to left, so the group hugs the meter's right edge: COMPACT, the caption, the segments,
        # the CTX key. The caption is the only part that ever gives way: it wraps (fit_to), never cut.
        if _TextAction is not None:
            self.compact_btn = _TextAction(self, theme, text="Compact", upper=True, command=self._on_compact_clicked,
                                           state="disabled", ground=t["bg"])
        else:
            self.compact_btn = tk.Button(
                self, text="Compact", command=self._on_compact_clicked, relief="flat", padx=8, state="disabled",
                bg=t["panel"], fg=t["bright"], activebackground=t["bright"], activeforeground=t["bg"],
                disabledforeground=t["dim"],
            )
        self.compact_btn.pack(side="right")
        self._compact_rest_text = str(tk.Misc.cget(self.compact_btn, "text"))      # COMPACT as drawn at rest
        self.label = tk.Label(self, text="", bg=t["bg"], fg=t["value"], font=C9, bd=0, padx=0, pady=0,
                              justify="left", anchor="w")
        self._label_pad = (s(CAPTION_GAP), max(0, s(COMPACT_GAP) - s(_TA_PADX)))
        self.label.pack(side="right", padx=self._label_pad)
        # Segmented instrument-panel bar (tick marks, not a smooth fill) - a tk.Canvas rather than
        # ttk.Progressbar since ttk has no segmented-fill layout.
        self._bar_w = SEGMENTS * (s(SEGMENT_W) + s(SEGMENT_GAP)) - s(SEGMENT_GAP)
        self.bar = tk.Canvas(
            self, width=self._bar_w, height=s(SEGMENT_H) + 2 * s(TICK_OVER), bg=t["bg"], highlightthickness=0, bd=0,
        )
        self.bar.pack(side="right")
        self.key = tk.Canvas(self, width=s(KEY_W), height=s(KEY_H), bg=t["bg"], highlightthickness=0, bd=0)
        self.key.pack(side="right", padx=(0, s(KEY_GAP)))
        self._filled_segments = 0
        self._fill_color = t["ok"]
        self._fx_band = None              # B14 H20: (conv_id, band) of the last context.level emitted
        self._draw_key(False)
        self._draw_segments(0, t["ok"])

        self._visible = False
        self.pack_forget()

    def _s(self, n):
        return int(round(n * self.ratio))

    # ---- the status row's layout hooks (B7b; kit.composer.StatusRow) ----

    def _caption_px(self):
        try:
            return int(self.tk.call("font", "measure", self.label.cget("font"), str(self.label.cget("text"))))
        except tk.TclError:
            return 0

    def natural_width(self):
        """The meter's width with the caption on one line (its own pack padding not included). COMPACT counts at its
        resting width, so the status row never re-flows while it reads COMPACTING... for the few seconds of a
        compaction (the status line, the row's flexible part, gives up those pixels meanwhile)."""
        return (self.key.winfo_reqwidth() + self._s(KEY_GAP) + self.bar.winfo_reqwidth() + sum(self._label_pad)
                + self._caption_px() + self._compact_rest_width())

    def _compact_rest_width(self):
        b = self.compact_btn
        try:
            font = b.cget("font")
            shown = str(tk.Misc.cget(b, "text"))          # what Tk draws (past a kit cget override)
            extra = (int(self.tk.call("font", "measure", font, shown))
                     - int(self.tk.call("font", "measure", font, self._compact_rest_text)))
            return int(b.winfo_reqwidth()) - max(0, extra)
        except (tk.TclError, ValueError, AttributeError):
            return int(b.winfo_reqwidth())

    def fit_to(self, width):
        """Fit the meter into `width` px (None = no limit): the caption wraps when it would not fit on one
        line beside the key, the segments and COMPACT, and unwraps when the room comes back."""
        self._fit_width = width
        self._fit_caption()

    def _fit_caption(self):
        width = self._fit_width
        wl = 0
        if width is not None:
            cap = self._caption_px()
            avail = int(width) - (self.natural_width() - cap)
            if cap > avail:
                try:
                    cell = int(self.tk.call("font", "measure", self.label.cget("font"), "0"))
                except tk.TclError:
                    cell = 7
                wl = max(MIN_CAPTION_CHARS * cell, avail)
        if wl != self._wrap:
            self._wrap = wl
            try:
                self.label.configure(wraplength=wl)
            except tk.TclError:
                pass

    def _draw_key(self, danger):
        """CTX in value; at >= DANGER_THRESHOLD a danger-filled box with CTX in the ground's ink."""
        if danger == self._danger_key:
            return
        self._danger_key = danger
        t, k = self._t, self.key
        k.delete("all")
        w, h = self._s(KEY_W), self._s(KEY_H)
        if danger:
            k.create_rectangle(0, 0, w, h, fill=t["danger"], outline="", width=0, tags="box")
        k.create_text(w // 2, h // 2, text="CTX", fill=t["bg"] if danger else t["value"], font=C9B, tags="key")

    def show(self):
        if not self._visible:
            self.pack(fill="x", padx=10, pady=(0, 4))
            self._visible = True

    def hide(self):
        if self._visible:
            self.pack_forget()
            self._visible = False
            # B7b: the status row takes the meter's room back (no <Unmap> reaches it while Chat is hidden)
            if self.on_layout is not None:
                self.on_layout()

    # ---- refresh (the 3 trigger points: load conversation, new
    # conversation, after every streamed reply finishes) ----

    def refresh(self, conv_id):
        self.conv_id = conv_id
        self._refresh_retry_count = 0
        self._start_refresh_attempt(conv_id)

    def _start_refresh_attempt(self, conv_id):
        threading.Thread(target=self._refresh_worker, args=(conv_id, self._refresh_queue), daemon=True).start()
        self.after(50, self._poll_refresh_queue)

    def _refresh_worker(self, conv_id, q):
        try:
            usage = self.api.get_context_usage(conv_id)
            q.put(("ok", conv_id, usage))
        except ApiError as e:
            q.put(("error", conv_id, str(e)))

    def _poll_refresh_queue(self):
        try:
            kind, for_conv_id, payload = self._refresh_queue.get_nowait()
        except queue.Empty:
            self.after(50, self._poll_refresh_queue)
            return
        if for_conv_id != self.conv_id:
            return  # stale - conversation switched again before this landed
        if kind == "error":
            # Most commonly hit for the very first refresh right after
            # startup, before llama-server has finished coming up (a 503
            # "model server not reachable") - since none of this widget's 3
            # trigger points (load conversation, new conversation, after a
            # streamed reply) include "health just became ready", without a
            # retry the meter would otherwise stay hidden for the entire
            # session unless the user happens to send a message. Retry with
            # backoff a few times (the model becoming ready shortly after
            # launch is the common case, not the exception) before
            # accepting this as a real, lasting failure.
            if self._refresh_retry_count < 5:
                self._refresh_retry_count += 1
                self.after(2000, lambda: self._start_refresh_attempt(for_conv_id))
            else:
                self.hide()
            return
        self._render(payload)

    def _draw_segments(self, pct, color):
        """20 flat segments 8 x 12 (fill only, exactly that many pixels; tag "seg"), the first
        floor(pct / 5) lit in `color`, the rest solid `unlit`; then the 75 % / 90 % ticks (tag "tick")."""
        t, s = self._t, self._s
        self.bar.delete("seg")
        self.bar.delete("tick")
        seg_w, seg_h, gap, over = s(SEGMENT_W), s(SEGMENT_H), s(SEGMENT_GAP), s(TICK_OVER)
        filled = _segments_filled(pct, SEGMENTS)
        for i, (x0, y0, x1, y1) in enumerate(_segment_rects(0, over, SEGMENTS, seg_w, seg_h, gap)):
            self.bar.create_rectangle(x0, y0, x1, y1, fill=color if i < filled else t["unlit"], outline="", width=0,
                                      tags="seg")
        for thr, ink in ((WARN_THRESHOLD, t["warn_eff"]), (DANGER_THRESHOLD, t["danger"])):
            x = _tick_x(thr, 0, self._bar_w)
            self.bar.create_rectangle(x, 0, x + max(1, s(1)), seg_h + 2 * over, fill=ink, outline="", width=0,
                                      tags="tick")
        self._filled_segments = filled
        self._fill_color = color

    def _render(self, usage):
        used, mx = usage.get("used", 0), usage.get("max") or 1
        pct = min(100, round(used / mx * 100))
        # B14 H20: context.level once per threshold crossing (75 / 90 %, or another conversation's first reading),
        # never on every reading - the meter re-renders the same band many times; the bridge's caution is opt-in
        band = (self.conv_id, 2 if pct >= DANGER_THRESHOLD else (1 if pct >= WARN_THRESHOLD else 0))
        if band != self._fx_band and _fx is not None:
            self._fx_band = band
            _fx.get(self).emit(self, "context.level", pct=pct, conv_id=self.conv_id)
        t = self._t
        if pct >= DANGER_THRESHOLD:
            color = t["danger"]
        elif pct >= WARN_THRESHOLD:
            color = t["warn_eff"]
        else:
            color = t["ok"]
        self._draw_segments(pct, color)
        self._draw_key(pct >= DANGER_THRESHOLD)
        note = COMPACTED_NOTE if usage.get("compacted") else ""
        self.label.config(text=f"{used:,} / {mx:,} tokens ({pct}%){note}")
        compactable = usage.get("compactable_messages", 0)
        self.compact_btn.config(state="normal" if (compactable > 6 and not self._compacting) else "disabled")
        self.show()
        self._fit_caption()
        if self.on_layout is not None:      # the status row: a new caption can change the row's layout
            self.on_layout()

    # ---- compact ----

    def _on_compact_clicked(self):
        if self.conv_id is None:
            return
        if self._compacting:
            return                        # B7B-H6: one compaction at a time
        conv_id = self.conv_id
        self._compacting = True
        self.compact_btn.config(state="disabled", text="Compacting...")
        threading.Thread(target=self._compact_worker, args=(conv_id, self._compact_queue), daemon=True).start()
        self.after(50, self._poll_compact_queue)

    def _compact_worker(self, conv_id, q):
        try:
            result = self.api.compact_conversation(conv_id)
            q.put(("ok", conv_id, result))
        except ApiError as e:
            q.put(("error", conv_id, str(e)))

    def _poll_compact_queue(self):
        try:
            kind, for_conv_id, payload = self._compact_queue.get_nowait()
        except queue.Empty:
            self.after(50, self._poll_compact_queue)
            return
        self._compacting = False          # B7B-H6: over (ok or error); the next reading may enable COMPACT again
        self.compact_btn.config(text="Compact")
        if kind == "error":
            show_toast(self, self._theme, f"Could not compact conversation: {payload}", kind="error")
            if for_conv_id == self.conv_id:
                self.refresh(for_conv_id)
            return
        if self.on_compacted:
            self.on_compacted(for_conv_id)
        if for_conv_id == self.conv_id:
            self.refresh(for_conv_id)
