"""Shared adaptive-thumbnail-grid mechanics for attachment UI (staged chat
images/docs, and project images), factored out of chat_view.py's original
staging-grid implementation so it exists in exactly one place. Worth
keeping as one implementation, not a hand-copied second one: the
hover-overlay-cleanup bug shape has already recurred four times in this
codebase (see memory local-ai-chatbot-attachment-preview-grid.md) - one
tested implementation directly reduces the chance of a fifth.

This module owns only the generic mechanics - tiering math, tile shell
styling, thumbnail decode/cache, and hover-overlay placement/lifecycle.
Callers own their own tile *content* (what a tile shows) and hover
*content* (what the enlarged preview shows), passed in as callbacks, so
this module never needs to know about "image" vs "doc" vs any other kind
of attachment.
"""

import tkinter as tk

from . import theme as theme_mod

# tk.PhotoImage only supports *integer* subsample()/zoom() factors - no
# arbitrary continuous resize - so "compression" is inherently a handful of
# discrete steps, not smooth scaling. The grid picks the largest tier whose
# tiles still fit the container's current width for the current item count;
# beyond the smallest ("cube") tier, tiles show only a caption, no
# image/text content at all.
STAGE_TIERS = [("large", 128), ("medium", 84), ("small", 56), ("cube", 36)]
STAGE_GAP = 6
STAGE_ROW_HEIGHT = STAGE_TIERS[0][1] + 16  # fixed regardless of active tier, so the
                                            # host layout never jumps vertically as tiles resize


def pick_tier(n_items, available_width):
    for name, size in STAGE_TIERS:
        row_width = n_items * size + max(0, n_items - 1) * STAGE_GAP
        if row_width <= available_width:
            return name, size
    return STAGE_TIERS[-1]


def make_tile_shell(container, theme, size):
    tile = tk.Frame(
        container, width=size, height=size, bg=theme.panel,
        highlightthickness=1, highlightbackground=theme.dim,
    )
    tile.pack_propagate(False)
    return tile


def add_remove_button(tile, theme, on_click):
    remove_btn = tk.Label(
        tile, text="×", bg=theme.panel, fg=theme.dim, font=theme_mod.FONT_CAPTION, cursor="hand2",
    )
    remove_btn.place(relx=1.0, rely=0.0, anchor="ne")
    remove_btn.bind("<Button-1>", lambda e: on_click())


class HoverPreview:
    """Owns a single enlarge-on-hover overlay - only one can be showing at
    a time. bind() attaches Enter/Leave handlers (recursively, so hovering
    any child of a tile counts) that call build_content(overlay_frame) to
    populate the popup; the caller decides what goes inside, this class
    only handles positioning/lifecycle."""

    def __init__(self):
        self._overlay = None

    def bind(self, widget, theme, build_content):
        widget.bind("<Enter>", lambda e: self.show(widget, theme, build_content))
        widget.bind("<Leave>", lambda e: self.hide())
        for child in widget.winfo_children():
            self.bind(child, theme, build_content)

    def show(self, anchor_widget, theme, build_content):
        # A tile can be destroyed (removed, or rebuilt when a sibling
        # changes) while its hover overlay is still showing - nothing else
        # ever tears it down in that case, so it's left dangling on screen
        # indefinitely. Always clear any existing overlay before showing a
        # new one, and callers must also call hide() before destroying/
        # rebuilding a grid's children (not just on the next hover).
        self.hide()
        root = anchor_widget.winfo_toplevel()
        overlay = tk.Frame(root, bg=theme.panel, highlightthickness=2, highlightbackground=theme.bright)
        build_content(overlay)
        overlay.update_idletasks()
        ow, oh = overlay.winfo_reqwidth(), overlay.winfo_reqheight()
        ax = anchor_widget.winfo_rootx() - root.winfo_rootx()
        ay = anchor_widget.winfo_rooty() - root.winfo_rooty()
        ah = anchor_widget.winfo_height()
        y = ay - oh - 8
        if y < 0:
            y = ay + ah + 8
        x = min(max(0, ax), max(0, root.winfo_width() - ow))
        overlay.place(x=x, y=y)
        overlay.lift()
        self._overlay = overlay

    def hide(self):
        if self._overlay is not None:
            try:
                self._overlay.destroy()
            except tk.TclError:
                pass
            self._overlay = None


class PhotoCache:
    """tk.PhotoImage decode/subsample cache keyed by a caller-supplied
    hashable id - Python id() for staged pre-send dicts that can't hold a
    Tcl object themselves (those dicts are sent verbatim through
    json.dumps() elsewhere - a PhotoImage attached there would crash the
    next send), or a real DB row id for persisted images. Expects
    base64-encoded image data (the same form staged images and
    server-fetched bytes both get encoded to before calling get())."""

    def __init__(self):
        self._cache = {}

    def get(self, key, base64_data, target_px):
        cache = self._cache.setdefault(key, {})
        if "full" not in cache:
            try:
                cache["full"] = tk.PhotoImage(data=base64_data)
            except tk.TclError:
                cache["full"] = None  # e.g. BMP/JPEG - tk.PhotoImage can't decode these, no Pillow in this project
        full = cache["full"]
        if full is None:
            return None
        if target_px not in cache:
            factor = max(1, -(-max(full.width(), full.height()) // target_px))  # ceil division
            cache[target_px] = full.subsample(factor, factor) if factor > 1 else full
        return cache[target_px]

    def evict(self, key):
        self._cache.pop(key, None)

    def clear(self):
        self._cache.clear()
