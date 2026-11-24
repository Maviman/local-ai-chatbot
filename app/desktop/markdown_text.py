"""
Markdown -> tkinter Text-tag rendering.

Scope matches what this app's markdown actually produces (marked.js with
{breaks:true, gfm:true} + the subset style.css specially styles) rather
than full CommonMark: headings, bold/italic/bold+italic, strikethrough,
inline code, fenced code blocks, blockquotes, ordered/unordered lists,
and links. Real tables aren't laid out as tables (Text has no box-layout
model) - they render as plain lines rather than crashing.
"""

import html
import re
import tkinter as tk
import webbrowser

from . import theme as theme_mod

_T = theme_mod.load_theme()

FENCE_RE = re.compile(r"^```(\w*)\s*$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# Group 1 captures every leading ">" (with each one's own optional trailing
# space) so nesting depth is known, not just whether it's a quote at all -
# ">> deeper" used to leak a literal ">" into the rendered text (only the
# first ">" was ever stripped).
QUOTE_RE = re.compile(r"^((?:>\s?)+)(.*)$")
UL_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
OL_RE = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
# A list line's nesting comes from its indentation (BF-16): 2 columns a level (a tab is 4), at most LIST_MAX_DEPTH,
# as the tags list_item (none), list_item1 .. list_item3; each level is LIST_INDENT_PX further in.
LIST_INDENT_RE = re.compile(r"^(\s*)")
LIST_MAX_DEPTH = 3
LIST_INDENT_PX = 18
# GFM task list item: "- [ ] todo" / "- [x] done" - matched against UL_RE's
# own captured content, not the raw line. "-" isn't standard GFM but is a
# widely-used third state ("in progress"/"cancelled") from Obsidian's own
# community task plugins and several models' default habits - confirmed
# real: without this, "- [-] in progress" fell all the way through to an
# ordinary bullet with the literal "[-]" left sitting in the item text.
TASK_ITEM_RE = re.compile(r"^\[([ xX\-])\]\s+(.*)$")
# GFM table rows, restricted to the leading-and-trailing-pipe form ("| a | b |")
# rather than the full spec's optional-outer-pipes variant - that's what
# marked.js's gfm:true actually emits and what models overwhelmingly write,
# and requiring both pipes is a much stronger signal against misclassifying
# ordinary prose that happens to contain a stray "|" as a table row.
TABLE_ROW_RE = re.compile(r"^\s*\|.+\|\s*$")
TABLE_SEPARATOR_RE = re.compile(r"^\s*\|(\s*:?-+:?\s*\|)+\s*$")
# Markdown's thematic-break/horizontal-rule syntax: a standalone line of 3+
# of the same character among -, *, _, optionally spaced out ("---", "***",
# "- - -" are all valid). Checked ahead of UL_RE in _process_complete_line -
# "- - -" would otherwise also satisfy UL_RE (a dash, then "- -" as the item
# text), and a run of nothing-but-dashes/stars/underscores is unambiguously
# meant as a rule, never a list item, by markdown convention.
HR_RE = re.compile(r"^ {0,3}(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$")
# A standalone raw <hr> tag (its own line, models sometimes emit this
# instead of "---") is the same thematic break as HR_RE, just spelled as
# HTML - checked alongside it wherever HR_RE is.
HTML_HR_RE = re.compile(r"^\s*<hr\s*/?>\s*$", re.IGNORECASE)

INLINE_RE = re.compile(
    # A backslash-escaped special character (\*, \_, \[, ...) is unformatted
    # literal text - checked first since it's the only alternative that can
    # ever start at a backslash, so there's no ordering conflict with
    # anything below; it just has to exist at all; confirmed real: without
    # it, "\*asterisks\*" rendered as *italic* text with the backslashes
    # still visible ("\asterisks\"), since the plain-text fallback below has
    # no concept of escaping and the italic alternative doesn't care what
    # precedes its opening "*".
    r"\\(?P<escaped>[\\`*_~\[\]()<>#+\-.!|])"
    r"|`(?P<code>[^`]+)`"
    r"|<code>(?P<htmlcode>.*?)</code>"
    # <br> forces a real line break, same as a literal "\n" segment - models
    # commonly use it inside table cells and list items where an actual
    # newline in the source would break the surrounding block syntax.
    r"|(?P<br><br\s*/?>)"
    r"|\*\*\*(?P<boldit>[^*]+)\*\*\*"
    r"|(?:\*\*(?P<bold>[^*]+)\*\*|__(?P<bold2>[^_]+)__|<(?:b|strong)>(?P<boldhtml>.*?)</(?:b|strong)>)"
    r"|(?:\*(?P<ital>[^*]+)\*|(?<!\w)_(?P<ital2>[^_]+)_(?!\w)|<(?:i|em)>(?P<italhtml>.*?)</(?:i|em)>)"
    r"|~~(?P<strike>[^~]+)~~"
    r"|<(?:del|s|strike)>(?P<strikehtml>.*?)</(?:del|s|strike)>"
    r"|<u>(?P<underline>.*?)</u>"
    r"|\[(?P<linktext>[^\]]+)\]\((?P<linkurl>[^)]+)\)"
    # Tk can't load/display a remote image inline - a labeled link to the
    # image URL is the honest fallback instead of a bare, confusing "!"
    # left over from a ![alt](url) markdown image the [text](url) link
    # alternative above only partially recognizes.
    r"|!\[(?P<imgalt>[^\]]*)\]\((?P<imgurl>[^)]+)\)"
    r"|<(?P<autolink>https?://[^\s<>]+)>"
    r"|(?P<bareurl>https?://\S+[^\s.,;:!?)\]])"
    , re.IGNORECASE
)


# ---------------------------------------------------------------- math symbols
#
# No LaTeX typesetting engine here (a Tk Text widget has no box-layout model
# to build one on, same constraint as a plain terminal) - so instead of
# rendering \alpha or x^2 as raw source text, convert it to the Unicode
# symbols/superscripts that read correctly in plain text, mirroring how this
# assistant itself writes math when talking through a terminal that can't
# render LaTeX either.
#
# Backslash-prefixed LaTeX commands (\alpha, \leq, \sum, ...) are converted
# unconditionally - that syntax is unambiguous, it never occurs in ordinary
# prose. Bare word-forms ("alpha", "sqrt") are only converted *inside*
# recognized math delimiters ($...$, \(...\), \[...\]), since outside of
# them a word like "beta" is at least as likely to be ordinary English
# ("beta version") as a Greek letter - unconditionally rewriting every such
# word would silently corrupt normal prose.

_GREEK_LATEX = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ",
    "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ", "chi": "χ",
    "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}
_LATEX_SYMBOLS = {
    "leq": "≤", "geq": "≥", "neq": "≠", "approx": "≈", "equiv": "≡",
    "times": "×", "div": "÷", "cdot": "·", "pm": "±", "mp": "∓",
    "infty": "∞", "partial": "∂", "nabla": "∇",
    "sum": "Σ", "prod": "∏", "int": "∫", "oint": "∮",
    "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆",
    "supset": "⊃", "supseteq": "⊇", "cup": "∪", "cap": "∩",
    "forall": "∀", "exists": "∃", "nexists": "∄", "emptyset": "∅",
    "rightarrow": "→", "to": "→", "leftarrow": "←", "leftrightarrow": "↔",
    "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔",
    "cong": "≅", "sim": "∼", "propto": "∝", "perp": "⊥", "parallel": "∥",
    "angle": "∠", "degree": "°", "therefore": "∴", "because": "∵",
    "wedge": "∧", "vee": "∨", "neg": "¬", "oplus": "⊕", "otimes": "⊗",
    **_GREEK_LATEX,
}
_BARE_WORD_SYMBOLS = {**_GREEK_LATEX, "infty": "∞", "infinity": "∞", "sum": "Σ", "partial": "∂"}

_SUPERSCRIPT_CHARS = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶",
    "7": "⁷", "8": "⁸", "9": "⁹", "+": "⁺", "-": "⁻", "n": "ⁿ", "i": "ⁱ",
}
_SUBSCRIPT_CHARS = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆",
    "7": "₇", "8": "₈", "9": "₉", "+": "₊", "-": "₋",
    # Common index-variable letters (x_i, a_n, ...) - Unicode only defines
    # subscript forms for a subset of lowercase letters, this is that subset.
    "a": "ₐ", "e": "ₑ", "h": "ₕ", "i": "ᵢ", "j": "ⱼ", "k": "ₖ", "l": "ₗ",
    "m": "ₘ", "n": "ₙ", "o": "ₒ", "p": "ₚ", "r": "ᵣ", "s": "ₛ", "t": "ₜ",
    "u": "ᵤ", "v": "ᵥ", "x": "ₓ",
}

