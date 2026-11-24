"""Chat screen: chat-list sidebar + transcript + context meter + composer +
action rail, wired to streaming chat via ApiClient.

Originally had no session list on this screen at all - switching always
went through the separate Chats screen - but that cost too many clicks for
everyday switching/creating, so a persistent sidebar (quick-switch, +New
chat, right-click rename/delete) was added directly here. The Chats screen
itself is untouched and still reachable from the icon rail for search/
folder management. The streaming worker/queue/poll logic is unrelated to
that change; this pass restyled it and conformed it to the Screen base
class so the router can host it uniformly.
"""

import base64
import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog

from . import attachment_grid as ag
from . import theme as theme_mod
from .api_client import ApiError
from .async_call import Latest, run_async
from .boot_fx import SignalAcquiring
from .context_menu import SharedContextMenu
from .context_meter import ContextMeter
from .markdown_text import MarkdownText
from .screens.base import (
    Screen, clear_prompt_slot, make_header, make_prompt_slot, make_scrollable, show_inline_confirm,
    show_inline_prompt, show_toast,
)

_KIT_IMPORT_ERROR = None     # B7a ADV1-03: the real import failure, reported once by ChatView.__init__
try:  # B7a: the chat chrome is the kit's (guarded: a broken kit still leaves a working screen)
    from .kit import C9, C9B, C10, C10B, C11B, geom_ratio as _geom_ratio, tokens as _kit_tokens
    from .kit.chat import ChatEmptyState, ChatHeader, mark_images as _mark_images
    from .kit.widgets import KitButton as _KitButton, StatusLine as _StatusLine, TextAction as _TextAction, Well as _Well
    # B7b: the status row, the composer's STOP and placeholder, the editing / queued line
    from .kit.composer import (
        AboveLine as _AboveLine, ActiveOnlyButton as _ActiveOnlyButton, ComposerPlaceholder as _ComposerPlaceholder,
        ChatStatusLine as _ChatStatusLine, StatusRow as _StatusRow,
    )
except Exception as _kit_exc:  # noqa: BLE001
    _KIT_IMPORT_ERROR = _kit_exc
    C9, C9B, C10 = ("Consolas", 9), ("Consolas", 9, "bold"), ("Consolas", 10)
    C10B, C11B = ("Consolas", 10, "bold"), ("Consolas", 11, "bold")
    ChatEmptyState = ChatHeader = _mark_images = _StatusLine = _TextAction = _kit_tokens = None
    _KitButton = _Well = _AboveLine = _ActiveOnlyButton = _ComposerPlaceholder = _StatusRow = _ChatStatusLine = None

    def _geom_ratio(_w):
        return 1.0

_KIT_FAILURES = set()


def _report_kit_failure(what, exc):
    """Once per kind, on stderr: the screen still opens with plain widgets."""
    if what in _KIT_FAILURES:
        return
    _KIT_FAILURES.add(what)
    try:
        import sys
        sys.stderr.write("[kit] chat %s failed; plain widgets stand in: %r\n" % (what, exc))
    except Exception:  # noqa: BLE001
        pass


try:  # document tool P2/P3: the creating row, the card, the viewer, the file side (guarded like the kit: a failure
    # leaves the chat working without them)
    from .document_card import CreatingRow as _CreatingRow, DocumentCard as _DocumentCard, FailedLine as _FailedLine
    from .document_card import failed_text as _failed_text
    from .document_viewer import DocumentViewer as _DocumentViewer, open_document_window as _open_document_window
    from . import file_ops as _file_ops
except Exception:  # noqa: BLE001
    _CreatingRow = _DocumentCard = _FailedLine = _failed_text = _DocumentViewer = _open_document_window = _file_ops = None
try:  # document tool P4 / P5: the image viewer and the context menus (guarded the same way)
    from .image_viewer import ImageViewer as _ImageViewer, open_image_window as _open_image_window
    from .context_menu import SharedContextMenu as _SharedContextMenu
except Exception:  # noqa: BLE001
    _ImageViewer = _open_image_window = _SharedContextMenu = None
if _file_ops is not None:
    try:
        _file_ops.clean_temp_copies()           # last run's "View image" / "Open in..." copies go at start-up
    except Exception:  # noqa: BLE001
        pass

try:  # B14: the sound hooks go through the fx seam (NullFX, i.e. nothing at all, when fx is off, absent or failed)
    from . import fx as _fx
except Exception:  # noqa: BLE001
    _fx = None


def _fx_emit(widget, event, **data):
    """B14: one seam event from a Chat site (AESTHETIC_SPEC 4.3 / BUILD_PLAN B14). Every caller is a Tk callback, so
    this runs on the Tk thread; never raises; a no-op when the fx package could not be imported."""
    if _fx is not None:
        _fx.get(widget).emit(widget, event, **data)

# Same allowlist as app.js's PROJECT_FILE_EXTENSIONS - matches what
# server.py's extract_upload_text() can actually turn into text (pdf/docx
# special-cased, everything else attempted as plain utf-8).
DOCUMENT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".json", ".py", ".js", ".ts", ".html", ".css",
    ".yaml", ".yml", ".log", ".xml", ".ini", ".cfg", ".pdf", ".docx",
}
# Extensions extract_upload_text() special-cases as binary-container formats
# rather than plain text - never worth a content-snippet preview attempt
# (server.py stores the extracted text either way; this is purely a "don't
# show a snippet" presentation choice, per an explicit user instruction).
_NO_SNIPPET_EXTENSIONS = {".pdf", ".docx"}
# Deliberately narrower than "anything a browser calls an image": llama.cpp's
# vision path decodes via stb_image, which has no webp/tiff/svg support - an
# allowed-but-undecodable file would fail opaquely deep in the model server
# instead of with a clear message here.
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp"}
# Recognizable image formats that stb_image (llama.cpp's vision decoder)
# cannot decode - kept separate from the generic "unsupported type" skip path
# so the toast can name the format and explain why, instead of leaving the
# user to guess whether it was even recognized as an image.
UNSUPPORTED_IMAGE_EXTENSIONS = {".heic", ".heif", ".webp", ".tif", ".tiff"}
_IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".bmp": "image/bmp",
}
# Mirrors server.py's MAX_IMAGE_BYTES - handle_chat() silently drops any
# image over this instead of erroring, so the only way to warn the user is
# to catch it here before the upload ever happens.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# No exact vision-token count exists anywhere (llama-server's /tokenize is
# text-only) - this is a labeled estimate only, always shown with "~"/
# "(est.)", never as a bare/exact-looking number. Gemma 3's original
# implementation uses a fixed ~256 tokens/image for its vision encoder;
# nothing confirms llama.cpp's GGUF/mtmd path matches exactly for this
# quantized model+mmproj.
ESTIMATED_IMAGE_TOKENS = 256

# Inline transcript image display size (tk.PhotoImage.subsample() target,
# via PhotoCache - see load_conversation()/embed_image()). Deliberately
# smaller than the 400px used for the staged-attachment hover-enlarge
# preview elsewhere in this file - this sits permanently in the scrolling
# transcript rather than a transient popup, so it shouldn't dominate it.
TRANSCRIPT_IMAGE_PX = 320

SNIPPET_MAX_LINES = 40
SNIPPET_MAX_CHARS = 2000

# Composer auto-grow bounds (text lines) - large enough to read a full
# paragraph without scrolling, capped well short of "can cover the screen".
# Once content needs more than COMPOSER_MAX_LINES displaylines, the Text
# widget's own built-in scrolling takes over (no extra code needed - a
# plain tk.Text already scrolls via mouse wheel/arrow keys once content
# overflows its visible height).
COMPOSER_MIN_LINES = 3
COMPOSER_MAX_LINES = 10
STAGE_MAX_ROWS = 3           # B7B-H2: staged tiles wrap onto up to this many rows; past that a "+N more" tile
# Debounce before an unsent composer draft is written to the DB. Long enough
# that ordinary typing collapses into one request per pause, short enough that
# an abrupt kill (Task Manager, power loss - neither of which reaches
# flush_draft) loses at most this much typing.
DRAFT_SAVE_DELAY_MS = 700

# Chat-list sidebar status marks (per conversation, at the row's right edge;
# B7a, approved shared_glyphs.status_mark): idle/already-seen draws nothing
# (was "X"), a completed reply the user hasn't switched to yet is a hollow 7x7
# square (was "O"), a reply generating (any stage) is a steady 5x10 block (was
# the \ - / | spinner, whose own 150 ms after() loop is gone: one timer fewer;
# the blink arrives in B10 through fx.clock). The values name kit.chat's mark
# images. SPINNER_FRAMES keeps its one steady frame (the harness pins frame 0).
STATUS_SYMBOL_IDLE = ""
STATUS_SYMBOL_UNSEEN = "unseen"
SPINNER_FRAMES = ["generating"]

# Sidebar geometry (B7a, approved r2 mock_chat at 100 %; scaled by the display
# ratio). Window x 52..250: the + NEW CHAT word starts at x 58 (its 6 px hit pad
# overhangs to 52), folder headings at x 58, rows x 58..250, the transcript at 290.
SIDEBAR_W = 198
SIDEBAR_TOP = 15            # header band (44) -> + NEW CHAT's hit area (59; its word centred on y 70)
SIDEBAR_LIST_TOP = 11       # the prompt slot -> the first heading (93; centred on y 104)
SIDEBAR_LIST_X = 3          # the list starts at x 55: a heading's name (pad 3) at 58, rows (pad 3) from 58
SIDEBAR_BOTTOM = 9
SIDEBAR_ROW_H = 28          # a one-line row; 2 px apart: pitch 30
SIDEBAR_ROW_GAP = 2
SIDEBAR_INSET = 4           # row edge -> its text / mark: 1-2 px ring + band (focus ring 2 px)
SIDEBAR_TEXT_PAD = 4        # + the inset: the title 8 px into the row (x 66), the mark 8 px from its end (x 242)
SIDEBAR_WRAP = 145          # title wraplength: it keeps 24 px clear of the mark (D-27 #14: wrap, never cut)
MARK_CELL_W = 7             # kit.chat MARK_W: the status mark's width (x the kit image factor)
SIDEBAR_HEAD_PAD = 3        # heading name inset (its focus chip), from x 55: the name at x 58
SIDEBAR_HEAD_GAP = 21       # a later heading is centred 34 px below the previous group's last row
SIDEBAR_HEAD_BELOW = 1      # a heading (centre) -> its first row: 12 px
TRANSCRIPT_LEFT = 40        # sidebar (250) -> transcript (290)
TRANSCRIPT_TOP = 4          # its first line on the + NEW CHAT row (y 70)
TRANSCRIPT_PADX = 1         # its inner pad (MarkdownText's default is 14): bands from x 292, body text at x 300
TRANSCRIPT_TEXT_X = 8       # the assistant body's lmargin in MarkdownText: where AWAITING INPUT starts (x 300)

# Status row + composer geometry (B7b, approved r2_chat / mock_chat at 1100x800; scaled by the display ratio).
# The column now runs to the window's right edge (the right column is gone); each row keeps its own right pad, so
# the transcript, the attach line and the composer well end at the 1084 gutter and SEND / STOP at 1085, and the
# status row's words (the meter's COMPACT, the editing line's CANCEL) end at 1084 with their 6 px hit pad past it.
RIGHT_GUTTER = 16           # window right edge -> the transcript / attach line (x 1084)
COMPOSER_RIGHT = 15         # window right edge -> SEND / STOP (x 1085)
COMPOSER_BOTTOM = 9         # the composer well's bottom (767) -> the legend row (776)
ATTACH_GAP = 2              # the attach line's slot -> the composer (688)
ATTACH_MIN_H = 20           # the attach line's slot (y 666-686): kept while empty, so the status row stays at y 652
BTN_W, BTN_H, BTN_GAP = 93, 35, 9   # SEND (992,688)-(1085,723); STOP (992,732)-(1085,767)
BTN_COL_GAP = 11            # the well's right edge (981) -> SEND (992)
COMPOSER_PADX, COMPOSER_PADY = 6, 4  # the Text's inner pad: the first character at (298, 694)
THOUGHT_BODY_MAX_LINES = 25  # an opened thought's box: at most this many lines, then it scrolls inside the box


def _swallow_tabs(widget):
    """B7B-H3 (as kit.loadcard.swallow_tabs): Tab / Shift-Tab bound to "break" on `widget` and everything in it, so
    Tk's "all"-tag focus traversal never walks out of a card to the widgets under its backdrop."""
    stack = [widget]
    while stack:
        w = stack.pop()
        for seq in ("<Tab>", "<Shift-Tab>", "<ISO_Left_Tab>", "<Control-Tab>", "<Control-Shift-Tab>"):
            try:
                w.bind(seq, lambda _e: "break", add="+")
            except tk.TclError:
                continue
        try:
            stack.extend(w.winfo_children())
        except tk.TclError:
            pass


def _thought_body_lines(text, chars_per_line, count_lines):
    """The height (in lines) of an opened thought's box: its display-line count, capped at THOUGHT_BODY_MAX_LINES.
    count_lines() lays the whole text out (Tk 'count -displaylines'), which takes seconds for a 300 KB thought
    (B7b ADV1-02), so it runs only for a text short enough to fit the cap. A text with as many line breaks as the
    cap, or longer than twice the cap's worth of full lines (a display line holds at most chars_per_line monospace
    cells; the factor 2 covers a wrapped line's trailing spaces), needs the cap anyway. The length test runs on the
    text with its space runs collapsed: in a word-wrapped Text a run of spaces hangs past the right edge and takes no
    display lines (B7b ADV2-01: 6,000 spaces between two short phrases made a 25-line box holding 2 lines). Tabs are
    NOT collapsed: a tab run does wrap (measured: 6,000 tabs = 751 lines; counting 300,000 tabs took 36 s)."""
    cap = THOUGHT_BODY_MAX_LINES
    n = len(re.sub(r" {2,}", " ", text))
    if text.count("\n") >= cap or n > 2 * cap * (max(1, int(chars_per_line)) + 1):
        return cap
    return max(1, min(int(count_lines()), cap))


def _show_thought_body(frame, text, padx):
    """B7B-H10: packs an opened thought's body (`frame`, holding `text`) so the Text is first laid out at its FINAL
    width. Packed with the Text already inside, the frame mapped the Text at the list's current width; when the body
    then made the list outgrow its canvas, make_scrollable's scrollbar came up and narrowed the list by its width,
    and the Text wrapped its whole text a second time (0.4 s a pass for a 300 KB thought at 1100x800, 2-5 s in all
    on the 820 px card). Now the frame goes in first as an empty box of the Text's requested height: the list grows,
    the scrollbar (when the body brings it on) comes up and every width settles inside update_idletasks (Tk delivers
    <Configure> synchronously); then the Text is packed into the settled box, which keeps its height. A re-opened
    body (the scrollbar may have gone down meanwhile) takes the same road: its Text is lifted out first, and a
    re-pack at the same width is no new layout."""
    if text.winfo_manager():
        text.pack_forget()
    frame.pack_propagate(False)
    frame.configure(width=1, height=max(1, text.winfo_reqheight()))
    frame.pack(fill="x")
    frame.update_idletasks()
    frame.pack_propagate(True)
    text.pack(fill="both", expand=True, padx=padx)


# Recognizes an in-chat image-generation request ("generate an image of a
# house", "draw me a picture of a cat", "create an illustration: a robot in
# a garden") so it can be routed to the real /api/generate-image backend
# instead of the chat model - confirmed live that the active chat model
# (a Qwythos/Claude-Mythos merge) otherwise hallucinates fake tool-call XML
# tags for this exact phrasing instead of answering or generating anything.
# Anchored at the start of the message (only a "please"/"can you" preamble
# allowed before the verb) so a question *about* image generation - "can you
# explain how to generate an image of a fractal in Python" - doesn't false-
# positive: "explain" there isn't one of the recognized verbs in that slot.
IMAGE_GEN_INTENT_RE = re.compile(
    r"^\s*(?:please\s+|can you\s+|could you\s+)*"
    r"(?:generate|create|draw|make|paint|render)\s+"
    r"(?:me\s+)?(?:an?\s+)?"
    r"(?:image|picture|photo|illustration|drawing|painting)\b"
    r"[:\-]?\s*(?:of|showing|depicting)?\s*"
    r"(.+)",
    re.IGNORECASE,
)

IMAGE_GEN_STAGE_LABELS = {
    "unloading_chat_model": "IMAGE GEN: UNLOADING CHAT MODEL...",
    "loading_image_model": "IMAGE GEN: LOADING IMAGE MODEL...",
    "generating": "IMAGE GEN: GENERATING IMAGE...",
    "restoring_chat_model": "IMAGE GEN: RESTORING CHAT MODEL...",
}


# The intent patterns below backtrack badly on long whitespace runs (adjacent \s*
# groups: "try x" followed by 500 spaces froze the window for 27 s, 1,000 spaces
# never finished). An intent phrase is short, so longer text is never one, and every
# run of whitespace is collapsed to a single character before matching (a newline
# if the run had one - the patterns' "." stops at newlines, as it always did).
MAX_INTENT_TEXT_CHARS = 2000
_WHITESPACE_RUN_RE = re.compile(r"\s{2,}")


def _intent_text(text):
    if not isinstance(text, str) or len(text) > MAX_INTENT_TEXT_CHARS:
        return None
    return _WHITESPACE_RUN_RE.sub(lambda m: "\n" if "\n" in m.group() else " ", text)


def match_image_generation_intent(text):
    text = _intent_text(text)
    if text is None:
        return None
    m = IMAGE_GEN_INTENT_RE.match(text)
    if not m:
        return None
    prompt = m.group(1).strip().rstrip(".!?")
    return prompt or None


# Recognizes a follow-up *edit* of an image that was just generated/edited in
# this same conversation ("make it nighttime", "change the roof to blue",
# "add a dragon to it", "turn it into a painting") - the Grok-like part of
# this feature: no explicit "edit" button, just keep talking about the image
# you already have. Deliberately requires a definite reference ("it"/"the
# image"/"this image"/"that image") rather than accepting a bare object the
# way IMAGE_GEN_INTENT_RE does for indefinite "a/an image" - that's what lets
# the two coexist without colliding ("make an image of a cat" vs "make the
# image darker"). Even so, "make it ..."/"change it ..." are common enough
# English constructions that this is only ever *checked* when
# ChatView._last_image_by_conv confirms the conversation's last message
# really is an image - never against arbitrary chat text - so it can't
# hijack an unrelated "make it happen"-style reply days after the image.
IMAGE_EDIT_INTENT_RE = re.compile(
    r"^\s*(?:please\s+|can you\s+|could you\s+|now\s+)*"
    r"(?:"
    r"edit\s+(?:the\s+|this\s+|that\s+)?(?:image|picture|photo)\b"
    r"|make\s+(?:it|the\s+image|this\s+image|that\s+image)\b"
    r"|turn\s+(?:it|the\s+image|this\s+image|that\s+image)\s+into\b"
    r"|change\s+(?:it|the\s+image|this\s+image|that\s+image)\b"
    r"|(?:add|remove)\s+.+?\s+(?:to|from)\s+(?:it|the\s+image|this\s+image|that\s+image)\b"
    r")",
    re.IGNORECASE,
)


# A handful of common "make it ..." idioms that would otherwise match
# IMAGE_EDIT_INTENT_RE's bare "it" form but are essentially never about an
# image ("make it happen", not "make it [nighttime/darker/etc]"). Checked as
# a *whole-message* match (allowing only trailing punctuation), not a
# substring - "make it happen, we need this by Friday" is intentionally left
# alone, since it's a rarer, more composite case not worth the false-
# negative risk of a broader substring match. This is best-effort, not
# exhaustive: several idioms ("make it work", "make it clear") are genuinely
# ambiguous even to a human reading them in isolation and are deliberately
# not included - full disambiguation isn't realistically solvable with
# regex alone, only mitigated where the idiom is unambiguous.
IMAGE_EDIT_FALSE_POSITIVE_RE = re.compile(
    r"^\s*(?:please\s+|can you\s+|could you\s+|now\s+)*"
    r"make\s+it\s+(?:happen|home|back|through|official|count|in\s+time)\s*[.!]?\s*$",
    re.IGNORECASE,
)


def match_image_edit_intent(text):
    text = _intent_text(text)
    if text is None or not IMAGE_EDIT_INTENT_RE.match(text):
        return False
    return not IMAGE_EDIT_FALSE_POSITIVE_RE.match(text)


# Recognizes an implicit *new* image request phrased as an alternative to the
# one just shown ("what about a man standing in an office building", "how
# about a cat instead", "try a sunset") - confirmed live (2026-08-03) that
# without this, such a follow-up doesn't match IMAGE_GEN_INTENT_RE (no
# generate/create/draw/... verb) or IMAGE_EDIT_INTENT_RE (no definite "it"/
# "the image" reference), falls through to the chat model, which - having
# just seen a real "Generated an image for: X" turn in its own context -
# hallucinated an equally-formatted fake success reply with no image
# actually created. This routes to a fresh *generate* (not edit): "what
# about X" reads as "try a different subject", not "modify the existing
# picture," and Flux Kontext's edit conditioning anchors to the source image
# in a way that wouldn't suit a totally different scene anyway. Gated the
# same way as EDIT (only checked when _last_image_by_conv confirms the
# conversation's last message really is an image) - "what about the weather
# tomorrow" is ordinary conversation and would misfire without that gate.
IMAGE_ALT_GENERATE_INTENT_RE = re.compile(
    r"^\s*(?:please\s+|now\s+)*"
    r"(?:what about|how about|try)\s+(.+?)\s*(?:instead)?\s*[.!?]?\s*$",
    re.IGNORECASE,
)
# "try again"/"try it"/etc are far more likely as an ordinary reply than an
# image request, even right after an image - same best-effort denylist
# philosophy as IMAGE_EDIT_FALSE_POSITIVE_RE above.
IMAGE_ALT_GENERATE_FALSE_POSITIVE_RE = re.compile(
    r"^\s*(?:please\s+|now\s+)*try\s+(?:again|it|that|harder|later|once\s+more)\s*[.!?]?\s*$",
    re.IGNORECASE,
)


def match_image_alt_generate_intent(text):
    text = _intent_text(text)
    if text is None:
        return None
    m = IMAGE_ALT_GENERATE_INTENT_RE.match(text)
    if not m:
        return None
    if IMAGE_ALT_GENERATE_FALSE_POSITIVE_RE.match(text):
        return None
    prompt = m.group(1).strip().rstrip(".!?")
    return prompt or None


class _StreamState:
    """Tracks one in-flight generation for one conversation. Only one
    conversation's transcript is ever on screen at a time (self.transcript
    is a single shared widget), but up to the server's configured
    max_parallel_chats can be generating in the background at once - so all
    of a stream's mutable state lives here, keyed by conv_id in
    ChatView._streams, instead of as
    flat ChatView instance attributes tied to whatever's currently
    displayed."""

    def __init__(self, stop_event):
        self.stream_queue = queue.Queue()
        self.stop_event = stop_event
        self.prefill_done = False
        self.gen_token_count = 0
        self.gen_first_chunk_time = None
        self.thinking_token_count = 0
        self.thinking_first_time = None
        self.assistant_started = False
        self.thinking_started = False
        self.thinking_finished = False
        self.progress_queue = queue.Queue()
        self.last_progress_poll_time = 0
        self.progress_fetch_in_flight = False
        self.last_prompt_progress = None
        # Mirrors what's been fed into the transcript widget so far. Kept
        # here rather than only in self.transcript._raw (like the old
        # single-stream version did) because the widget only ever shows one
        # conversation - a backgrounded stream for a *different* conv_id has
        # nowhere else to accumulate its text until the user switches to it.
        self.raw_text = ""
        self.thinking_raw = ""
        # Batch 33 part 2: what a dispatched QUEUED message carried (text / images / docs / retries), so a 409 met at
        # the server's still-held slot can put it back in front of the queue; None for a stream the user sent.
        self.queued_payload = None
        self.started_at = time.time()           # batch 34: the pull-down's "since"
        self._notified_stage = None             # batch 34: the last stage word the pull-down was told
        # Document tool P2: this reply's create_document events ({"index", "state": creating | done | failed, "title",
        # "offset" into raw_text, ...}), for the pull-down and for replaying the rows / cards when the chat is
        # shown; doc_widgets: index -> the row / card / line on screen (only while this chat is displayed).
        self.documents = []
        self.doc_widgets = {}
        # Last computed rail_status text, restored verbatim when switching
        # back to this conversation instead of recomputing from counters.
        self.status_text = "SENDING REQUEST"
        # The assistant message this stream is replacing, when it came from
        # [Regenerate] rather than a fresh send - see _on_regenerate_clicked.
        # None for every other kind of stream.
        self.regenerating_message_id = None
        # Kept because the error and the "done" frame don't have to land in
        # the same 50ms poll tick, and a regenerate that failed has to report
        # why in a toast - its inline "_error: ..._" text is wiped by the
        # reload that restores the reply it didn't manage to replace.
        self.last_error = None
        # The server's "truncated" frame, when the reply was cut short (the
        # reply limit, or a full context window) - kept across ticks like
        # last_error, since it and the "done" frame can land in different ones.
        self.truncated = None


def cut_off_toast(info):
    """The one-line notice for a reply the server says was cut short (its
    "truncated" frame): by the reply limit the user set, or because the context
    window filled up."""
    if isinstance(info, dict) and info.get("reason") == "server":
        return f"The reply was interrupted: the connection to the model server ended. {info.get('message') or ''}".strip()
    if isinstance(info, dict) and info.get("reason") == "limit" and info.get("limit"):
        return f"Reply cut off at the reply limit ({int(info['limit']):,} tokens). Raise or remove it in Settings."
    context = info.get("context") if isinstance(info, dict) else None
    window = f" ({int(context):,} tokens)" if isinstance(context, (int, float)) and context else ""
    top = info.get("max_context") if isinstance(info, dict) else None
    room = f" (this model allows up to {int(top):,})" if isinstance(top, (int, float)) and top else ""
    return f"Reply cut off: the context window{window} is full. Compact the chat, start a new one or raise the context size in Settings{room}."


def _reply_outcome(state):
    """B14 H16: reply.done's outcome (AESTHETIC_SPEC S-07): stopped / error / ok / empty. A truncated reply that produced
    text is 'ok' - it landed; its cut-off notice stays silent (the sound bridge's cut_off rule). One exception (B14 FIX1,
    ADV1-01): cut short because the model server died (reason 'server') is a stream error, so it is 'error' (fault)."""
    if state.stop_event.is_set():
        return "stopped"
    if state.last_error or (isinstance(state.truncated, dict) and state.truncated.get("reason") == "server"):
        return "error"
    return "ok" if state.raw_text.strip() else "empty"


def _image_outcome(error):
    """B14 H17: image.finished's outcome from _finish_image_generation's `error`: cancelled / error / ok."""
    return "cancelled" if error == "cancelled" else ("error" if error else "ok")