_LATEX_CMD_RE = re.compile(r"\\([a-zA-Z]+)")
_FRAC_RE = re.compile(r"\\frac\{([^{}]*)\}\{([^{}]*)\}")
_SQRT_BRACE_RE = re.compile(r"\\sqrt\{([^{}]*)\}")
_SUPERSCRIPT_RE = re.compile(r"\^(\{[0-9ni+\-]+\}|[0-9ni+\-])")
_SUBSCRIPT_LETTERS = "aehijklmnoprstuvx"
# Requires a word character immediately before the "_" (real subscript
# notation always attaches directly to its base variable: x_i, a_n) - without
# this, "_" here collides with markdown's underscore-italic syntax, which by
# definition never has a word character touching its opening "_" (there's
# always a boundary: start of text, whitespace, or punctuation before it).
# Confirmed real: "**bold with _italic_ inside**" used to come out as
# "bold with ᵢtalic_ inside" - the bare regex matched "_i" as subscript-i
# before the italic parser (a single left-to-right pass, no nested
# re-parsing of an already-matched bold span's contents) ever got a chance
# to treat those underscores as emphasis markers.
_SUBSCRIPT_RE = re.compile(
    r"(?<=\w)_(\{[0-9" + _SUBSCRIPT_LETTERS + r"+\-]+\}|[0-9" + _SUBSCRIPT_LETTERS + r"+\-])"
)
# $...$ requires non-whitespace immediately inside both delimiters (real
# LaTeX convention: $x^2$, never $ x^2 $) so adjacent currency mentions like
# "$5 and $10" - where the naive [^$]+ span "5 and " starts/ends clean but
# happens to have a trailing space before the next $ - don't get swallowed.
_MATH_DELIM_RE = re.compile(
    r"\$(?!\s)([^$]+?)(?<!\s)\$"
    r"|\\\((.+?)\\\)"
    r"|\\\[(.+?)\\\]"
)
_BARE_NUMBER_RE = re.compile(r"^[\d.,]+$")
# Raw HTML sub/superscript tags - models mixing a bit of inline HTML into
# markdown for chemistry/math notation (H<sub>2</sub>O, x<sup>2</sup>) is
# common enough to be worth converting to the same Unicode characters the
# LaTeX-notation path above already produces, rather than leaving the literal
# tags visible.
_HTML_SUB_RE = re.compile(r"<sub>(.*?)</sub>", re.IGNORECASE | re.DOTALL)
_HTML_SUP_RE = re.compile(r"<sup>(.*?)</sup>", re.IGNORECASE | re.DOTALL)


def _apply_structural_latex(text):
    text = _FRAC_RE.sub(lambda m: f"({m.group(1)})/({m.group(2)})", text)
    text = _SQRT_BRACE_RE.sub(lambda m: "√(" + m.group(1) + ")", text)
    text = _LATEX_CMD_RE.sub(lambda m: _LATEX_SYMBOLS.get(m.group(1), m.group(0)), text)
    text = _SUPERSCRIPT_RE.sub(lambda m: "".join(_SUPERSCRIPT_CHARS.get(c, c) for c in m.group(1).strip("{}")), text)
    text = _SUBSCRIPT_RE.sub(lambda m: "".join(_SUBSCRIPT_CHARS.get(c, c) for c in m.group(1).strip("{}")), text)
    return text.replace("<=", "≤").replace(">=", "≥").replace("!=", "≠")


def _apply_bare_word_symbols(text):
    for word, sym in _BARE_WORD_SYMBOLS.items():
        text = re.sub(rf"\b{word}\b", sym, text)
    return text


def _convert_html_scripts(text):
    text = _HTML_SUB_RE.sub(lambda m: "".join(_SUBSCRIPT_CHARS.get(c, c) for c in m.group(1)), text)
    text = _HTML_SUP_RE.sub(lambda m: "".join(_SUPERSCRIPT_CHARS.get(c, c) for c in m.group(1)), text)
    return text


def substitute_math_symbols(text):
    """Convert LaTeX-ish math notation to Unicode for plain-text display,
    plus two unconditional cleanups that piggyback on this same pass since
    every call site already runs it over every non-code segment: decoding
    HTML entities (&amp;, &lt;, ...) and converting <sub>/<sup> tags to the
    same Unicode characters the LaTeX-notation path uses. Neither is
    ambiguous with anything else this app's markdown dialect uses, unlike
    the bare-word Greek-letter substitution below, which is why they don't
    need a delimited-context guard.
    See the module comment above for the unambiguous-vs-delimited-context
    distinction that substitution *does* rely on."""
    text = html.unescape(text)
    text = _convert_html_scripts(text)

    def repl_delim(m):
        inner = m.group(1) or m.group(2) or m.group(3)
        if m.group(1) is not None and _BARE_NUMBER_RE.match(inner):
            return m.group(0)  # bare "$5$" - almost certainly currency, leave untouched
        inner = _apply_structural_latex(inner)
        inner = _apply_bare_word_symbols(inner)
        return inner

    text = _MATH_DELIM_RE.sub(repl_delim, text)
    # Backslash commands/superscripts/operators outside any $...$ wrapper are
    # still unambiguous on their own (a model may write \alpha directly).
    return _apply_structural_latex(text)


# The inline patterns above go quadratic on unclosed delimiters: every "[", "<b>",
# "\(" ... that never finds its closer scans on to the end of the line, so a line
# made of them costs O(n^2). Measured on this machine, 64 KB of "[" took 14.5 s,
# of "\(" 11 s, of "<b>" 7.3 s - on the Tk thread, and for a streamed reply again
# on every chunk. Real models do produce such lines (a repetition loop emitting
# "[[[[..." or "<b><b>..." until the token limit), and so does pasted text. No real
# line is anywhere near this long (a paragraph is a few hundred to a few thousand
# characters), so a longer one is shown as plain text, entities decoded, instead of
# being parsed: the worst case is then ~0.3 s, not minutes. The same length is also
# how much of a still-unfinished line is drawn while it streams (_render_incremental).
MAX_INLINE_PARSE_CHARS = 8000

# A table is a Frame holding one tk.Label per cell, and on Windows laying those out
# costs about a millisecond a cell, a little more at scale (measured: a 50-row x
# 4-column table 0.3 s, 500 rows 4 s, 1,000 rows 9 s, 2,000 rows 39 s - the whole
# window frozen, when the message finished streaming and again every time the
# conversation was opened; no Label option changes it, a bare Label costs the same).
# So a table over MAX_TABLE_GRID_CELLS cells is drawn as aligned plain text instead
# (linear: ~0.1 s per thousand rows). Horizontal rules are one Frame each but cost
# ~0.03 ms (4,000 of them: 0.13 s), so they need no limit.
MAX_TABLE_GRID_CELLS = 500
MAX_TABLE_TEXT_COLUMN = 40

# Names of the marks a message leaves in the transcript (start_message, add_static_message,
# _append_version_span) - the ones MarkdownText._unset_marks has to remove with its text.
_PER_MESSAGE_MARK_PREFIXES = ("msgblock_", "msgmark_", "vspan_", "jump_")
# ...and the click-link tags (_append_message_action_link, _insert_version_arrow), one per span.
_PER_MESSAGE_TAG_PREFIXES = ("editlink_", "regenlink_", "verlink_")


def parse_inline(text):
    """Yield (text, tags_tuple, link_url_or_None) for one line of text."""
    if len(text) > MAX_INLINE_PARSE_CHARS:
        return [(text, (), None)]
    segments = []
    pos = 0
    for m in INLINE_RE.finditer(text):
        if m.start() > pos:
            segments.append((text[pos:m.start()], (), None))
        if m.group("escaped"):
            segments.append((m.group("escaped"), (), None))
        elif m.group("code"):
            segments.append((m.group("code"), ("code_inline",), None))
        elif m.group("htmlcode") is not None:
            segments.append((m.group("htmlcode"), ("code_inline",), None))
        elif m.group("br"):
            segments.append(("\n", (), None))
        elif m.group("boldit"):
            segments.append((m.group("boldit"), ("bolditalic",), None))
        elif m.group("bold") or m.group("bold2") or m.group("boldhtml") is not None:
            segments.append((m.group("bold") or m.group("bold2") or m.group("boldhtml"), ("bold",), None))
        elif m.group("ital") or m.group("ital2") or m.group("italhtml") is not None:
            segments.append((m.group("ital") or m.group("ital2") or m.group("italhtml"), ("italic",), None))
        elif m.group("strike"):
            segments.append((m.group("strike"), ("strike",), None))
        elif m.group("strikehtml") is not None:
            segments.append((m.group("strikehtml"), ("strike",), None))
        elif m.group("underline") is not None:
            segments.append((m.group("underline"), ("underline",), None))
        elif m.group("linktext"):
            segments.append((m.group("linktext"), ("link",), m.group("linkurl")))
        elif m.group("imgurl") is not None:
            segments.append((f"[image: {m.group('imgalt') or 'untitled'}]", ("link",), m.group("imgurl")))
        elif m.group("autolink"):
            segments.append((m.group("autolink"), ("link",), m.group("autolink")))
        elif m.group("bareurl"):
            segments.append((m.group("bareurl"), ("link",), m.group("bareurl")))
        pos = m.end()
    if pos < len(text):
        segments.append((text[pos:], (), None))
    return segments


def _parse_inline_math(text):
    """parse_inline() plus math-symbol substitution on every non-code
    segment - code_inline segments are left completely untouched, since
    converting operators/symbols inside literal code would corrupt it.
    Substitution runs *after* markdown tokenization (not on the raw line
    before it), specifically so a LaTeX command shown inside a code span
    (e.g. `` `\alpha` `` as literal source) is protected the same way."""
    if len(text) > MAX_INLINE_PARSE_CHARS:
        return [(html.unescape(text), (), None)]  # see MAX_INLINE_PARSE_CHARS: no patterns, no quadratic scans
    return [
        (text if "code_inline" in tags else substitute_math_symbols(text), tags, url)
        for text, tags, url in parse_inline(text)
    ]