def _read_local_snippet(path):
    """First few lines of a local text-ish file, read directly off disk
    (called from _drop_worker, already off the Tk main thread) - lets the
    staging tile/hover preview show real content immediately, without
    waiting on the server's extraction+upload round trip. None on any
    read failure (e.g. permissions, file vanished between drop and read)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines, total = [], 0
            for _ in range(SNIPPET_MAX_LINES):
                line = f.readline()
                if not line:
                    break
                lines.append(line)
                total += len(line)
                if total >= SNIPPET_MAX_CHARS:
                    break
            return "".join(lines)[:SNIPPET_MAX_CHARS]
    except OSError:
        return None


class ChatView(Screen):
    def __init__(self, parent, ctx):
        super().__init__(parent, ctx)
        theme = ctx.theme
        self.api = ctx.api
        self.conv_id = None
        if _KIT_IMPORT_ERROR is not None:       # the import's own error, not just the stand-ins it caused
            _report_kit_failure("import", _KIT_IMPORT_ERROR)
        # B7a: the compact header (approved r2_chat): CHAT, the conversation title beside it
        # (_set_header_title: elided in the middle with a hover tip when long), and the header text
        # actions NEW SESSION, IMG UPLINK, THOUGHTS (n), JUMP TO..., EXPORT LOG (D-27 #3 keeps
        # NEW SESSION and IMG UPLINK; they were the right column's buttons). Below a 900 px content
        # column the actions take their own row(s) under the band: never hidden, never clipped.
        # export_btn / jump_btn / thoughts_btn are kit TextActions: .configure(state=, text=) and
        # .config(...) work (Override rule), and jump_btn.winfo_rootx() still positions the Jump menu.
        self.header = None
        if ChatHeader is not None:
            try:
                self.header = ChatHeader(self, theme, screen=self)
                self.header.pack(fill="x")
                self._refresh_datetime = lambda: None      # on_show calls it; nothing to refresh (D-32)
            except Exception as exc:  # noqa: BLE001 - the header must never stop the screen being built
                _report_kit_failure("header", exc)
                if self.header is not None:
                    try:
                        self.header.destroy()
                    except tk.TclError:
                        pass
                self.header = None
        if self.header is None:
            self.header, self._refresh_datetime = make_header(self, theme, "Chat", compact=True)

        def header_action(text, command):
            add = getattr(self.header, "add_action", None)
            if add is not None:
                try:
                    return add(text, command)
                except Exception as exc:  # noqa: BLE001 - the action must always be there
                    _report_kit_failure("header action", exc)
            word = tk.Label(
                getattr(self.header, "actions", None) or self.header, text=text.upper(), bg=theme.bg,
                fg=theme.text, font=theme_mod.FONT_CAPTION, cursor="hand2", padx=6, pady=4, takefocus=1,
            )
            word.pack(side="left", padx=(4, 0))
            for seq in ("<Button-1>", "<Return>", "<space>"):
                word.bind(seq, lambda e, w=word: None if str(w.cget("state")) == "disabled" else command())
            return word

        self.new_session_btn = header_action("New session", lambda: self.ctx.new_session())
        self.uplink_btn = header_action("Img uplink", lambda: self.ctx.go_to_screen("image_uplink"))
        self.thoughts_btn = header_action("Thoughts", self._on_show_thoughts)
        self.jump_btn = header_action("Jump to...", self._on_jump_menu)
        self.export_btn = header_action("Export log", self._on_export)
        # the conversation title the header (and the empty state) shows; see _set_header_title
        self._header_title = ""
        # AWAITING INPUT over an empty transcript (kit ChatEmptyState, made on first need); see _set_empty_state
        self.empty_state = None
        # conv_id -> _StreamState, one entry per conversation currently
        # generating a reply (in the foreground - the one being displayed -
        # or in the background). See _StreamState's own docstring.
        self._streams = {}
        # Set to {"conv_id": ..., "stage": ...} while an image-generation
        # job started from send_message() is in flight, else None. Unlike
        # self._streams this is never keyed per-conversation: server.py's
        # generate_image_worker fully unloads the shared chat model to make
        # room for sd-server, so only one of these (system-wide, blocking all
        # chats too) can ever be running at a time - see any_chat_active()/
        # image_gen_status["active"] mutual exclusion in server.py.
        self._image_gen = None
        # conv_id -> {"message_id":, "image_id":} for the most recent message
        # in that conversation IF it's an image (see load_conversation()) -
        # lets a short follow-up like "make it nighttime" be recognized as an
        # edit of that specific image instead of requiring the user to name
        # a target explicitly every time.
        self._last_image_by_conv = {}
        # images staged via drag-and-drop, sent with the next message then
        # cleared - see handle_file_drop()/send_message()
        self._staged_images = []
        # documents already uploaded (server-side, permanent) during this
        # composing session but still shown in the staging grid rather than
        # the plain attached-files list - id -> {filename, content_tokens,
        # snippet}; cleared (visually promoted to the plain list) on send.
        self._staged_docs = {}
        # cache of the last GET .../files response, so promoting a doc from
        # the staging grid to the plain list on send doesn't need a refetch.
        self._conversation_files = []
        # id(img_dict) -> {"full": PhotoImage|None, <target_px>: PhotoImage} -
        # keyed by id(), never written onto the staged-image dict itself,
        # since those same dicts are sent verbatim through json.dumps() in
        # ApiClient.stream_chat() - a PhotoImage attached there would crash
        # the next send.
        self._photo_cache = ag.PhotoCache()
        self._hover = ag.HoverPreview()
        # Set by load_conversation() from the loaded conversation's own
        # project_id field - None means "ordinary chat". Drives which mode
        # _refresh_chat_list()/_render_chat_list_rows() render, so the
        # sidebar always matches whatever conversation is actually on
        # screen regardless of how the user navigated here.
        self._current_project_id = None
        self._current_project_name = None
        # Cache of the sidebar's last-fetched conversation list - either
        # every non-project chat (same scope as the Chats screen's "All"
        # view) or just the current project's siblings, depending on
        # self._current_project_id. Kept around so _render_chat_list_rows()
        # can be called (e.g. right after a rename) without necessarily
        # re-fetching first.
        self._chat_list_conversations = []
        # Cache of the sidebar's last-fetched folder list, in the same
        # server order the Chats screen's chips use - drives the section
        # headers in _render_chat_list_rows(). Always empty while viewing a
        # project chat, since project conversations are never foldered.
        self._chat_list_folders = []
        # The sidebar is fetched off the Tk thread (see _refresh_chat_list); a
        # newer refresh supersedes an older one that is still in flight.
        self._chat_list_loader = Latest(self)
        # The one right-click menu shared by every sidebar row and folder
        # header, filled in only when opened - see context_menu.py.
        self._sidebar_menu = SharedContextMenu(self, theme)
        # Same for the conversation itself (see load_conversation). Not tied to
        # visibility: the loading flag below must always be released.
        self._conv_loader = Latest(self)
        # The conversation whose history is currently on its way (None when
        # nothing is loading); see load_conversation.
        self._loading_conv_id = None
        # An Enter pressed while that history loads is remembered here and sent
        # as soon as it has been drawn.
        self._send_when_loaded = False
        # True while a "new chat in this project/folder" request is in flight,
        # so a double-click can't create (and abandon) two.
        self._creating_chat = False
        # a DRAFT new chat on screen (conv_id None): {"project_id", "folder_id"} of the row its first message creates
        self._draft_where = None
        # Set around one router-driven navigation that is about to load a
        # specific conversation - see skip_next_show_reload().
        self._skip_show_reload = False
        # conv_ids whose most recent reply finished while the user wasn't
        # actually watching it happen (a different conversation displayed,
        # a different screen entirely, or both) - cleared the moment they
        # load that conversation. See _poll_stream_queue's `done` handling
        # and load_conversation().
        self._unseen_conv_ids = set()
        # conv_id -> how many saved reasoning transcripts this conversation
        # has, purely so the rail button can read "Thoughts (3)" without
        # fetching the transcripts themselves (they can be longer than the
        # answers they produced - see handle_thoughts in server.py). Seeded
        # from the has_reasoning flags on load_conversation()'s message list
        # and bumped when a stream finishes; the panel itself always fetches
        # live, so this only ever drives the label.
        #
        # The transcripts used to live here in full and vanished on exit;
        # they're now a `reasoning` column on messages, so they survive a
        # restart. Still never sent back to the model on later turns -
        # build_chat_messages_payload only reads `content`.
        self._thoughts_counts = {}
        # conv_id -> unsent composer text. Originally in-memory only, to stop
        # self.input (one shared Text widget across every conversation - see
        # load_conversation()) leaking a draft typed in one chat into
        # whatever chat was loaded next. It now also autosaves to the
        # conversation's `draft` column, so an unsent message survives closing
        # the window and shutting the backend down entirely; this dict stays
        # as the in-memory mirror the UI reads, seeded from the server on
        # every load_conversation().
        self._drafts = {}
        # conv_id -> text written but not yet persisted, drained by a single
        # background worker so writes for one conversation can never land out
        # of order (a later keystroke overtaking an earlier save would
        # resurrect stale text). Latest value per conversation wins, so a
        # burst of typing collapses to one request.
        self._draft_pending = {}
        self._draft_lock = threading.Lock()
        self._draft_saver_running = False
        self._draft_after_id = None
        # conv_id -> {"text":, "images":, "docs":} captured from the composer
        # while that conversation was busy (a reply streaming, or an image
        # generating) - Send becomes "Queue" during that window instead of
        # being disabled outright, and the captured message auto-fires the
        # moment that conversation is free again (see _fire_queued_message,
        # called from _poll_stream_queue's "done" handling and from
        # load_conversation for the case where it finished while the user
        # was looking at a different chat). In-memory only, same as _drafts.
        self._queued_messages = {}
        # Ordered list of {"conv_id":, "text":, "images":, "docs":} - for the
        # DIFFERENT busy case above's dict doesn't cover: sending in a
        # conversation that is itself idle, but every max_parallel_chats
        # slot is occupied by OTHER conversations. Used to just fail with a
        # 409 shown as a garbled assistant-bubble error; now captured here
        # instead and dispatched in FIFO order (append to send, pop(0) to
        # dispatch) as slots actually free up - see send_message()'s
        # capacity check, _queue_globally, and _poll_stream_queue's "done"
        # handling (which drains this unconditionally, not just when the
        # freed conversation is the one displayed - a global scheduler has
        # to react to every completion, not just the one being watched).
        self._global_send_queue = []
        self.on_activity_changed = None         # batch 34: MainWindow's coalesced notifier (the pull-down)
        # B14 H15 (hand-off H4 / H9): the conversation whose NEXT launch is an automatic dispatch - chat.queued_dispatch
        # goes out right before that launch's chat.launch (_launch_stream / _start_image_job), however long the
        # history takes to land, and a dispatch that launches nothing arms nothing
        self._fx_queued = None
        # Best-effort local cache of the server's max_parallel_chats setting
        # (refreshed on every on_show() - see _refresh_max_parallel_chats_cache)
        # so send_message()'s capacity check doesn't need a blocking network
        # round trip on every single send. Starts at the conservative
        # DEFAULT_SETTINGS value (1) until the first real refresh lands.
        self._cached_max_parallel_chats = 1
        # Set while the composer holds a past message's text for editing
        # (Edit link clicked) rather than a fresh draft - send_message()
        # branches to _submit_edit() instead of the normal new-message path
        # while this is set. Cleared on submit, on explicit Cancel, and on
        # conversation switch (see load_conversation).
        self._editing_message_id = None
        # conv_id -> the sidebar row's status-mark Label for a row whose reply is
        # generating, rebuilt every _render_chat_list_rows() call. B7a: the mark
        # is a steady block, so nothing ticks it (the spinner's own after() loop
        # is gone); B10 blinks these marks through fx.clock.
        self._chat_status_labels = {}
        self._spinner_frame = 0
        self._spinner_after_id = None      # no spinner timer any more (destroy() still sweeps the name)

        body = tk.Frame(self, bg=theme.bg)
        body.pack(fill="both", expand=True)

        # left-hand chat-list sidebar - quick switch/create without leaving
        # this screen (see module docstring). packed before `left` below so
        # it claims the leftmost strip; `left` then fills what remains
        # between here and the rail.
        # B7a: unboxed (approved r2_chat): no frame, + NEW CHAT a text action, folder headings in the
        # user's own case, rows C10 at a 30 px pitch with the open chat inverse (see _render_chat_list_row).
        self._sb_ratio = _geom_ratio(self)
        s = self._sb_s
        chat_list = tk.Frame(body, bg=theme.bg, width=s(SIDEBAR_W), highlightthickness=0)
        chat_list.pack(side="left", fill="y", padx=(s(10), 0), pady=(s(SIDEBAR_TOP), s(SIDEBAR_BOTTOM)))
        chat_list.pack_propagate(False)
        self.chat_list_frame = chat_list

        new_chat_btn = None
        if _TextAction is not None:
            try:
                new_chat_btn = _TextAction(chat_list, theme, text="+ New chat", upper=True,
                                           command=self._on_new_chat_from_sidebar)
            except Exception as exc:  # noqa: BLE001
                _report_kit_failure("sidebar action", exc)
        if new_chat_btn is None:
            new_chat_btn = tk.Label(chat_list, text="+ NEW CHAT", bg=theme.bg, fg=theme.text,
                                    font=theme_mod.FONT_CAPTION, cursor="hand2", padx=6, pady=4, takefocus=1)
            for seq in ("<Button-1>", "<Return>", "<space>"):
                new_chat_btn.bind(seq, lambda e: self._on_new_chat_from_sidebar())
        new_chat_btn.pack(anchor="w")
        self.new_chat_btn = new_chat_btn

        # Reserved slot for the inline rename form (see show_inline_prompt) -
        # empty and zero-height until a rename is in progress.
        self.chat_list_prompt_slot = make_prompt_slot(chat_list, theme)

        chat_list_scroll_outer, self.chat_list_rows = make_scrollable(chat_list, theme)
        chat_list_scroll_outer.pack(fill="both", expand=True, padx=(s(SIDEBAR_LIST_X), 0),
                                    pady=(s(SIDEBAR_LIST_TOP), 0))
        # Sidebar row / heading events: one registered command and bind tags bound ONCE (the B5 list
        # technique), so drawing a row registers no Python callback. widget path -> record; folder id ->
        # its heading's reveal state (the reveal-only "+").
        self._sb_recs = {}
        self._sb_groups = {}
        self._sb_cmd = self.register(self._on_sidebar_event)
        uid = "%x" % id(self)
        self._sb_tag, self._sb_part = "ChatSideItem" + uid, "ChatSidePart" + uid
        c = self._sb_cmd
        for seq, act in (("<Return>", "activate"), ("<KP_Enter>", "activate"), ("<space>", "activate")):
            self.bind_class(self._sb_tag, seq, 'if {"[%s %s %%W]" == "break"} break' % (c, act))
        for tag in (self._sb_tag, self._sb_part):
            self.bind_class(tag, "<Button-1>", "%s click %%W" % c)
            self.bind_class(tag, "<Enter>", "%s enter %%W %%d" % c)
            self.bind_class(tag, "<Leave>", "%s leave %%W %%d" % c)
            self.bind_class(tag, "<MouseWheel>", "%s wheel %%W %%D" % c)
        self.bind_class(self._sb_tag, "<FocusIn>", "%s focusin %%W" % c)
        self.bind_class(self._sb_tag, "<FocusOut>", "%s focusout %%W" % c)

        # B7b: the right-hand column is gone (B7a moved its buttons to the header; rail_status moved to the
        # status row below), so the column runs to the window's right edge and each row keeps its own gutter.
        left = tk.Frame(body, bg=theme.bg)
        # B7a: the column starts 30 px after the sidebar, so the transcript, and the rows under it that
        # keep their 10 px pad, start at x 290 (approved r2_chat)
        left.pack(side="left", fill="both", expand=True, padx=(s(TRANSCRIPT_LEFT) - 10, 0))
        kt = self._b7b_tokens()

        # B2 fix ADV1-01: a small REQUESTED height (expand still fills the column) so a short window
        # takes its shortage out of the transcript, not the context meter packed after it
        # B7a: the transcript is the page: no frame (highlightthickness=0; markdown_text.py unchanged),
        # x 290 to the right gutter, its first line on the + NEW CHAT row; a 1 px inner pad puts the
        # message bands at x 292 and the body text at x 300, as approved (r2_chat)
        self.transcript = MarkdownText(left, height=5, highlightthickness=0, padx=s(TRANSCRIPT_PADX))
        self.transcript.pack(fill="both", expand=True, side="top", padx=(10, s(RIGHT_GUTTER)),
                             pady=(s(TRANSCRIPT_TOP), 4))
        # any change to the transcript takes the empty state (AWAITING INPUT) away - see _set_empty_state
        self.transcript.bind("<<Modified>>", self._on_transcript_modified, add="+")
        self.transcript.bind("<Configure>", self._on_room_changed, add="+")   # B7B-H7: the composer's cap follows the room
        self.transcript.on_edit_message = self._on_edit_message_clicked
        self.transcript.on_regenerate_message = self._on_regenerate_clicked
        self.transcript.on_switch_version = self._on_switch_version_clicked
        self.transcript.on_image_click = self._on_image_click           # document tool P4: the image viewer
        self.transcript.on_image_menu = self._on_image_menu             # document tool P5: the image's right-click menu
        self.transcript.on_image_focus = lambda photo, focused: self._set_legend_focus("image" if focused else None)
        # Batch 32 (the UI session's shape): "JUMP TO LATEST · END" over the transcript's bottom-right corner while a
        # reply streams in this chat and the reader has scrolled away from the end (batch 31 follows the end only
        # while the view is there). No timer: the transcript's yscrollcommand (every view change, inserts included)
        # and the stream ticks call _sync_jump_cue. The Thoughts card's in-flight caption lives in _thoughts_inflight.
        self.jump_latest = None
        self._thoughts_inflight = None
        if _TextAction is not None:
            self.jump_latest = _TextAction(self.transcript, theme, text="Jump to latest \u00b7 End", upper=True,
                                           ground=theme.bg, command=self._on_jump_latest)
        self.transcript.configure(yscrollcommand=lambda first, last: self._sync_jump_cue())

        # B7b: under the transcript, bottom to top - the composer (well + SEND / QUEUE / STOP, approved
        # (290,688)-(1085,767)), the editing / queued line when shown (right above the composer), the attach
        # line's slot (the staging grid and the attached-files line, y 662-682) and the status row (y 652).
        composer = tk.Frame(left, bg=theme.bg)
        composer.pack(fill="x", side="bottom", padx=(10, s(COMPOSER_RIGHT)), pady=(0, s(COMPOSER_BOTTOM)))
        self._composer_frame = composer

        # Shown (via pack(before=composer), the call sites' technique) only while the composer holds a past
        # message's text for editing rather than a fresh draft - hidden the rest of the time, never packed at
        # construction. B7b: one C9B line + a CANCEL text action (kit AboveLine, which reads that
        # pack(side="bottom", before=composer) as "right above the composer"; the call sites are unchanged).
        # The same for the queued line: a message captured from the composer waiting to auto-send once this
        # conversation is free (see _queue_current_message/_fire_queued_message).
        self.editing_indicator = self.queued_indicator = None
        if _AboveLine is not None:
            try:
                # (lambdas, as before: the handlers are looked up when clicked)
                self.editing_indicator = _AboveLine(left, theme, "Editing message", lambda: self._cancel_edit(),
                                                    anchor=composer, ink=kt["bright"])
                self.queued_indicator = _AboveLine(left, theme, "Message queued - sends when ready",
                                                   lambda: self._cancel_queued_message(), anchor=composer,
                                                   ink=kt["value"])
            except Exception as exc:  # noqa: BLE001 - plain lines stand in
                _report_kit_failure("editing line", exc)
                self.editing_indicator = self.queued_indicator = None
        if self.editing_indicator is None:
            self.editing_indicator = self._plain_cancel_line(left, "Editing message", lambda: self._cancel_edit(),
                                                             theme.bright)
            self.queued_indicator = self._plain_cancel_line(left, "Message queued - sends when ready",
                                                            lambda: self._cancel_queued_message(), theme.dim)

        # The attach line's slot (D-27 #13: the staging grid and the attached-files line are kept): staged_row /
        # files_row are shown / hidden later via pack(before=self._attach_end) by _render_staging_grid /
        # _render_conversation_files, so the two keep their order whichever is shown first. A 1 px strut keeps
        # the slot 20 px tall while it is empty, so the status row above it stays where it is (approved y 652).
        self.attach_panel = tk.Frame(left, bg=theme.bg)
        self.attach_panel.pack(fill="x", side="bottom", padx=(10, s(RIGHT_GUTTER)), pady=(0, s(ATTACH_GAP)))
        attach_panel = self.attach_panel
        tk.Frame(attach_panel, bg=theme.bg, width=1, height=s(ATTACH_MIN_H), bd=0,
                 highlightthickness=0).pack(side="right")
        self._attach_end = tk.Frame(attach_panel, bg=theme.bg, height=1, bd=0, highlightthickness=0)
        self._attach_end.pack(side="top", fill="x")
        self.staged_row = tk.Frame(attach_panel, bg=theme.bg)
        self.files_row = tk.Frame(attach_panel, bg=theme.bg)
        self._files_flow_width = None
        self.files_row.bind("<Configure>", self._flow_conversation_files, add="+")   # B7B-H1: re-flow on a new width

        # The status row (approved r2_chat, y 652, ONE line): rail_status on the left - a kit StatusLine, so its
        # writers (_sync_rail_for_current_conv, _on_export, _poll_export_queue: rail_status.config(text=...)) are
        # untouched and still reach a one-line rendering through the kit Override rule (X-1): a two-line stage
        # text shows as one line, a text too wide for the row is shortened (a stage text at word boundaries, a
        # saved: path in the middle) with the whole text in a hover tip, and cget("text") keeps the original.
        # Then '+ ADD FILES...' (a text action), then the context meter, which ends at the gutter. A narrow
        # window gives the meter a row of its own (kit StatusRow; BF-10).
        self.status_row = None
        if _StatusRow is not None:
            try:
                # ContextMeter.show() packs the meter with padx=10, pady=(0, 4): the row's words share that line
                self.status_row = _StatusRow(left, theme, bottom=4, right=10)
                self.rail_status = _ChatStatusLine(self.status_row, theme, text="", font=C9B, fg=kt["value"],
                                                   bg=theme.bg, padx=0, pady=1, bd=0)
                self.add_files_link = _TextAction(self.status_row, theme, text="+ Add files...", upper=True,
                                                  command=lambda: self._on_add_files_click())
                self.context_meter = ContextMeter(self.status_row.meter_slot, ctx.api,
                                                  on_compacted=self._on_compacted, theme=theme)
                self.status_row.attach(self.rail_status, self.add_files_link, self.context_meter)
            except Exception as exc:  # noqa: BLE001 - plain widgets stand in
                _report_kit_failure("status row", exc)
                if self.status_row is not None:
                    try:
                        self.status_row.destroy()
                    except tk.TclError:
                        pass
                self.status_row = None
        if self.status_row is None:
            self.status_row = tk.Frame(left, bg=theme.bg)
            slot = tk.Frame(self.status_row, bg=theme.bg)
            slot.pack(side="right")
            self.add_files_link = tk.Label(self.status_row, text="+ Add files...", bg=theme.bg, fg=theme.bright,
                                           font=theme_mod.FONT_CAPTION_LINK, cursor="hand2", anchor="w")
            self.add_files_link.pack(side="right", padx=(12, 16))
            self.add_files_link.bind("<Button-1>", lambda e: self._on_add_files_click())
            self.rail_status = tk.Label(self.status_row, text="", bg=theme.bg, fg=theme.dim,
                                        font=theme_mod.FONT_CAPTION, anchor="w", justify="left")
            self.rail_status.pack(side="left", fill="x", expand=True)
            self.context_meter = ContextMeter(slot, ctx.api, on_compacted=self._on_compacted, theme=theme)
        self.status_row.pack(fill="x", side="bottom", padx=(10, 0))
        self.status_row.bind("<Configure>", self._on_room_changed, add="+")   # B7B-H7: a squeezed status row gives lines back
        # Stylizes the real time-to-first-token wait only - started when a
        # send begins, stopped the instant the first chunk (or error) lands.
        # B7b: static text + a steady block cursor (boot_fx; B10 blinks it).
        self._signal_fx = SignalAcquiring(self.rail_status, base_text="ACQUIRING")
        # B2: the transcript goes last in the packing order, so a short window (the shell's 24 px
        # legend row takes its share) squeezes the scrollable transcript, never the rows below it
        # (B7b: the status row, the attach line, the composer). Where everything fits the layout is
        # unchanged: the bottom rows keep their order and the transcript still fills what is left.
        self.transcript.pack_configure(after=self.status_row)

        # The composer: a kit Well (panel, 1 px dim edge; 2 px bright inside while focused) around the Text, a
        # block insert caret one Consolas 11 cell wide (2 px if Tk refuses), 3-10 lines of auto-grow kept
        # (_on_composer_modified). The well is at least as tall as the SEND + STOP column (79 px), so neither
        # STOP appearing nor the first lines of text move the rows above.
        well = None
        if _Well is not None:
            try:
                well = _Well(composer, theme)
            except Exception as exc:  # noqa: BLE001
                _report_kit_failure("composer well", exc)
                well = None
        self._composer_well = well
        text_parent = well.body if well is not None else composer
        self.input = tk.Text(
            text_parent, height=COMPOSER_MIN_LINES, font=theme_mod.FONT_BODY, wrap="word",
            bg=theme.panel, fg=theme.text, insertbackground=theme.bright,
            relief="flat", bd=0, highlightthickness=0 if well is not None else 1, highlightbackground=theme.dim,
            highlightcolor=theme.bright, padx=s(COMPOSER_PADX), pady=s(COMPOSER_PADY),
        )
        # The block caret is Tk's -blockcursor: a block one character cell wide (about 8 px in Consolas 11) that sits
        # ON the insert point, so at the end of the text it stands right after the last character, as approved. Tk
        # draws it from x - insertwidth/2 over cell + insertwidth px, so insertwidth stays 2 (1 px each side). A wide
        # -insertwidth alone would be centred on the insert point and cover half of the character before it (the
        # runs_laneB\B7b caret captures). A Tk without -blockcursor keeps the GAP_VISUAL fallback, a 2 px bar.
        self.input.configure(insertwidth=2)
        try:
            self.input.configure(blockcursor=True)
        except tk.TclError:
            pass
        if well is not None:
            self.input.pack(fill="both", expand=True)
            well.attach(self.input)
            well.pack(side="left", fill="both", expand=True)
        else:
            self.input.pack(side="left", fill="both", expand=True)
        self.input.bind("<Return>", self._on_enter)
        self.input.bind("<Shift-Return>", lambda e: None)  # allow default newline insertion
        self.input.bind("<<Modified>>", self._on_composer_modified)

        # The drag-and-drop hint is the EMPTY composer's placeholder now (approved r2_chat_empty): an overlay
        # Label over the Text, never inserted, so it can never reach a draft (the 700 ms autosave reads
        # input.get) or a send; bound after _on_composer_modified with add="+", so that handler keeps running.
        self.drop_hint = None
        if _ComposerPlaceholder is not None and well is not None:
            try:
                self.drop_hint = _ComposerPlaceholder(
                    well.body, theme, self.input, text="drag & drop text / pdf / docx / image files here to attach")
            except Exception as exc:  # noqa: BLE001
                _report_kit_failure("composer placeholder", exc)
                self.drop_hint = None
        if self.drop_hint is None:
            self.drop_hint = tk.Label(
                attach_panel, text="drag & drop text / pdf / docx / image files here to attach",
                bg=theme.bg, fg=theme.dim, font=theme_mod.FONT_CAPTION, anchor="w", justify="left",
            )
            self.drop_hint.pack(side="top", fill="x", pady=(2, 4), before=self._attach_end)

        btn_frame = tk.Frame(composer, bg=theme.bg, width=s(BTN_W), height=s(2 * BTN_H + BTN_GAP))
        btn_frame.pack_propagate(False)
        # Packed BEFORE the Text so pack gives Send/Queue and Stop their requested width first;
        # otherwise the 80-char Text request swallowed the row and Tk unmapped both buttons at the
        # default 1100x800 window (B3h). Creation order - and so Tab order - is unchanged.
        btn_frame.pack(side="right", fill="y", padx=(s(BTN_COL_GAP), 0), before=well if well is not None else self.input)
        # B7b: SEND / QUEUE / QUEUED is a kit primary button; STOP a danger_outline one that is on screen ONLY
        # while a reply or an image job runs: the existing stop_btn.configure(state=...) calls show and hide it
        # (kit ActiveOnlyButton), so idle shows SEND alone (approved r2_chat_empty).
        self.send_btn = self.stop_btn = None
        if _KitButton is not None and _ActiveOnlyButton is not None:
            try:
                self.send_btn = _KitButton(btn_frame, theme, text="SEND", kind="primary", command=self.send_message,
                                           width=s(BTN_W), height=s(BTN_H))
                self.send_btn.pack(side="top", fill="x")
                self.stop_btn = _ActiveOnlyButton(btn_frame, theme, text="STOP", kind="danger_outline",
                                                  command=self.stop_streaming, state="disabled", width=s(BTN_W),
                                                  height=s(BTN_H))
                self.stop_btn.show_with(side="top", fill="x", pady=(s(BTN_GAP), 0))
                self.stop_btn.focus_on_hide = self.input     # ADV1-05: a focused STOP that hides hands it here
            except Exception as exc:  # noqa: BLE001 - plain buttons stand in
                _report_kit_failure("composer buttons", exc)
                for b in (self.send_btn, self.stop_btn):
                    if b is not None:
                        try:
                            b.destroy()
                        except tk.TclError:
                            pass
                self.send_btn = self.stop_btn = None
        if self.send_btn is None:
            btn_frame.pack_propagate(True)
            self.send_btn = tk.Button(
                btn_frame, text="SEND", command=self.send_message,
                bg=theme.bright, fg=theme.bg, relief="flat", padx=14, font=theme_mod.FONT_LABEL,
                activebackground=theme.dim, activeforeground=theme.bg,
            )
            self.send_btn.pack(fill="x")
            self.stop_btn = tk.Button(
                btn_frame, text="STOP", command=self.stop_streaming,
                bg=theme.panel, fg=theme.danger, relief="flat", padx=14, state="disabled",
                font=theme_mod.FONT_LABEL, activebackground=theme.danger, activeforeground=theme.bg,
                disabledforeground=theme.dim,
            )
            self.stop_btn.pack(fill="x", pady=(4, 0))
        # BF-11: the Thoughts card (placed on the root window) while it is open; see _close_thoughts
        self._thoughts_backdrop = None

    def _b7b_tokens(self):
        """The kit tokens for the status row / composer / Thoughts card (the theme's own colours without the kit)."""
        theme = self.ctx.theme
        if _kit_tokens is not None:
            try:
                return _kit_tokens(theme)
            except Exception as exc:  # noqa: BLE001
                _report_kit_failure("tokens", exc)
        return {"bg": theme.bg, "panel": theme.panel, "bright": theme.bright, "dim": theme.dim, "value": theme.bright,
                "danger": theme.danger}

    def _plain_cancel_line(self, parent, text, command, ink):
        """The editing / queued line without the kit (a broken kit): today's boxed line + Cancel link."""
        theme = self.ctx.theme
        line = tk.Frame(parent, bg=theme.panel, highlightthickness=1, highlightbackground=ink)
        tk.Label(line, text=text.upper(), bg=theme.panel, fg=ink, font=theme_mod.FONT_CAPTION,
                 anchor="w").pack(side="left", padx=(8, 0), pady=5)
        link = tk.Label(line, text="Cancel", bg=theme.panel, fg=theme.dim, font=theme_mod.FONT_CAPTION_LINK,
                        cursor="hand2")
        link.pack(side="right", padx=8)
        link.bind("<Button-1>", lambda e: command())
        return line

    # ---- Screen lifecycle ----

    def legend_items(self):
        """B2 key legend. Enter is the composer's own binding (_on_enter): it sends, queues while the
        Send button reads Queue (a reply streaming / every slot busy), and only says "already queued"
        while it reads Queued. The send-state code repaints the legend when that verb changes
        (_sync_legend_verb, B2 fix ADV1-06). Esc / Ctrl+N are the router's global keys."""
        enter = self._legend_verb = self._enter_verb()
        if getattr(self, "_legend_focus_kind", None):
            # document tool P6: the keyboard focus is on a document card or an image in the transcript
            return [("ENTER", "open"), ("SHIFT+F10", "menu"), ("ESC", "back"), ("CTRL+N", "new chat")]
        return [("ENTER", enter), ("SHIFT+ENTER", "newline"), ("ESC", "back"), ("CTRL+N", "new chat")]

    def _set_legend_focus(self, kind):
        """The chat legend reads 'ENTER open · SHIFT+F10 menu' while a card / an image has the keyboard focus."""
        if getattr(self, "_legend_focus_kind", None) == kind:
            return
        self._legend_focus_kind = kind
        if not self.is_visible():
            return
        refresh = getattr(self.ctx, "refresh_legend", None)
        if refresh is not None:
            try:
                refresh()
            except Exception:  # noqa: BLE001
                pass

    def _enter_verb(self):
        try:
            word = str(self.send_btn.cget("text")).replace(" ", "").upper()
        except (AttributeError, tk.TclError):
            word = ""
        if word == "QUEUED":
            return "queued"
        return "queue" if word.startswith("QUEUE") else "send"

    def _sync_legend_verb(self):
        """Repaint the shell legend when Enter's verb changed while Chat is on screen (no-op otherwise:
        the router reads legend_items() on every show)."""
        if self._enter_verb() == getattr(self, "_legend_verb", None) or not self.is_visible():
            return
        refresh = getattr(self.ctx, "refresh_legend", None)
        if refresh is not None:
            refresh()

    def on_show(self):
        self._refresh_datetime()
        # load_conversation() itself now safely handles the case where a
        # reply is still streaming for this conversation (see its own
        # comment) - no separate guard needed here. It also refreshes the
        # sidebar, but only when a conversation is actually loaded - refresh
        # it unconditionally here too so the list still populates the very
        # first time this screen is shown with nothing open yet.
        if self.conv_id is not None:
            if not self._skip_show_reload:
                self.load_conversation(self.conv_id)
        else:
            self._refresh_chat_list()
        self.input.focus_set()
        self._refresh_max_parallel_chats_cache()

    def skip_next_show_reload(self):
        """Called by the router right before it shows this screen to open a
        *specific* conversation (MainWindow._go_to_chat): on_show() would
        otherwise reload whichever conversation was open before, a full
        round trip thrown away the instant the real one loads."""
        self._skip_show_reload = True

    def clear_skip_show_reload(self):
        """Always paired with skip_next_show_reload() - the router may not
        actually call on_show() (it doesn't when this screen is already
        showing), and a flag left set would silently suppress the *next*
        genuine return to this screen's refresh."""
        self._skip_show_reload = False

    def _refresh_max_parallel_chats_cache(self):
        # Worker thread + queue, never a direct self.api call here - same
        # rule as every other network call in this file, this would block
        # the Tk main thread otherwise.
        q = queue.Queue()

        def worker():
            # Always puts exactly one item - None on failure (a sentinel,
            # not a real value) - so the poller below has a guaranteed exit
            # instead of looping forever waiting on a queue nothing was ever
            # put onto.
            try:
                q.put(self.api.get_settings().get("max_parallel_chats", 1))
            except ApiError:
                q.put(None)

        threading.Thread(target=worker, daemon=True).start()
        self.after(50, self._poll_max_parallel_chats_queue, q)

    def _poll_max_parallel_chats_queue(self, q):
        try:
            value = q.get_nowait()
        except queue.Empty:
            self.after(50, self._poll_max_parallel_chats_queue, q)
            return
        if value is not None:
            self._cached_max_parallel_chats = max(1, int(value))
        # else: keep whatever was cached before - stale is fine, wrong isn't

    def on_hide(self):
        super().on_hide()
        self._close_thoughts()      # BF-11: the Thoughts card is on the root window; it goes with Chat
        self._close_document_viewer()       # document tool P3: the viewer overlay is on the root window too
        self._close_image_viewer()          # document tool P4: so is the image viewer
        # Navigating away is the most common way an unsent draft stops being
        # touched, and the debounce timer keeps running while another screen
        # is up - but the user could shut down from there, so queue it now.
        # Queue only (wait=False): the blocking flush in _do_shutdown/_on_close
        # waits for whatever is still in flight, and a wait here stalled the Tk
        # thread on every navigation while the backend was slow.
        self.flush_draft(wait=False)
        clear_prompt_slot(self.chat_list_prompt_slot)

    # ---- chat-list sidebar ----

    def _refresh_chat_list(self):
        # Two or three API round trips - fetched off the Tk thread (this runs on
        # every screen show and conversation load, and during a model load on a
        # hard disk each one can take most of a second). Best-effort exactly as
        # before: a failed refresh just leaves the previous list showing. A newer
        # refresh supersedes one still in flight, so a slow old response can
        # never overwrite a newer list.
        project_id = self._current_project_id

        def fetch():
            conversations = self.api.list_conversations()
            if project_id is not None:
                # Never foldered, so there's nothing to fetch/group by folder.
                try:
                    project_name = self.api.get_project(project_id).get("name") or "Project"
                except Exception:
                    project_name = None  # keep whatever name was cached before - stale is fine, missing isn't
                return conversations, [], project_name
            return conversations, self.api.list_folders(), None

        def apply(result):
            conversations, folders, project_name = result
            if project_id != self._current_project_id:
                return  # the conversation on screen changed kind meanwhile; its own refresh is coming
            if project_id is not None:
                # Sibling chats in the same project only - same filter
                # ProjectsScreen._reload_detail uses.
                self._chat_list_folders = []
                self._chat_list_conversations = [c for c in conversations if c.get("project_id") == project_id]
                if project_name:
                    self._current_project_name = project_name
            else:
                # Project conversations are managed from the Projects screen, same
                # scope boundary the Chats screen's "All" view already uses.
                self._chat_list_folders = folders
                self._chat_list_conversations = [c for c in conversations if not c.get("project_id")]
            self._render_chat_list_rows()

        self._chat_list_loader.start(fetch, apply)

    def _chat_status_symbol(self, conv_id):
        """(mark, is_animated) for the sidebar's per-conversation status mark:
        "generating" (the steady 5x10 block), "unseen" (the hollow 7x7 square)
        or "" (idle: nothing). is_animated tells _render_chat_list_rows() to
        register the mark with _chat_status_labels (B10 blinks those)."""
        if conv_id in self._streams or (self._image_gen and self._image_gen["conv_id"] == conv_id):
            return SPINNER_FRAMES[self._spinner_frame % len(SPINNER_FRAMES)], True
        if conv_id in self._unseen_conv_ids:
            return STATUS_SYMBOL_UNSEEN, False
        return STATUS_SYMBOL_IDLE, False

    def _tick_chat_list_spinner(self):
        """B7a: no timer calls this any more - the generating mark is a steady
        block (its 150 ms after() loop is gone). It just re-applies the marks of
        the rows whose reply is generating; B10 drives the blink through fx.clock."""
        for conv_id, label in list(self._chat_status_labels.items()):
            rec = self._sb_recs.get(str(label))
            symbol, animated = self._chat_status_symbol(conv_id)
            if rec is not None and animated:
                self._paint_sidebar_mark(rec, symbol)

    def destroy(self):
        # The spinner above reschedules itself unconditionally and the draft
        # debounce may be mid-countdown; either would outlive the widget and
        # come back as an "invalid command name" Tcl background error (see
        # MainWindow.destroy, and cancel_pending_after_callbacks() - reached
        # via super() - which sweeps up whichever short-lived poll loops
        # happen to be in flight alongside these two).
        # getattr, not attribute access: a ChatView whose __init__ raised
        # partway still gets destroy()d when its parent frame goes away, and
        # neither id would be set yet (same idiom as MainWindow.destroy).
        for attr in ("_spinner_after_id", "_draft_after_id"):
            after_id = getattr(self, attr, None)
            if after_id is not None:
                try:
                    self.after_cancel(after_id)
                except tk.TclError:
                    pass
            setattr(self, attr, None)
        super().destroy()

    def _render_chat_list_rows(self):
        """Sectioned by folder - named folders first (server order, same as
        the Chats screen's chip order), an "Unfiled" section last, matching
        ProjectsScreen's "named groups then Ungrouped" convention. Folders
        always get a header (even with zero chats currently in them) so
        their per-header "+" stays reachable to seed the first chat; Unfiled
        is only shown as its own section once there's at least one other
        section to distinguish it from (a flat unfoldered list needs no
        label).

        B7a: unboxed rows and headings (see _render_chat_list_header / _row).
        Keyboard focus inside the sidebar survives the redraw, and the header
        follows the open conversation's title as the list reports it (a rename
        from the sidebar, the AI title after the first reply).

        B7A-H2: a redraw that changes nothing structural - the open chat, a status mark, the header title (a send,
        a stream or an image job starting or ending, opening a chat whose list is unchanged) - repaints the rows
        that exist instead of remaking every row and heading (2.3 s at 500 chats, on each of those)."""
        shape = self._chat_list_shape()
        if shape == getattr(self, "_sb_shape", None) and self._sb_recs:
            self._repaint_chat_list_status()
            return
        refocus = self._sidebar_focus_key()
        for child in self.chat_list_rows.winfo_children():
            child.destroy()
        self._chat_status_labels = {}
        self._sb_recs = {}
        self._sb_groups = {}

        if self._current_project_id is not None:
            # Flat list of this project's own chats only - no folder
            # sectioning (project chats are never foldered). Reuses the
            # same header widget as the "Unfiled" section below: passing
            # folder_id=None already suppresses its add-button and menu.
            self._render_chat_list_header(self._current_project_name or "Project", None)
            for conv in self._chat_list_conversations:
                self._render_chat_list_row(conv)
        else:
            known_folder_ids = {folder["id"] for folder in self._chat_list_folders}
            by_folder = {}
            unfiled = []
            for conv in self._chat_list_conversations:
                folder_id = conv.get("folder_id")
                # A folder_id that doesn't match any currently-known folder
                # (e.g. briefly stale cache) falls back to Unfiled rather than
                # silently vanishing from the sidebar - it'd otherwise land in
                # a by_folder bucket that's never iterated below.
                if folder_id is not None and folder_id in known_folder_ids:
                    by_folder.setdefault(folder_id, []).append(conv)
                else:
                    unfiled.append(conv)

            for folder in self._chat_list_folders:
                self._render_chat_list_header(folder["name"], folder["id"])
                for conv in by_folder.get(folder["id"], []):
                    self._render_chat_list_row(conv, group=folder["id"])

            if unfiled and self._chat_list_folders:
                self._render_chat_list_header("Unfiled", None)
            for conv in unfiled:
                self._render_chat_list_row(conv)

        if self.conv_id is not None:
            for conv in self._chat_list_conversations:
                if conv.get("id") == self.conv_id:
                    self._set_header_title(conv.get("title"))
                    break
        self._sb_shape = shape
        self._sidebar_refocus(refocus)

    def _chat_list_shape(self):
        """What a full redraw would draw, as a hashable key (B7A-H2): the project's id and name or the folders' (id,
        name), every conversation's (id, title, folder) in order, and the sidebar's tokens (a palette change redraws).
        The open chat, the status marks and the focus are not part of it: _repaint_chat_list_status paints those."""
        t = self._sidebar_tokens()
        convs = tuple((c.get("id"), c.get("title"), c.get("folder_id")) for c in self._chat_list_conversations)
        palette = (t.get("bg"), t.get("bright"), t.get("value"))
        if self._current_project_id is not None:
            return ("project", self._current_project_id, self._current_project_name, convs, palette)
        return ("folders", tuple((f.get("id"), f.get("name")) for f in self._chat_list_folders), convs, palette)

    def _repaint_chat_list_status(self):
        """Repaint, on the rows that exist, everything a redraw used to remake every row for (B7A-H2): the open
        chat's inverse fill and ink (_paint_sidebar_row and the two labels), each row's status mark (and the
        _chat_status_labels B10 blinks), the row's conversation record for its menu, then the header's title from
        the list, as the full redraw does. Nothing is destroyed or made, so the keyboard focus stays where it is."""
        t = self._sidebar_tokens()
        by_id = {c.get("id"): c for c in self._chat_list_conversations}
        self._chat_status_labels = {}
        seen = set()
        for rec in list(self._sb_recs.values()):
            if rec["kind"] != "row" or id(rec) in seen:
                continue
            seen.add(id(rec))
            conv_id = rec["conv_id"]
            conv = by_id.get(conv_id)
            if conv is not None:
                rec["conv"] = conv
            active = conv_id == self.conv_id
            if active != rec["active"]:
                rec["active"] = active
                rec["fill"] = t["bright"] if active else t["bg"]
                rec["ink"] = t["bg"] if active else t["value"]
                try:
                    rec["status"].configure(bg=rec["fill"])
                    rec["title"].configure(bg=rec["fill"], fg=rec["ink"])
                except tk.TclError:
                    pass
                self._paint_sidebar_row(rec)
                rec["symbol"] = None                      # the mark is drawn in the row's ink: repainted below
            symbol, animated = self._chat_status_symbol(conv_id)
            if animated:
                self._chat_status_labels[conv_id] = rec["status"]
            if rec.get("symbol") != symbol:
                rec["symbol"] = symbol
                self._paint_sidebar_mark(rec, symbol)
        if self.conv_id is not None:
            for conv in self._chat_list_conversations:
                if conv.get("id") == self.conv_id:
                    self._set_header_title(conv.get("title"))
                    break

    def _render_chat_list_header(self, name, folder_id):
        """A folder heading (B7a, approved r2_chat): the folder's name as the user typed it (OI-14: mixed
        case), C9 value at x 58, no rule; a later heading starts 34 px below the group before it. Children,
        in order: the name (a tk.Label; a name too long for the row is the kit's eliding line, whole in
        cget and its hover tip), the slot that keeps the "+" room, so the heading's size never moves on
        reveal, and the folder's "+" (reveal-only: packed into the slot while the heading or one of its
        rows is hovered or focused, pack_forget otherwise - no timer, never destroyed). Unfiled and the project
        heading have no "+" (and no menu). A folder heading takes the keyboard focus (an inverse name chip)
        and opens its menu on right-click, Shift+F10 and the App key (BF-12).
        Creation order is stacking order (and winfo_children order): name, slot, "+". The "+" is packed
        INTO the slot, a sibling, so it must be created after it: a "+" created first sits below the slot's
        opaque Frame and would be mapped but painted over (the B7a probe measured it)."""
        theme = self.ctx.theme
        t, s = self._sidebar_tokens(), self._sb_s
        top = 0 if not self.chat_list_rows.winfo_children() else s(SIDEBAR_HEAD_GAP)
        header = tk.Frame(self.chat_list_rows, bg=t["bg"], highlightthickness=0, bd=0,
                          takefocus=1 if folder_id is not None else 0)
        text = "" if name is None else str(name)
        pad = s(SIDEBAR_HEAD_PAD)
        plus_w = self._sb_text_w(("Consolas", 9, "bold"), "+") + 2 * s(6)
        plus_h = self._sb_linespace(("Consolas", 9, "bold")) + 2 * s(4)
        room = s(SIDEBAR_W - SIDEBAR_LIST_X) - plus_w - 2 * pad
        long_name = _StatusLine is not None and ("\n" in text or self._sb_text_w(C9, text) > room)
        if long_name:
            name_label = _StatusLine(header, theme, text=text, bg=t["bg"], fg=t["value"], font=C9, bd=0,
                                     padx=pad, pady=0)
        else:
            name_label = tk.Label(header, text=text, bg=t["bg"], fg=t["value"], font=C9, bd=0, padx=pad, pady=0,
                                  anchor="w")
        slot = tk.Frame(header, bg=t["bg"], width=plus_w, height=plus_h, highlightthickness=0, bd=0)
        slot.pack_propagate(False)
        add_btn = None
        if folder_id is not None:
            command = (lambda fid=folder_id: self._on_new_chat_in_folder(fid))
            if _TextAction is not None:
                try:
                    add_btn = _TextAction(header, theme, text="+", command=command, ground=t["bg"])
                except Exception as exc:  # noqa: BLE001
                    _report_kit_failure("folder +", exc)
            if add_btn is None:
                add_btn = tk.Label(header, text="+", bg=t["bg"], fg=t["value"], font=("Consolas", 9, "bold"),
                                   cursor="hand2", padx=6, pady=4, takefocus=1)
                add_btn.bind("<Button-1>", lambda e, cmd=command: cmd())
                add_btn.bind("<Return>", lambda e, cmd=command: cmd())
        slot.pack(side="right")                 # packed first: a long name can never squeeze it out
        name_label.pack(side="left", fill="x", expand=True)
        header.pack(fill="x", pady=(top, s(SIDEBAR_HEAD_BELOW)))

        rec = {"kind": "head", "widget": header, "name_label": name_label, "folder_id": folder_id, "name": text,
               "focus": False, "t": t}
        for w in (header, name_label, slot):
            self._sb_recs[str(w)] = rec
        self._sb_tags(header, self._sb_tag)
        for w in (name_label, slot):
            self._sb_tags(w, self._sb_part)
        if folder_id is None:
            return
        self._sb_groups[folder_id] = {"plus": add_btn, "slot": slot, "hover": set(), "members": {str(header)},
                                      "shown": False}
        if add_btn is not None:
            prec = {"kind": "plus", "widget": add_btn, "head": rec, "folder_id": folder_id}
            self._sb_recs[str(add_btn)] = prec
            self._sb_tags(add_btn, self._sb_tag)
            self._sb_groups[folder_id]["members"].add(str(add_btn))
        # Right-click / Shift+F10 / App: the folder's own menu (Rename folder / Delete folder) through the
        # shared menu, built only when opened (context_menu.py); string scripts, so no callback per heading
        menu_script = 'if {"[%s menu %%W %%X %%Y]" == "break"} break' % self._sb_cmd
        key_script = 'if {"[%s kmenu %%W]" == "break"} break' % self._sb_cmd
        for w in (header, name_label):
            w.bind("<Button-3>", menu_script)
        header.bind("<Shift-F10>", key_script)
        header.bind("<App>", key_script)

    def _render_chat_list_row(self, conv, group=None):
        """One chat row (B7a, approved r2_chat list_row): unboxed, the title in C10 value at x 66, wrapped
        (D-27 #14: never cut), a status mark at the right edge (x 242: nothing idle, a hollow 7x7 square
        unseen, a steady 5x10 block generating); the open chat is inverse (bright fill, bg ink); hover = a
        1 px outline, keyboard focus = a 2 px outline (on the open chat: its edge, a bg band, then the fill).
        The 2-child shape is kept: the status mark first (packed right), the title second (a tk.Label).
        Click / Return / space open it; right-click, Shift+F10 and the App key open its menu (BF-12)."""
        t, s = self._sidebar_tokens(), self._sb_s
        conv_id = conv["id"]
        title = conv.get("title") or "New chat"
        is_active = conv_id == self.conv_id
        fill = t["bright"] if is_active else t["bg"]
        ink = t["bg"] if is_active else t["value"]
        row = tk.Frame(self.chat_list_rows, bg=fill, takefocus=1, bd=0)
        symbol, is_animated = self._chat_status_symbol(conv_id)
        # The mark's cell (one image: a Label ignores padx / pady around an image) is 4 + 7 + 4 px wide and as
        # tall as a one-line row's inside, so it sets the 28 px row whatever the title font's line height;
        # a wrapped title grows the row and the mark stays centred on it
        cell = (2 * s(SIDEBAR_TEXT_PAD) + MARK_CELL_W * self._sb_mark_factor(),
                s(SIDEBAR_ROW_H) - 2 * s(SIDEBAR_INSET))
        status_label = tk.Label(row, bg=fill, bd=0, padx=0, pady=0, text="")
        status_label.pack(side="right", fill="y")
        if is_animated:
            self._chat_status_labels[conv_id] = status_label
        ls = self._sb_linespace(C10)
        label = tk.Label(
            row, text=title, bg=fill, fg=ink, font=C10, anchor="w", justify="left", bd=0,
            wraplength=s(SIDEBAR_WRAP), padx=s(SIDEBAR_TEXT_PAD),
            pady=max(0, (s(SIDEBAR_ROW_H) - 2 * s(SIDEBAR_INSET) - ls) // 2),
        )
        label.pack(side="left", fill="both", expand=True)
        row.pack(fill="x", padx=(s(SIDEBAR_HEAD_PAD), 0), pady=(0, s(SIDEBAR_ROW_GAP)))

        rec = {"kind": "row", "widget": row, "status": status_label, "title": label, "conv": conv,
               "conv_id": conv_id, "active": is_active, "hover": False, "focus": False, "group": group,
               "fill": fill, "ink": ink, "t": t, "cell": cell, "symbol": symbol}
        for w in (row, status_label, label):
            self._sb_recs[str(w)] = rec
        self._sb_tags(row, self._sb_tag)
        for w in (status_label, label):
            self._sb_tags(w, self._sb_part)
        if group is not None and group in self._sb_groups:
            self._sb_groups[group]["members"].add(str(row))
        self._paint_sidebar_row(rec)
        self._paint_sidebar_mark(rec, symbol)
        # Described here, built only when the row's menu is actually opened (a menu - and its "Move"
        # submenu - per row, per redraw, was most of the cost of drawing this sidebar; see context_menu.py).
        # The scripts call the one registered command: no Python callback per row.
        menu_script = 'if {"[%s menu %%W %%X %%Y]" == "break"} break' % self._sb_cmd
        key_script = 'if {"[%s kmenu %%W]" == "break"} break' % self._sb_cmd
        for w in (row, status_label, label):
            w.bind("<Button-3>", menu_script)
        row.bind("<Shift-F10>", key_script)
        row.bind("<App>", key_script)

    # ---- chat-list sidebar: look and events (B7a) ----

    def _sb_s(self, n):
        """n approved pixels at this screen's display ratio."""
        return int(round(n * getattr(self, "_sb_ratio", 1.0)))

    def _sb_mark_factor(self):
        """The kit's integer image factor (the mark is MARK_CELL_W x it wide)."""
        try:
            from .kit import scale_factor
            return scale_factor(self)
        except Exception:  # noqa: BLE001
            return 1

    def _sb_linespace(self, font):
        try:
            return int(self.tk.call("font", "metrics", font, "-linespace"))
        except tk.TclError:
            return 15

    def _sb_text_w(self, font, text):
        try:
            return int(self.tk.call("font", "measure", font, text))
        except tk.TclError:
            return 7 * len(text)

    def _sidebar_tokens(self):
        theme = self.ctx.theme
        if _kit_tokens is not None:
            try:
                return _kit_tokens(theme)
            except Exception:  # noqa: BLE001
                pass
        get = (lambda k, d: getattr(theme, k, d))
        return {"bg": get("bg", "#000000"), "bright": get("bright", "#FFB000"), "dim": get("dim", "#6A4A00"),
                "value": get("value", get("text", "#C08000")), "panel": get("panel", "#111111")}

    @staticmethod
    def _sb_tags(widget, tag):
        try:
            tags = widget.bindtags()
            widget.bindtags((tags[0], tag) + tuple(tags[1:]))
        except tk.TclError:
            pass

    def _paint_sidebar_row(self, rec):
        """rest: no box; hover: 1 px bright outline; focus: 2 px bright outline (on the open chat's bright
        fill: the edge, a 2 px bg band, then the fill). Ring + band always total SIDEBAR_INSET, so no state
        moves the row's text."""
        t = rec["t"]
        inset = self._sb_s(SIDEBAR_INSET)
        fill = rec["fill"]
        if rec["focus"]:
            ring, ring_w, band = t["bright"], self._sb_s(2), t["bg"]
        elif rec["hover"] and not rec["active"]:
            ring, ring_w, band = t["bright"], 1, fill
        else:
            ring, ring_w, band = fill, 1, fill
        try:
            rec["widget"].configure(bg=band, highlightthickness=ring_w, highlightbackground=ring, highlightcolor=ring,
                                    padx=inset - ring_w, pady=inset - ring_w)
        except tk.TclError:
            pass

    def _paint_sidebar_mark(self, rec, symbol):
        """The row's status mark in its title ink (kit.chat mark images; a text stand-in without the kit)."""
        label = rec["status"]
        try:
            if _mark_images is not None:
                images = _mark_images(label, rec["ink"], cell=rec["cell"], pad_right=self._sb_s(SIDEBAR_TEXT_PAD))
                label.configure(image=images[symbol or ""], text="")
            else:
                label.configure(text={"unseen": "o", "generating": "#"}.get(symbol, " "), fg=rec["ink"],
                                font=C9)
        except (tk.TclError, KeyError):
            pass

    def _paint_sidebar_heading(self, rec):
        """A focused folder heading: its name as an inverse chip (the B5 focus-chip grammar)."""
        t = rec["t"]
        focus = rec["focus"]
        try:
            rec["name_label"].configure(bg=t["bright"] if focus else t["bg"], fg=t["bg"] if focus else t["value"])
        except tk.TclError:
            pass

    def _sidebar_reveal(self, folder_id):
        """Show the folder's "+" while its heading, the "+" or one of its rows is hovered or holds the
        focus; hide it otherwise (pack / pack_forget; never destroyed; no timer)."""
        g = self._sb_groups.get(folder_id)
        if g is None or g["plus"] is None:
            return
        try:
            focus = self.focus_get()
        except (tk.TclError, KeyError):
            focus = None
        show = bool(g["hover"]) or (focus is not None and str(focus) in g["members"])
        if show == g["shown"]:
            return
        g["shown"] = show
        try:
            if show:
                g["plus"].pack(in_=g["slot"], fill="both", expand=True)
            else:
                g["plus"].pack_forget()
        except tk.TclError:
            pass

    def _sidebar_see(self, widget):
        """Scroll the sidebar so a row / heading that took the keyboard focus is wholly in view."""
        cv = self.chat_list_rows.master
        try:
            total = self.chat_list_rows.winfo_height()
            ch = cv.winfo_height()
            if total <= ch or total <= 1:
                return
            top = widget.winfo_y()
            bot = top + widget.winfo_height()
            y0 = cv.canvasy(0)
            if top < y0:
                cv.yview_moveto(top / float(total))
            elif bot > y0 + ch:
                cv.yview_moveto((bot - ch) / float(total))
        except (tk.TclError, ZeroDivisionError):
            pass

    def _sidebar_focus_key(self):
        """What in the sidebar holds the keyboard focus now, as a key that survives a redraw (or None)."""
        try:
            focus = self.focus_get()
        except (tk.TclError, KeyError):
            return None
        rec = self._sb_recs.get(str(focus)) if focus is not None else None
        if rec is None:
            return None
        if rec["kind"] == "row":
            return ("row", rec["conv_id"])
        return ("head", rec.get("folder_id"))

    def _sidebar_refocus(self, key):
        if key is None:
            return
        for rec in self._sb_recs.values():
            if (rec["kind"] == "row" and key == ("row", rec["conv_id"])) or (
                    rec["kind"] == "head" and key == ("head", rec["folder_id"])):
                try:
                    rec["widget"].focus_set()
                except tk.TclError:
                    pass
                return

    def _sidebar_row_items(self, conv):
        """The row's menu: Rename / Move (a cascade: Unfiled + every OTHER folder) / Delete."""
        conv_id = conv["id"]
        title = conv.get("title") or "New chat"
        items = [("Rename", lambda cid=conv_id, t=title: self._rename_chat_from_sidebar(cid, t))]
        current_folder_id = conv.get("folder_id")
        # "Move to: X" per folder used to flatten into the top-level menu -
        # compressed into a single cascading "Move" submenu instead, same
        # change as ChatsScreen's _context_items_for.
        move_targets = []
        if current_folder_id is not None:
            move_targets.append(("Unfiled", None))
        for folder in self._chat_list_folders:
            if folder["id"] != current_folder_id:
                move_targets.append((folder["name"], folder["id"]))
        if move_targets:
            items.append((
                "Move",
                [
                    (move_label, lambda cid=conv_id, fid=fid: self._move_chat_from_sidebar(cid, fid))
                    for move_label, fid in move_targets
                ],
            ))
        items.append(("Delete", lambda cid=conv_id, t=title: self._delete_chat_from_sidebar(cid, t)))
        return items

    def _sidebar_folder_items(self, folder_id, name):
        """A folder heading's menu (test_shared_menu pins exactly these two; no "New chat here": the
        heading's "+" keeps that one click away)."""
        return [
            ("Rename folder", lambda fid=folder_id, n=name: self._rename_folder_from_sidebar(fid, n)),
            ("Delete folder", lambda fid=folder_id, n=name: self._delete_folder_from_sidebar(fid, n)),
        ]

    def _on_sidebar_event(self, action, path, *args):
        """The one registered command behind every sidebar row / heading binding (see __init__)."""
        if action == "wheel":
            try:
                delta = int(float(args[0]))
                if delta:
                    self.chat_list_rows.master.yview_scroll(int(-1 * (delta / 120)) or (-1 if delta > 0 else 1),
                                                            "units")
            except (IndexError, ValueError, tk.TclError):
                pass
            return None
        rec = self._sb_recs.get(path)
        if rec is None:
            return None
        kind = rec["kind"]
        if kind == "plus":
            if action in ("enter", "leave", "focusin", "focusout"):
                self._sidebar_track(rec["folder_id"], path, action, args)
            return None
        if action in ("activate", "click"):
            if kind == "row":
                cid = rec["conv_id"]
                if cid != self.conv_id:
                    self.load_conversation(cid)
                return "break" if action == "activate" else None
            return None
        if action in ("enter", "leave"):
            if kind == "row":
                # a move onto the row's own title / mark (NotifyInferior) keeps the hover
                if action == "enter":
                    rec["hover"] = True
                elif not (args and args[0] == "NotifyInferior"):
                    rec["hover"] = False
                self._paint_sidebar_row(rec)
                group = rec.get("group")
            else:
                group = rec.get("folder_id")
            if group is not None:
                self._sidebar_track(group, path, action, args)
            return None
        if action in ("focusin", "focusout"):
            rec["focus"] = action == "focusin"
            if kind == "row":
                self._paint_sidebar_row(rec)
                group = rec.get("group")
            else:
                self._paint_sidebar_heading(rec)
                group = rec.get("folder_id")
            if action == "focusin":
                self._sidebar_see(rec["widget"])
            if group is not None:
                self._sidebar_reveal(group)
            return None
        if action in ("menu", "kmenu"):
            if kind == "row":
                items = self._sidebar_row_items(rec["conv"])
            elif rec.get("folder_id") is not None:
                items = self._sidebar_folder_items(rec["folder_id"], rec["name"])
            else:
                return None
            w = rec["widget"]
            if action == "menu":
                x, y = int(args[0]), int(args[1])
            else:
                x, y = w.winfo_rootx() + self._sb_s(16), w.winfo_rooty() + w.winfo_height()
            self._sidebar_menu.popup(items, x, y)
            return "break"
        return None

    def _sidebar_track(self, folder_id, path, action, args):
        """Hover bookkeeping for a folder's reveal-only "+": a widget of the group joins the hover set on
        <Enter> and leaves it on <Leave> (except a move onto its own child, NotifyInferior)."""
        g = self._sb_groups.get(folder_id)
        if g is None:
            return
        if action == "enter":
            g["hover"].add(path)
        elif action == "leave" and not (args and args[0] == "NotifyInferior"):
            g["hover"].discard(path)
        self._sidebar_reveal(folder_id)

    def _on_new_chat_from_sidebar(self):
        if self._current_project_id is not None:
            self.start_draft(project_id=self._current_project_id)
        else:
            self.ctx.new_session()          # the router opens the draft (MainWindow._new_session)

    def _on_new_chat_in_project(self, project_id):
        self.start_draft(project_id=project_id)

    def _on_new_chat_in_folder(self, folder_id):
        self.start_draft(folder_id=folder_id)

    # ---- a draft new chat: no conversations row until its first message ----

    def start_draft(self, project_id=None, folder_id=None):
        """A new chat is a DRAFT on screen - no conversations row, no sidebar row - until its first message is sent
        (_materialise_draft_and_send creates the row then, in this project / folder). The user's report: deleting
        the open chat created a fresh empty row at once, so the list could never be emptied; every New chat / New
        session path lands here instead. An open draft is reused: its text is kept, the project / folder follow
        the call. Everything the previous chat left on screen is put away as load_conversation does."""
        where = {"project_id": project_id, "folder_id": folder_id}
        if self.conv_id is None and self._draft_where is not None:
            self._draft_where = where
            self._current_project_id = project_id
            self._refresh_chat_list()
            self.input.focus_set()
            return
        self._close_thoughts()
        if self.conv_id is not None:
            outgoing = self.input.get("1.0", "end-1c")
            self._drafts[self.conv_id] = outgoing
            self._queue_draft(self.conv_id, outgoing)
        self._editing_message_id = None
        self.editing_indicator.pack_forget()
        self.queued_indicator.pack_forget()
        self.conv_id = None
        self._loading_conv_id = None
        self._send_when_loaded = False
        self._fx_queued = None
        self._draft_where = where
        self._current_project_id = project_id
        self.transcript.clear()
        self._set_header_title(None)
        self._update_thoughts_button()
        self._staged_images = []
        self._staged_docs = {}
        self._render_staging_grid()
        self.input.delete("1.0", "end")
        self._render_conversation_files([])
        self.context_meter.hide()
        self._set_empty_state(True)
        self._sync_jump_cue()
        self._repaint_chat_list_status()
        self._sync_rail_for_current_conv()
        self._refresh_chat_list()
        self.input.focus_set()

    def _materialise_draft_and_send(self):
        """The draft's first Send: create its conversations row (off the Tk thread, in the draft's project / folder),
        adopt it as the open chat and send through the ordinary path. An empty composer creates nothing."""
        if self._creating_chat:
            return
        text = self.input.get("1.0", "end-1c").strip()
        if not text and not self._staged_images:
            return
        where = dict(self._draft_where or {})
        self._creating_chat = True

        def created(conv):
            self._creating_chat = False
            if conv.get("__not_running__") is not None:
                # the model server is not running at all (no model file, a missing DLL, its port taken): the row
                # would only have held a rolled-back turn - the ghost "New chat" of a fresh install. The text stays.
                show_toast(self, self.ctx.theme, "Can't send while the model server is not running - add or restart a "
                           "model in Settings > Model (the notice on Home says why).", kind="warning")
                return
            if self.conv_id is not None or self._draft_where != where:
                return                  # the user opened something else while the row was being made
            self.conv_id = conv["id"]
            self._draft_where = None
            self._drafts[self.conv_id] = ""
            self._current_project_id = conv.get("project_id")
            self._thoughts_counts[self.conv_id] = 0
            self._set_header_title(conv.get("title"))
            self._refresh_chat_list()
            self.send_message()

        def failed(error):
            self._creating_chat = False
            show_toast(self, self.ctx.theme, f"Could not create a new chat: {error}", kind="error")

        def work():
            # the row is made only once the model server can answer: /api/health "error" means it is not running
            # (a fresh copy with no model) and the first message would come straight back as a rolled-back 502
            try:
                health = self.api.health() or {}
            except Exception:  # noqa: BLE001 - an unreachable backend is reported by create_conversation below
                health = {}
            if isinstance(health, dict) and health.get("status") == "error":
                return {"__not_running__": health.get("error") or ""}
            return self.api.create_conversation(**where)

        run_async(self, work, created, failed)

    def _rename_chat_from_sidebar(self, conv_id, current_title):
        show_inline_prompt(
            self.chat_list_prompt_slot, self.ctx.theme, "Chat name:",
            lambda name: self._do_rename_chat(conv_id, name), initial=current_title,
        )

    def _sidebar_write(self, work, error_prefix, then=None):
        """Runs a sidebar edit (rename/move/delete) off the Tk thread, then - on
        success - `then` if given, else just a sidebar refresh. On failure shows
        "<error_prefix>: <why>". Writes are round trips to a backend that can be
        slow (see async_call.py), same as the reads."""
        run_async(
            self, work, lambda _result: (then or self._refresh_chat_list)(),
            lambda e: show_toast(self, self.ctx.theme, f"{error_prefix}: {e}", kind="error"),
        )

    def _do_rename_chat(self, conv_id, name):
        self._sidebar_write(lambda: self.api.rename_conversation(conv_id, name), "Could not rename")

    def _move_chat_from_sidebar(self, conv_id, folder_id):
        self._sidebar_write(
            lambda: self.api.update_conversation(conv_id, folder_id=folder_id), "Could not move conversation"
        )

    def _rename_folder_from_sidebar(self, folder_id, current_name):
        show_inline_prompt(
            self.chat_list_prompt_slot, self.ctx.theme, "Folder name:",
            lambda name: self._do_rename_folder(folder_id, name), initial=current_name,
        )

    def _do_rename_folder(self, folder_id, name):
        self._sidebar_write(lambda: self.api.rename_folder(folder_id, name), "Could not rename folder")

    def _delete_folder_from_sidebar(self, folder_id, name):
        show_inline_confirm(
            self, self.ctx.theme, f'Delete folder "{name}"? Chats inside move to Unfiled, not deleted.',
            on_confirm=lambda: self._do_delete_folder(folder_id), confirm_label="Delete", danger=True,
        )

    def _do_delete_folder(self, folder_id):
        self._sidebar_write(lambda: self.api.delete_folder(folder_id), "Could not delete folder")

    def _delete_chat_from_sidebar(self, conv_id, title):
        show_inline_confirm(
            self, self.ctx.theme, f'Delete "{title}"? This cannot be undone.',
            on_confirm=lambda: self._do_delete_chat(conv_id), confirm_label="Delete", danger=True,
        )

    def _do_delete_chat(self, conv_id):
        def deleted():
            self._drafts.pop(conv_id, None)
            self.forget_conversation(conv_id)      # B14-H7: its queued message, if any
            if conv_id == self.conv_id:
                # the open chat is gone: a DRAFT takes its place, in the same project - never a fresh empty row (the
                # user's report: one was created at once, so the list could never be emptied)
                self.start_draft(project_id=self._current_project_id)
            else:
                self._refresh_chat_list()

        self._sidebar_write(lambda: self.api.delete_conversation(conv_id), "Could not delete", then=deleted)

    # ---- action rail ----

    def _on_jump_menu(self):
        """Posts a popup menu of every user prompt in this transcript (bare
        tk.Menu, the same context-menu mechanism list_screen.py already uses
        for row actions - natively arrow-key navigable) so a long chat can be
        jumped to a specific turn instead of only scrolling."""
        marks = self.transcript.jump_marks()
        if not marks:
            show_toast(self, self.ctx.theme, "No messages to jump to yet.", kind="warning")
            return
        theme = self.ctx.theme
        menu = tk.Menu(
            self, tearoff=0, bg=theme.panel, fg=theme.text,
            activebackground=theme.bright, activeforeground=theme.bg,
            font=theme_mod.FONT_CAPTION,
        )
        for i, (mark_name, preview) in enumerate(marks, start=1):
            menu.add_command(
                label=f"{i:02d}  {preview}",
                command=lambda m=mark_name: self.transcript.scroll_to_mark(m),
            )
        menu.tk_popup(self.jump_btn.winfo_rootx(), self.jump_btn.winfo_rooty() + self.jump_btn.winfo_height())

    # ---- saved "thinking" history (see _thoughts_counts' own comment for
    # why this exists separately from the transcript widget) ----

    # ---- batch 32: the JUMP TO LATEST cue and the Thoughts card's in-flight caption ----

    def _sync_jump_cue(self):
        """Shown only while a reply streams in the open chat AND the transcript's last line is off screen; placed
        8 px in from the transcript's bottom-right corner (inside it: never over the composer). Never raises."""
        cue = getattr(self, "jump_latest", None)
        if cue is None:
            return
        try:
            want = (self.conv_id is not None and self.conv_id in self._streams
                    and not self.transcript._at_end())
            shown = bool(cue.winfo_manager())
            if want and not shown:
                s = self._sb_s
                cue.place(in_=self.transcript, relx=1.0, rely=1.0, x=-s(8), y=-s(8), anchor="se")
                cue.lift()
            elif shown and not want:
                cue.place_forget()
        except tk.TclError:
            pass

    def _on_jump_latest(self):
        try:
            self.transcript._follow_end(True)
        except tk.TclError:
            return
        self._sync_jump_cue()

    def _sync_thoughts_inflight(self):
        """The Thoughts card's caption for a turn still thinking goes away the moment that thinking finishes (or
        the stream ends); the card's saved entries are untouched."""
        note = getattr(self, "_thoughts_inflight", None)
        if note is None:
            return
        label, conv_id = note
        st = self._streams.get(conv_id)
        if st is None or st.thinking_finished or not (st.thinking_started or st.thinking_raw):
            self._thoughts_inflight = None
            try:
                label.destroy()
            except tk.TclError:
                pass

    def _notify_activity(self):
        """Batch 34: tell the window the activity snapshot changed (queues, streams, image jobs). Never raises."""
        cb = getattr(self, "on_activity_changed", None)
        if cb is not None:
            try:
                cb()
            except Exception:  # noqa: BLE001
                pass

    def _update_thoughts_button(self):
        n = self._thoughts_counts.get(self.conv_id, 0)
        label = f"Thoughts ({n})" if n else "Thoughts"
        self.thoughts_btn.config(text=label.upper())       # B7a: a header text action (kit Override rule)

    # ---- the document tool (tests/DOCUMENT_TOOL_SPEC.md P2 / P3 / the file side of P5) ----

    def _fx_level(self):
        try:
            return self.ctx.fx.level()
        except Exception:  # noqa: BLE001
            return "off"

    def _record_document_event(self, conv_id, state, frame, offset):
        """One streamed `document` frame into state.documents (merged by index) - whether or not the chat is on
        screen - and the pull-down told."""
        idx = frame.get("index", 0)
        entry = next((d for d in state.documents if d.get("index") == idx), None)
        if entry is None:
            entry = {"index": idx, "state": "creating", "title": None, "offset": offset, "since": time.time()}
            state.documents.append(entry)
        if frame.get("state") == "creating":
            if frame.get("title"):
                entry["title"] = frame.get("title")
        elif frame.get("state") == "done":
            entry.update({k: frame.get(k) for k in ("id", "title", "filename", "bytes", "version")})
            entry["state"] = "done"
        elif frame.get("state") == "failed":
            entry["state"] = "failed"
            entry["reason"] = frame.get("reason")
        self._notify_activity()
        return entry

    def _feed_with_documents(self, state, pieces, doc_events):
        """This tick's text with its document frames rendered at their places (the row / card between the text that
        came before the call and the text after it)."""
        text = "".join(pieces)
        pos = 0
        for rel, frame in doc_events:
            rel = max(pos, min(len(text), rel))
            if rel > pos:
                self.transcript.feed(text[pos:rel])
                pos = rel
            self._render_document_frame(state, frame)
        if pos < len(text):
            self.transcript.feed(text[pos:])

    def _replay_documents(self, state, raw):
        """The in-flight reply redrawn from its raw text with its rows / cards back in place (the chat was reopened)."""
        state.doc_widgets = {}
        pos = 0
        for d in sorted(state.documents, key=lambda d: d.get("offset", 0)):
            offset = max(pos, min(len(raw), int(d.get("offset") or 0)))
            if offset > pos:
                self.transcript.feed(raw[pos:offset])
                pos = offset
            frame = dict(d)
            if d.get("state") == "creating":
                frame = {"state": "creating", "index": d.get("index", 0), "title": d.get("title")}
            self._render_document_frame(state, frame)
        if pos < len(raw):
            self.transcript.feed(raw[pos:])

    def _render_document_frame(self, state, frame):
        """The creating row / the card / the failed line for one frame, in the displayed transcript."""
        reason = " ".join(str(frame.get("reason") or "").split()) or "the model's call could not be used"
        if _CreatingRow is None:
            if frame.get("state") == "failed":
                show_toast(self, self.ctx.theme, f"Could not create the document - {reason}", kind="error")
            return
        theme = self.ctx.theme
        idx = frame.get("index", 0)
        current = state.doc_widgets.get(idx)
        kind = frame.get("state")
        if kind == "creating":
            if current is None:
                row = _CreatingRow(self.transcript, theme, frame.get("title"), motion_level=self._fx_level())
                self._track_document_widget(row)
                self.transcript.embed_widget_inline(row)
                state.doc_widgets[idx] = row
            elif isinstance(current, _CreatingRow):
                current.set_title(frame.get("title"))
            return
        if kind == "done":
            doc = {k: frame.get(k) for k in ("id", "title", "filename", "bytes", "version")}
            doc["offset"] = next((d.get("offset", 0) for d in state.documents if d.get("index") == idx), 0)
            widget = self._make_document_card(doc)
        elif kind == "failed":
            widget = _FailedLine(self.transcript, theme, reason)
            self._track_document_widget(widget)              # P6: wraps to the transcript's width, never cut
            show_toast(self, theme, f"Could not create the document - {reason}", kind="error")
        else:
            return
        if isinstance(current, _CreatingRow):
            current.stop()
        if current is None or not self.transcript.replace_embedded_widget(current, widget):
            self.transcript.embed_widget_inline(widget)
        state.doc_widgets[idx] = widget

    def _document_inserts(self, msg):
        """(offset, card) pairs for a message's live document cards (the conversation GET / the version switch)."""
        docs = msg.get("documents") or [] if isinstance(msg, dict) else []
        if _DocumentCard is None or not docs:
            return []
        return [(int(d.get("offset") or 0), self._make_document_card(d)) for d in docs]

    def _make_document_card(self, doc):
        card = _DocumentCard(self.transcript, self.ctx.theme, doc, on_open=self._on_document_open,
                             on_save_as=lambda d, w: self._save_document_as(d), on_download=lambda d, w: self._download_document(d),
                             on_menu=self._on_document_menu, on_focus=lambda focused: self._set_legend_focus("card" if focused else None))
        self._track_document_widget(card)
        return card

    def _track_document_widget(self, widget):
        """A row / card embedded in the transcript follows the transcript's width (up to the card maximum)."""
        cards = getattr(self, "_document_cards", None)
        if cards is None:
            cards = self._document_cards = []
            self.transcript.bind("<Configure>", lambda e: self._fit_document_cards(), add="+")
        cards.append(widget)
        widget.set_available_width(self._document_card_room())

    def _document_card_room(self):
        try:
            width = int(self.transcript.winfo_width())
            padx = int(str(self.transcript.cget("padx")) or 0)
        except (tk.TclError, ValueError):
            return 560
        margin = int(getattr(self.transcript, "DOC_WIDGET_PADX", 8))
        return max(200, width - 2 * padx - 2 * margin - self._sb_s(24))

    def _fit_document_cards(self):
        cards = getattr(self, "_document_cards", None) or []
        live = []
        room = self._document_card_room()
        for card in cards:
            try:
                if card.winfo_exists():
                    card.set_available_width(room)
                    live.append(card)
            except tk.TclError:
                pass
        self._document_cards = live

    def _on_document_open(self, doc, opener=None):
        self._open_document(doc, opener)

    def _open_document(self, doc, opener=None, popout=False):
        """The viewer (overlay, or a pop-out window with popout=True) for a card: the content is fetched off the Tk
        thread; the card carries only the card data."""
        if _DocumentViewer is None:
            return
        doc_id = doc.get("id")
        conv_id = self.conv_id

        def done(full):
            if conv_id != self.conv_id or not self.is_visible():
                return
            merged = dict(doc, **{k: full.get(k) for k in ("title", "filename", "bytes", "version") if full.get(k) is not None})
            if popout:
                _open_document_window(self, merged, full.get("content") or "")
                return
            self._close_document_viewer()
            self._document_viewer = _DocumentViewer(self, merged, full.get("content") or "", opener=opener)

        def failed(e):
            show_toast(self, self.ctx.theme, f"Could not open the document: {e}", kind="error")

        run_async(self, lambda: self.api.get_document(doc_id), done, failed)

    def _open_document_window(self, doc, content=None):
        """NEW WINDOW: the same viewer in a Toplevel (from the overlay, with the content it already has)."""
        if _open_document_window is None:
            return
        if content is None:
            self._open_document(doc, popout=True)
            return
        _open_document_window(self, doc, content)

    def _close_document_viewer(self):
        viewer, self._document_viewer = getattr(self, "_document_viewer", None), None
        if viewer is not None:
            viewer.close()

    def _save_document_as(self, doc):
        """SAVE AS: the Windows Save dialog (default name = the document's filename, starting in the last folder
        used), then the bytes fetched and written atomically off the Tk thread; a notice says where it went."""
        if _file_ops is None:
            return
        filename = doc.get("filename") or "document.md"
        initial_dir = getattr(self, "_document_save_dir", None) or _file_ops.downloads_dir()
        try:
            path = filedialog.asksaveasfilename(
                parent=self.winfo_toplevel(), title="Save document as", initialfile=filename, initialdir=initial_dir,
                defaultextension=os.path.splitext(filename)[1] or ".md", filetypes=_file_ops.filetypes_for(filename),
            )
        except tk.TclError:
            path = ""
        if not path:
            return
        self._document_save_dir = os.path.dirname(path)
        remember = {"document_save_dir": self._document_save_dir}

        def work():
            data, _name = self.api.get_document_bytes(doc.get("id"))
            _file_ops.atomic_write_bytes(path, data)
            try:
                self.api.save_settings(remember)
            except ApiError:
                pass
            return path

        run_async(self, work, lambda p: show_toast(self, self.ctx.theme, f"Saved to {p}", kind="success"),
                  lambda e: show_toast(self, self.ctx.theme, f"Could not save {filename}: {e}", kind="error"))

    def _download_document(self, doc):
        """DOWNLOAD: straight into the user's Downloads folder (the Windows known folder), never overwriting."""
        if _file_ops is None:
            return
        filename = doc.get("filename") or "document.md"

        def work():
            data, _name = self.api.get_document_bytes(doc.get("id"))
            return _file_ops.download_bytes(filename, data)

        run_async(self, work,
                  lambda p: show_toast(self, self.ctx.theme, f"Downloaded to Downloads\\{os.path.basename(p)}", kind="success"),
                  lambda e: show_toast(self, self.ctx.theme, f"Could not save {filename}: {e}", kind="error"))

    # ---- the image viewer and the context menus (tests/DOCUMENT_TOOL_SPEC.md P4 / P5) ----

    def _image_ref_of(self, photo):
        return getattr(photo, "_image_ref", None)

    def _on_image_click(self, photo, opener=None):
        """A click on a transcript image: the full-size bytes are fetched off the Tk thread, decoded, and shown in
        the full-window overlay (the transcript's own PhotoImage is a 320 px subsample)."""
        ref = self._image_ref_of(photo)
        if ref is None or _ImageViewer is None:
            return
        self._open_image(ref, popout=False)

    def _open_image(self, ref, popout=False):
        message_id, image_id, filename = ref
        conv_id = self.conv_id

        def work():
            raw, _mime = self.api.get_message_image_bytes(message_id, image_id)
            return raw

        def done(raw):
            if conv_id != self.conv_id or not self.is_visible():
                return
            try:
                photo = tk.PhotoImage(data=base64.b64encode(raw).decode("ascii"))
            except tk.TclError:
                photo = None                                    # a format Tk cannot decode: the viewer says so
            meta = {"filename": filename, "bytes": len(raw)}
            if popout:
                _open_image_window(self, photo, ref, meta)
                return
            self._close_image_viewer()
            self._image_viewer = _ImageViewer(self, photo, ref, meta)

        run_async(self, work, done, lambda e: show_toast(self, self.ctx.theme, f"Could not open the image: {e}", kind="error"))

    def _open_image_window(self, ref, meta, photo=None):
        if _open_image_window is None:
            return
        if photo is None:
            self._open_image(ref, popout=True)
            return
        _open_image_window(self, photo, ref, meta)

    def _close_image_viewer(self):
        viewer, self._image_viewer = getattr(self, "_image_viewer", None), None
        if viewer is not None:
            viewer.close()

    def _image_filename(self, ref):
        name = os.path.basename(str(ref[2] or "")) or f"image-{ref[1]}.png"
        if not os.path.splitext(name)[1]:
            name += ".png"
        return name

    def _save_image_as(self, ref, meta=None):
        """SAVE AS for an image: the Windows Save dialog (default `image-<id>.png` / the stored name), atomic write."""
        if _file_ops is None:
            return
        filename = self._image_filename(ref)
        initial_dir = getattr(self, "_document_save_dir", None) or _file_ops.downloads_dir()
        try:
            path = filedialog.asksaveasfilename(
                parent=self.winfo_toplevel(), title="Save image as", initialfile=filename, initialdir=initial_dir,
                defaultextension=os.path.splitext(filename)[1] or ".png", filetypes=_file_ops.filetypes_for(filename),
            )
        except tk.TclError:
            path = ""
        if not path:
            return
        self._document_save_dir = os.path.dirname(path)
        remember = {"document_save_dir": self._document_save_dir}

        def work():
            raw, _mime = self.api.get_message_image_bytes(ref[0], ref[1])
            _file_ops.atomic_write_bytes(path, raw)
            try:
                self.api.save_settings(remember)
            except ApiError:
                pass
            return path

        run_async(self, work, lambda p: show_toast(self, self.ctx.theme, f"Saved to {p}", kind="success"),
                  lambda e: show_toast(self, self.ctx.theme, f"Could not save {filename}: {e}", kind="error"))

    def _download_image(self, ref, meta=None):
        if _file_ops is None:
            return
        filename = self._image_filename(ref)

        def work():
            raw, _mime = self.api.get_message_image_bytes(ref[0], ref[1])
            return _file_ops.download_bytes(filename, raw)

        run_async(self, work,
                  lambda p: show_toast(self, self.ctx.theme, f"Downloaded to Downloads\\{os.path.basename(p)}", kind="success"),
                  lambda e: show_toast(self, self.ctx.theme, f"Could not save {filename}: {e}", kind="error"))

    def _view_image_externally(self, ref):
        """View image: a temp copy under the app's own temp folder, opened with the Windows default viewer (Photos)."""
        if _file_ops is None:
            return
        filename = self._image_filename(ref)

        def work():
            raw, _mime = self.api.get_message_image_bytes(ref[0], ref[1])
            path = _file_ops.write_temp_copy(filename, raw)
            _file_ops.open_with_default(path)
            return path

        run_async(self, work, lambda p: None,
                  lambda e: show_toast(self, self.ctx.theme, f"Could not open {filename}: {e}", kind="error"))

    def _open_document_externally(self, doc):
        """Open in...: a temp copy of the document, handed to the Windows "Open with" chooser."""
        if _file_ops is None:
            return
        filename = doc.get("filename") or "document.md"

        def work():
            data, _name = self.api.get_document_bytes(doc.get("id"))
            path = _file_ops.write_temp_copy(filename, data)
            _file_ops.open_with_chooser(path)
            return path

        run_async(self, work, lambda p: None,
                  lambda e: show_toast(self, self.ctx.theme, f"Could not open {filename}: {e}", kind="error"))

    def _copy_document_text(self, doc):
        """Copy text: the whole document onto the clipboard (fetched off the Tk thread)."""
        def done(full):
            try:
                self.clipboard_clear()
                self.clipboard_append(full.get("content") or "")
            except tk.TclError:
                pass

        run_async(self, lambda: self.api.get_document(doc.get("id")), done,
                  lambda e: show_toast(self, self.ctx.theme, f"Could not copy the document: {e}", kind="error"))

    def _image_menu_items(self, ref):
        """The image's right-click menu entries (label, callback) - also what the tests read."""
        return [
            ("View image", lambda: self._view_image_externally(ref)),
            ("Open in new window", lambda: self._open_image_window(ref, {"filename": ref[2]})),
            ("Save as...", lambda: self._save_image_as(ref)),
            ("Download", lambda: self._download_image(ref)),
        ]

    def _document_menu_items(self, doc):
        return [
            ("Open", lambda: self._open_document(doc)),
            ("Open in new window", lambda: self._open_document_window(doc)),
            ("Open in...", lambda: self._open_document_externally(doc)),
            ("Save as...", lambda: self._save_document_as(doc)),
            ("Download", lambda: self._download_document(doc)),
            ("Copy text", lambda: self._copy_document_text(doc)),
        ]

    def _shared_context_menu(self):
        menu = getattr(self, "_transcript_menu", None)
        if menu is None and _SharedContextMenu is not None:
            menu = self._transcript_menu = _SharedContextMenu(self, self.ctx.theme)
        return menu

    def _popup_items(self, items, event, widget=None):
        menu = self._shared_context_menu()
        if menu is None:
            return
        try:
            if event is not None and getattr(event, "x_root", None) is not None and getattr(event, "keysym", "") not in ("App", "F10"):
                x, y = int(event.x_root), int(event.y_root)
            elif widget is not None:
                x, y = widget.winfo_rootx() + 8, widget.winfo_rooty() + widget.winfo_height() // 2
            else:
                x, y = self.winfo_rootx() + 40, self.winfo_rooty() + 40
            menu.popup(items, x, y)
        except tk.TclError:
            pass

    def _on_image_menu(self, photo, event):
        ref = self._image_ref_of(photo)
        if ref is None:
            return
        self._popup_items(self._image_menu_items(ref), event)

    def _on_document_menu(self, doc, card, event=None):
        self._popup_items(self._document_menu_items(doc), event, widget=card)

    def _on_show_thoughts(self):
        theme = self.ctx.theme
        root = self.winfo_toplevel()
        # BF-11: one card at a time; it goes with the Chat screen (on_hide -> _close_thoughts), since it is
        # placed on the root window and hiding Chat would otherwise leave it drawn over the next screen.
        self._close_thoughts()
        # Fetched here rather than held in memory so the panel shows what's
        # actually on disk - including transcripts from previous runs of the
        # app, which is the whole point of persisting them. A failure is
        # rendered inside the panel instead of raising: the button should
        # always open something.
        # BF-04 (bug-fix session): the fetch runs off the Tk thread (run_async): the card opens at once reading
        # "Loading saved thoughts...", the entries (or the error) land when the request returns, and a card closed
        # or a chat switched meanwhile drops them. B7B-H3: the card takes the keyboard - typing goes nowhere, Esc
        # closes it, Tab never walks out to the widgets under the backdrop - and hands it back to the composer.
        conv_id = self.conv_id

        # B7b (approved r2_chat_thoughts): an opaque panel backdrop (a click on it closes the card), the card at
        # 0.7 x 0.8 of the window with ONE 1 px bright border, an inverse 24 px title bar THOUGHTS + the close x,
        # today's intro sentence in full (D-27 #5) in C9 value, then the unboxed entries.
        t = self._b7b_tokens()
        s = self._sb_s
        backdrop = tk.Frame(root, bg=theme.panel)
        backdrop.place(x=0, y=0, relwidth=1, relheight=1)
        backdrop.tkraise()
        self._thoughts_backdrop = backdrop

        def cleanup(_event=None):
            if self._thoughts_backdrop is backdrop:
                self._thoughts_backdrop = None
            try:
                backdrop.destroy()
            except tk.TclError:
                pass
            if self.is_visible():                   # B7B-H3: the keyboard goes back to the composer
                try:
                    self.input.focus_set()
                except tk.TclError:
                    pass
            return "break"

        backdrop.bind("<Button-1>", cleanup)
        backdrop.bind("<Escape>", cleanup)          # B7B-H3: Esc closes the card; "break" keeps the router's Back out

        card = tk.Frame(backdrop, bg=theme.bg, highlightthickness=1, highlightbackground=theme.bright,
                        highlightcolor=theme.bright)
        card.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.7, relheight=0.8)
        card.bind("<Button-1>", lambda e: "break")

        header = tk.Frame(card, bg=theme.bright, height=s(24))
        header.pack(fill="x")
        header.pack_propagate(False)
        header.bind("<Button-1>", lambda e: "break")
        tk.Label(header, text="THOUGHTS", bg=theme.bright, fg=theme.bg, font=C9B, bd=0, padx=0,
                 pady=0).pack(side="left", padx=(s(11), 0))
        close_btn = tk.Label(header, text="×", bg=theme.bright, fg=theme.bg, font=C11B, bd=0, padx=s(4),
                             pady=0, cursor="hand2")
        close_btn.pack(side="right", padx=(0, s(9)))
        close_btn.bind("<Button-1>", cleanup)

        intro = tk.Label(
            card,
            text="What the model worked through before answering, saved with each reply and kept "
            "across restarts. Never sent back to the model on later turns. Deleting a message or "
            "chat deletes its thoughts too.",
            bg=theme.bg, fg=t["value"], font=C9, justify="left", anchor="w", bd=0, padx=0, pady=0,
        )
        intro.pack(fill="x", padx=s(19), pady=(s(12), s(14)))

        outer, inner = make_scrollable(card, theme)
        outer.pack(fill="both", expand=True, padx=(s(19), s(10)), pady=(0, s(12)))
        # every text in the card wraps to the room it has (never cut, D-27 #14): the intro to the card, the entry
        # headers / messages to the list (label, px left of them)
        inner._thought_wraps = []

        def rewrap_intro(e):
            wl = max(1, e.width - 2 * s(19) - 2)
            if int(intro.cget("wraplength") or 0) != wl:
                intro.configure(wraplength=wl)

        def rewrap_list(e):
            for label, room in list(inner._thought_wraps):
                try:
                    wl = max(1, e.width - room)
                    if int(label.cget("wraplength") or 0) != wl:
                        label.configure(wraplength=wl)
                except tk.TclError:
                    pass

        card.bind("<Configure>", rewrap_intro, add="+")
        inner.bind("<Configure>", rewrap_list, add="+")

        loading = tk.Label(
            inner, text="Loading saved thoughts...",
            bg=theme.bg, fg=t["value"], font=C10, justify="left", anchor="w", bd=0, padx=0,
        )
        loading.pack(anchor="w", fill="x", pady=(s(4), 0))
        inner._thought_wraps.append((loading, 0))
        _swallow_tabs(backdrop)
        backdrop.focus_set()

        def show_entries(entries):
            if self._thoughts_backdrop is not backdrop:
                return                              # closed, or replaced, while the request ran
            # The count came from the last conversation load, so it can lag behind
            # what's really stored (e.g. a background stream finished in another
            # chat). What we just fetched is authoritative - resync the label.
            if conv_id == self.conv_id:
                self._thoughts_counts[self.conv_id] = len(entries)
                self._update_thoughts_button()
            try:
                inner._thought_wraps.remove((loading, 0))
                loading.destroy()
            except (ValueError, tk.TclError):
                pass
            if not entries:
                msg = tk.Label(
                    inner, text="No thinking recorded for this chat. Only reasoning models produce it.",
                    bg=theme.bg, fg=t["value"], font=C10, justify="left", anchor="w", bd=0, padx=0,
                )
                msg.pack(anchor="w", fill="x", pady=(s(4), 0))
                inner._thought_wraps.append((msg, 0))
            for i, entry in enumerate(entries, start=1):
                self._build_thought_entry(
                    inner, i, entry.get("reasoning") or "", prompt=entry.get("prompt"),
                    created_at=entry.get("created_at"),
                )
            # Batch 32: the open chat's stream is still thinking - the card lists saved entries only, and this turn
            # is not saved yet (the transcript's live block shows it); one dim line says so, gone when it finishes
            st = self._streams.get(conv_id)
            if st is not None and (st.thinking_started or st.thinking_raw) and not st.thinking_finished:
                note = tk.Label(
                    inner, text="The current reply is still thinking - its thoughts appear here when it finishes.",
                    bg=theme.bg, fg=t["value"], font=C9, justify="left", anchor="w", bd=0, padx=0,
                )
                note.pack(anchor="w", fill="x", pady=(s(10), 0))
                inner._thought_wraps.append((note, 0))
                self._thoughts_inflight = (note, conv_id)
            _swallow_tabs(inner)

        def show_error(e):
            if self._thoughts_backdrop is not backdrop:
                return
            load_error = str(e)
            try:
                loading.configure(text=f"Could not load saved thoughts: {load_error}")
            except tk.TclError:
                pass

        run_async(backdrop, lambda: self.api.get_thoughts(conv_id).get("thoughts", []), show_entries, show_error)
        return backdrop

    def _close_thoughts(self):
        """BF-11: take the Thoughts card down, if it is open. It is placed on the root window (over the whole app),
        so Chat's on_hide calls this: navigating away never leaves it drawn over the next screen. The next THOUGHTS
        click opens a fresh card with the stored thoughts."""
        backdrop, self._thoughts_backdrop = getattr(self, "_thoughts_backdrop", None), None
        self._thoughts_inflight = None
        if backdrop is not None:
            try:
                backdrop.destroy()
            except tk.TclError:
                pass

    def _build_thought_entry(self, parent, index, text, prompt=None, created_at=None):
        """One collapsible entry in the Thoughts panel - starts collapsed
        (a one-line header + short preview) so a chat with many prompts
        stays scannable; expanding reveals the full text in a disabled
        (read-only, but still mouse-selectable/copyable - same as the main
        transcript) Text widget.

        B7b (approved r2_chat_thoughts): unboxed; the toggle is a plain + / - (C10B, it replaces the banned
        chevrons) in its own 16 px column, the header line beside it in C10 (value; bright while expanded) with
        today's content kept (D-27): Thought NN  (n words)  -  "prompt..."  (-  created_at when expanded); the
        body Consolas 10 bright, indented 16 px. A click on the toggle or the header line opens / closes it."""
        theme = self.ctx.theme
        t = self._b7b_tokens()
        s = self._sb_s
        flat_text = " ".join(text.split())
        word_count = len(flat_text.split()) if flat_text else 0
        # Now that these survive restarts, the collapsed row has to answer
        # "which question was this?" - a preview of the thinking itself
        # doesn't, since reasoning transcripts all open much the same way.
        flat_prompt = " ".join((prompt or "").split())
        prompt_preview = flat_prompt[:70] + ("..." if len(flat_prompt) > 70 else "")
        label_for = f'"{prompt_preview}"' if prompt_preview else "(prompt no longer in this chat)"
        state = {"expanded": False}

        entry = tk.Frame(parent, bg=theme.bg)
        entry.pack(fill="x", pady=(0, s(14)))
        head = tk.Frame(entry, bg=theme.bg, cursor="hand2")
        head.pack(fill="x")
        head.columnconfigure(0, minsize=s(16))
        head.columnconfigure(1, weight=1)
        toggle_label = tk.Label(head, bg=theme.bg, font=C10B, bd=0, padx=0, pady=0, cursor="hand2", anchor="nw")
        toggle_label.grid(row=0, column=0, sticky="nw")
        header_label = tk.Label(
            head, bg=theme.bg, font=C10, anchor="w", cursor="hand2", bd=0, padx=0, pady=0, justify="left",
        )
        header_label.grid(row=0, column=1, sticky="ew")
        wraps = getattr(parent, "_thought_wraps", None)
        if wraps is not None:
            wraps.append((header_label, s(16)))

        # B7b fix ADV1-04: the body (a Frame + a Text holding the whole thought) is built on the FIRST expand, not
        # for every entry up front - a chat with 200 thoughts opened its card in about twice the base's time.
        body = {"frame": None, "text": None}

        def build_body():
            body_frame = tk.Frame(entry, bg=theme.bg)
            # No live-layout line-count measurement (the widget isn't reliably
            # sized yet inside a freshly-packed scrollable canvas) - a rough
            # chars-per-line estimate is good enough for a preview panel, capped
            # so one very long thought can't dominate the whole list.
            est_lines = max(1, (len(text) + 99) // 100)
            body_text = tk.Text(
                body_frame, wrap="word", bg=theme.bg, fg=theme.bright, font=theme_mod.FONT_MONO,
                relief="flat", borderwidth=0, highlightthickness=0, padx=0, pady=s(6),
                height=min(est_lines, THOUGHT_BODY_MAX_LINES),
            )
            body_text.insert("1.0", text)
            body_text.configure(state="disabled", takefocus=0)
            # packed by _show_thought_body on each open (B7B-H10: first laid out at its final width)
            _swallow_tabs(body_text)            # B7B-H3: a click focuses a Text on Windows; Tab must not leave the card

            def fit_body(_e=None):
                # B7b: the estimate above is only the first guess. Once the box has its real width (its own
                # <Configure>: shown, or the window resized) it shows every line as it wraps there, up to the same
                # 25 (a longer thought scrolls inside the box); on a narrow card (820x560) the 100-chars-per-line
                # guess hid the last lines. A height change re-sends <Configure> with the same width: no loop.
                # ADV1-02: a thought that cannot fit 25 lines anyway is never laid out in full here (a 300 KB one
                # froze the window for 5-14 s): _thought_body_lines counts only a short text.
                try:
                    cell = max(1, int(body_text.tk.call("font", "measure", body_text.cget("font"), "0")))
                    per_line = max(1, int(body_text.winfo_width()) // cell)
                    want = _thought_body_lines(text, per_line, lambda: int(body_text.tk.call(
                        body_text._w, "count", "-update", "-displaylines", "1.0", "end")))
                except (tk.TclError, ValueError):
                    return
                if int(body_text.cget("height")) != want:
                    body_text.configure(height=want)

            body_text.bind("<Configure>", fit_body, add="+")
            body["frame"] = body_frame
            body["text"] = body_text
            return body_frame

        def set_header_text():
            ink = theme.bright if state["expanded"] else t["value"]
            line = f"Thought {index:02d}  ({word_count} words)  -  {label_for}"
            if state["expanded"] and created_at:
                line += f"  -  {created_at}"
            toggle_label.config(text="-" if state["expanded"] else "+", fg=ink)
            header_label.config(text=line, fg=ink)

        def toggle(_event=None):
            state["expanded"] = not state["expanded"]
            if state["expanded"]:
                frame = body["frame"] or build_body()
                _show_thought_body(frame, body["text"], (s(16), 0))     # B7B-H10: laid out once, at its final width
            elif body["frame"] is not None:
                body["frame"].pack_forget()
            set_header_text()

        for w in (head, toggle_label, header_label):
            w.bind("<Button-1>", toggle)
        set_header_text()

    def _on_export(self):
        if self.conv_id is None:
            return
        self.export_btn.configure(state="disabled")
        self.rail_status.config(text="exporting...")
        q = queue.Queue()
        conv_id = self.conv_id

        def worker():
            try:
                settings = self.api.get_settings()
                if settings.get("confirm_save_location"):
                    content, filename = self.api.get_raw(
                        f"/api/conversations/{conv_id}/export?format=md"
                    )
                    q.put(("choose_path", content, filename or "conversation.md"))
                else:
                    result = self.api._request(
                        "POST", f"/api/conversations/{conv_id}/export", body={"format": "md"}
                    )
                    q.put(("ok", result.get("path", "ok")))
            except Exception as e:
                # Broad on purpose - anything short of this reaching q.put()
                # leaves export_btn disabled forever, since ChatView is a
                # singleton screen reused across every conversation (see
                # app/server.py's _dispatch_safely comment for the matching
                # server-side half of this fix).
                q.put(("error", str(e)))

        threading.Thread(target=worker, daemon=True).start()
        self.after(50, self._poll_export_queue, q)

    def _poll_export_queue(self, q):
        try:
            item = q.get_nowait()
        except queue.Empty:
            self.after(50, self._poll_export_queue, q)
            return
        if item[0] == "choose_path":
            self._finish_export_with_dialog(item[1], item[2])
            return
        self.export_btn.configure(state="normal")
        kind, payload = item
        self._show_export_result(kind, payload)

    def _show_export_result(self, kind, payload):
        """B7B-H8: the export's outcome. While a reply or an image job runs on the displayed chat the status line
        belongs to the stream (its next tick rewrote "saved: <path>" after one frame, and the delayed blanking
        then wiped the stage text mid-stream): then the outcome is a notice. Otherwise the line shows it, and is
        blanked later only if it still shows it."""
        if kind == "error":
            text, toast_kind, toast = f"export failed: {payload}", "error", f"Could not export the chat: {payload}"
        elif kind == "cancelled":
            text, toast_kind, toast = "export cancelled", "warning", "Export cancelled."
        else:
            text, toast_kind, toast = f"saved: {payload}", "success", f"Export saved: {payload}"
        if self.conv_id in self._streams or self._image_gen is not None:
            if kind == "error":
                show_toast(self, self.ctx.theme, f"Could not export the chat: {payload}", kind="error")
            elif kind == "cancelled":
                show_toast(self, self.ctx.theme, "Export cancelled.", kind="warning")
            else:
                show_toast(self, self.ctx.theme, f"Export saved: {payload}", kind="success")
            return
        self.rail_status.config(text=text)

        def clear():
            try:
                if str(self.rail_status.cget("text")) == text:
                    self.rail_status.config(text="")
            except tk.TclError:
                pass
        self.after(3000 if kind == "cancelled" else 6000, clear)

    def _finish_export_with_dialog(self, content, suggested_filename):
        # Reached only when "ask where to save exports" is on - the worker
        # already fetched the file content, so this just needs a path (a Tk
        # dialog, must run on the main thread, which _poll_export_queue's
        # .after() polling already guarantees).
        self.export_btn.configure(state="normal")
        ext = os.path.splitext(suggested_filename)[1] or ".md"
        path = filedialog.asksaveasfilename(
            initialfile=suggested_filename, defaultextension=ext,
            filetypes=[("Markdown", "*.md"), ("All files", "*.*")],
        )
        if not path:
            self._show_export_result("cancelled", None)
            return
        try:
            with open(path, "wb") as f:
                f.write(content)
        except OSError as e:
            self._show_export_result("error", e)
        else:
            self._show_export_result("ok", path)

    # ---- loading a conversation ----

    def load_conversation(self, conv_id, on_loaded=None):
        """Opens conv_id. Everything about the *view* (which conversation is
        current, the cleared transcript, the composer's draft, staged files)
        is set up synchronously, exactly as before; the conversation itself -
        the message list plus any inline images - is fetched on a worker
        thread and rendered by _apply_conversation when it lands, so a slow
        backend (see server.py's note above db()) can't freeze the window.
        on_loaded, if given, is called once the transcript is built (or the
        load failed) - for callers that need to act on the finished render."""
        if conv_id != self.conv_id:
            self._close_thoughts()      # B7B-H3: another chat opens (Ctrl+N, the sidebar): the old chat's card goes
        # If a reply is still generating for this conversation - started
        # before navigating away (or even before this exact call), and
        # possibly still going the whole time in the background - capture
        # its accumulated raw text *before* clear()/rebuild below, from the
        # _StreamState (not self.transcript._raw: the transcript widget only
        # ever shows one conversation, so a background stream for this
        # conv_id has been accumulating into the state object instead, with
        # nowhere else to put it while some other conversation was on
        # screen). Without this, the still-running background stream's next
        # feed() call appends new chunks onto whatever the reload left
        # behind, under the wrong bubble tag - confirmed real bug pre-dating
        # multi-chat support, reproduced by leaving and returning to a
        # mid-stream chat, which glued the model's reply onto the user's own
        # question text inside the user bubble.
        #
        # Gated on assistant_started (not just "has a stream") since the
        # assistant's start_message() call is lazy - it only happens once
        # real answer content arrives, which can be well after send_message()
        # (a "thinking" phase, or the plain prefill wait, can come first).
        # Without this gate, reloading mid-thinking/mid-wait would resume the
        # *previous* finished message's leftover text as if it were this
        # turn's in-progress reply.
        # The user is looking at this conversation now, by definition -
        # clears any "unseen reply" sidebar indicator regardless of whether
        # the load succeeds below (an attempted view still counts as seen).
        self._unseen_conv_ids.discard(conv_id)

        state = self._streams.get(conv_id)
        # (What to resume is worked out in _apply_conversation, once the history
        # has arrived: chunks that stream in while it is on its way are held in
        # the stream state - see _loading_conv_id - and must be part of it.)
        if state is not None and not state.assistant_started:
            # The transcript widget is about to be cleared/rebuilt below, so
            # any "[ THINKING ]" chip it may have shown before navigating
            # away is gone - the next thinking chunk (if any) needs a fresh
            # start_thinking() call once this becomes the displayed
            # conversation again, not a feed_thinking() into a chip that no
            # longer exists.
            state.thinking_started = False
            state.thinking_finished = False

        if self.conv_id is not None:
            outgoing = self.input.get("1.0", "end-1c")
            self._drafts[self.conv_id] = outgoing
            # Persist the chat being left immediately rather than waiting out
            # the debounce - by the time it fired, self.conv_id would already
            # be the new conversation and _capture_draft would file this text
            # under the wrong chat.
            self._queue_draft(self.conv_id, outgoing)
            if self._draft_after_id is not None:
                try:
                    self.after_cancel(self._draft_after_id)
                except tk.TclError:
                    pass
                self._draft_after_id = None
        # An in-progress edit is scoped to the conversation it started in -
        # doesn't make sense to carry "editing message #12" across a switch
        # to a different chat entirely.
        self._editing_message_id = None
        self.editing_indicator.pack_forget()
        # Hidden unconditionally here too - re-shown below (if this
        # conversation is still busy) or fired (if it finished while the
        # user was elsewhere), never left showing stale state from whatever
        # was previously on screen.
        self.queued_indicator.pack_forget()
        self.conv_id = conv_id
        self.transcript.clear()
        # B7a ADV1-02 (B7A-H3): the header, the empty state and the sidebar's inverse row follow the switch NOW, not
        # when the history lands (most of a second on a hard disk during a model load): the title from the sidebar's
        # list, the old chat's AWAITING INPUT hidden until _apply_conversation decides for this one
        self._set_header_title(self._cached_conv_title(conv_id))
        self._set_empty_state(False)
        self._repaint_chat_list_status()
        # Staged images/docs are tied to the conversation the user was about
        # to send them to - carrying them over into whatever's loaded next
        # would silently attach them to the wrong chat. Unlike the composer
        # text below, they're deliberately wiped, not restored - staying
        # attached to the wrong chat is a real risk; a moment's unsent text
        # is not.
        self._staged_images = []
        self._staged_docs = {}
        self._render_staging_grid()
        # Restore this conversation's own draft (if any) - must happen
        # before the try/except below, since that except branch returns
        # early and would otherwise leave the *previous* conversation's text
        # stranded in the box despite self.conv_id already having moved on.
        self.input.delete("1.0", "end")
        self.input.insert("1.0", self._drafts.get(conv_id, ""))
        # Reset before the fetch (not just on success) so a failed load
        # below falls back to the ordinary sidebar instead of leaving
        # stale project-mode state from whatever was loaded previously.
        self._current_project_id = None
        # Held until the history is rendered: stream chunks for this
        # conversation that arrive meanwhile are only *recorded* in its stream
        # state (see _poll_stream_queue's is_displayed) and re-attached by
        # _apply_conversation, and send_message() waits - either would
        # otherwise land in the transcript above history that isn't drawn yet.
        self._loading_conv_id = conv_id
        self._send_when_loaded = False
        self._fx_queued = None             # a deferred dispatch this load drops takes its arm with it

        def fetch():
            conv = self.api.get_conversation(conv_id)
            # Inline images are fetched here too, in transcript order, so the
            # render step never makes a network call. (Base64-encoded here for
            # the same reason: only the PhotoImage itself must be built on the
            # Tk thread.)
            images = {}
            for msg in conv.get("messages", []):
                for img in msg.get("images") or []:
                    # A single image missing/undecodable (e.g. deleted between
                    # the message list fetch and here) shouldn't blank the
                    # rest of the conversation's history.
                    try:
                        raw, _ = self.api.get_message_image_bytes(msg["id"], img["id"])
                    except ApiError:
                        continue
                    images[(msg["id"], img["id"])] = base64.b64encode(raw).decode("ascii")
            return conv, images

        self._conv_loader.start(
            fetch,
            lambda result: self._apply_conversation(conv_id, result, on_loaded),
            lambda error: self._conversation_load_failed(conv_id, error, on_loaded),
        )

    def _conversation_load_failed(self, conv_id, error, on_loaded):
        if conv_id != self.conv_id:
            return  # superseded by a later load
        self._loading_conv_id = None
        self._send_when_loaded = False
        self._fx_queued = None
        # B7A-H4: a note, not an assistant reply - no [Regenerate] (there is no message to regenerate)
        self.transcript.add_static_message("note", f"Could not load conversation: {error}")
        self._set_header_title(self._cached_conv_title(conv_id))     # B7a ADV1-02: not the previous chat's
        self._render_conversation_files([])
        self.context_meter.hide()
        self._sync_rail_for_current_conv()
        self._refresh_chat_list()
        if on_loaded is not None:
            on_loaded()

    def _apply_conversation(self, conv_id, result, on_loaded):
        """Second half of load_conversation(): draws the fetched history."""
        if conv_id != self.conv_id:
            return  # the user opened something else while this was loading
        conv, image_data = result
        self._loading_conv_id = None
        # If a reply is still generating for this conversation, resume it from
        # the raw text accumulated so far - worked out here (not when the load
        # began) so it includes anything that streamed in while the history was
        # on its way. Non-empty raw_text is the right test: a stream state is
        # created fresh for every send, so it only ever holds this turn's
        # content. (The old test, assistant_started, missed a reply that had
        # been streaming in the background from its very first chunk - its
        # partial text vanished on return - because that flag only flips while
        # the conversation is displayed.)
        state = self._streams.get(conv_id)
        resume_raw = state.raw_text if (state is not None and (state.assistant_started or state.raw_text)) else None
        if resume_raw is not None:
            state.assistant_started = True
        self._current_project_id = conv.get("project_id")
        # Seed the composer from the saved draft, but only the first time this
        # conversation is opened in this session. After that the in-memory
        # mirror is the fresher of the two: text typed since the last autosave
        # exists there and not yet in the DB, and re-seeding would silently
        # roll it back to the last persisted state. Also only into an empty
        # composer now: the user can type while the history loads, and the
        # saved draft must never overwrite that.
        if conv_id not in self._drafts:
            saved_draft = conv.get("draft") or ""
            self._drafts[conv_id] = saved_draft
            if saved_draft and not self.input.get("1.0", "end-1c"):
                self.input.insert("1.0", saved_draft)
        messages = conv.get("messages", [])
        # Straight from the DB, so the button is right for conversations from
        # previous runs too - not just ones streamed in this session.
        self._thoughts_counts[conv_id] = sum(1 for m in messages if m.get("has_reasoning"))
        for msg in messages:
            text = msg.get("content") or ""
            imgs = msg.get("images") or []
            if not text and not imgs and not msg.get("documents"):
                continue
            label = "You" if msg["role"] == "user" else "Assistant"
            photos = []                         # BF-13: decoded first, then drawn inside the message
            for img in imgs:
                # Real inline image, not a "[image attached]" placeholder -
                # confirmed live that a successful image generation left the
                # user thinking nothing had happened, because this used to
                # just print that placeholder string no matter what. A
                # single image missing/undecodable (e.g. deleted between the
                # message list fetch and here) shouldn't blank the rest of
                # the conversation's history - fetch() in load_conversation
                # simply left it out of image_data.
                b64 = image_data.get((msg["id"], img["id"]))
                if b64 is None:
                    continue
                photo = self._photo_cache.get(("msg_image", msg["id"], img["id"]), b64, TRANSCRIPT_IMAGE_PX)
                if photo is not None:
                    photo._image_ref = (msg["id"], img["id"], img.get("filename") or "image.png")   # P4: the viewer's key
                    photos.append(photo)
            self.transcript.add_static_message(
                msg["role"], text, label=label, message_id=msg["id"],
                # 1/1 for anything the server didn't report on (a message that
                # was never regenerated, or an older backend) - the transcript
                # draws no version control at all in that case.
                version_pos=msg.get("version_pos") or 1,
                version_count=msg.get("version_count") or 1,
                images=photos,                  # BF-13: under the text, before [Edit] / [Regenerate]
                inserts=self._document_inserts(msg),      # document tool P2: the cards at the point of the call
            )

        # Tracks whether the very last thing in this conversation is a
        # generated/edited image, so a short in-chat follow-up like "make it
        # nighttime" can be recognized as an edit of *that* image (see
        # match_image_edit_intent()) instead of only matching full "generate
        # an image of..." commands. Only the tail message counts - an image
        # from earlier in the conversation, with ordinary chat since, isn't
        # what a bare "make it..." is about.
        if messages and messages[-1].get("images"):
            img = messages[-1]["images"][0]
            self._last_image_by_conv[conv_id] = {"message_id": messages[-1]["id"], "image_id": img["id"]}
        else:
            self._last_image_by_conv.pop(conv_id, None)

        if state is not None and state.thinking_raw:
            # Batch 31: the thinking that arrived while this chat was off screen (another chat on screen, or its
            # own history still loading) is replayed whole into a fresh live thinking block, BEFORE the replayed
            # reply, in the live order; it used to be dropped (the block restarted with whatever came after the
            # switch). Finished at once when the reply has already begun - thinking_finished is only ever set while
            # the chat is displayed, so a finish that happened off screen is inferred from the reply text.
            self.transcript.start_thinking()
            self.transcript.feed_thinking(state.thinking_raw)
            state.thinking_started = True
            if state.thinking_finished or resume_raw is not None:
                self.transcript.finish_thinking()
                state.thinking_finished = True
        if resume_raw is not None:
            # Re-attach a fresh in-progress assistant block from the raw text
            # accumulated so far, with freshly-placed tracking marks, so
            # chunks arriving from here on render correctly instead of
            # corrupting whatever the reload just built.
            self.transcript.start_message("assistant", label="Assistant")
            if state.documents:
                self._replay_documents(state, resume_raw)       # document tool P2: the rows / cards back in place
            else:
                self.transcript.feed(resume_raw)

        self._set_header_title(conv.get("title"))
        self._set_empty_state(not messages)
        self._sync_jump_cue()
        self.context_meter.refresh(conv_id)
        self._render_conversation_files(conv.get("files", []))
        self._sync_rail_for_current_conv()
        self._refresh_chat_list()
        self._update_thoughts_button()
        if conv_id in self._streams:
            # Still busy - re-show the indicator if there's something
            # waiting, rather than firing early. (A global-queue entry
            # can't coexist with conv_id already streaming - see
            # _queue_globally - but check both anyway for robustness.)
            self._sync_queued_indicator()
        elif self._backlog(conv_id):
            # This conversation's own stream finished while the user was
            # looking elsewhere - send it now instead of waiting for them
            # to notice and switch back.
            self._fire_queued_message(conv_id)
        elif any(e["conv_id"] == conv_id for e in self._global_send_queue):
            # Waiting on a GLOBAL slot, not this conversation's own stream
            # finishing - real bug found live (2026-08-07): this branch
            # didn't exist before, so navigating away and back to a
            # globally-queued conversation hid the indicator (the
            # unconditional pack_forget() above) and never re-showed it -
            # the entry was still genuinely queued and would still have
            # fired eventually, but Cancel was unreachable in the meantime.
            # Capacity should normally already have been rechecked by
            # _dispatch_next_global_send the moment a slot actually freed
            # (it's event-driven, not navigation-driven) - if it's somehow
            # still free right now, dispatch opportunistically instead of
            # leaving this stranded until the next unrelated completion;
            # otherwise just make the indicator (and its Cancel link)
            # visible again.
            if self._image_gen is None and len(self._streams) < self._cached_max_parallel_chats:
                self._dispatch_next_global_send()
            else:
                self._sync_queued_indicator()
        # Whoever asked for this load gets to act on the finished render first
        # (e.g. restoring scroll position and staged attachments after a
        # version switch)...
        if on_loaded is not None:
            on_loaded()
        # ...then an Enter pressed while the history was still loading, which
        # send_message() held back so it couldn't land above it, goes out now.
        if self._send_when_loaded:
            self._send_when_loaded = False
            self.send_message()

    def _cached_conv_title(self, conv_id):
        """B7a ADV1-02: conv_id's title from the sidebar's last list, or None (the header then reads New chat)."""
        for c in self._chat_list_conversations or ():
            if isinstance(c, dict) and c.get("id") == conv_id:
                return c.get("title")
        return None

    def _set_header_title(self, title):
        """B7a: the open conversation's title beside CHAT in the header (the kit Header elides a long one in
        the middle and shows it whole in a hover tip) and under AWAITING INPUT. No title reads "New chat",
        as its sidebar row does."""
        text = title if (isinstance(title, str) and title.strip()) else "New chat"
        self._header_title = text
        setter = getattr(self.header, "set_subtitle", None)
        if setter is not None:
            try:
                setter(text)
            except tk.TclError:
                pass
        if self.empty_state is not None and hasattr(self.empty_state, "set_caption"):
            try:
                self.empty_state.set_caption(text)
            except tk.TclError:
                pass

    def _set_empty_state(self, empty):
        """B7a: AWAITING INPUT + a block cursor + the title where the transcript's first line starts, while the
        open conversation has no messages AND the transcript is really empty. A kit ChatEmptyState placed over
        the transcript (never text in it, so it can never reach a reply or an export); B10 animates its
        cursor (.mark) inside kit/. Any later change to the transcript takes it away (_on_transcript_modified),
        so the first message - or a stream resumed into it - needs no hook of its own. A busy chat (a stream,
        an image job whose prompt the server stores only at completion, or a queued message) is not awaiting
        input, so it never shows it (ADV1-01)."""
        es = self.empty_state
        if not (empty and self._transcript_is_empty() and not self._conv_is_busy(self.conv_id)):
            if es is not None and es.winfo_manager():
                es.place_forget()
            return
        if es is None:
            theme = self.ctx.theme
            try:
                if ChatEmptyState is None:
                    raise RuntimeError("kit unavailable")
                es = ChatEmptyState(self.transcript.master, theme, "AWAITING INPUT", caption=self._header_title)
            except Exception as exc:  # noqa: BLE001 - a plain line stands in
                _report_kit_failure("empty state", exc)
                es = tk.Label(self.transcript.master, text="AWAITING INPUT", bg=theme.bg, fg=theme.bright,
                              font=("Consolas", 13, "bold"), anchor="w")
            self.empty_state = es
        try:
            # place coordinates start inside the Text's internal border (bd + its padx / pady): (0, 0) is where
            # the transcript's first character would be drawn; the body text sits TRANSCRIPT_TEXT_X further in
            x = self._sb_s(TRANSCRIPT_TEXT_X)
            es.place(in_=self.transcript, x=x, y=0, relwidth=1.0, width=-x)
            es.lift(self.transcript)
            self.transcript.edit_modified(False)        # armed: the next change to the transcript hides it
        except tk.TclError:
            pass

    def _conv_is_busy(self, conv_id):
        """B7a ADV1-01: a reply streaming, an image job running, or a message queued for conv_id."""
        if conv_id is None:
            return False
        gen = self._image_gen
        return (conv_id in self._streams or bool(gen and gen.get("conv_id") == conv_id)
                or self._has_pending_queued_message(conv_id))

    def _transcript_is_empty(self):
        t = self.transcript
        try:
            if t.compare("end-1c", "==", "1.0"):
                return True
            if t.window_names() or t.image_names():
                return False
            return not t.search(r"\S", "1.0", "end", regexp=True)
        except tk.TclError:
            return False

    def _on_transcript_modified(self, _event=None):
        """<<Modified>> on the transcript: while the empty state shows, re-arm the flag and take the empty
        state away once the transcript holds anything. While it is hidden the flag is left set, so the
        transcript raises no further <<Modified>> events (streaming costs nothing here)."""
        es = self.empty_state
        if es is None or not es.winfo_manager():
            return
        try:
            self.transcript.edit_modified(False)
        except tk.TclError:
            return
        if not self._transcript_is_empty():
            es.place_forget()

    def _on_compacted(self, conv_id):
        # Compaction only changes what gets sent to the model on future
        # turns - the visible transcript/exports are untouched (nothing to
        # re-render here). Hook kept in case a "compacted" divider is added
        # to the transcript later.
        pass

    # ---- sending / streaming ----

    def _on_enter(self, event):
        self.send_message()
        return "break"

    def _on_composer_modified(self, _event=None):
        # <<Modified>> fires for every content change regardless of source
        # (typing, paste, drag-drop text, or send_message()'s own
        # self.input.delete() clearing it after send) - catches all of them
        # with one binding, unlike <KeyRelease> which would miss paste/
        # programmatic changes. Tk's modified flag is sticky until cleared,
        # or this would only ever fire once.
        self.input.edit_modified(False)
        self._fit_composer_height()
        # Same binding covers autosave: it already fires on every source of
        # change, including the post-send clear (which persists the now-empty
        # draft, so a sent message doesn't come back next launch).
        self._schedule_draft_save()

    def _composer_lines_needed(self):
        """B7B-H4: the display lines the text needs, counted only as far as the cap (+1): a whole-text count over
        a 1 MB paste cost 1-4 s on every <<Modified>>, to choose a height between 3 and 10 lines."""
        w = self.input
        try:
            if int(w.tk.call(w._w, "count", "-chars", "1.0", "end")) <= 4000:
                needed = int(w.tk.call(w._w, "count", "-displaylines", "1.0", "end"))
            else:
                stop = w.index("1.0 + %d displaylines" % (COMPOSER_MAX_LINES + 1))
                needed = int(w.tk.call(w._w, "count", "-displaylines", "1.0", stop))
                if w.compare(stop, "<", "end-1c"):
                    needed = max(needed, COMPOSER_MAX_LINES)
        except (tk.TclError, ValueError):
            needed = COMPOSER_MIN_LINES
        return max(COMPOSER_MIN_LINES, min(COMPOSER_MAX_LINES, needed))

    def _fit_composer_height(self):
        """The composer grows 3..10 lines with its text (B7B-H4: counted only to the cap) but, B7B-H7, never past
        the room the window has: the transcript gives way first (it may reach 0, B2's design); the status row,
        which holds the context meter, never does - a composer above its minimum gives lines back when that
        row is short. Run on <<Modified>> and when the room changes (_on_room_changed)."""
        if getattr(self, "_fitting_composer", False):
            return
        self._fitting_composer = True
        try:
            w = self.input
            wanted = self._composer_lines_needed()
            for _ in range(COMPOSER_MAX_LINES):             # until the height is stable (each pass re-measures)
                needed = wanted
                current = int(w.cget("height"))
                if self.winfo_height() > 1 and needed > COMPOSER_MIN_LINES:
                    line_px = max(1, int(self.tk.call("font", "metrics", w.cget("font"), "-linespace")))
                    short = max(0, self.status_row.winfo_reqheight() - self.status_row.winfo_height())
                    if short:                                        # the row is squeezed: lines go back, whatever the slack
                        give = -((short + line_px - 1) // line_px)
                    else:                                            # else growth only into slack past the transcript's request
                        give = max(0, self.transcript.winfo_height() - self.transcript.winfo_reqheight()) // line_px
                    needed = max(COMPOSER_MIN_LINES, min(needed, current + give))
                if current == needed:
                    break
                w.configure(height=needed)
                self.update_idletasks()
        except tk.TclError:
            pass
        finally:
            self._fitting_composer = False

    def _on_room_changed(self, _event=None):
        """B7B-H7: the transcript's size changed (the window, the status row, a staged row): a composer above its
        minimum is fitted to the room again."""
        if getattr(self, "input", None) is None or getattr(self, "_fitting_composer", False):
            return
        self._fit_composer_height()

    # ---- draft autosave ----

    def _schedule_draft_save(self):
        """Debounced - a save per keystroke would be one HTTP round trip per
        character. Anything that must not wait out the debounce (switching
        chats, leaving the screen, closing the app) calls flush_draft()."""
        if self.conv_id is None:
            return
        if self._draft_after_id is not None:
            try:
                self.after_cancel(self._draft_after_id)
            except tk.TclError:
                pass
        self._draft_after_id = self.after(DRAFT_SAVE_DELAY_MS, self._capture_draft)

    def _capture_draft(self):
        """Snapshots the composer for the *currently displayed* conversation
        and queues it for persisting. Main thread only - it touches Tk."""
        self._draft_after_id = None
        if self.conv_id is None:
            return
        try:
            text = self.input.get("1.0", "end-1c")
        except tk.TclError:
            return  # widget already destroyed (app closing)
        self._drafts[self.conv_id] = text
        self._queue_draft(self.conv_id, text)

    def _queue_draft(self, conv_id, text):
        with self._draft_lock:
            self._draft_pending[conv_id] = text
            if self._draft_saver_running:
                return  # the running worker will pick this up before it exits
            self._draft_saver_running = True
        threading.Thread(target=self._draft_saver, daemon=True).start()

    def _draft_saver(self):
        """One worker at a time, draining whatever is pending. Failures are
        swallowed deliberately: a draft that couldn't be persisted is not
        worth interrupting the user over, the text is still in the box, and
        the next keystroke (or the next flush) retries anyway."""
        while True:
            with self._draft_lock:
                if not self._draft_pending:
                    self._draft_saver_running = False
                    return
                conv_id, text = self._draft_pending.popitem()
            try:
                self.api.update_conversation(conv_id, draft=text)
            except Exception:
                pass

    def flush_draft(self, timeout=2.0, wait=True):
        """Persists the current composer immediately and waits (briefly) for
        the write to land. Called before anything that can stop the backend
        or destroy the window, where the debounce timer would never fire and
        an in-flight background save would be cut off mid-request.

        wait=False only queues the write (still immediately, still ahead of the
        debounce) and returns: for navigation, where nothing is about to stop
        the backend. Waiting there blocked the Tk thread - up to `timeout` - on
        every Chat -> elsewhere switch whenever the backend was slow to answer
        (a model loading from a hard disk). The text stays in self._drafts, so
        coming straight back shows it even before the write has landed, and
        the blocking flush at shutdown/close still waits for anything queued."""
        if self._draft_after_id is not None:
            try:
                self.after_cancel(self._draft_after_id)
            except tk.TclError:
                pass
            self._draft_after_id = None
        self._capture_draft()
        if not wait:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._draft_lock:
                if not self._draft_pending and not self._draft_saver_running:
                    return True
            time.sleep(0.02)
        return False

    def _sync_rail_for_current_conv(self):
        """Shows whatever's happening in the *currently displayed*
        conversation - status text, Send/Stop enablement, the pre-output
        pulse animation - regardless of what else might be generating in the
        background for a different conversation. Called after send_message()
        starts a new stream, after every poll tick for the displayed
        conversation's stream, and after load_conversation() switches which
        conversation is on screen."""
        if self._image_gen is not None:
            # Global, not per-conversation - blocks Send everywhere the same
            # way server.py's any_chat_active()/image_gen_status exclusion
            # does, since generating an image unloads the one shared chat
            # model out from under every conversation at once.
            self._signal_fx.stop()
            self.rail_status.config(text=IMAGE_GEN_STAGE_LABELS.get(self._image_gen["stage"], "IMAGE GEN: WORKING..."))
            self._set_send_button_for_busy()
            self.stop_btn.configure(state="normal")
            return
        state = self._streams.get(self.conv_id)
        if state is None:
            if self._has_pending_queued_message(self.conv_id):
                # Not actively streaming - idle, waiting on a global slot
                # (see _queue_globally) rather than its own job finishing.
                self._signal_fx.stop()
                self.rail_status.config(text="QUEUED\nwaiting for a free slot")
                self._set_send_button_for_busy()
                self.stop_btn.configure(state="disabled")
                return
            if self._image_gen is not None or len(self._streams) >= self._cached_max_parallel_chats:
                # Nothing queued yet and this conversation isn't streaming
                # either, but capacity is still full elsewhere - clicking
                # Send right now would actually queue, not send
                # immediately (see send_message()'s own capacity check).
                # Show "Queue" here too instead of "Send", which would
                # otherwise imply an immediate send that won't happen -
                # found live via a test that cancelled a globally-queued
                # message and expected this exact label.
                self._signal_fx.stop()
                self.rail_status.config(text="")
                self._set_send_button_for_busy()
                self.stop_btn.configure(state="disabled")
                return
            self._signal_fx.stop()
            self.rail_status.config(text="")
            self.send_btn.configure(state="normal", text="SEND")
            self._sync_legend_verb()
            self.stop_btn.configure(state="disabled")
            return
        self._set_send_button_for_busy()
        self.stop_btn.configure(state="normal")
        if not state.prefill_done:
            self._signal_fx.base_text = state.status_text
            self._signal_fx.start()
        else:
            self._signal_fx.stop()
            self.rail_status.config(text=state.status_text)

    def _set_send_button_for_busy(self):
        # While the current conversation can't send normally (a reply
        # streaming, an image generating, or every parallel-chat slot
        # occupied elsewhere), Send stays clickable but becomes "Queue"
        # instead of disabled outright - clicking it (or pressing Enter)
        # captures the composer instead of sending it, see
        # _queue_current_message/_queue_globally. Once something's
        # actually captured, it locks to "Queued" (disabled) -
        # _cancel_queued_message is the only way back, so a second
        # Enter/click can't silently overwrite it.
        if self._has_pending_queued_message(self.conv_id):
            self.send_btn.configure(state="disabled", text="QUEUED")
        else:
            self.send_btn.configure(state="normal", text="QUEUE")
        self._sync_legend_verb()

    def send_message(self):
        if self.conv_id is None:
            if self._draft_where is not None:
                self._materialise_draft_and_send()      # a draft's first message creates its row, then sends
            return
        if self._loading_conv_id == self.conv_id:
            # The history is still on its way: a message sent now would be drawn
            # above it. Remember the request; _apply_conversation re-issues it
            # the moment the history is on screen.
            self._send_when_loaded = True
            return
        # image generation is a GLOBAL exclusive resource (unloads the
        # shared chat model - see _sync_rail_for_current_conv's comment),
        # not scoped to a conversation - only treat it as "my own
        # conversation is busy" when it's actually this conversation's own
        # image job, not some other conversation's.
        image_gen_here = self._image_gen is not None and self._image_gen.get("conv_id") == self.conv_id
        if self._editing_message_id is not None and (self.conv_id in self._streams or image_gen_here
                                                     or self._image_gen is not None
                                                     or len(self._streams) >= self._cached_max_parallel_chats):
            # B14-H5: an edit is never queued - queued, it went out later as a NEW message, and a dispatch that met
            # edit mode went out as the EDIT of that message (the server then deleted every later message, no
            # undo). The edit stays in the composer; the user submits it again once a slot is free.
            show_toast(self, self.ctx.theme, "Can't submit an edit while a reply is generating - try again when it finishes.",
                       kind="warning")
            return
        if self.conv_id in self._streams or image_gen_here:
            # Can't send right now - capture what's in the composer instead
            # of just no-op'ing (the old behavior: a message typed during
            # this window silently went nowhere). _editing_message_id can't
            # be set here - entering edit mode is itself refused while busy,
            # see _on_edit_message_clicked - so there's no edit-vs-queue
            # ambiguity to resolve.
            self._queue_current_message()
            return
        if self._image_gen is not None or len(self._streams) >= self._cached_max_parallel_chats:
            # This conversation is idle, but nothing can actually start
            # right now - either the model is unloaded for someone else's
            # image job, or every max_parallel_chats slot is occupied by
            # OTHER conversations. Used to just hit the server's real 409
            # and render it as a garbled assistant-bubble error; queue
            # globally instead and dispatch in the order captured, once a
            # slot actually frees up (see _queue_globally).
            self._queue_globally()
            return
        if self._editing_message_id is not None:
            # Takes priority over every intent check below - text that
            # happens to match an image-generation phrase while editing a
            # past message should still be treated as the edited text, not
            # accidentally trigger image generation instead.
            self._submit_edit()
            return
        text = self.input.get("1.0", "end-1c").strip()
        images = self._staged_images
        if not text and not images:
            return
        # Only treated as an image-gen command when there's nothing else
        # staged - an attached image/doc means the user wants to chat about
        # it, not spawn a text-to-image request instead of sending it.
        image_prompt = match_image_generation_intent(text) if not images and not self._staged_docs else None
        if image_prompt:
            self.input.delete("1.0", "end")
            self.transcript.add_static_message("user", text, label="You")
            self._staged_docs = {}
            self._render_staging_grid()
            self._render_conversation_files(self._conversation_files)
            self._start_image_generation(self.conv_id, image_prompt)
            return
        # Same "nothing else staged" gate as above, plus the conversation's
        # last message must actually be an image (see load_conversation()) -
        # otherwise phrasing like "make it ..." is almost certainly ordinary
        # conversation, not an edit request with nothing to target.
        edit_source = self._last_image_by_conv.get(self.conv_id)
        if edit_source and not images and not self._staged_docs and match_image_edit_intent(text):
            self.input.delete("1.0", "end")
            self.transcript.add_static_message("user", text, label="You")
            self._staged_docs = {}
            self._render_staging_grid()
            self._render_conversation_files(self._conversation_files)
            self._start_image_edit(self.conv_id, text, edit_source)
            return
        # Same gate as the edit check above - only meaningful as "try a
        # different image" right after one was just shown.
        alt_prompt = (
            match_image_alt_generate_intent(text)
            if edit_source and not images and not self._staged_docs
            else None
        )
        if alt_prompt:
            self.input.delete("1.0", "end")
            self.transcript.add_static_message("user", text, label="You")
            self._staged_docs = {}
            self._render_staging_grid()
            self._render_conversation_files(self._conversation_files)
            self._start_image_generation(self.conv_id, alt_prompt)
            return
        # Falling through to a normal chat turn - the conversation's tail is
        # about to become plain text, so a *later* "make it ..." shouldn't
        # still think the last message was an image.
        self._last_image_by_conv.pop(self.conv_id, None)
        self.input.delete("1.0", "end")
        photos = []
        for img in images:
            # Staged images are already fully in memory (base64, from either
            # a drag-drop read or a fresh upload) - no fetch needed, just
            # decode straight into the same path load_conversation() uses for
            # history, instead of the old "[image attached]" text stub.
            photo = self._photo_cache.get(("staged_echo", id(img)), img["data"], TRANSCRIPT_IMAGE_PX)
            if photo is not None:
                photos.append(photo)
        self.transcript.add_static_message("user", text, label="You", images=photos)   # BF-13: under the text
        # The assistant bubble (and, if this turns out to be a reasoning
        # model, a "[ THINKING ]" block before it) is started lazily in
        # _poll_stream_queue() the moment real output actually exists -
        # not here - since which one comes first isn't known until the
        # stream tells us.
        self._staged_images = []
        # Staged docs were already uploaded server-side the moment they were
        # dropped - "sending" just promotes them from the staging grid to
        # the plain attached-files list, no new API call needed.
        self._staged_docs = {}
        self._render_staging_grid()
        self._render_conversation_files(self._conversation_files)

        conv_id = self.conv_id
        state = _StreamState(threading.Event())
        self._streams[conv_id] = state
        # Real-stage progress tracking (rail_status label) - see
        # _poll_stream_queue(). Token counts are actual SSE deltas received
        # (llama.cpp's OpenAI-compatible stream sends one per generated
        # token, on either the content or reasoning_content field), first-
        # chunk timestamps are set the instant the first one of each kind
        # lands, so every tok/s figure is a real measurement, never an
        # estimate.
        self._sync_rail_for_current_conv()
        # Re-render now (cheap - uses the already-cached conversation list,
        # no network round trip) so this row is registered in
        # _chat_status_labels immediately; otherwise it'd keep showing idle
        # until the next full _refresh_chat_list(), and the spinner tick
        # would have nothing to animate in the meantime.
        self._render_chat_list_rows()
        stream_fn = lambda: self.api.stream_chat(conv_id, text, images=images, stop_event=state.stop_event)
        self._launch_stream(conv_id, state, stream_fn, "send")

    def _launch_stream(self, conv_id, state, stream_fn, kind):
        """Start one reply stream - its worker thread and its 50 ms poll - and (B14 H13) say so through the fx seam:
        chat.launch{conv_id, kind} (send / background / edit / regenerate), on the Tk thread. The four stream launches
        (send_message, _start_background_chat_send, _submit_edit, _on_regenerate_clicked) all come through here."""
        threading.Thread(target=self._stream_worker, args=(stream_fn, state), daemon=True).start()
        self.after(50, self._poll_stream_queue, conv_id)
        self._notify_activity()                 # batch 34: a stream started
        if self._fx_queued == conv_id:
            state.queued_payload = getattr(self, "_dispatch_payload", None)     # part 2: a queued dispatch remembers itself
            self._fx_queued = None
            _fx_emit(self, "chat.queued_dispatch", conv_id=conv_id)      # B14 H15: this launch is an automatic dispatch
        _fx_emit(self, "chat.launch", conv_id=conv_id, kind=kind)

    def _fx_seen(self, conv_id):
        """B14: reply.done / image.finished / chat.first_output's presence fields (AESTHETIC_SPEC 4.3): is conv_id the
        conversation on screen, and is the Chat screen the one shown. Never raises."""
        try:
            visible = bool(self.is_visible())
        except tk.TclError:
            visible = False
        return {"displayed": conv_id == self.conv_id, "visible": visible}

    # ---- message queueing (prepare a follow-up while busy) ----

    # ---- batch 33: a real FIFO per conversation (the user: with one slot only ONE message could be queued per chat;
    # a second was refused, so lining up several meant spreading them over several chats) ----
    QUEUE_CAP = 20                  # per chat; a clear notice at the cap
    QUEUE_RETRY_MS = 300            # part 2: a dispatched queued message that met the slot still held is retried after this
    QUEUE_RETRY_MAX = 12            # ... this many times (a few seconds), then the error shows as any other

    def _backlog(self, conv_id):
        """The chat's own queued messages, oldest first (a list; a single dict seeded by an older test counts as one)."""
        q = self._queued_messages.get(conv_id)
        if q is None:
            return []
        if isinstance(q, dict):
            q = [q]
            self._queued_messages[conv_id] = q
        return q

    def _queued_count(self, conv_id):
        """Everything waiting for this chat: its head in the global queue (waiting for a slot) + its own backlog."""
        return len(self._backlog(conv_id)) + sum(1 for e in self._global_send_queue if e["conv_id"] == conv_id)

    def _has_pending_queued_message(self, conv_id):
        return self._queued_count(conv_id) > 0

    def _sync_queued_indicator(self):
        """The line above the composer: hidden with nothing queued, "Message queued - sends when ready" for one,
        "N queued - next: <text>" for more (the kit line draws it upper-case)."""
        self._notify_activity()
        ind = self.queued_indicator
        if ind is None or self.conv_id is None:
            return
        try:
            n = self._queued_count(self.conv_id)
            if n == 0:
                ind.pack_forget()
                return
            head = next((e for e in self._global_send_queue if e["conv_id"] == self.conv_id), None)
            if head is None:
                head = self._backlog(self.conv_id)[0]
            regen = head.get("kind") == "regenerate"      # part 3: a queued regenerate
            if n == 1:
                text = "Regenerate queued - runs when ready" if regen else "Message queued - sends when ready"
            else:
                preview = "regenerate" if regen else (" ".join((head.get("text") or "").split())[:40] or "(image)")
                text = f"{n} queued - next: {preview}"
            label = getattr(ind, "label", None)
            if label is not None:
                label.configure(text=text.upper())
            if not ind.winfo_manager():
                ind.pack(fill="x", side="bottom", padx=10, before=self._composer_frame)
        except tk.TclError:
            pass

    def _warn_queue_full(self):
        show_toast(self, self.ctx.theme,
                   f"The queue is full ({self.QUEUE_CAP} messages in this chat) - cancel one or wait for a reply to finish.",
                   kind="warning")

    def _requeue_after_slot_409(self, conv_id, state, is_displayed):
        """Batch 33 part 2 (the user: with one slot, a message queued in another chat never went out). A queued message is
        dispatched the instant the previous reply ends on the client; when that reply was STOPPED, the server notices
        the gone client only at its next token write, so for up to one token interval its slot is still held and the
        dispatched request meets 409 "already generating the maximum of N replies". Not an error: the message goes
        back to the FRONT of the global queue, the queued line stays, the transcript is redrawn from the DB (the
        server stored nothing of it), and the dispatch is tried again shortly - QUEUE_RETRY_MAX times at most."""
        payload = dict(state.queued_payload)
        payload["retries"] = payload.get("retries", 0) + 1
        self._streams.pop(conv_id, None)
        self._global_send_queue.insert(0, {"conv_id": conv_id, **payload})
        self._render_chat_list_rows()
        if is_displayed:
            self.load_conversation(conv_id)      # the unsent user bubble goes; the queued line comes back
        else:
            self._sync_rail_for_current_conv()
        self._notify_activity()
        self.after(self.QUEUE_RETRY_MS, self._dispatch_next_global_send)

    def _capture_composer(self):
        """Common capture step for both queue types - returns None if
        there's nothing worth capturing."""
        text = self.input.get("1.0", "end-1c").strip()
        images = self._staged_images
        docs = dict(self._staged_docs)
        if not text and not images:
            return None                # B14-H8: what Send refuses (docs alone) is never queued either
        return {"text": text, "images": images, "docs": docs}

    def _clear_composer_after_capture(self):
        self.input.delete("1.0", "end")
        self._staged_images = []
        self._staged_docs = {}
        self._render_staging_grid()
        self._sync_queued_indicator()
        self._set_empty_state(False)        # B7a ADV1-01: a queued message is not "awaiting input"
        self._sync_rail_for_current_conv()

    def _queue_current_message(self):
        # My own conversation is streaming (or generating an image of its
        # own) - fires via _poll_stream_queue's "done" handling or
        # load_conversation(), once THIS conversation's own job ends.
        if self.conv_id is None:
            return
        if self._queued_count(self.conv_id) >= self.QUEUE_CAP:
            self._warn_queue_full()
            return
        captured = self._capture_composer()
        if captured is None:
            return
        q = self._backlog(self.conv_id)
        q.append(captured)                  # batch 33: behind whatever is already queued, in order
        self._queued_messages[self.conv_id] = q
        self._clear_composer_after_capture()

    def _queue_globally(self):
        # This conversation is idle, but nothing can start yet - fires from
        # whichever completion (any conversation's stream, or an image job)
        # first leaves a free slot, in the order captured (see
        # _poll_stream_queue's "done" handling and _finish_image_generation).
        if self.conv_id is None:
            return
        if self._queued_count(self.conv_id) >= self.QUEUE_CAP:
            self._warn_queue_full()
            return
        captured = self._capture_composer()
        if captured is None:
            return
        if self._has_pending_queued_message(self.conv_id):
            # batch 33: one already waits for a slot (the chat's head in the global queue): this one goes behind
            # it in the chat's own backlog, so the order across both queues is the order typed
            q = self._backlog(self.conv_id)
            q.append(captured)
            self._queued_messages[self.conv_id] = q
        else:
            self._global_send_queue.append({"conv_id": self.conv_id, **captured})
        self._clear_composer_after_capture()

    def _warn_already_queued(self):
        # Send is already disabled/relabeled "Queued" once one exists (see
        # _set_send_button_for_busy), so this only happens via Enter -
        # which isn't gated on the button's state, since it's a binding on
        # a different widget. Refuse rather than silently overwrite
        # whatever's already captured.
        show_toast(
            self, self.ctx.theme,
            "You already have a queued message - cancel it first to queue a different one.",
            kind="warning",
        )

    def _cancel_queued_message(self):
        if self.conv_id is None:
            return
        q = self._backlog(self.conv_id)
        queued = q.pop() if q else None     # batch 33: the LAST queued one comes back for editing
        if not q:
            self._queued_messages.pop(self.conv_id, None)
        if queued is None:
            for i, entry in enumerate(self._global_send_queue):
                if entry["conv_id"] == self.conv_id:
                    entry = self._global_send_queue.pop(i)
                    queued = {"text": entry["text"], "images": entry["images"], "docs": entry["docs"]}
                    break
        self._sync_queued_indicator()
        if queued and queued.get("kind") == "regenerate":
            queued = None                       # part 3: a dropped regenerate has nothing to hand back
        if queued:
            # Handed back for editing, not discarded - cancelling a queue
            # almost always means "actually, let me change this", not
            # "throw it away" (which deleting the restored text afterward
            # already covers for the rare case it really does mean that).
            self.input.delete("1.0", "end")
            self.input.insert("1.0", queued["text"])
            self._staged_images = queued["images"]
            self._staged_docs = queued["docs"]
            self._render_staging_grid()
        self._sync_rail_for_current_conv()

    def _fire_queued_message(self, conv_id):
        """Called only once conv_id is confirmed free (popped from
        self._streams) - either live, from _poll_stream_queue's "done"
        handling while this conversation is the one on screen, or from
        load_conversation() discovering on arrival that it already finished
        while the user was looking elsewhere. Both callers already know
        conv_id == self.conv_id before calling this."""
        q = self._backlog(conv_id)
        if not q:
            self._queued_messages.pop(conv_id, None)
            return
        if conv_id == self.conv_id and self._editing_message_id is not None:
            return                              # B14-H5: held while an edit is open; fired after it
        head = q.pop(0)                         # batch 33: the oldest first
        if self._image_gen is not None or len(self._streams) >= self._cached_max_parallel_chats:
            # no slot right now (another chat took it, an image job): the head waits in the global queue, in order
            self._global_send_queue.append({"conv_id": conv_id, **head})
        else:
            self._dispatch_captured_message(conv_id, head["text"], head["images"], head["docs"],
                                            kind=head.get("kind"), message_id=head.get("message_id"))
        if not q:
            self._queued_messages.pop(conv_id, None)
        if conv_id == self.conv_id:
            self._sync_queued_indicator()
        if q and conv_id not in self._streams and not any(e["conv_id"] == conv_id for e in self._global_send_queue):
            self.after_idle(self._pump_backlogs)    # part 4: the head launched nothing - the next one must not strand

    def _dispatch_next_global_send(self):
        """Drains as much of the global queue as current capacity allows -
        called after ANY completion (a chat stream's "done", or an image
        job finishing), never gated on what's currently displayed, since a
        global scheduler has to react to every completion, not just the
        one being watched. Order preserved: always the front of the list
        (oldest first)."""
        held = []
        while self._global_send_queue and self._image_gen is None \
                and len(self._streams) < self._cached_max_parallel_chats:
            entry = self._global_send_queue.pop(0)
            if self._dispatch_captured_message(entry["conv_id"], entry["text"], entry["images"], entry["docs"],
                                               retries=entry.get("retries", 0), kind=entry.get("kind"),
                                               message_id=entry.get("message_id")) is False:
                held.append(entry)          # B14-H5: the displayed chat is in edit mode - back in front, after it
        self._global_send_queue[0:0] = held
        self._pump_backlogs()
        self._notify_activity()

    def _pump_backlogs(self):
        """Batch 33 part 4: a chat whose own backlog has a head but no stream of its own and no entry in the global
        queue (its previous dispatch launched nothing - a dropped regenerate, a refused docs-only capture) would wait
        for a completion that never comes; while a slot is free, such heads go out (or are parked in the global
        queue by _fire_queued_message when the slot was taken meanwhile). Bounded: one pass over the chats."""
        try:
            for conv_id in list(self._queued_messages):
                if self._image_gen is not None or len(self._streams) >= self._cached_max_parallel_chats:
                    return
                if not self._backlog(conv_id) or conv_id in self._streams:
                    continue
                if any(e["conv_id"] == conv_id for e in self._global_send_queue):
                    continue
                if conv_id == self.conv_id and self._editing_message_id is not None:
                    continue
                self._fire_queued_message(conv_id)
        except tk.TclError:
            pass

    def _dispatch_captured_message(self, conv_id, text, images, docs, retries=0, kind=None, message_id=None):
        """Sends a previously-captured (queued) message for conv_id -
        whichever queue it came from. If conv_id is currently displayed,
        reuses the exact normal send path (restore into composer, call
        send_message()) to get full image-gen/edit intent detection for
        free; otherwise starts a plain background chat send that never
        touches the composer/transcript widgets, which are showing a
        different conversation right now - correctly rebuilt from the DB
        whenever the user does navigate here, same as any other background
        stream (see load_conversation's resume_raw handling). -> False when the message was NOT dispatched (B14-H5:
        the displayed chat is in edit mode - it must wait, never go out as that edit), else True."""
        if conv_id == self.conv_id and self._editing_message_id is not None:
            return False
        self._dispatch_payload = {"text": text, "images": images, "docs": docs, "retries": retries,
                                  "kind": kind, "message_id": message_id}       # part 2: for a 409
        self._fx_queued = conv_id          # B14 H15: the launch this leads to is an automatic one (silent)
        if kind == "regenerate":
            # part 3: a queued regenerate runs through the real regenerate path (the old reply archived as a version;
            # never an edit, never a new message). On screen it must still be the latest reply; off screen the
            # server decides ("only the most recent response can be regenerated" -> the failed-regenerate notice).
            if conv_id == self.conv_id:
                self.queued_indicator.pack_forget()
                if not self.transcript.is_last_message(message_id):
                    self._fx_queued = None
                    show_toast(self, self.ctx.theme, "Regenerate dropped: that reply is no longer the latest.", kind="warning")
                    self._sync_queued_indicator()
                    return True
                self._start_regenerate(conv_id, message_id)
            else:
                self._start_regenerate(conv_id, message_id, background=True)
            self._fx_queued = None
            return True
        if conv_id == self.conv_id:
            self.queued_indicator.pack_forget()
            self.input.delete("1.0", "end")
            self.input.insert("1.0", text)
            self._staged_images = images
            self._staged_docs = docs
            self._render_staging_grid()
            self.send_message()
            if not self._send_when_loaded:
                self._fx_queued = None     # launched (the arm is spent) or nothing to launch: never a stale arm
            self._sync_queued_indicator()   # batch 33: the rest of the backlog stays announced
        else:
            self._start_background_chat_send(conv_id, text, images)
            self._fx_queued = None
        return True

    def forget_conversation(self, conv_id):
        """B14-H7: a deleted conversation's queued message (either queue) is dropped - never dispatched to a chat
        that is gone (the server failed it in the background: a fault with nothing on screen to explain it).
        ChatView's own delete and the Chats / Projects screens (ctx.on_conversation_deleted) call it. Never raises."""
        try:
            self._queued_messages.pop(conv_id, None)
            self._global_send_queue[:] = [e for e in self._global_send_queue if e["conv_id"] != conv_id]
            self._notify_activity()                 # batch 34: a chat is gone
            if self._fx_queued == conv_id:
                self._fx_queued = None
            if conv_id == self.conv_id:
                self.queued_indicator.pack_forget()
                self._sync_rail_for_current_conv()
        except tk.TclError:
            pass

    def _start_background_chat_send(self, conv_id, text, images):
        """Plain chat send for a conversation that isn't currently
        displayed - no image-gen/edit intent detection (those need the
        composer/transcript context this deliberately skips), just a real
        message + reply like any other background stream. Staged docs
        need nothing here - they were already uploaded server-side at drop
        time (see send_message()'s own comment on _staged_docs); promoting
        them from "staging grid" to "plain list" is a purely client-side
        display distinction for whichever conversation is on screen, not
        something the server needs to be told about."""
        state = _StreamState(threading.Event())
        self._streams[conv_id] = state
        self._render_chat_list_rows()
        stream_fn = lambda: self.api.stream_chat(conv_id, text, images=images, stop_event=state.stop_event)
        self._launch_stream(conv_id, state, stream_fn, "background")
        self._sync_rail_for_current_conv()      # B7B-H9: the displayed chat's SEND / QUEUE follows the slot now taken

    def _on_edit_message_clicked(self, message_id):
        if self.conv_id is None or self.conv_id in self._streams:
            show_toast(self, self.ctx.theme, "Can't edit a message while a reply is generating.", kind="warning")
            return
        if self._has_pending_queued_message(self.conv_id):
            # B14-H5: a queued message dispatched while an edit is open would go out as that EDIT (every later
            # message deleted, the half-typed correction gone too): the same refusal as while streaming
            show_toast(self, self.ctx.theme, "Can't edit a message while a message is queued - cancel the queued message first.",
                       kind="warning")
            return
        raw_text = self.transcript.raw_text_for(message_id)
        if raw_text is None:
            return
        self._editing_message_id = message_id
        self.input.delete("1.0", "end")
        self.input.insert("1.0", raw_text)
        self.input.focus_set()
        self.editing_indicator.pack(fill="x", side="bottom", padx=10, before=self._composer_frame)

    def _cancel_edit(self):
        self._editing_message_id = None
        self.editing_indicator.pack_forget()
        self.input.delete("1.0", "end")
        self._dispatch_next_global_send()  # B14-H5: a dispatch held back by the edit goes out now

    def _submit_edit(self):
        message_id = self._editing_message_id
        new_text = self.input.get("1.0", "end-1c").strip()
        if not new_text:
            return
        self._editing_message_id = None
        self.editing_indicator.pack_forget()
        self.input.delete("1.0", "end")
        # Optimistic, same as a fresh send's add_static_message() call above -
        # the real edit hasn't round-tripped to the server yet, but there's
        # nothing meaningful to roll back to display-wise if it fails (the
        # error still shows inline via _poll_stream_queue's error handling,
        # same as any other failed turn).
        self.transcript.truncate_after_message(message_id)
        self.transcript.replace_message_content(message_id, new_text)

        conv_id = self.conv_id
        state = _StreamState(threading.Event())
        self._streams[conv_id] = state
        self._sync_rail_for_current_conv()
        self._render_chat_list_rows()
        stream_fn = lambda: self.api.stream_edit_message(
            conv_id, message_id, content=new_text, stop_event=state.stop_event
        )
        self._launch_stream(conv_id, state, stream_fn, "edit")

    def _on_regenerate_clicked(self, message_id):
        # B14-H6: send_message's busy gate too (an image job, or every slot held by other chats): the request
        # used to reach the server and come back as its 409
        if self.conv_id is None:
            show_toast(self, self.ctx.theme, "Can't regenerate while a reply is generating.", kind="warning")
            return
        if (self.conv_id in self._streams or self._image_gen is not None
                or len(self._streams) >= self._cached_max_parallel_chats):
            self._queue_regenerate(message_id)     # part 3: queued, in click order with the messages
            return
        self._start_regenerate(self.conv_id, message_id)

    def _queue_regenerate(self, message_id):
        """Batch 33 part 3: Regenerate while this chat (or, at one slot, the whole app) is busy joins the queue - the
        chat's own backlog when its own reply is streaming, else the global queue behind nothing (or the backlog
        behind the chat's head already waiting for a slot). One queued regenerate per reply."""
        conv_id = self.conv_id
        already = any(e.get("kind") == "regenerate" and e.get("message_id") == message_id
                      for e in self._backlog(conv_id) + [g for g in self._global_send_queue if g["conv_id"] == conv_id])
        if already:
            show_toast(self, self.ctx.theme, "Can't queue another regenerate for that reply while one is already queued.",
                       kind="warning")
            return
        if self._queued_count(conv_id) >= self.QUEUE_CAP:
            self._warn_queue_full()
            return
        entry = {"kind": "regenerate", "message_id": message_id, "text": "", "images": [], "docs": {}}
        own_busy = conv_id in self._streams or (self._image_gen is not None and self._image_gen.get("conv_id") == conv_id)
        if own_busy or self._has_pending_queued_message(conv_id):
            q = self._backlog(conv_id)
            q.append(entry)
            self._queued_messages[conv_id] = q
        else:
            self._global_send_queue.append({"conv_id": conv_id, **entry})
        self._sync_queued_indicator()
        self._set_empty_state(False)
        self._sync_rail_for_current_conv()

    def _start_regenerate(self, conv_id, message_id, background=False):
        """The regenerate launch (the body of the old click handler): on screen the stale reply's rendering goes
        first; a background regenerate (part 3: a queued one for a chat off screen) touches no widget - the next
        load draws the DB (the old reply until the new one lands, then the new one with its versions)."""
        # No confirmation dialog - regenerating is no longer destructive: the
        # reply being replaced is kept as a version and the "[<] 2/3 [>]"
        # control put on the new one flips straight back to it.
        # Clears the stale reply's own rendering (chip/content/action link)
        # first, or the fresh one streaming in below would just be appended
        # after it instead of visually replacing it.
        if not background:
            self.transcript.remove_message(message_id)
        state = _StreamState(threading.Event())
        # Marks this stream as replacing an existing reply rather than adding
        # one, which changes two things once it finishes: the Thoughts count
        # doesn't grow (this turn's reasoning overwrites the old row's, it
        # isn't a new transcript), and a run that produces nothing needs a
        # reload, since the reply still in the DB is no longer on screen.
        state.regenerating_message_id = message_id
        self._streams[conv_id] = state
        self._sync_rail_for_current_conv()
        self._render_chat_list_rows()
        stream_fn = lambda: self.api.stream_edit_message(conv_id, message_id, stop_event=state.stop_event)
        self._launch_stream(conv_id, state, stream_fn, "regenerate")

    def _on_switch_version_clicked(self, message_id, position):
        """Flip a regenerated reply to another of its saved versions. A plain
        DB swap server-side - no generation - so this is a short synchronous
        call rather than a streamed one, same as every other transcript click
        handler here."""
        conv_id = self.conv_id
        if conv_id is None:
            return
        if conv_id in self._streams:
            show_toast(
                self, self.ctx.theme,
                "Can't switch response while a reply is generating.", kind="warning",
            )
            return
        try:
            result = self.api.switch_message_version(conv_id, message_id, position)
        except ApiError as e:
            show_toast(self, self.ctx.theme, f"Could not switch response: {e}", kind="warning")
            return
        if self.transcript.is_last_message(message_id):
            # Fast path, and the one that actually matters: re-render just
            # this bubble. replace_message_content() only works on the last
            # message (it rebuilds by appending, the only thing Tk's Text can
            # do), which is exactly where a reply sits right after being
            # regenerated.
            photos = []                         # BF-13: decoded first, then drawn inside the replaced message
            for img in result.get("images") or []:
                try:
                    raw, _ = self.api.get_message_image_bytes(message_id, img["id"])
                except ApiError:
                    continue
                b64 = base64.b64encode(raw).decode("ascii")
                photo = self._photo_cache.get(("msg_image", message_id, img["id"]), b64, TRANSCRIPT_IMAGE_PX)
                if photo is not None:
                    photo._image_ref = (message_id, img["id"], img.get("filename") or "image.png")
                    photos.append(photo)
            self.transcript.replace_message_content(message_id, result.get("content") or "", images=photos,
                                                    inserts=self._document_inserts(result))
            self.transcript.set_message_versions(
                message_id, result.get("version_pos") or 1, result.get("version_count") or 1
            )
        else:
            # Switching a version further up the conversation - rebuild the
            # whole transcript from the DB, since the replacement text can be
            # any length and everything below it has to move. Staged
            # attachments are saved across the reload: load_conversation()
            # deliberately drops them on a conversation *switch*, but this
            # isn't one, and silently binning a file the user just dropped
            # would be a nasty surprise.
            staged_images, staged_docs = self._staged_images, self._staged_docs
            top_fraction = self.transcript.yview()[0]

            def restore_view():
                # Runs once the rebuilt transcript is on screen (the reload is
                # asynchronous now), not straight after load_conversation().
                self._staged_images, self._staged_docs = staged_images, staged_docs
                self._render_staging_grid()
                # Close enough to be unnoticeable: only one message's text
                # changed, so the same scroll fraction lands within a line or
                # two of where the user was reading.
                self.transcript.yview_moveto(top_fraction)

            self.load_conversation(conv_id, on_loaded=restore_view)

    def stop_streaming(self):
        # Image generation is global (see self._image_gen's own comment) so
        # this takes priority - there's only ever one to cancel, regardless
        # of which conversation is displayed. Otherwise, only ever stops
        # whatever's generating for the *displayed* conversation - a
        # background chat stream keeps going untouched, same as any other
        # screen navigation.
        if self._image_gen is not None:
            if not self._image_gen.get("stop_acked"):    # B14 H22: acknowledged once per job (FIX1, ADV1-02)
                self._image_gen["stop_acked"] = True
                _fx_emit(self, "chat.stop_requested", conv_id=self._image_gen.get("conv_id"), target="image")
            threading.Thread(target=self._cancel_image_gen_worker, daemon=True).start()
            return
        state = self._streams.get(self.conv_id)
        if state is not None and not state.stop_event.is_set():     # B14 H22: acknowledged once, at the click
            _fx_emit(self, "chat.stop_requested", conv_id=self.conv_id, target="chat")
            state.stop_event.set()

    def _cancel_image_gen_worker(self):
        # Fire-and-forget: sd-server has no in-place cancel for a request
        # it's already computing (see cancel_image_gen()'s docstring), so
        # this just kills the process - the existing status-polling loop
        # (_poll_image_gen_status/_handle_image_gen_status) picks up the
        # resulting "cancelled" status on its own next tick regardless of
        # whether this call itself succeeds or errors.
        try:
            self.api.cancel_image_gen()
        except Exception:
            pass

    def _stream_worker(self, stream_fn, state):
        """stream_fn: zero-arg callable returning a stream_chat/
        stream_edit_message generator - parameterized (rather than always
        calling self.api.stream_chat directly) so the same worker/queue/poll
        machinery serves both a normal send and an edit-and-resubmit/
        regenerate turn, which hit a different endpoint (see _submit_edit/
        _on_regenerate_clicked)."""
        try:
            for kind, value in stream_fn():
                state.stream_queue.put((kind, value))
        except ApiError as e:
            state.stream_queue.put(("error", str(e)))
        except Exception as e:
            state.stream_queue.put(("error", str(e)))
        finally:
            state.stream_queue.put(("done", None))

    def _fetch_prompt_progress(self, conv_id, state):
        # Runs off the Tk main thread - a real HTTP round trip to server.py's
        # /api/generation-progress (which itself peeks at llama-server's own
        # /slots endpoint, for the specific slot this conv_id was pinned to),
        # never called from inside the poll loop directly. Only touches
        # `state`, a plain object - never a Tk widget - so this is safe to
        # run on a worker thread same as before.
        try:
            result = self.api.get_generation_progress(conv_id)
            state.progress_queue.put(result.get("prompt_tokens_processed"))
        except ApiError:
            pass
        finally:
            state.progress_fetch_in_flight = False

    def _poll_stream_queue(self, conv_id):
        state = self._streams.get(conv_id)
        if state is None:
            return  # already finished and popped by an earlier tick

        pieces = []
        thinking_pieces = []
        doc_events = []                         # document tool P2: (offset within this tick's text, frame), in order
        done = False
        error = None
        connected = False
        message_ids = None
        try:
            while True:
                kind, payload = state.stream_queue.get_nowait()
                if kind == "chunk":
                    pieces.append(payload)
                elif kind == "thinking":
                    thinking_pieces.append(payload)
                elif kind == "status" and payload == "connected":
                    connected = True
                elif kind == "error":
                    error = payload
                elif kind == "done":
                    done = True
                elif kind == "message_ids":
                    message_ids = payload
                elif kind == "truncated":
                    state.truncated = payload
                elif kind == "document":
                    doc_events.append((len("".join(pieces)), payload))
        except queue.Empty:
            pass
        try:
            while True:
                state.last_prompt_progress = state.progress_queue.get_nowait()
        except queue.Empty:
            pass

        # Not "displayed" while its history is still loading: the transcript is
        # empty until _apply_conversation draws it, so anything fed in now would
        # sit above the history. The chunks are still recorded in the stream
        # state below, and _apply_conversation re-attaches them.
        is_displayed = conv_id == self.conv_id and conv_id != self._loading_conv_id
        if (error and state.queued_payload is not None and not state.raw_text and not state.thinking_raw
                and not state.assistant_started and "already generating" in error
                and state.queued_payload.get("retries", 0) < self.QUEUE_RETRY_MAX):
            # batch 33 part 2: the server's slot was still held for a moment - back in front of the queue, retried
            self._requeue_after_slot_409(conv_id, state, is_displayed)
            return

        # Real-stage progress, in order: SENDING REQUEST (before connected) ->
        # WAITING FOR RESPONSE / PROCESSING PROMPT (connected, model hasn't
        # produced any output yet - see _fetch_prompt_progress for where the
        # live token count comes from) -> THINKING (a reasoning model's
        # chain-of-thought, if any) -> GENERATING (the real answer). Every
        # number shown is a real observed count, never fabricated. Computed
        # into state.status_text unconditionally (not just when displayed)
        # so switching to this conversation later shows an up-to-date figure
        # immediately instead of a stale one from whenever it was last shown.
        if connected and not state.prefill_done:
            state.status_text = "WAITING FOR RESPONSE"
            now = time.time()
            if not state.progress_fetch_in_flight and now - state.last_progress_poll_time >= 0.3:
                state.last_progress_poll_time = now
                state.progress_fetch_in_flight = True
                threading.Thread(target=self._fetch_prompt_progress, args=(conv_id, state), daemon=True).start()
            if state.last_prompt_progress:
                state.status_text = f"PROCESSING PROMPT...\n{state.last_prompt_progress:,} tokens"
        if error:
            state.last_error = error
        if (pieces or thinking_pieces or error or done) and not state.prefill_done:
            state.prefill_done = True
            if (pieces or thinking_pieces) and not state.stop_event.is_set():     # B14 H18: once per reply
                _fx_emit(self, "chat.first_output", conv_id=conv_id, **self._fx_seen(conv_id))
        if thinking_pieces and state.gen_token_count == 0:
            if state.thinking_first_time is None:
                state.thinking_first_time = time.time()
            state.thinking_token_count += len(thinking_pieces)
        if pieces:
            if state.gen_first_chunk_time is None:
                state.gen_first_chunk_time = time.time()
            state.gen_token_count += len(pieces)

        if not done:
            if state.gen_token_count > 0:
                elapsed = time.time() - state.gen_first_chunk_time
                if elapsed >= 0.2:
                    speed = state.gen_token_count / elapsed
                    state.status_text = f"GENERATING...\n{state.gen_token_count} tokens, {speed:.1f} tok/s"
                else:
                    state.status_text = f"GENERATING...\n{state.gen_token_count} tokens"
            elif state.thinking_token_count > 0:
                elapsed = time.time() - state.thinking_first_time
                if elapsed >= 0.2:
                    speed = state.thinking_token_count / elapsed
                    state.status_text = f"THINKING...\n{state.thinking_token_count} tokens, {speed:.1f} tok/s"
                else:
                    state.status_text = f"THINKING...\n{state.thinking_token_count} tokens"

        stage_word = (state.status_text or "").split("\n")[0]
        if stage_word != state._notified_stage:
            state._notified_stage = stage_word
            self._notify_activity()                 # batch 34: the stage word changed (not the token count)
        # Mirror what would be fed into the transcript regardless of whether
        # this conversation is displayed right now - see _StreamState and
        # load_conversation()'s resume_raw comment for why.
        if thinking_pieces:
            state.thinking_raw += "".join(thinking_pieces)
        tick_base = len(state.raw_text)
        if pieces:
            state.raw_text += "".join(pieces)
        for rel, frame in doc_events:
            self._record_document_event(conv_id, state, frame, tick_base + rel)
        if done and any(d.get("state") == "creating" for d in state.documents):
            # the reply ended with a call still open (stopped, or the server died mid-call): nothing was stored
            for d in list(state.documents):
                if d.get("state") == "creating":
                    ended = {"state": "failed", "index": d.get("index", 0),
                             "reason": "the reply ended before the document was complete"}
                    doc_events.append((len("".join(pieces)), ended))
                    self._record_document_event(conv_id, state, ended, len(state.raw_text))

        if is_displayed:
            # start_thinking()/start_message() are both called lazily, right
            # here, the first time each kind of real output actually shows up
            # - never eagerly in send_message() - since whether a "thinking"
            # block precedes the answer isn't known until the stream says so.
            # Guarded on thinking_finished, not gen_token_count == 0 (unlike
            # the counting block above) - by this point in the tick
            # gen_token_count may already include *this same tick's* pieces
            # (counted earlier above), which would wrongly read as "content
            # already started, drop these thinking pieces" if a tick ever
            # contains both kinds at once (the last thinking chunks and the
            # first content chunk landing in the same 50ms poll window).
            if thinking_pieces and not state.thinking_finished:
                if not state.thinking_started:
                    self.transcript.start_thinking()
                    state.thinking_started = True
                self.transcript.feed_thinking("".join(thinking_pieces))
            if pieces or error or doc_events:
                if state.thinking_started and not state.thinking_finished:
                    self.transcript.finish_thinking()
                    state.thinking_finished = True
                if not state.assistant_started:
                    self.transcript.start_message("assistant", label="Assistant")
                    state.assistant_started = True
            if doc_events:
                self._feed_with_documents(state, pieces, doc_events)
            elif pieces:
                self.transcript.feed("".join(pieces))
            if error:
                self.transcript.feed(f"\n\n_error: {error}_")
            self._sync_thoughts_inflight()          # batch 32: the card's caption follows the thinking
            self._sync_jump_cue()
            if done:
                if state.thinking_started and not state.thinking_finished:
                    # Stopped/errored out entirely during the thinking phase -
                    # no answer content ever arrived, so there's no assistant
                    # bubble to finish, just the thinking block itself.
                    self.transcript.finish_thinking()
                    state.thinking_finished = True
                if state.assistant_started:
                    self.transcript.finish_message()
            if message_ids:
                # Backfills the just-streamed pair's real DB ids onto their
                # already-rendered transcript entries (see
                # MarkdownText.tag_last_message_ids) so the Edit/Regenerate
                # links added by finish_message() above become clickable -
                # only meaningful while this conversation is the one on
                # screen; a backgrounded stream's message_ids are simply
                # dropped here and picked up correctly anyway the next time
                # load_conversation() reloads this conversation from scratch.
                self.transcript.tag_last_message_ids(message_ids["user"], message_ids["assistant"])
                # Same frame carries how many versions the reply now has, so
                # a just-regenerated one gets its "[<] 2/3 [>]" control the
                # moment it lands rather than only after a reload.
                self.transcript.set_message_versions(
                    message_ids["assistant"],
                    message_ids.get("version_pos") or 1,
                    message_ids.get("version_count") or 1,
                )

        if done:
            # Bumps the label only - the transcript itself was persisted
            # server-side alongside the reply (see _stream_and_save). Gated on
            # raw_text for the same reason the DB write is: a turn stopped
            # during the thinking phase never produced an assistant message,
            # so there was no row to hang the reasoning on and nothing was
            # saved. Counting it here would leave the button promising an
            # entry the panel then can't show.
            # A regenerate is exempt: its reasoning overwrites the row it
            # replaces rather than adding a row, so the number of saved
            # transcripts is unchanged and bumping here would have the button
            # promise one more than /thoughts can return.
            if (
                state.thinking_raw.strip() and state.raw_text.strip()
                and state.regenerating_message_id is None
            ):
                self._thoughts_counts[conv_id] = self._thoughts_counts.get(conv_id, 0) + 1
                if is_displayed:
                    self._update_thoughts_button()
            # B14 H16: every outcome (done / stopped / error / empty; a truncated reply is 'ok'), before the pop
            _fx_emit(self, "reply.done", conv_id=conv_id, outcome=_reply_outcome(state), **self._fx_seen(conv_id))
            self._streams.pop(conv_id, None)
            self._notify_activity()                 # batch 34: a stream ended
            self._sync_jump_cue()                   # batch 32: the stream is over - the cue goes
            self._sync_thoughts_inflight()
            if state.truncated and is_displayed and not (
                state.regenerating_message_id is not None and not state.raw_text.strip()
            ):
                # The reply says so in its own last line too (the server saved it that way); the toast is for someone who has
                # scrolled away from the end of a long one. (A regenerate that produced nothing reports it just below.)
                show_toast(self, self.ctx.theme, cut_off_toast(state.truncated), kind="warning", duration_ms=12000)
            if state.regenerating_message_id is not None and not state.raw_text.strip():
                # Regenerating produced nothing - stopped, errored, or the
                # model answered with reasoning only. The reply it was meant
                # to replace is still in the DB untouched (the server no
                # longer deletes it up front), but the transcript already
                # removed it optimistically, so the screen is now missing a
                # message that still exists. Put it back.
                if is_displayed:
                    self.load_conversation(conv_id)
                    detail = f": {state.last_error}" if state.last_error else (
                        f": {cut_off_toast(state.truncated)}" if state.truncated else "."
                    )
                    show_toast(
                        self, self.ctx.theme,
                        f"Regenerate produced no reply{detail} Kept the previous response.",
                        kind="warning",
                    )
            # "Unseen" only for a reply the user didn't actually watch land -
            # requires both that this conversation wasn't the one displayed
            # *and* that the chat screen itself was visible (self.conv_id
            # can still equal conv_id while some other screen, e.g.
            # Settings, is what's actually on screen - that shouldn't count
            # as having seen it either). Gated on state.raw_text (not
            # state.assistant_started - that flag only flips once
            # transcript.start_message() is actually called, which itself
            # only happens while is_displayed, so a stream that's
            # backgrounded from its very first chunk would never set it)
            # so a stopped/errored turn that never produced real content
            # doesn't flag a "new message" that isn't actually there to read.
            if state.raw_text and not (is_displayed and self.is_visible()):
                self._unseen_conv_ids.add(conv_id)
            # A completed reply can change this conversation's title (the
            # synchronous truncated placeholder, set the instant the first
            # message was sent) or its position (updated_at bump) in the
            # sidebar - refresh now for that, and start polling for the
            # AI-generated title too, which lands in its own background
            # thread server-side well after the reply itself finishes
            # streaming (see _start_title_poll - a single fixed-delay retry
            # here used to miss it entirely on slow/CPU-fallback models).
            # Unconditional, not gated on is_displayed - a background
            # conversation's title can change just as well.
            self._refresh_chat_list()
            self._start_title_poll(conv_id)
            if is_displayed:
                # Refresh after every reply - success, error, or stopped
                # mid-stream - matching the web UI (a stopped/failed turn
                # still changed what's stored, so the count can too).
                self._sync_rail_for_current_conv()
                self.context_meter.refresh(conv_id)
            if self._backlog(conv_id):
                # batch 33: the chat's own next queued message goes out whether or not the chat is on screen (a
                # background send when it is not - the old single entry waited to be looked at)
                self._fire_queued_message(conv_id)
            # Unconditional, not gated on is_displayed - a slot just freed
            # up regardless of which conversation is on screen, and the
            # global queue (see _queue_globally) can belong to any
            # conversation. Runs after the per-conversation fire above so
            # it sees accurate remaining capacity if that fire just started
            # a new stream of its own.
            self._dispatch_next_global_send()
            return
        if is_displayed:
            self._sync_rail_for_current_conv()
        self.after(50, self._poll_stream_queue, conv_id)

    def _start_title_poll(self, conv_id):
        """Bounded background poll of /title-status, same worker-thread +
        queue.Queue + self.after() shape as _stream_worker/_poll_stream_queue
        - the actual HTTP round-trip must never happen directly inside an
        after() callback, that would block the Tk main thread. Harmless to
        call even when this turn never triggered title generation at all
        (not the conversation's first message) - the server just reports
        pending=False immediately and this resolves in one quick round trip."""
        q = queue.Queue()
        threading.Thread(target=self._title_poll_worker, args=(conv_id, q), daemon=True).start()
        self.after(200, self._poll_title_queue, conv_id, q)

    def _title_poll_worker(self, conv_id, q):
        # ~10s of fast polling covers the common case; a still-pending result
        # after that (a large/CPU-fallback model) is reported as such so the
        # Tk-side poller can fall back to one distant safety-net refresh
        # instead of hammering the endpoint further.
        for _ in range(20):
            time.sleep(0.5)
            try:
                status = self.api.get_title_status(conv_id)
            except Exception:
                q.put("unknown")
                return
            if not status.get("pending"):
                q.put("done")
                return
        q.put("still_pending")

    def _poll_title_queue(self, conv_id, q):
        try:
            result = q.get_nowait()
        except queue.Empty:
            self.after(200, self._poll_title_queue, conv_id, q)
            return
        self._refresh_chat_list()
        if result != "done":
            self.after(15000, self._refresh_chat_list)

    # ---- in-chat image generation/editing ----
    # ("generate an image of ..." starts a new one; a follow-up like "make it
    # nighttime" - see match_image_edit_intent() - edits whatever
    # _last_image_by_conv says this conversation's last image was.)

    def _start_image_generation(self, conv_id, prompt):
        self._start_image_job(conv_id, lambda: self.api.generate_image(conv_id, prompt))

    def _start_image_edit(self, conv_id, prompt, source):
        self._start_image_job(
            conv_id,
            lambda: self.api.edit_image(conv_id, source["message_id"], source["image_id"], prompt),
        )

    def _start_image_job(self, conv_id, launch_call):
        self._image_gen = {"conv_id": conv_id, "stage": "unloading_chat_model", "started_at": time.time()}
        self._notify_activity()                     # batch 34: an image job started
        self._sync_rail_for_current_conv()
        self._render_chat_list_rows()
        q = queue.Queue()
        threading.Thread(target=self._image_gen_launch_worker, args=(q, launch_call), daemon=True).start()
        self.after(50, self._poll_image_gen_launch, q)
        if self._fx_queued == conv_id:
            self._fx_queued = None
            _fx_emit(self, "chat.queued_dispatch", conv_id=conv_id)      # B14 H15: an automatic dispatch's image launch
        _fx_emit(self, "chat.launch", conv_id=conv_id, kind="image")      # B14 H14

    def _image_gen_launch_worker(self, q, launch_call):
        try:
            launch_call()
            q.put(("ok", None))
        except ApiError as e:
            q.put(("error", str(e)))
        except Exception as e:
            # Broad on purpose - an uncaught exception here would otherwise
            # kill this thread silently and leave self._image_gen set
            # forever (Send disabled app-wide with no way out but restart).
            q.put(("error", str(e)))

    def _poll_image_gen_launch(self, q):
        try:
            kind, payload = q.get_nowait()
        except queue.Empty:
            self.after(50, self._poll_image_gen_launch, q)
            return
        if kind == "error":
            # Never actually started server-side (e.g. "busy", or "no image
            # model configured") - nothing to poll for.
            self._finish_image_generation(error=payload)
            return
        self.after(500, self._poll_image_gen_status)

    def _poll_image_gen_status(self):
        if self._image_gen is None:
            return  # stopped tracking (e.g. screen torn down) mid-flight
        q = queue.Queue()
        threading.Thread(target=self._image_gen_status_worker, args=(q,), daemon=True).start()
        self.after(50, self._handle_image_gen_status, q)

    def _image_gen_status_worker(self, q):
        try:
            q.put(("ok", self.api.image_gen_status()))
        except ApiError as e:
            q.put(("error", str(e)))
        except Exception as e:
            q.put(("error", str(e)))

    def _handle_image_gen_status(self, q):
        if self._image_gen is None:
            return
        try:
            kind, payload = q.get_nowait()
        except queue.Empty:
            self.after(50, self._handle_image_gen_status, q)
            return
        if kind == "error":
            # A single status-check hiccup shouldn't report "failed" for a
            # job that's actually still running server-side (the status
            # endpoint just reads an in-memory dict - it should never
            # legitimately fail while the server is up) - retry a few times
            # before giving up, so a transient blip doesn't lie about the
            # outcome of a job that would otherwise have succeeded.
            fails = self._image_gen.get("status_fails", 0) + 1
            if fails <= 3:
                self._image_gen["status_fails"] = fails
                self.after(1000, self._poll_image_gen_status)
                return
            self._finish_image_generation(error=payload)
            return
        status = payload
        self._image_gen["status_fails"] = 0
        if status.get("active"):
            self._image_gen["stage"] = status.get("stage")
            self._notify_activity()                 # batch 34: the image stage changed
            self._sync_rail_for_current_conv()
            self.after(1000, self._poll_image_gen_status)
            return
        self._finish_image_generation(error=status.get("error"))

    def _finish_image_generation(self, error=None):
        conv_id = self._image_gen["conv_id"] if self._image_gen else None
        self._image_gen = None
        self._notify_activity()                     # batch 34: the image job ended
        if conv_id is not None:
            # B14 H17: the image's outcome (its error toast owns the fault cue; a cancel is acknowledged at the click)
            _fx_emit(self, "image.finished", conv_id=conv_id, outcome=_image_outcome(error), **self._fx_seen(conv_id))
            if error == "cancelled":
                show_toast(self, self.ctx.theme, "Image generation cancelled", kind="warning")
            elif error:
                if conv_id == self.conv_id:
                    self.transcript.add_static_message(
                        "assistant", f"Image generation failed: {error}", label="Assistant"
                    )
                show_toast(self, self.ctx.theme, f"Image generation failed: {error}", kind="error")
            elif conv_id == self.conv_id:
                # generate_image_worker wrote its own user+assistant messages
                # (with the inline image) straight to the DB - nothing was
                # streamed to build the transcript incrementally like a
                # normal chat reply, so a full reload is what picks them up.
                self.load_conversation(conv_id)
            self._refresh_chat_list()
        self._sync_rail_for_current_conv()
        self._render_chat_list_rows()
        # An image job finishing frees the shared chat model for regular
        # generation again - the global queue (see _queue_globally) may
        # have entries that were only ever blocked on this, not on a chat
        # slot specifically.
        self._dispatch_next_global_send()

    # ---- drag-and-drop file attachment (see win_dnd.py) ----

    def handle_file_drop(self, paths):
        if self.conv_id is None:
            show_toast(self, self.ctx.theme, "Open or start a chat before attaching files", kind="warning")
            return
        conv_id = self.conv_id
        q = queue.Queue()
        threading.Thread(target=self._drop_worker, args=(paths, conv_id, q), daemon=True).start()
        self.after(50, self._poll_drop_queue, q, conv_id)

    def _drop_worker(self, paths, conv_id, q):
        # Runs off the Tk main thread - win_dnd's WM_DROPFILES hook fires
        # from inside the window's own message loop, so disk/network work
        # can't happen inline there without freezing the whole app.
        images, uploaded, new_docs, errors, skipped, unsupported_images = [], [], [], [], [], []
        for path in paths:
            name = os.path.basename(path)
            if os.path.isdir(path):
                skipped.append(name + "/")
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in UNSUPPORTED_IMAGE_EXTENSIONS:
                unsupported_images.append(name)
            elif ext in IMAGE_EXTENSIONS:
                try:
                    with open(path, "rb") as f:
                        raw = f.read()
                except OSError as e:
                    errors.append(f"{name}: {e}")
                    continue
                if not raw:
                    errors.append(f"{name}: empty file")
                elif len(raw) > MAX_IMAGE_BYTES:
                    errors.append(f"{name}: too large (max {MAX_IMAGE_BYTES // (1024 * 1024)} MB)")
                else:
                    images.append({
                        "filename": name,
                        "mime_type": _IMAGE_MIME.get(ext, "image/png"),
                        "data": base64.b64encode(raw).decode("ascii"),
                    })
            elif ext in DOCUMENT_EXTENSIONS:
                try:
                    resp = self.api.upload_conversation_file(conv_id, path)
                except ApiError as e:
                    errors.append(f"{name}: {e}")
                    continue
                uploaded.append(name)
                # Read locally rather than waiting on the server round trip -
                # the file's on disk right here, and the staging tile/hover
                # preview should show real content immediately.
                snippet = _read_local_snippet(path) if ext not in _NO_SNIPPET_EXTENSIONS else None
                if resp.get("id") is not None:
                    new_docs.append({
                        "id": resp["id"], "filename": name,
                        "content_tokens": resp.get("content_tokens"), "snippet": snippet,
                    })
            else:
                skipped.append(name)
        files_snapshot = None
        if uploaded:
            try:
                files_snapshot = self.api.get_conversation(conv_id).get("files", [])
            except ApiError:
                pass
        q.put({
            "images": images, "uploaded": uploaded, "new_docs": new_docs, "errors": errors,
            "skipped": skipped, "unsupported_images": unsupported_images, "files": files_snapshot,
        })

    def _poll_drop_queue(self, q, conv_id):
        try:
            result = q.get_nowait()
        except queue.Empty:
            self.after(50, self._poll_drop_queue, q, conv_id)
            return
        if conv_id != self.conv_id:
            return  # user switched conversations before this landed
        if result["images"]:
            self._staged_images.extend(result["images"])
        for doc in result["new_docs"]:
            self._staged_docs[doc["id"]] = doc
        if result["images"] or result["new_docs"]:
            self._render_staging_grid()
        if result["files"] is not None:
            self._render_conversation_files(result["files"])

        parts = []
        if result["uploaded"]:
            parts.append(f"attached {len(result['uploaded'])} file(s): {', '.join(result['uploaded'])}")
        if result["images"]:
            parts.append(f"staged {len(result['images'])} image(s) for the next message")
        if result["skipped"]:
            parts.append(f"skipped (unsupported type): {', '.join(result['skipped'])}")
        if result["unsupported_images"]:
            parts.append(
                f"skipped (image format not supported by the vision model): "
                f"{', '.join(result['unsupported_images'])}"
            )
        if result["errors"]:
            parts.append(f"failed: {'; '.join(result['errors'])}")
        if not parts:
            return
        if result["errors"]:
            kind = "error"
        elif (result["skipped"] or result["unsupported_images"]) and not (result["uploaded"] or result["images"]):
            kind = "warning"
        else:
            kind = "success"
        show_toast(self, self.ctx.theme, " — ".join(parts), kind=kind)

    def _on_add_files_click(self):
        paths = filedialog.askopenfilenames(title="Choose files to attach")
        if paths:
            self.handle_file_drop(list(paths))

    # ---- adaptive staging grid (images + just-uploaded docs, pre-send) ----

    def _render_staging_grid(self):
        # A tile can be destroyed (removed, or just rebuilt when a sibling
        # changes) while its hover overlay is still showing - nothing else
        # ever tears that overlay down in that case (only a real <Leave> or
        # the next hover.show() call does), so it's left dangling on
        # screen indefinitely, still referencing a now-destroyed widget.
        # Confirmed live: removing several staged images left an empty
        # bordered box floating over the transcript.
        self._hover.hide()
        for child in self.staged_row.winfo_children():
            child.destroy()
        n = len(self._staged_images) + len(self._staged_docs)
        if n == 0:
            self.staged_row.pack_propagate(True)
            self.staged_row.pack_forget()
            return
        self.attach_panel.update_idletasks()
        width = self.attach_panel.winfo_width()
        if width <= 1:
            width = 700  # not yet laid out (e.g. first drop before any geometry pass)
        tier_name, size = ag.pick_tier(n, width)
        # B7B-H2: when even the smallest tier does not fit on one row, the tiles wrap onto more rows (up to
        # STAGE_MAX_ROWS); past that the last cell is a "+N more" tile whose tip lists the rest (D-27 #14: never cut).
        # The tiles are gridded (uniform cells), the row's direct children as before.
        per_row = max(1, (width + ag.STAGE_GAP) // (size + ag.STAGE_GAP))
        rows = min(STAGE_MAX_ROWS, max(1, -(-n // per_row)))
        tiles = [("image", idx, img) for idx, img in enumerate(self._staged_images)]
        tiles += [("doc", doc_id, doc) for doc_id, doc in self._staged_docs.items()]
        hidden = []
        if n > rows * per_row:
            hidden, tiles = tiles[rows * per_row - 1:], tiles[:rows * per_row - 1]
        self.staged_row.configure(height=max(ag.STAGE_ROW_HEIGHT, rows * (size + 16)) if rows > 1 else ag.STAGE_ROW_HEIGHT)
        self.staged_row.pack_propagate(False)
        for i, (kind, key, item) in enumerate(tiles):
            tile = (self._build_image_tile(item, key, size, tier_name) if kind == "image"
                    else self._build_doc_tile(key, item, size, tier_name))
            tile.grid(row=i // per_row, column=i % per_row, padx=(0, ag.STAGE_GAP), pady=8, sticky="nw")
        if hidden:
            i = len(tiles)
            self._build_more_tile(hidden, size).grid(row=i // per_row, column=i % per_row, padx=(0, ag.STAGE_GAP), pady=8,
                                                     sticky="nw")
        if self.staged_row.winfo_manager() != "pack":
            self.staged_row.pack(side="top", fill="x", before=self._attach_end)     # B7b: the attach slot's end

    def _build_more_tile(self, hidden, size):
        """B7B-H2: the last cell when the staged files outnumber the rows: "+N", its tip listing every file it stands
        for (they stay staged and are sent; a × on a shown tile makes room for the next one)."""
        theme = self.ctx.theme
        tile = ag.make_tile_shell(self.staged_row, theme, size)
        tk.Label(tile, text="+%d" % len(hidden), bg=theme.panel, fg=theme.text, font=theme_mod.FONT_CAPTION).pack(expand=True)
        names = [str(item.get("filename", key)) for _kind, key, item in hidden]
        text = "%d more staged file%s:\n" % (len(hidden), "" if len(hidden) == 1 else "s") + "\n".join(names)
        self._hover.bind(tile, theme, lambda overlay, text=text: tk.Label(
            overlay, text=text, bg=theme.panel, fg=theme.text, font=theme_mod.FONT_MONO, justify="left", padx=14,
            pady=10).pack())
        tile.more_names = names
        return tile

    def _bind_hover(self, widget, descriptor):
        self._hover.bind(widget, self.ctx.theme, self._hover_content_builder(descriptor))

    def _hover_content_builder(self, descriptor):
        return lambda overlay: self._populate_hover_content(overlay, descriptor)

    def _build_image_tile(self, img, index, size, tier_name):
        theme = self.ctx.theme
        tile = ag.make_tile_shell(self.staged_row, theme, size)
        cost_text = (
            f"~{ESTIMATED_IMAGE_TOKENS}" if tier_name == "cube" else f"~{ESTIMATED_IMAGE_TOKENS} tok (est.)"
        )
        if tier_name == "cube":
            tk.Label(
                tile, text=cost_text, bg=theme.panel, fg=theme.text, font=theme_mod.FONT_CAPTION,
                wraplength=size - 4, justify="center",
            ).pack(expand=True)
        else:
            # Caption packed *before* the image/placeholder content, not
            # after - pack() claims cavity in call order, so an expand=True
            # widget packed first (the image) would stake a claim on the
            # whole tile before the side="bottom" caption ever gets a slice,
            # clipping it to a sliver. Confirmed visually via screenshot
            # before this fix (caption showed only "tok", not "~256 tok
            # (est.)") - packing the fixed-size caption first reserves its
            # space, then the image fills whatever's left above it.
            if size >= ag.STAGE_TIERS[2][1]:  # "small" and up - room for a caption
                tk.Label(
                    tile, text=cost_text, bg=theme.panel, fg=theme.dim, font=theme_mod.FONT_CAPTION,
                    wraplength=size - 4,
                ).pack(side="bottom")
            photo = self._photo_cache.get(id(img), img["data"], max(8, size - 8))
            if photo is not None:
                tk.Label(tile, image=photo, bg=theme.panel).pack(expand=True)
            else:
                ext_label = os.path.splitext(img["filename"])[1].lstrip(".").upper() or "IMG"
                tk.Label(
                    tile, text=ext_label, bg=theme.panel_hover, fg=theme.dim, font=theme_mod.FONT_LABEL,
                ).pack(expand=True, fill="both", padx=2, pady=2)
        self._bind_hover(tile, ("image", img))
        ag.add_remove_button(tile, theme, lambda i=index: self._remove_staged_image(i))
        return tile

    def _build_doc_tile(self, doc_id, doc, size, tier_name):
        theme = self.ctx.theme
        tile = ag.make_tile_shell(self.staged_row, theme, size)
        cost_text = self._doc_cost_text(doc)
        if tier_name == "cube":
            tk.Label(
                tile, text=cost_text, bg=theme.panel, fg=theme.text, font=theme_mod.FONT_CAPTION,
                wraplength=size - 4, justify="center",
            ).pack(expand=True)
        else:
            # Caption packed before the content, same reasoning/fix as
            # _build_image_tile - see the comment there.
            if size >= ag.STAGE_TIERS[2][1]:
                tk.Label(
                    tile, text=cost_text, bg=theme.panel, fg=theme.dim, font=theme_mod.FONT_CAPTION,
                    wraplength=size - 4,
                ).pack(side="bottom")
            ext = os.path.splitext(doc["filename"])[1].lower()
            if doc.get("snippet") and ext not in _NO_SNIPPET_EXTENSIONS:
                n_lines = 2 if tier_name == "small" else (4 if tier_name == "medium" else 6)
                preview_text = "\n".join(doc["snippet"].splitlines()[:n_lines])
                tk.Label(
                    tile, text=preview_text, bg=theme.panel, fg=theme.dim, font=(theme_mod.FONT_FAMILY, 7),
                    justify="left", anchor="nw", wraplength=size - 6,
                ).pack(expand=True, fill="both", padx=3, pady=(3, 0))
            else:
                ext_label = ext.lstrip(".").upper() or "DOC"
                tk.Label(
                    tile, text=ext_label, bg=theme.panel_hover, fg=theme.dim, font=theme_mod.FONT_LABEL,
                ).pack(expand=True, fill="both", padx=2, pady=2)
        self._bind_hover(tile, ("staged_doc", doc))
        ag.add_remove_button(tile, theme, lambda d=doc_id: self._remove_staged_doc(d))
        return tile

    @staticmethod
    def _doc_cost_text(doc):
        tokens = doc.get("content_tokens")
        return f"{tokens:,} tok" if tokens is not None else "cost unknown"

    # ---- removal ----

    def _remove_staged_image(self, index):
        if 0 <= index < len(self._staged_images):
            removed = self._staged_images.pop(index)
            self._photo_cache.evict(id(removed))
            self._render_staging_grid()

    def _remove_staged_doc(self, doc_id):
        if doc_id not in self._staged_docs:
            return
        del self._staged_docs[doc_id]
        self._render_staging_grid()
        conv_id = self.conv_id
        q = queue.Queue()
        threading.Thread(target=self._delete_doc_worker, args=(conv_id, doc_id, q), daemon=True).start()
        self.after(50, self._poll_delete_doc_queue, q, conv_id, doc_id)

    def _delete_doc_worker(self, conv_id, doc_id, q):
        try:
            self.api.delete_conversation_file(conv_id, doc_id)
            q.put(("ok", None))
        except ApiError as e:
            q.put(("error", str(e)))

    def _poll_delete_doc_queue(self, q, conv_id, doc_id):
        try:
            kind, payload = q.get_nowait()
        except queue.Empty:
            self.after(50, self._poll_delete_doc_queue, q, conv_id, doc_id)
            return
        if conv_id != self.conv_id:
            return
        if kind == "error":
            show_toast(self, self.ctx.theme, f"Could not remove file: {payload}", kind="error")
            return
        # The removed doc was already excluded from the plain list (it was
        # still in self._staged_docs) - drop it from the cached files list
        # too so it can't reappear there now that it's actually gone.
        self._conversation_files = [f for f in self._conversation_files if f.get("id") != doc_id]
        self._render_conversation_files(self._conversation_files)

    # ---- already-sent files (plain list, hover-preview only) ----

    def _render_conversation_files(self, files):
        # Same dangling-overlay hazard as _render_staging_grid() - a hovered
        # filename Label can be destroyed and rebuilt here (e.g. a doc just
        # got promoted from the staging grid, or the file list refreshed)
        # while its hover overlay is still showing.
        self._hover.hide()
        self._conversation_files = files or []
        for child in self.files_row.winfo_children():
            child.destroy()
        conv_files = [
            f for f in self._conversation_files
            if f.get("scope") == "conversation" and f.get("id") not in self._staged_docs
        ]
        if not conv_files:
            self.files_row.pack_forget()
            return
        theme = self.ctx.theme
        ink = self._b7b_tokens()["value"]           # B7b: the attach line in value ink (approved r2_chat, y 672)
        tk.Label(
            self.files_row, text="attached: ", bg=theme.bg, fg=ink, font=theme_mod.FONT_CAPTION, bd=0, padx=0,
        )
        for i, f in enumerate(conv_files):
            tokens = f.get("content_tokens")
            cost = f"{tokens:,} tokens" if tokens is not None else "tokens: n/a"
            suffix = "" if i == len(conv_files) - 1 else ", "
            lbl = tk.Label(
                self.files_row, text=f"{f['filename']} ({cost}){suffix}", bg=theme.bg, fg=ink,
                font=theme_mod.FONT_CAPTION, cursor="hand2", bd=0, padx=0,
            )
            lbl.bind("<Enter>", lambda e, ff=f, w=lbl: self._hover.show(w, theme, self._hover_content_builder(("sent_doc", ff))))
            lbl.bind("<Leave>", lambda e: self._hover.hide())
        # B7B-H1: the names flow onto as many lines as the row's width needs (D-27 #14: wrap, never cut), re-flowed
        # when the width changes; the labels stay the row's direct children, in order
        self._files_flow_width = None
        self._flow_conversation_files()
        if self.files_row.winfo_manager() != "pack":
            self.files_row.pack(side="top", fill="x", before=self._attach_end)     # B7b: the attach slot's end

    def _flow_conversation_files(self, _event=None):
        """B7B-H1: place the attached-files labels left to right, wrapping to a new line where the row's width
        runs out; the row is as tall as its lines. Run at render and on the row's <Configure> (a new width)."""
        row = self.files_row
        try:
            labels = [w for w in row.winfo_children() if isinstance(w, tk.Label)]
            if not labels:
                return
            width = row.winfo_width()
            if width <= 1:
                width = self.attach_panel.winfo_width()
            if width <= 1:
                width = 700
            if width == self._files_flow_width:
                return
            self._files_flow_width = width
            line_h = max(w.winfo_reqheight() for w in labels)
            x = y = 0
            for w in labels:
                rw = w.winfo_reqwidth()
                if x and x + rw > width:
                    x, y = 0, y + line_h
                w.place(x=x, y=y)
                x += rw
            row.pack_propagate(False)
            row.configure(height=y + line_h)
        except tk.TclError:
            pass

    # ---- hover-enlarge overlay ----

    @staticmethod
    def _doc_hover_text(payload, snippet_key):
        ext = os.path.splitext(payload["filename"])[1].lower()
        snippet = payload.get(snippet_key)
        if snippet and ext not in _NO_SNIPPET_EXTENSIONS:
            return f"{payload['filename']}\n\n{snippet}"
        tokens = payload.get("content_tokens")
        cost = f"{tokens:,} tokens" if tokens is not None else "tokens: n/a"
        return f"{payload['filename']}\n({cost})\nno preview for this format"

    def _populate_hover_content(self, overlay, descriptor):
        theme = self.ctx.theme
        kind, payload = descriptor
        if kind == "image":
            photo = self._photo_cache.get(id(payload), payload["data"], 400)
            if photo is not None:
                tk.Label(overlay, image=photo, bg=theme.panel).pack(padx=10, pady=10)
            else:
                tk.Label(
                    overlay, text=f"{payload['filename']}\n(no preview available for this format)\n"
                    f"~{ESTIMATED_IMAGE_TOKENS} tok (est.)",
                    bg=theme.panel, fg=theme.text, font=theme_mod.FONT_CAPTION, justify="left", padx=14, pady=10,
                ).pack()
        else:
            snippet_key = "snippet" if kind == "staged_doc" else "content_snippet"
            text = self._doc_hover_text(payload, snippet_key)
            tk.Label(
                overlay, text=text, bg=theme.panel, fg=theme.text, font=theme_mod.FONT_MONO,
                justify="left", wraplength=480, padx=14, pady=10,
            ).pack()