def _list_tag(line):
    """The tag of a list line by its indentation: list_item (none), list_item1..3 (2 columns a level, a tab
    counting 4, capped at LIST_MAX_DEPTH). Sub-items used to render at their parent's level (BF-16)."""
    lead = LIST_INDENT_RE.match(line).group(1).expandtabs(4)
    depth = min(len(lead) // 2, LIST_MAX_DEPTH)
    return "list_item" if depth == 0 else f"list_item{depth}"


def _parse_lines(lines, in_fence):
    """Parse a sequence of already-complete lines (no trailing newlines),
    carrying fence state in from a prior call (e.g. from a previous chunk of
    the same streamed message). Returns (segments, updated_in_fence) so a
    caller can resume exactly where this left off instead of re-parsing
    everything from the start of the message each time."""
    segments = []
    for line in lines:
        fence_m = FENCE_RE.match(line)
        if fence_m:
            in_fence = not in_fence
            continue
        if in_fence:
            segments.append((line + "\n", ("code_block",), None))
            continue
        heading_m = HEADING_RE.match(line)
        if heading_m:
            level = len(heading_m.group(1))
            for text, tags, url in _parse_inline_math(heading_m.group(2)):
                segments.append((text, (f"h{level}",) + tags, url))
            segments.append(("\n", (), None))
            continue
        quote_m = QUOTE_RE.match(line)
        if quote_m:
            depth = min(quote_m.group(1).count(">"), 3)
            quote_tag = f"blockquote{depth}"
            for text, tags, url in _parse_inline_math(quote_m.group(2)):
                segments.append((text, (quote_tag,) + tags, url))
            segments.append(("\n", (), None))
            continue
        ul_m = UL_RE.match(line)
        if ul_m:
            item_tag = _list_tag(line)
            content = ul_m.group(1)
            task_m = TASK_ITEM_RE.match(content)
            if task_m:
                marker = task_m.group(1)
                if marker in ("x", "X"):
                    bullet = "☑ "
                elif marker == "-":
                    bullet = "◐ "  # in progress / cancelled, depending on the source tool's own convention
                else:
                    bullet = "☐ "
                content = task_m.group(2)
            else:
                bullet = "• "
            segments.append((bullet, (item_tag,), None))
            for text, tags, url in _parse_inline_math(content):
                segments.append((text, (item_tag,) + tags, url))
            segments.append(("\n", (), None))
            continue
        ol_m = OL_RE.match(line)
        if ol_m:
            item_tag = _list_tag(line)
            segments.append((f"{ol_m.group(1)}. ", (item_tag,), None))
            for text, tags, url in _parse_inline_math(ol_m.group(2)):
                segments.append((text, (item_tag,) + tags, url))
            segments.append(("\n", (), None))
            continue
        if not line.strip():
            segments.append(("\n", (), None))
            continue
        for text, tags, url in _parse_inline_math(line):
            segments.append((text, tags, url))
        segments.append(("\n", (), None))
    return segments, in_fence


def parse_markdown(raw):
    """Parse a full message's markdown into (text, tags, link_url) segments."""
    segments, _ = _parse_lines(raw.split("\n"), False)
    return segments


def _split_table_row(line):
    """"| a | b \\| c |" -> ["a", "b | c"] - splits on unescaped pipes only,
    and drops the single empty cell the leading/trailing "|" itself
    produces (a row with N columns has N+1 pipe-separated fields once you
    include those two outer ones)."""
    line = line.strip().replace("\\|", "\x00")
    parts = [p.strip() for p in line.split("|")]
    if parts and parts[0] == "":
        parts = parts[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return [p.replace("\x00", "|") for p in parts]


def _clean_table_cell_text(text):
    """Table cells are plain tk.Label text - no per-span bold/italic/link
    styling is possible inside one, so this runs the exact same inline
    pipeline the rest of a message uses (bold/italic/code/strike markers,
    HTML tag equivalents, <br>, entities, <sub>/<sup>, escapes) and keeps
    just the resulting text, discarding the tags a Label can't render.
    Without this, a cell written as "**Bold**" showed the literal asterisks
    instead of at least clean text - a Label showing *some* formatting
    marker literally isn't meaningfully different from any other markup
    leaking through unconverted, the thing the whole rest of this pass
    guards against. <br> still becomes a real embedded newline (a Label
    renders one as a second line) even though no other tag survives."""
    return "".join(part_text for part_text, tags, url in _parse_inline_math(text))


def _table_column_alignment(separator_cell):
    cell = separator_cell.strip()
    left, right = cell.startswith(":"), cell.endswith(":")
    if left and right:
        return "center"
    if right:
        return "right"
    return "left"  # explicit ":---" or bare "---" both default left


def _table_as_text(headers, aligns, rows):
    """A table too big for a grid of Labels (see MAX_TABLE_GRID_CELLS) as aligned
    monospaced text, one line per row. A cell longer than MAX_TABLE_TEXT_COLUMN is
    never cut - its row just runs past the column."""
    ncols = max(len(headers), max((len(r) for r in rows), default=0), 1)
    grid = [[_clean_table_cell_text(cell).replace("\n", " / ") for cell in list(r) + [""] * (ncols - len(r))] for r in [headers] + rows]
    widths = [min(max(len(r[c]) for r in grid), MAX_TABLE_TEXT_COLUMN) for c in range(ncols)]

    def line(cells):
        padded = []
        for c, text in enumerate(cells):
            align = aligns[c] if c < len(aligns) else "left"
            padded.append(text.rjust(widths[c]) if align == "right" else text.center(widths[c]) if align == "center" else text.ljust(widths[c]))
        return " │ ".join(padded).rstrip()

    return "\n".join([line(grid[0]), "─┼─".join("─" * w for w in widths)] + [line(r) for r in grid[1:]]) + "\n"


class MarkdownText(tk.Text):
    """A Text widget holding a whole chat transcript, with per-message
    markdown rendering and streaming-safe incremental updates."""

    def __init__(self, parent, **kwargs):
        kwargs.setdefault("wrap", "word")
        # Tk's word-wrap layout is pathologically slow (multi-second stalls,
        # observed empirically) on long unbroken text if the widget hasn't
        # been geometry-resolved yet (e.g. right after creation, before the
        # window is mapped) and has no explicit width to wrap against.
        # pack(fill="both", expand=True) still lets it grow to fill its
        # parent once mapped - this is just a sane initial wrap reference.
        kwargs.setdefault("width", 80)
        kwargs.setdefault("bg", _T.bg)
        kwargs.setdefault("fg", _T.bright)
        kwargs.setdefault("insertbackground", _T.bright)
        kwargs.setdefault("relief", "flat")
        kwargs.setdefault("highlightthickness", 1)
        kwargs.setdefault("highlightbackground", _T.dim)
        kwargs.setdefault("highlightcolor", _T.dim)
        kwargs.setdefault("padx", 14)
        kwargs.setdefault("pady", 12)
        kwargs.setdefault("font", ("Consolas", 11))
        kwargs.setdefault("state", "disabled")
        kwargs.setdefault("spacing3", 4)
        super().__init__(parent, **kwargs)
        self._configure_tags()
        self._links = []
        self._current_bubble_tag = None
        self._raw = ""
        self._committed_len = 0
        self._thinking_line_len = 0   # characters on the live Thinking block's current line (see feed_thinking)
        self._in_fence = False
        # Complete lines held back from rendering while they might still be
        # part of a GFM table (see _process_complete_line/_flush_table_buffer)
        # - a table only renders as one real grid widget once its full extent
        # is known, not as plain text line by line.
        self._table_buffer = []
        # (mark_name, preview_text) per user message, in order - lets
        # chat_view.py offer a "jump to a prompt" list without keeping its
        # own separate copy of message text in sync with what's rendered.
        self._jump_marks = []
        # One dict per rendered message (both loaded history via
        # add_static_message and a live streamed reply), in document order:
        # {"message_id": int-or-None, "role": ..., "start_mark": <unique Tk
        # mark name>, "end_mark": <unique Tk mark name, or None while still
        # streaming>, "raw_text": <original markdown source, filled in once
        # end_mark is>}. message_id is None until tag_last_message_ids()
        # backfills it for a just-streamed pair (server.py doesn't allocate
        # the DB row until the reply is fully saved, well after rendering
        # starts) - every other field is known up front. Drives the Edit/
        # Regenerate affordances (see _append_message_action_link) and
        # truncate_after_message/replace_message_content, called by
        # chat_view.py's edit-and-resubmit flow. Never touches msg_start/
        # tail_start/_committed_len/_raw/_in_fence above - those are
        # feed()'s own incremental-rendering state for whichever message is
        # *currently* being built, unrelated to this permanent registry.
        self._message_marks = []
        self._next_mark_seq = 0
        self._stale_tags = []   # per-message link tags of the transcript cleared last time (see clear())
        self._link_commands = []    # Tcl commands behind the current transcript's link bindings (see _bind_message_link)
        self._stale_commands = []   # ...and those of the transcript cleared last time
        # Tk mark-name pair for the single currently-shown "[Regenerate]"
        # span (there's only ever one - it always belongs to whichever
        # message is the current last assistant reply), or False if none is
        # shown right now. Not a message_id - deliberately reused/moved
        # rather than re-created per message, see _append_message_action_link.
        self._regen_span = False
        # Set by chat_view.py after construction - kept as plain callback
        # attributes rather than constructor args so this widget doesn't
        # need to know ChatView's shape, just that these get called with a
        # message_id when the user clicks one of the action links.
        self.on_edit_message = None
        self.on_regenerate_message = None
        # Called with (message_id, position) when one of the "[<] 2/3 [>]"
        # arrows on a regenerated reply is clicked.
        self.on_switch_version = None
        self.tag_bind("link", "<Button-1>", self._on_link_click)
        self.tag_bind("link", "<Enter>", lambda e: self.config(cursor="hand2"))
        self.tag_bind("link", "<Leave>", lambda e: self.config(cursor=""))

    def _configure_tags(self):
        base_font = ("Consolas", 11)
        mono = ("Consolas", 10)
        self.tag_configure("bold", font=(base_font[0], base_font[1], "bold"))
        self.tag_configure("italic", font=(base_font[0], base_font[1], "italic"))
        self.tag_configure("bolditalic", font=(base_font[0], base_font[1], "bold italic"))
        self.tag_configure("strike", overstrike=1)
        self.tag_configure("underline", underline=1)
        self.tag_configure("code_inline", font=mono, background=_T.panel, foreground=_T.bright)
        self.tag_configure(
            "code_block", font=mono, background=_T.panel, foreground=_T.bright,
            lmargin1=12, lmargin2=12, rmargin=12, spacing1=4, spacing3=4,
        )
        for level, size in zip(range(1, 7), (18, 16, 14, 13, 12, 12)):
            self.tag_configure(f"h{level}", font=("Consolas", size, "bold"), spacing1=10, spacing3=4)
        for depth in (1, 2, 3):
            self.tag_configure(f"blockquote{depth}", lmargin1=14 * depth, lmargin2=14 * depth, foreground=_T.dim)
        self.tag_configure("link", foreground=_T.bright, underline=1)
        # Small/dim like role_label's chip caption, not styled as a link
        # (underline=1 there) - this is a click target too but visually
        # subordinate, not something to draw the eye like an inline URL.
        self.tag_configure("edit_action", foreground=_T.dim, font=(base_font[0], 9))
        # The "2/3" readout between the version arrows - same size/dimness as
        # the action links around it, but never a click target, so it's kept
        # as its own tag rather than sharing edit_action's (whose spans all
        # get a cursor/binding treatment this must not pick up).
        self.tag_configure("version_count", foreground=_T.dim, font=(base_font[0], 9))
        # User turns get the lifted-panel fill + indent; assistant turns sit
        # directly on the base surface - the phosphor terminal reads as one
        # continuous log rather than chat bubbles.
        self.tag_configure(
            "user_bubble", background=_T.panel, lmargin1=40, lmargin2=40, rmargin=8,
            spacing1=6, spacing3=6,
        )
        self.tag_configure(
            "assistant_bubble", background=_T.bg, lmargin1=8, lmargin2=8, rmargin=40,
            spacing1=6, spacing3=6,
        )
        # The list margins are configured AFTER the bubble tags: a tag made later outranks an earlier one, and
        # the bubbles' own lmargin used to win over list_item's, so no list was ever indented and sub-items sat
        # at their parent's level (BF-16). An assistant list starts 12 px into its 8 px column, each level
        # LIST_INDENT_PX further; a user turn's list starts 12 px into its 40 px panel column (_insert_segments
        # maps the parser's tag to the user_ variant), so a list never leaves its bubble.
        for depth in range(LIST_MAX_DEPTH + 1):
            suffix = str(depth) if depth else ""
            self.tag_configure(f"list_item{suffix}", lmargin1=20 + LIST_INDENT_PX * depth,
                               lmargin2=34 + LIST_INDENT_PX * depth)
            self.tag_configure(f"user_list_item{suffix}", lmargin1=52 + LIST_INDENT_PX * depth,
                               lmargin2=66 + LIST_INDENT_PX * depth)
        # Uppercase, letter-spaced, with a panel-color background behind just
        # the label span - the closest a Text tag can get to the mockup's
        # bordered "chip" look (Text tags have no border/box-drawing option).
        self.tag_configure(
            "role_label", foreground=_T.dim, background=_T.panel, font=(base_font[0], 9), spacing3=2,
        )
        self.tag_configure("thinking_label", foreground=_T.dim, background=_T.panel, font=(base_font[0], 9), spacing3=2)
        self.tag_configure("thinking_text", font=(base_font[0], base_font[1], "italic"), foreground=_T.dim)

    def _on_link_click(self, event):
        idx = self.index(f"@{event.x},{event.y}")
        for start, end, url in self._links:
            if self.compare(start, "<=", idx) and self.compare(idx, "<", end):
                webbrowser.open(url)
                return

    # ---- message rendering ----

    def _unset_marks(self, names):
        """Deleting text does not delete the marks inside it - they collapse onto
        the deletion point and stay. Every message adds a few (and a "1.0" full of
        them is where clear() leaves them), and each one slows every later
        operation near that point: measured, reopening a 100-message conversation
        cost 0.3 s the 5th time, 1 s the 10th, 3.9 s the 20th and 8.5 s the 30th -
        the transcript got slower the longer the app stayed open. So a message's
        marks are removed together with its text."""
        names = [n for n in names if n]
        if names:
            try:
                self.mark_unset(*names)
            except tk.TclError:
                pass

    @staticmethod
    def _entry_marks(entry):
        return [entry["block_start_mark"], entry["start_mark"], entry["end_mark"], *(entry.get("version_span") or ())]

    def _bind_message_link(self, tag_name, on_click):
        """Click and hover bindings for one per-message link tag. tag_bind registers
        a Tcl command for each (and the Python closure behind it, which holds the
        whole message text), and tkinter frees those only when the widget is
        destroyed - 180 of them and ~125 KB per reopen of a 60-message chat - so
        their names are kept to be deleted together with the tag (see clear())."""
        self._link_commands.append(self.tag_bind(tag_name, "<Button-1>", on_click))
        self._link_commands.append(self.tag_bind(tag_name, "<Enter>", lambda e: self.config(cursor="hand2")))
        self._link_commands.append(self.tag_bind(tag_name, "<Leave>", lambda e: self.config(cursor="")))

    def _destroy_embedded_windows(self, start="1.0", end="end"):
        """Tk destroys an embedded window when the text holding it is deleted, but
        the Python widget objects (a table's Frame and every cell Label, a rule's
        Frame, an image's Label) stay in this widget's .children for good, with
        everything they reference: measured, every reopen of a chat whose replies
        contain tables leaked ~60 KB. Destroying them through tkinter first frees
        both sides."""
        try:
            found = self.dump(start, end, window=True)
        except tk.TclError:
            return
        for _kind, name, _index in found:
            if not name:
                continue
            try:
                self.nametowidget(name).destroy()
            except (KeyError, tk.TclError):
                pass

    def clear(self):
        self.configure(state="normal")
        self._destroy_embedded_windows()
        self.delete("1.0", "end")
        self.configure(state="disabled")
        # Only the per-message marks: msg_start/tail_start/regen_* are single names
        # that start_message and the action links move, so they cannot pile up.
        self._unset_marks([m for m in self.mark_names() if m.startswith(_PER_MESSAGE_MARK_PREFIXES)])
        # The per-message link tags (one for each Edit / Regenerate / [<] [>] span)
        # pile up the same way - 6,000 of them made every open 2-3x slower - but
        # switching a version further up the chat reloads the transcript from inside
        # that very link's click binding, and the tag whose binding is running is not
        # one to delete. So the tags of the transcript being cleared now are deleted
        # at the NEXT clear(): at most one transcript's worth is ever left over.
        if self._stale_tags:
            try:
                self.tag_delete(*self._stale_tags)
            except tk.TclError:
                pass
        for name in self._stale_commands:   # their callbacks go with them (see _bind_message_link)
            try:
                self.deletecommand(name)
            except tk.TclError:
                pass
        self._stale_tags = [t for t in self.tag_names() if t.startswith(_PER_MESSAGE_TAG_PREFIXES)]
        self._stale_commands, self._link_commands = self._link_commands, []
        self._links = []
        self._jump_marks = []
        self._table_buffer = []
        self._message_marks = []
        self._regen_span = False

    # ---- follow-the-end (bug-fix batch 31) ----
    # Every streamed insert used to end with yview_moveto(1.0), so the view was yanked to the bottom on each
    # chunk and the user could never scroll back while a reply or its thinking streamed. Now the insert follows
    # the end only when the view was AT the end before it; a user who scrolls up keeps their place, and following
    # resumes once they return to the bottom (scroll, End). A fresh turn of the user's own always comes into view.
    def _at_end(self):
        """True while the transcript's last line is on screen. dlineinfo, not yview()[1]: the yview fractions are
        built on Tk's ESTIMATED line metrics for lines it has not laid out yet, so right after a jump to the end
        they can read 0.95 while the last line is in plain view (and following would never resume)."""
        try:
            return self.dlineinfo("end-1c") is not None
        except tk.TclError:
            return True

    def _follow_end(self, was_at_end):
        if was_at_end:
            self.see("end-1c")                  # exact (see lays the line out); yview_moveto(1.0) can land short

    def start_message(self, role, label=None, message_id=None, version_pos=1, version_count=1):
        """Begin a new message block. Call feed()/finish_message() to fill
        it. message_id, if already known (loaded history, via
        add_static_message), registers this message right away; if not (a
        fresh streamed reply - server.py doesn't allocate that DB row until
        the reply is fully saved) pass None and call tag_last_message_ids()
        once it's known.

        version_pos/version_count describe this reply's place in its
        regenerate carousel; anything other than 1/1 draws the
        "[<] 2/3 [>]" control. A streamed reply passes neither and gets them
        backfilled by tag_last_message_ids() from the same SSE frame that
        carries its id."""
        was_at_end = self._at_end()
        self.configure(state="normal")
        self._next_mark_seq += 1
        # Captured *before* the leading separator/chip below, unlike
        # start_mark further down (which marks the start of the message's
        # own content, after its chip) - lets remove_message() delete a
        # whole stale message (separator, chip, content, and action link
        # together) for the regenerate flow, distinct from
        # truncate_after_message/replace_message_content, which operate on
        # [start_mark, end_mark) and must never touch the chip.
        block_start_mark = f"msgblock_{self._next_mark_seq}_start"
        self.mark_set(block_start_mark, "end-1c")
        self.mark_gravity(block_start_mark, "left")
        # "Is there any content yet?" - a search stops at the first non-blank character
        # (the chip of the first message), where get().strip() copied the whole
        # transcript for every message started: 2.6 ms at 650 KB, so opening a long
        # conversation was quadratic.
        if self.search(r"\S", "1.0", "end-1c", regexp=True):
            self.insert("end-1c", "\n\n")
        self._current_bubble_tag = "user_bubble" if role == "user" else "assistant_bubble"
        if label:
            # Bracket-chip text ("[ USER ]") matching the approved mockup -
            # Text tags can't draw a real border, so the bracket characters
            # carry that look instead of relying on the background fill alone.
            chip = "[ " + theme_mod.letter_spaced(label) + " ]"
            self.insert("end-1c", chip + "\n", ("role_label", self._current_bubble_tag))
        self.mark_set("msg_start", "end-1c")
        self.mark_gravity("msg_start", "left")
        self.mark_set("tail_start", "end-1c")
        self.mark_gravity("tail_start", "left")
        self._raw = ""
        self._committed_len = 0
        self._in_fence = False
        self._last_nl_abs = -1
        self._table_buffer = []
        start_mark = f"msgmark_{self._next_mark_seq}_start"
        self.mark_set(start_mark, "end-1c")
        self.mark_gravity(start_mark, "left")
        self._message_marks.append({
            "message_id": message_id, "role": role, "block_start_mark": block_start_mark,
            "start_mark": start_mark, "end_mark": None, "raw_text": "",
            "version_pos": version_pos, "version_count": version_count,
            # Mark pair around this message's own "[<] 2/3 [>]" span, or None
            # while it has none. Per-message and permanent, unlike the single
            # shared regen span below it - every regenerated reply keeps its
            # arrows for the life of the transcript, but only the last one
            # can still be regenerated.
            "version_span": None,
        })
        self.configure(state="disabled")
        self._follow_end(was_at_end)

    def feed(self, chunk):
        """Append raw markdown text to the message currently being built."""
        # Track the last newline's absolute position incrementally, from the
        # new chunk alone - self._raw.rfind("\n") over the *whole* accumulated
        # message would itself be an O(message length) scan on every single
        # streamed token, quietly reintroducing the O(n^2) cost this whole
        # rewrite exists to remove (found via a timing test showing per-chunk
        # cost still growing ~20x over a 4000-chunk stream despite the
        # tail-only rendering below).
        was_at_end = self._at_end()
        old_len = len(self._raw)
        self._raw += chunk
        rel = chunk.rfind("\n")
        if rel != -1:
            self._last_nl_abs = old_len + rel
        self._render_incremental()
        self._follow_end(was_at_end)

    def finish_message(self):
        was_at_end = self._at_end()
        self._render_incremental(final=True)
        if self._message_marks and self._message_marks[-1]["end_mark"] is None:
            entry = self._message_marks[-1]
            self.configure(state="normal")
            entry["raw_text"] = self._raw
            end_mark = entry["start_mark"].replace("_start", "_end")
            self.mark_set(end_mark, "end-1c")
            self.mark_gravity(end_mark, "left")
            entry["end_mark"] = end_mark
            self._append_message_action_link(entry)
            self.configure(state="disabled")
        self._follow_end(was_at_end)

    def add_static_message(self, role, text, label=None, message_id=None,
                           version_pos=1, version_count=1, images=(), inserts=()):
        """Render a complete (non-streaming) message, e.g. loaded history. images: decoded tk.PhotoImages drawn
        inside the message, under its text (BF-13): within [start_mark, end_mark), so replace / truncate / remove
        treat them as its content, and before its [Edit] / [Regenerate] link."""
        if role == "user":
            # Placed before start_message()'s own separator/label insert, so
            # jumping here lands right at the top of this message's block
            # rather than mid-way into it.
            mark_name = f"jump_{len(self._jump_marks)}"
            self.mark_set(mark_name, "end-1c")
            self.mark_gravity(mark_name, "left")
            preview = re.sub(r"\s+", " ", text).strip()[:60] or "(empty message)"
            self._jump_marks.append((mark_name, preview))
        self.start_message(
            role, label=label, message_id=message_id,
            version_pos=version_pos, version_count=version_count,
        )
        # inserts: (offset, widget) pairs - the document tool's cards sit at the point of the reply where the model
        # made them (the text before the call, the card, the text after it); see embed_widget_inline.
        pos = 0
        for offset, widget in sorted(inserts, key=lambda p: p[0]):
            offset = max(pos, min(len(text), int(offset or 0)))
            if offset > pos:
                self.feed(text[pos:offset])
            self.embed_widget_inline(widget)
            pos = offset
        self.feed(text[pos:])
        if images:
            self._render_incremental(final=True)    # the text is complete: the images go after all of it
            self.configure(state="normal")
            for photo in images:
                self._embed_image_window(photo)
            self.mark_set("tail_start", "end-1c")   # finish_message's own final flush must not delete them
            self.mark_gravity("tail_start", "left")
            self.configure(state="disabled")
        self.finish_message()
        if role == "user":
            self.see("end-1c")                  # the user's own fresh turn always comes into view (batch 31)

    # ---- per-message edit/regenerate registry ----
    #
    # message_id_at()-style mouse hit-testing (continuous <Motion> tracking +
    # bbox-positioned hover overlay) was considered and deliberately dropped
    # for a simpler, lower-risk design: a small always-rendered "[Edit]"/
    # "[Regenerate]" text span per message, using the exact same tag_bind
    # click mechanism _on_link_click already proves out above - one unique
    # tag per span (so each click resolves to a specific message with zero
    # position math), styled via the shared "edit_action" tag.

    def _message_mark_index(self, message_id):
        for i, entry in enumerate(self._message_marks):
            if entry["message_id"] == message_id:
                return i
        return None

    def _append_message_action_link(self, entry):
        """Called once a message's content is fully rendered (finish_message
        or replace_message_content) - appends its action link just past
        end_mark, so the link text itself is never mistaken for message
        content by truncate_after_message/replace_message_content (both
        operate on [start_mark, end_mark), which stops short of it).

        An assistant reply gets up to two spans here, in this order: its own
        permanent "[<] 2/3 [>]" version control (only once it has more than
        one version), then the single shared "[Regenerate]" link, which is
        moved here off whichever reply used to be last."""
        if not getattr(self, "show_message_actions", True):
            return                      # a read-only rendering (the document viewer): no [Edit] / [Regenerate] links
        if entry["role"] == "user":
            self._next_mark_seq += 1
            tag_name = f"editlink_{self._next_mark_seq}"
            self._bind_message_link(tag_name, lambda e, ent=entry: self._invoke_action("edit", ent))
            self.insert("end-1c", "  [Edit]", (tag_name, "edit_action", self._current_bubble_tag))
        elif entry["role"] == "assistant":
            self._append_version_span(entry)
            # Only one "[Regenerate]" span ever exists (it only ever makes
            # sense on the true last assistant reply) - remove whichever
            # message's span is currently showing before adding this one's,
            # rather than leaving a stale, still-clickable link sitting
            # after an assistant reply that's no longer actually last.
            if self._regen_span:
                try:
                    self.delete("regen_start", "regen_end")
                except tk.TclError:
                    pass
                self._regen_span = False
            self.mark_set("regen_start", "end-1c")
            self.mark_gravity("regen_start", "left")
            self._next_mark_seq += 1
            tag_name = f"regenlink_{self._next_mark_seq}"
            self._bind_message_link(tag_name, lambda e, ent=entry: self._invoke_action("regenerate", ent))
            self.insert("end-1c", "  [Regenerate]", (tag_name, "edit_action", self._current_bubble_tag))
            self.mark_set("regen_end", "end-1c")
            self.mark_gravity("regen_end", "left")
            self._regen_span = True

    def _append_version_span(self, entry):
        """Draws (or redraws) one assistant reply's "[<] 2/3 [>]" control at
        the current end of the transcript. Nothing at all for a reply that
        has never been regenerated - a lone "1/1" would be noise on every
        single message.

        Only ever called with `entry` as the last thing in the widget: either
        while its message is being appended normally, or from
        set_message_versions() on the last message right after a regenerate.
        The arrows for older messages are never redrawn - they can't change
        without a version switch, and a switch on a non-last message re-renders
        through chat_view.py's reload path instead (Tk's Text can only build
        content by appending, so rewriting a message mid-transcript isn't
        something this widget does)."""
        old = entry.get("version_span")
        if old:
            try:
                # A no-op zero-width delete when the caller already removed
                # this text some other way (replace_message_content wipes the
                # whole message body, collapsing both marks onto one index) -
                # the mark_unset is what actually matters in that case.
                self.delete(*old)
                self.mark_unset(*old)
            except tk.TclError:
                pass
            entry["version_span"] = None
        count = entry.get("version_count") or 1
        if count < 2:
            return
        pos = entry.get("version_pos") or 1
        self._next_mark_seq += 1
        seq = self._next_mark_seq
        start_mark, end_mark = f"vspan_{seq}_start", f"vspan_{seq}_end"
        self.mark_set(start_mark, "end-1c")
        self.mark_gravity(start_mark, "left")
        # Wrap around at both ends, so a single arrow can walk the whole
        # carousel - matches how the [Edit]/[Regenerate] links behave in
        # never being disabled-but-present.
        prev_pos = count if pos <= 1 else pos - 1
        next_pos = 1 if pos >= count else pos + 1
        self.insert("end-1c", "  ", ("edit_action", self._current_bubble_tag))
        self._insert_version_arrow(entry, "[<]", prev_pos)
        self.insert("end-1c", f" {pos}/{count} ", ("version_count", self._current_bubble_tag))
        self._insert_version_arrow(entry, "[>]", next_pos)
        self.mark_set(end_mark, "end-1c")
        self.mark_gravity(end_mark, "left")
        entry["version_span"] = (start_mark, end_mark)

    def _insert_version_arrow(self, entry, glyph, target_position):
        self._next_mark_seq += 1
        tag_name = f"verlink_{self._next_mark_seq}"
        self._bind_message_link(tag_name, lambda e, ent=entry, p=target_position: self._invoke_version_switch(ent, p))
        self.insert("end-1c", glyph, (tag_name, "edit_action", self._current_bubble_tag))

    def _invoke_version_switch(self, entry, position):
        if entry["message_id"] is None or not self.on_switch_version:
            return
        self.on_switch_version(entry["message_id"], position)

    def set_message_versions(self, message_id, version_pos, version_count):
        """Updates one message's version state and redraws its arrows. Used
        after a regenerate, whose replacement reply is streamed in before the
        server reports the new version count (see chat_view.py's message_ids
        handling)."""
        idx = self._message_mark_index(message_id)
        if idx is None:
            return
        entry = self._message_marks[idx]
        was_showing = entry.get("version_span") is not None
        entry["version_pos"] = version_pos
        entry["version_count"] = version_count
        if version_count < 2 and not was_showing:
            # Nothing drawn before, nothing to draw now - the ordinary case on
            # every non-regenerated reply. Returning here keeps the common
            # path clear of the delete-and-relay-the-links dance below.
            return
        if idx != len(self._message_marks) - 1:
            # Not the last message, so "end-1c" isn't where its control
            # belongs - leave the stale arrows rather than draw them in the
            # wrong place. Only reachable if a later message somehow arrived
            # first; the reload path fixes it either way.
            return
        self.configure(state="normal")
        # The regenerate link sits immediately after the version span and has
        # to be re-laid after it, or redrawing the arrows would leave it
        # stranded ahead of them.
        if self._regen_span:
            try:
                self.delete("regen_start", "regen_end")
            except tk.TclError:
                pass
            self._regen_span = False
        self._current_bubble_tag = "user_bubble" if entry["role"] == "user" else "assistant_bubble"
        self._append_message_action_link(entry)
        self.configure(state="disabled")

    def message_versions(self, message_id):
        """(version_pos, version_count) as currently rendered, or (None, None)
        if this message isn't in the transcript."""
        idx = self._message_mark_index(message_id)
        if idx is None:
            return None, None
        entry = self._message_marks[idx]
        return entry.get("version_pos") or 1, entry.get("version_count") or 1

    def is_last_message(self, message_id):
        """True if message_id is the final message rendered - the condition
        for re-rendering it in place (see replace_message_content)."""
        return bool(self._message_marks) and self._message_marks[-1]["message_id"] == message_id

    def _invoke_action(self, kind, entry):
        # message_id can still be None here in the narrow window right after
        # optimistically rendering the user's own just-sent message, before
        # the server has actually saved it (see tag_last_message_ids) - a
        # click in that window is a no-op rather than acting on a message
        # this app can't yet address by id.
        if entry["message_id"] is None:
            return
        if kind == "edit" and self.on_edit_message:
            self.on_edit_message(entry["message_id"])
        elif kind == "regenerate" and self.on_regenerate_message:
            self.on_regenerate_message(entry["message_id"])

    def tag_last_message_ids(self, user_id, assistant_id):
        """Backfills message_id for the two most-recently-appended entries
        that don't have one yet (a freshly-streamed user+assistant pair,
        whose real DB ids aren't known until the whole exchange completes
        server-side - see server.py's message_ids SSE frame). Walks
        backward and fills the first two untagged entries found, newest
        first, rather than assuming they're literally the last two list
        items."""
        pending = [assistant_id, user_id]
        for entry in reversed(self._message_marks):
            if not pending:
                break
            if entry["message_id"] is None:
                entry["message_id"] = pending.pop(0)

    def raw_text_for(self, message_id):
        """Original markdown source for a rendered message, e.g. to
        prefill the composer for editing - None if unknown/not yet
        finished rendering."""
        idx = self._message_mark_index(message_id)
        if idx is None:
            return None
        return self._message_marks[idx]["raw_text"]

    def truncate_after_message(self, message_id):
        """Removes every message rendered after message_id, including any
        embedded images/tables (Tk drops an embedded window when the text
        range containing it is deleted) - the first half of the edit-and-
        truncate flow, always followed by replace_message_content() on the
        same message_id. Only ever called on an already-finished message,
        never the actively-streaming one - the Send/Stop button state
        already prevents that overlap, so end_mark is guaranteed to be set."""
        idx = self._message_mark_index(message_id)
        if idx is None:
            return
        entry = self._message_marks[idx]
        self.configure(state="normal")
        cutoff = self.index(entry["end_mark"])
        self._destroy_embedded_windows(entry["end_mark"], "end-1c")
        self.delete(entry["end_mark"], "end-1c")
        self._links = [l for l in self._links if self.compare(l[0], "<", cutoff)]
        dropped = [m for m, p in self._jump_marks if not self.compare(m, "<", cutoff)]
        self._jump_marks = [(m, p) for m, p in self._jump_marks if self.compare(m, "<", cutoff)]
        self._unset_marks(dropped + [m for e in self._message_marks[idx + 1:] for m in self._entry_marks(e)])
        del self._message_marks[idx + 1:]
        # Whatever "[Regenerate]" span existed belonged to a now-deleted
        # assistant reply (it was always positioned after this point, since
        # assistant replies always follow their user turn) - the text is
        # already gone with everything else past cutoff; just drop the
        # bookkeeping so a later finish_message() doesn't try to delete an
        # already-gone range.
        self._regen_span = False
        # message_id is now the last message in the transcript, but its own
        # action link was just deleted too (it sat right after end_mark,
        # same as everything else past cutoff) - restore it, or this
        # message would be left with no Edit link at all. Redundant-but-
        # harmless when (as always in practice) replace_message_content()
        # runs right after and re-does this itself.
        self._current_bubble_tag = "user_bubble" if entry["role"] == "user" else "assistant_bubble"
        self._append_message_action_link(entry)
        self.configure(state="disabled")

    def remove_message(self, message_id):
        """Removes message_id's own rendering entirely - its leading
        separator, chip, content, and action link - plus anything after it
        (in practice never more than its own decoration, since this is only
        ever used to clear the true last assistant reply before a fresh one
        streams in for it - see chat_view.py's _on_regenerate_clicked).
        Distinct from truncate_after_message, which keeps its target
        message and removes only what comes after."""
        idx = self._message_mark_index(message_id)
        if idx is None:
            return
        entry = self._message_marks[idx]
        self.configure(state="normal")
        cutoff = self.index(entry["block_start_mark"])
        self._destroy_embedded_windows(entry["block_start_mark"], "end-1c")
        self.delete(entry["block_start_mark"], "end-1c")
        self.configure(state="disabled")
        self._links = [l for l in self._links if self.compare(l[0], "<", cutoff)]
        dropped = [m for m, p in self._jump_marks if not self.compare(m, "<", cutoff)]
        self._jump_marks = [(m, p) for m, p in self._jump_marks if self.compare(m, "<", cutoff)]
        self._unset_marks(dropped + [m for e in self._message_marks[idx:] for m in self._entry_marks(e)])
        del self._message_marks[idx:]
        self._regen_span = False

    def replace_message_content(self, message_id, new_text, images=(), inserts=()):
        """Re-renders message_id's own content in place - the second half
        of the edit-and-truncate flow. Only ever called on the LAST message
        in the transcript (truncate_after_message removes everything after
        it first), so "end-1c" is exactly this message's own trailing edge
        once its old content is deleted - reuses the normal end-of-
        transcript insertion path (_process_complete_line, same as
        add_static_message/feed) rather than a second insertion
        implementation, so tables/code fences/etc. in the edited text render
        exactly like they would anywhere else."""
        idx = self._message_mark_index(message_id)
        if idx is None:
            return
        entry = self._message_marks[idx]
        self.configure(state="normal")
        start_idx = self.index(entry["start_mark"])
        self._links = [l for l in self._links if self.compare(l[0], "<", start_idx)]
        self._destroy_embedded_windows(entry["start_mark"], "end-1c")
        self.delete(entry["start_mark"], "end-1c")
        prior_bubble_tag = self._current_bubble_tag
        prior_in_fence = self._in_fence
        prior_table_buffer = self._table_buffer
        self._current_bubble_tag = "user_bubble" if entry["role"] == "user" else "assistant_bubble"
        self._in_fence = False
        self._table_buffer = []
        pos = 0
        for offset, widget in sorted(inserts, key=lambda p: p[0]):     # the version's document cards, in place
            offset = max(pos, min(len(new_text), int(offset or 0)))
            for line in new_text[pos:offset].split("\n"):
                self._process_complete_line(line)
            self._flush_table_buffer()
            self._embed_widget_window(widget)
            pos = offset
        for line in new_text[pos:].split("\n"):
            self._process_complete_line(line)
        self._flush_table_buffer()
        for photo in images:                    # BF-13: inside the replaced message too
            self._embed_image_window(photo)
        entry["raw_text"] = new_text
        self.mark_set(entry["end_mark"], "end-1c")
        self.mark_gravity(entry["end_mark"], "left")
        self._append_message_action_link(entry)
        self._current_bubble_tag = prior_bubble_tag
        self._in_fence = prior_in_fence
        self._table_buffer = prior_table_buffer
        self.configure(state="disabled")
        self.yview_moveto(1.0)

    def embed_image(self, photo):
        """Embeds an already-decoded tk.PhotoImage right after the message
        just closed out by add_static_message()/finish_message() - same
        window_create technique as _insert_table/_insert_hr, since Text has
        no native way to show a widget. Unlike ordinary markdown ![]()
        syntax (which really can't show a *remote* URL - see INLINE_RE's
        comment above), these bytes are already local: the caller fetched
        them from this app's own server or, for a just-staged attachment,
        already had them in memory. Must be called after the message's own
        start_message()/feed()/finish_message() cycle has fully closed, not
        while one is still being fed - it inserts directly at "end-1c"
        without touching _raw/_render_incremental's tracking state."""
        was_at_end = self._at_end()
        self.configure(state="normal")
        self._embed_image_window(photo)
        self.configure(state="disabled")
        self._follow_end(was_at_end)

    def _embed_image_window(self, photo):
        """The image window at the current end of the text (the widget must be editable): one holder Label with
        the bubble's ground, then a newline (BF-13: shared by embed_image and the in-message paths)."""
        bg = _T.panel if self._current_bubble_tag == "user_bubble" else _T.bg
        holder = tk.Label(self, image=photo, bg=bg, borderwidth=0, takefocus=1, highlightthickness=1,
                          highlightbackground=bg, highlightcolor=_T.bright)
        holder.image = photo  # tkinter drops a PhotoImage with no live Python reference
        # Document tool P4 / P5: a click opens the image viewer, a right click its menu - through the owner's hooks
        # (on_image_click(photo), on_image_menu(photo, event)), set by the ChatView; None leaves the image inert.
        # P6: Tab reaches the image (a 1 px focus ring), Return opens, Shift+F10 / the menu key open its menu, and the
        # owner hears the focus (on_image_focus(photo, focused)) for the legend.
        holder.configure(cursor="hand2")
        holder.bind("<Button-1>", lambda e, p=photo: self._image_hook("on_image_click", p, e), add="+")
        holder.bind("<Button-3>", lambda e, p=photo: self._image_hook("on_image_menu", p, e), add="+")
        holder.bind("<Return>", lambda e, p=photo: self._image_hook("on_image_click", p, e), add="+")
        for seq in ("<Shift-F10>", "<App>"):
            try:
                holder.bind(seq, lambda e, p=photo: self._image_hook("on_image_menu", p, e), add="+")
            except tk.TclError:
                pass
        holder.bind("<FocusIn>", lambda e, p=photo: self._image_focus_hook(p, True), add="+")
        holder.bind("<FocusOut>", lambda e, p=photo: self._image_focus_hook(p, False), add="+")
        self.window_create("end-1c", window=holder)
        self.insert("end-1c", "\n")

    def _image_focus_hook(self, photo, focused):
        fn = getattr(self, "on_image_focus", None)
        if fn is not None:
            try:
                fn(photo, focused)
            except Exception:  # noqa: BLE001
                pass

    def _image_hook(self, name, photo, event):
        fn = getattr(self, name, None)
        if fn is None:
            return None
        try:
            if name == "on_image_menu":
                fn(photo, event)
            else:
                fn(photo)
        except Exception:  # noqa: BLE001 - a viewer failure never breaks the transcript's own bindings
            pass
        return "break"

    # ---- embedded widgets inside a message (the document tool's creating row / card) ----

    # the body text's left margin (the assistant bubble's lmargin1): the document rows / cards line up with it
    DOC_WIDGET_PADX = 8

    def _embed_widget_window(self, widget, padx=None):
        """`widget` (a child of this Text) at the current end, on its own line (the widget must be editable), indented
        to the body text's left margin."""
        self.window_create("end-1c", window=widget, padx=self.DOC_WIDGET_PADX if padx is None else padx)
        self.insert("end-1c", "\n")

    def embed_widget_inline(self, widget):
        """Inserts `widget` at the current end of the message being built - after everything fed so far - so that
        later chunks render AFTER it. Everything fed so far is committed first (the incremental renderer only ever
        re-renders the uncommitted tail; the widget sits before the new tail start and is never deleted by the next
        tick). A line the widget interrupts ends there: the text after it starts a new paragraph."""
        was_at_end = self._at_end()
        self._render_incremental(final=True)
        self.configure(state="normal")
        self._embed_widget_window(widget)
        self.mark_set("tail_start", "end-1c")
        self.mark_gravity("tail_start", "left")
        self.configure(state="disabled")
        self._follow_end(was_at_end)

    def replace_embedded_widget(self, old, new):
        """Swaps one embedded widget for another in place (the creating row becomes the card); the old widget is
        destroyed. False when `old` is no longer in the text."""
        try:
            index = self.index(old)
        except tk.TclError:
            return False
        self.configure(state="normal")
        try:
            self.window_configure(index, window=new)
        except tk.TclError:
            self.configure(state="disabled")
            return False
        self.configure(state="disabled")
        try:
            old.destroy()
        except tk.TclError:
            pass
        return True

    def jump_marks(self):
        """(mark_name, preview_text) for every user message currently
        rendered, in conversation order."""
        return list(self._jump_marks)

    def scroll_to_mark(self, mark_name):
        """Two-step jump: a coarse proportional-character-offset seek first,
        then `yview(mark_name)` (the Text widget's "scroll so this index is
        the top line" form - distinct from `see()`, which only guarantees
        *some* visibility and can leave the target sitting anywhere in the
        viewport, including right at the bottom edge) to land exactly at the
        top, matching where a jump target should actually put the reader.

        Resolving *any* arbitrary index's on-screen position under
        wrap="word" needs to lay out every display line between the current
        view and the target - measured as multiple *seconds* for a single
        very long paragraph with no line breaks (confirmed empirically: 100k
        characters of ordinary word-wrapping prose cost ~600ms, 400k cost
        ~9s) if attempted cold. That's what the plain character-offset seek
        below originally existed to dodge entirely, but proportional
        chars-to-pixels is only an *approximation* - markdown structure
        (headings/code blocks/blank lines) adds vertical space the raw
        character count doesn't account for, so longer conversations drifted
        further off with every checkpoint past the first (confirmed real:
        reported as "misses it by a few scrolls" for anything past the first
        prompt).

        The fix isn't choosing one over the other - it's both, in order.
        Confirmed empirically (60-checkpoint realistic transcript, and a
        30-message transcript with one ~150k-character pathological reply
        buried in the middle): every checkpoint on either side of the
        pathological content still resolves in well under 50ms and lands
        within a couple pixels of the top once primed by the coarse seek
        first - the coarse seek is what makes the follow-up exact-snap
        cheap, on top of already being close by itself. Net worst-case cost
        is about the same as the coarse seek always was; net accuracy is
        exact rather than approximate."""
        try:
            total = self.count("1.0", "end-1c", "chars")
            pos = self.count("1.0", mark_name, "chars")
        except tk.TclError:
            return
        total_chars = total[0] if total else 0
        pos_chars = pos[0] if pos else 0
        if total_chars <= 0:
            return
        self.yview_moveto(max(0.0, min(1.0, pos_chars / total_chars)))
        try:
            self.yview(mark_name)
        except tk.TclError:
            pass

    # ---- reasoning model "thinking" block (live, never persisted) ----
    #
    # Deliberately separate from start_message()/feed()/finish_message()'s
    # tail-reparse machinery rather than reusing it: reasoning traces are
    # plain prose (no markdown formatting to parse), so a plain sequential
    # append is O(1) per chunk with no need for feed()'s incremental
    # tail-delete-and-reinsert dance, which exists purely to make *markdown*
    # re-parsing cheap. Always finishes (if it ever starts) strictly before
    # start_message() begins the real answer below it - the two never
    # interleave, so there's no shared-state conflict with the marks/
    # committed-length tracking feed() uses.

    def start_thinking(self):
        """Begin a reasoning model's live chain-of-thought block, shown
        dim/italic above the eventual answer."""
        was_at_end = self._at_end()
        self._thinking_line_len = 0
        self.configure(state="normal")
        if self.get("1.0", "end-1c").strip():
            self.insert("end-1c", "\n\n")
        chip = "[ " + theme_mod.letter_spaced("Thinking") + " ]"
        self.insert("end-1c", chip + "\n", ("thinking_label",))
        self.configure(state="disabled")
        self._follow_end(was_at_end)

    def feed_thinking(self, chunk):
        """Append raw reasoning text - plain text, not markdown-parsed."""
        # Tk re-measures a whole line each time it grows, so one unbroken line gets
        # dearer with every chunk (measured, a looping model's 120 KB line: ~130 ms
        # per chunk, 25 s for the stream, hitches of 0.8 s). Real reasoning has line
        # breaks; after MAX_INLINE_PARSE_CHARS without one this live view starts a
        # new line, at the chunk's first space so a word is not cut. Only this
        # display copy changes - the Thoughts panel and the saved reasoning use the
        # model's own text.
        if "\n" in chunk:
            self._thinking_line_len = len(chunk) - chunk.rfind("\n") - 1
        elif self._thinking_line_len + len(chunk) > MAX_INLINE_PARSE_CHARS:
            cut = chunk.find(" ")
            chunk = chunk[:cut] + "\n" + chunk[cut + 1:] if cut != -1 else "\n" + chunk
            self._thinking_line_len = len(chunk) - chunk.rfind("\n") - 1
        else:
            self._thinking_line_len += len(chunk)
        was_at_end = self._at_end()
        self.configure(state="normal")
        self.insert("end-1c", chunk, ("thinking_text",))
        self.configure(state="disabled")
        self._follow_end(was_at_end)

    def finish_thinking(self):
        was_at_end = self._at_end()
        self.configure(state="normal")
        self.insert("end-1c", "\n", ("thinking_text",))
        self.configure(state="disabled")
        self._follow_end(was_at_end)

    def _insert_segments(self, segments):
        user = self._current_bubble_tag == "user_bubble"
        for text, tags, url in segments:
            if user and tags and tags[0].startswith("list_item"):
                tags = ("user_" + tags[0],) + tags[1:]      # the user column's own list margins (BF-16)
            all_tags = (self._current_bubble_tag,) + tags
            start = self.index("end-1c")
            self.insert("end-1c", text, all_tags)
            if url:
                end = self.index("end-1c")
                self._links.append((start, end, url))

    def _commit_plain_line(self, line):
        segments, self._in_fence = _parse_lines([line], self._in_fence)
        self._insert_segments(segments)

    def _process_complete_line(self, line):
        """Feeds one already-\\n-terminated line into the table detector: a
        table only ever renders as one real grid widget once its full extent
        is known (a Tk embedded window can't be "grown" a row at a time the
        way plain text can), so candidate table lines are held in
        self._table_buffer - not inserted - until either a non-table line or
        the end of the message resolves whether it really was one."""
        if self._table_buffer and (self._in_fence or FENCE_RE.match(line)):
            # A fence opened (or we're already inside one) while a table was
            # being tentatively collected - abandon the table attempt and
            # flush what's buffered as plain text first.
            self._flush_table_buffer()
        if self._in_fence or FENCE_RE.match(line):
            self._commit_plain_line(line)
            return
        if not self._table_buffer:
            if TABLE_ROW_RE.match(line):
                self._table_buffer = [line]
            elif HR_RE.match(line) or HTML_HR_RE.match(line):
                self._insert_hr()
            else:
                self._commit_plain_line(line)
            return
        if len(self._table_buffer) == 1:
            if TABLE_SEPARATOR_RE.match(line):
                self._table_buffer.append(line)
            else:
                # No valid separator followed - the candidate header was
                # just an ordinary line that happened to contain pipes.
                pending = self._table_buffer
                self._table_buffer = []
                self._commit_plain_line(pending[0])
                self._process_complete_line(line)
            return
        if TABLE_ROW_RE.match(line):
            self._table_buffer.append(line)
        else:
            self._flush_table_buffer()
            self._process_complete_line(line)

    def _flush_table_buffer(self):
        lines = self._table_buffer
        self._table_buffer = []
        if not lines:
            return
        if len(lines) < 2:
            # Never reached a valid separator row - not actually a table.
            for line in lines:
                self._commit_plain_line(line)
            return
        headers = _split_table_row(lines[0])
        sep_cells = _split_table_row(lines[1])
        aligns = [_table_column_alignment(c) for c in sep_cells]
        aligns += ["left"] * max(0, len(headers) - len(aligns))
        rows = [_split_table_row(line) for line in lines[2:]]
        ncols = max(len(headers), max((len(r) for r in rows), default=0), 1)
        if (len(rows) + 1) * ncols > MAX_TABLE_GRID_CELLS:
            self._insert_segments([(_table_as_text(headers, aligns, rows), ("code_block",), None)])
            return
        self._insert_table(headers, aligns, rows)

    def _insert_table(self, headers, aligns, rows):
        """Embeds an actual gridded table widget (a Frame of Labels laid out
        with .grid()) at the current insertion point via window_create,
        instead of leaving the raw "| a | b |" source visible - a Tk Text
        widget has no native table/box-layout primitive, so this is built by
        hand rather than via a tag."""
        ncols = max(len(headers), max((len(r) for r in rows), default=0), 1)
        bubble_bg = _T.panel if self._current_bubble_tag == "user_bubble" else _T.bg
        # Best-effort column width so long cell text wraps instead of
        # pushing the table wider than the transcript pane - Tk doesn't
        # reflow an already-embedded window on later resize, so this is a
        # one-time estimate at insert time, not a live constraint.
        usable_px = max(self.winfo_width(), 300) - 2 * int(self.cget("padx")) - 40
        col_px = max(70, usable_px // ncols)

        frame = tk.Frame(self, bg=bubble_bg, highlightthickness=1, highlightbackground=_T.dim)

        def add_cell(row_i, col_i, text, header):
            align = aligns[col_i] if col_i < len(aligns) else "left"
            anchor = {"left": "w", "center": "center", "right": "e"}[align]
            justify = {"left": "left", "center": "center", "right": "right"}[align]
            cell = tk.Label(
                frame, text=_clean_table_cell_text(text), anchor=anchor, justify=justify, wraplength=col_px,
                padx=8, pady=4, highlightthickness=1, highlightbackground=_T.dim,
                font=("Consolas", 10, "bold" if header else "normal"),
                bg=_T.panel if header else bubble_bg, fg=_T.bright if header else _T.text,
            )
            cell.grid(row=row_i, column=col_i, sticky="nsew")

        for c in range(ncols):
            add_cell(0, c, headers[c] if c < len(headers) else "", header=True)
        for r, row in enumerate(rows, start=1):
            for c in range(ncols):
                add_cell(r, c, row[c] if c < len(row) else "", header=False)
        for c in range(ncols):
            frame.grid_columnconfigure(c, weight=1)

        self.window_create("end-1c", window=frame)
        self.insert("end-1c", "\n")

    def _insert_hr(self):
        """A standalone "---"/"***"/"___" line (see HR_RE) is markdown's
        horizontal-rule syntax - rendered as an actual thin divider (same
        window_create technique as _insert_table) instead of literal dash
        characters."""
        usable_px = max(self.winfo_width(), 300) - 2 * int(self.cget("padx")) - 20
        rule = tk.Frame(self, bg=_T.dim, height=2, width=usable_px)
        self.window_create("end-1c", window=rule)
        self.insert("end-1c", "\n")

    def _render_incremental(self, final=False):
        """Re-render only the still-growing tail of the current message
        instead of re-parsing and re-inserting the whole accumulated reply on
        every streamed chunk (the latter is O(n^2) in the reply's length -
        confirmed via profiling that long replies caused visibly increasing
        per-chunk lag). Every line already terminated by "\n" is permanently
        fixed (streaming only ever appends), so it's parsed once and never
        touched again; only the trailing partial line - which can still
        change as more text arrives - gets deleted and reinserted each time.

        `final` is set only from finish_message(): the last line of a message
        commonly has no trailing "\n" at all (the raw text just ends), so it
        never reaches the "complete line" path below during normal streaming
        ticks - final=True treats it as complete too and force-flushes any
        table still being tentatively collected (e.g. a table that is the
        very last thing in the reply)."""
        self.configure(state="normal")
        # Deliberately end-1c, not "end": Tk's Text widget always maintains
        # an extra trailing newline beyond whatever you've explicitly
        # inserted, and a live mark sitting at the boundary of a delete that
        # reaches all the way to "end" collapses *backward past the label's
        # own newline* once that trailing region is removed (reproduced and
        # confirmed empirically - this silently merged the role label and
        # message body onto one line, e.g. "Youhello" instead of "You\nhello").
        # Consistently targeting end-1c for both the delete boundary and the
        # insertion point avoids the ambiguity entirely.
        self.delete("tail_start", "end-1c")
        # Only the tail's own (not-yet-committed) links need dropping here -
        # links belonging to already-committed lines are permanent.
        self._links = [l for l in self._links if self.compare(l[0], "<", "tail_start")]

        last_nl = self._last_nl_abs
        if last_nl >= self._committed_len:
            new_lines = self._raw[self._committed_len:last_nl + 1].split("\n")[:-1]
            for line in new_lines:
                self._process_complete_line(line)
            self._committed_len = last_nl + 1
            self.mark_set("tail_start", "end-1c")
            self.mark_gravity("tail_start", "left")

        tail = self._raw[self._committed_len:]
        if final:
            if tail:
                self._process_complete_line(tail)
                self._committed_len = len(self._raw)
            self._flush_table_buffer()
            self.mark_set("tail_start", "end-1c")
            self.mark_gravity("tail_start", "left")
        else:
            # Still-growing last line - always a live plain-text preview,
            # regardless of table-buffer state (a table row isn't evaluated
            # for table membership until it's actually complete).
            #
            # The preview is deleted and re-inserted on every chunk, and Tk's cost
            # for that grows faster than the line (measured with one unbroken line:
            # ~0.3 s per chunk at a 64 KB tail, up to 1.5 s near 120 KB - 105 s for
            # the whole stream, 6 s with the cap below - and a model stuck in a loop
            # can produce such a line). A line longer than
            # MAX_INLINE_PARSE_CHARS is plain text anyway (see parse_inline), so
            # only its end is previewed; the whole line is drawn once, when it
            # completes or the message ends. Anything shorter - every real
            # paragraph - streams in full, as before.
            preview = tail if len(tail) <= MAX_INLINE_PARSE_CHARS else "…" + tail[-MAX_INLINE_PARSE_CHARS:]
            tail_segments, _ = _parse_lines([preview], self._in_fence)
            self._insert_segments(tail_segments)

        self.configure(state="disabled")      # the callers (feed / finish_message) follow the end, see _follow_end
