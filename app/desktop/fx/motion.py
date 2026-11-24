"""fx/motion.py - the approved motion (D-24: TYPE-ON at FULL). BUILD_PLAN section 4 B10; GAP_FX 1.1-1.9.

    Motion(fx)                                   fx.motion of an installed FX (NullFX keeps the no-op namespace)
      .on_emit(widget, event, data)              called by FX.emit BEFORE sound.emit, in its own try/except; it is
                                                 NOT a subscribe() registration (subscriber counts stay as they were):
          nav.route, cause user, kind != initial -> type_title(the screen shown) + latch(the active rail cell)
          nav.route (any)                        -> finishes a running boot pass
          load.end                               -> latch(the active rail cell)
          app.shutdown                           -> power_down()
      .type_on(target, text=None, *, per=None, slot="type", cursor=True, done=None)
      .type_title(screen)                        screen.fx_title, per = ceil(n / 9)
      .print_value(target, text=None, *, was=None)   types only when `was` (what showed before) is a PENDING word
      .latch(target, prop="bg")                  110 ms colour settle, hot = mix(final, #FFFFFF, .5) -> final
      .power_down() / .cancel_power_down()       the collapse inside the existing 800 ms (destroy stays main_window's)
      .boot_pass() / .finish_boot()              the one-time pass on the first root map
      .level()                                   fx.level(): full | reduced | off (watchdog degrade included)
      .crt_glass(canvas, **spec)                 B11: the CRT glass of a kit canvas (fx.draw.attach_crt), or None
    WaitCursor(motion, target, cell=None)        the real-wait block cursor: start() / stop() / running / cursor
    block_cursor_box(cv, item), show_block_cursor(cv, item, on)   the type-on cursor of canvas text: a drawn box
    is_wait_text(text), is_pending(text), title_per(n), value_per(n), notice_per(n), frames_for(n, per)

Levels (GAP_FX 1.1). FULL: titles, newly arrived values and notices type on behind a block cursor, each finished
within ~140 ms; the rail cell settles; power-down collapse and boot pass. REDUCED: nothing moves and nothing types -
the final text at once, then one 110 ms colour settle (hot to final); the power-down is a static POWERING DOWN cover;
no boot pass. OFF: final states only, no settle; the static cover. The level is fx.level() (prefs.effective_motion -
explicit, else the Windows "Show animations" setting, else full - downgraded to reduced while the clock's watchdog
degrade is active), read when an effect starts; the clock finishes running spatial tweens when it changes.

Type-on frames (lab typeOn / notify): frame f shows min(n, (f + 1) * per) characters plus the cursor; frame 0 is drawn
at once, frame 1 on the first clock tick, the whole text on the last frame. per: titles ceil(n / 9), values
max(3, ceil(n / 6)), notices max(4, ceil(n / 9)). Every type-on is interruptible: any key or mouse press finishes it
(fx.clock runs the input recorder before every other handler). The app always wins: a target the app rewrote
mid-type (a kit target's generation changed, or a plain widget no longer shows the frame this effect last wrote)
is left alone - no further write, including the final one.

Targets. A type-on target is one of:
  * a kit "typeable" (kit.header.Header, kit.flag.StatusFlag, a kit.notice box): fx_type_text() -> the text it
    draws when not typing; fx_type_show(k, cursor) draws its first k characters (+ the cursor; k None = whole);
    fx_type_gen() changes whenever the app writes new content; fx_ink = (getter, setter) of its ink (the REDUCED
    settle);
  * (canvas, item_id) with text;
  * any Tk widget with a -text option, with text (written through Tcl, so no Python configure override runs).
The cursor: canvas text (the Header title, a (canvas, item) target) gets a drawn box (show_block_cursor: one '0'
cell wide, standing on the baseline - the display face has no U+2588 glyph, so Tk would draw a fallback font's
40 px block); the StatusFlag draws the approved 5 x 10 block; Label text (a notice box, a plain Label) prints the
CURSOR glyph in its monospace font, as the lab's notify does.
A latch target is a widget with fx_fill = (getter, setter) (prop "bg": the rail cell's plate), a typeable with
fx_ink (prop "fg"), (canvas, item_id) (fill) or a plain widget (-background / -foreground).

Rules kept (GAP_FX 1.5): no after() here - every timer is fx.clock's (tween / loop / later); every tween is owned
by the widget it writes, so a target destroyed mid-effect is dropped by the clock's liveness check (no step, no
bgerror); nothing touches Tk off the Tk thread; nothing reads or writes disk. Every public method never raises.

WaitCursor is the look of a real wait, so it works with no fx at all (the kill switch: a steady block); only its
blink needs the clock. The blink (530 / 530 ms, only with fx.crt == 2 and motion not off - REDUCED blinks too, as the
lab does, Q-2) is a CRT effect: X-19 says it ships with the CRT control, so B11 turned WAIT_BLINK on together with the
CRT row on Settings 08. A running cursor re-evaluates on every fx.* preference change (fx.crt / fx.motion).

B11: Motion.crt_glass(canvas, **spec) is the kit's CRT seam - a kit canvas whose glass carries the CRT physics (the
LOADING plate, a screen title) asks fx.get(widget).motion for it, the route every kit effect takes; it hands back an
fx.draw.CrtGlass while the FX is live, and NullFX's no-op namespace answers None (the kill switch: no CRT at all).
"""

import math
import sys
import tkinter as tk

from . import clock as _clock_mod
from . import palette as _palette

__all__ = ["Motion", "WaitCursor", "FRAME_MS", "CURSOR", "LATCH_MS", "BLINK_MS", "POWER_MS", "POWER_TOTAL_MS",
           "BOOT_MS", "PENDING_WORDS", "WAIT_TEXTS", "WAIT_BLINK", "CURSOR_TAG", "title_per", "value_per", "notice_per",
           "frames_for", "chars_at", "is_wait_text", "is_pending", "block_cursor_box", "show_block_cursor"]

FRAME_MS = _clock_mod.FRAME_MS        # 15.6
CURSOR = "█"                     # the block cursor
LATCH_MS = 110
BLINK_MS = 530
POWER_MS = (250, 450, 700)            # shutters close / beam shrinks / dot fades; black to the end
POWER_TOTAL_MS = 800                  # main_window._do_shutdown's after(800, destroy) - unchanged (SH-13)
COVER_LIFT_SLOT = "power_cover"       # the power-down's re-lift loop (static cover ADV1-03, FULL collapse ADV2-01)
COVER_LIFT_MS = 15                    # every clock tick (clock.TICK_MS): a late notice shows for at most one frame
BOOT_MS = 200
# the placeholders a newly arrived value replaces (upper-cased, trailing dots dropped): the lab's PENDING / LOADING,
# an empty slot or dots, and the app's own words, kept as they are (D-27): Home's 'checking...' and '-', Settings'
# '(unknown)'
PENDING_WORDS = ("PENDING", "LOADING", "", "CHECKING", "-", "(UNKNOWN)")
WAIT_TEXTS = ("SENDING REQUEST", "WAITING FOR RESPONSE")
WAIT_BLINK = True                     # B11: the CRT blink ships with the CRT row on Settings 08 (X-19)
WHITE = "#FFFFFF"
BLACK = "#000000"
_DOTS = ".…"

_PERS = {}


def title_per(n):
    """Characters per frame of a title type-on: ceil(n / 9) (the lab's titlePer), at least 1."""
    return max(1, int(math.ceil(max(0, int(n)) / 9.0)))


def value_per(n):
    """Characters per frame of a value print-in: max(3, ceil(n / 6)) - six frames at most."""
    return max(3, int(math.ceil(max(0, int(n)) / 6.0)))


def notice_per(n):
    """Characters per frame of a notice print-in: max(4, ceil(n / 9)) (the lab's notify)."""
    return max(4, int(math.ceil(max(0, int(n)) / 9.0)))


_PERS.update(title=title_per, value=value_per, notice=notice_per)


def frames_for(n, per):
    """How many frames a type-on of n characters at `per` per frame shows (the last one is the whole text)."""
    n = max(0, int(n))
    per = max(1, int(per))
    return int(math.ceil(n / float(per))) if n else 0


def chars_at(frame, n, per):
    """Characters shown on frame `frame` (0-based): min(n, (frame + 1) * per)."""
    return min(int(n), (int(frame) + 1) * max(1, int(per)))


def _norm_word(text):
    if text is None:
        return None
    try:
        s = " ".join(str(text).split()).upper()
    except Exception:  # noqa: BLE001
        return None
    return s.rstrip(_DOTS).strip()


def is_wait_text(text):
    """True for a real-wait status text (SENDING REQUEST / WAITING FOR RESPONSE; case, spacing, line breaks and
    trailing dots ignored)."""
    s = _norm_word(text)
    return s is not None and s in WAIT_TEXTS


def is_pending(text):
    """True when `text` is a placeholder a value replaces (PENDING_WORDS: PENDING / LOADING / empty / dots /
    checking... / - / (unknown)). None is not."""
    s = _norm_word(text)
    return s is not None and s in PENDING_WORDS


def _resolve_per(per, n):
    fn = _PERS.get(per) if isinstance(per, str) else per
    if fn is None:
        fn = title_per
    try:
        return max(1, int(fn(n)))
    except Exception:  # noqa: BLE001
        return title_per(n)


def _log(msg):
    try:
        pkg = sys.modules.get(__package__) if __package__ else None
        fn = getattr(pkg, "_log", None) if pkg is not None else None
        if callable(fn):
            fn(msg)
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        sys.stderr.write(str(msg).rstrip("\n") + "\n")
    except Exception:  # noqa: BLE001
        pass


def _fx_of(widget):
    """fx.get(widget) without importing the package at module load (fx/__init__ imports this module)."""
    try:
        pkg = sys.modules.get(__package__) if __package__ else None
        if pkg is None or widget is None:
            return None
        return pkg.get(widget)
    except Exception:  # noqa: BLE001
        return None


def _alive(w):
    try:
        return w is not None and bool(w.winfo_exists())
    except Exception:  # noqa: BLE001
        return False


def _to_hex(widget, colour):
    """'#RRGGBB' for any colour Tk knows (names included), or None."""
    if not isinstance(colour, str) or not colour:
        return None
    s = colour.strip()
    if len(s) == 7 and s[0] == "#":
        try:
            int(s[1:], 16)
            return s.upper()
        except ValueError:
            pass
    try:
        r, g, b = widget.winfo_rgb(s)
        return "#%02X%02X%02X" % (r >> 8, g >> 8, b >> 8)
    except Exception:  # noqa: BLE001
        return None


def _call(fn):
    if callable(fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            _log("fx.motion: done callback raised %r" % (e,))


# ---------------------------------------------------------------------------------------------- the block cursor
CURSOR_TAG = "fx_type_cursor"


def block_cursor_box(cv, item):
    """(x0, y0, x1, y1) of the type-on block cursor right after canvas text `item` as it is drawn now, or None.
    One '0' cell of the item's font wide, 0.83 x its ascent tall, standing on the baseline, a third of a cell after
    the text: the approved r2_motion_type_on cursor (12 x 24 px, 4 px after the 22 pt title at 100 %). A drawn box,
    not the U+2588 glyph: a display face without that glyph (Bahnschrift) gets a fallback font's, about 40 px wide
    there. Canvas text bboxes carry 1 px of padding left and right, none above or below (measured)."""
    try:
        font = cv.itemcget(item, "font")
        asc = int(cv.tk.call("font", "metrics", font, "-ascent"))
        cell = max(2, int(cv.tk.call("font", "measure", font, "0")))
        bb = cv.bbox(item)
        if not bb:
            return None
        right = bb[2] - 1 if cv.itemcget(item, "text") else bb[0] + 1
        x0 = right + max(2, int(round(cell / 3.0)))
        base = bb[1] + asc
        return (x0, base - max(2, int(round(asc * 0.83))), x0 + cell, base)
    except (tk.TclError, ValueError, TypeError):
        return None


def show_block_cursor(cv, item, on, fill=None):
    """Draw (on) or remove the block cursor of canvas text `item` (one rectangle, tags CURSOR_TAG and
    'fx_tc_<item>', in the text's fill unless `fill` is given). Never raises Tk errors."""
    tag = "fx_tc_%s" % (item,)
    try:
        box = block_cursor_box(cv, item) if on else None
        if box is None:
            cv.delete(tag)
            return None
        ink = fill or cv.itemcget(item, "fill") or WHITE
        found = cv.find_withtag(tag)
        if found:
            cv.coords(found[0], *box)
            cv.itemconfigure(found[0], fill=ink)
        else:
            cv.create_rectangle(*box, fill=ink, outline="", width=0, tags=(CURSOR_TAG, tag))
    except tk.TclError:
        pass
    return None


# ---------------------------------------------------------------------------------------------- targets
class _Typeable(object):
    """One protocol over the three kinds of type-on target (see the module docstring)."""

    __slots__ = ("owner", "text", "show", "gen", "ink", "abandon")


def _typeable(target, text=None):
    a = _Typeable()
    show = getattr(target, "fx_type_show", None)
    if callable(show) and callable(getattr(target, "fx_type_text", None)):
        a.owner = target
        a.text = target.fx_type_text
        a.show = show
        gen = getattr(target, "fx_type_gen", None)
        a.gen = gen if callable(gen) else (lambda: 0)
        a.ink = getattr(target, "fx_ink", None)
        a.abandon = None             # its own setter clears a typed frame
        return a
    if text is None:
        return None
    full = str(text)
    if isinstance(target, tuple) and len(target) == 2:
        cv, item = target

        def draw(k, cursor=False, cv=cv, item=item):
            whole = k is None or k >= len(full)
            cv.itemconfigure(item, text=full if whole else full[:max(0, k)])
            show_block_cursor(cv, item, bool(cursor) and not whole)

        a.owner = cv
        a.text = lambda: full
        a.show = draw
        a.gen = lambda cv=cv, item=item: cv.itemcget(item, "text")
        a.ink = (lambda: cv.itemcget(item, "fill"), lambda c: cv.itemconfigure(item, fill=c))
        a.abandon = lambda cv=cv, item=item: show_block_cursor(cv, item, False)    # the app won: no stale box
        return a
    if isinstance(target, tk.Misc):
        w = target

        def draw(k, cursor=False):
            s = full if k is None or k >= len(full) else full[:max(0, k)] + (CURSOR if cursor else "")
            w.tk.call(w._w, "configure", "-text", s)

        a.owner = w
        a.text = lambda: full
        a.show = draw
        a.gen = lambda: str(w.tk.call(w._w, "cget", "-text"))
        a.ink = None
        a.abandon = None             # the app's own text replaced the glyph
        return a
    return None


def _fill_access(target, prop):
    """(owner, getter, setter) for a latch of `prop` on target, or None."""
    if target is None:
        return None
    if prop == "bg":
        pair = getattr(target, "fx_fill", None)
        if isinstance(pair, tuple) and len(pair) == 2:
            return (target if isinstance(target, tk.Misc) else None), pair[0], pair[1]
    if prop == "fg":
        pair = getattr(target, "fx_ink", None)
        if isinstance(pair, tuple) and len(pair) == 2:
            owner = target if isinstance(target, tk.Misc) else getattr(target, "owner", None)
            return owner, pair[0], pair[1]
    if isinstance(target, tuple) and len(target) == 2:
        cv, item = target
        return cv, (lambda: cv.itemcget(item, "fill")), (lambda c: cv.itemconfigure(item, fill=c))
    if isinstance(target, tk.Misc):
        w = target
        opt = "-background" if prop == "bg" else "-foreground"
        return w, (lambda: str(w.tk.call(w._w, "cget", opt))), (lambda c: w.tk.call(w._w, "configure", opt, c))
    return None


# ---------------------------------------------------------------------------------------------- Motion
class Motion(object):
    """fx.motion. See the module docstring. Built by fx.install(); reads fx.clock / fx.palette at call time."""

    def __init__(self, fx):
        self.fx = fx
        self._booted = False
        self._boot = None            # (head, glow) while the boot pass runs
        self._power = None           # the power-down's widgets while it shows
        self._logged = set()
        self.counts = {"type_on": 0, "latch": 0, "power_down": 0, "boot_pass": 0, "app_won": 0}

    def __repr__(self):
        return "<fx.Motion level=%s>" % (self.level(),)

    # --- helpers ---------------------------------------------------------------------------------------
    def _log_once(self, key, msg):
        if key in self._logged or len(self._logged) > 256:
            return
        self._logged.add(key)
        _log(msg)

    def _clock(self):
        clk = getattr(self.fx, "clock", None)
        if clk is None or getattr(clk, "stopped", False):
            return None
        return clk

    def _tones(self):
        pal = getattr(self.fx, "palette", None)
        if not isinstance(pal, dict) or "bright" not in pal:
            pal = _palette.current()
        return pal

    def _px(self, n):
        try:
            return max(1, int(self.fx.px(n)))
        except Exception:  # noqa: BLE001
            return max(1, int(n))

    def level(self):
        try:
            lv = self.fx.level()
        except Exception:  # noqa: BLE001
            return "off"
        return lv if lv in ("full", "reduced", "off") else "off"

    def _root(self):
        return getattr(self.fx, "root", None)

    def _screen(self, name):
        root = self._root()
        screens = getattr(root, "_screens", None)
        if isinstance(screens, dict):
            return screens.get(name)
        return None

    def _rail_cell(self):
        fn = getattr(self._root(), "fx_active_rail_cell", None)
        if not callable(fn):
            return None
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return None

    # --- the emit hook (FX.emit, before sound.emit) --------------------------------------------------------
    def on_emit(self, widget, event, data):
        try:
            d = data if isinstance(data, dict) else {}
            if event == "nav.route":
                self.finish_boot()
                if d.get("cause") == "user" and d.get("kind") != "initial":
                    screen = self._screen(d.get("name"))
                    if screen is not None:
                        self.type_title(screen)
                    self.latch(self._rail_cell(), "bg")
            elif event == "load.end":
                self.latch(self._rail_cell(), "bg")
            elif event == "app.shutdown":
                self.power_down()
        except Exception as e:  # noqa: BLE001 - motion never breaks an emit
            self._log_once(("on_emit", event, type(e).__name__), "fx.motion: on_emit(%s) raised %r" % (event, e))
        return None

    # --- text ------------------------------------------------------------------------------------------------
    def type_on(self, target, text=None, *, per=None, slot="type", cursor=True, done=None):
        """Type `target` on (see the module docstring). -> the clock entry while it runs, else None."""
        try:
            a = _typeable(target, text)
            if a is None:
                return None
            full = a.text()
            full = "" if full is None else str(full)
            n = len(full)
            lv = self.level()
            clk = self._clock()
            p = _resolve_per(per, n)
            frames = frames_for(n, p)
            if lv != "full" or clk is None or frames <= 1 or not _alive(a.owner):
                if _alive(a.owner):
                    a.show(None, False)
                    if lv == "reduced" and n:
                        self.latch(target, "fg")
                _call(done)
                return None
            span = frames - 1
            st = {"gen": a.gen(), "dead": False}

            def step(q):
                if st["dead"]:
                    return
                if a.gen() != st["gen"]:            # the app wrote mid-type: the app wins
                    st["dead"] = True
                    self.counts["app_won"] += 1
                    if a.abandon is not None:
                        a.abandon()
                    return
                if q >= 1.0:
                    k = None
                else:
                    f = min(frames - 1, 1 + int(q * span))
                    k = chars_at(f, n, p)
                    if k >= n:
                        k = None
                a.show(k, bool(cursor) and k is not None)
                st["gen"] = a.gen()

            e = clk.tween(a.owner, slot, span * FRAME_MS, step, done, kind="spatial", interruptible=True)
            if e is None:
                if _alive(a.owner):
                    a.show(None, False)
                _call(done)
                return None
            if getattr(e, "dead", False):
                return None                           # finished at once (the level changed under us)
            a.show(chars_at(0, n, p), bool(cursor))
            st["gen"] = a.gen()
            self.counts["type_on"] += 1
            return e
        except Exception as ex:  # noqa: BLE001
            self._log_once(("type_on", type(ex).__name__), "fx.motion: type_on raised %r - text shown whole" % (ex,))
            try:
                a = _typeable(target, text)
                if a is not None:
                    a.show(None, False)
            except Exception:  # noqa: BLE001
                pass
            return None

    def type_title(self, screen):
        """Type the screen's title on (screen.fx_title; absent -> nothing). Titles use per = ceil(n / 9)."""
        try:
            t = getattr(screen, "fx_title", None)
            if t is None:
                return None
            if callable(getattr(t, "fx_type_show", None)):
                return self.type_on(t, per="title", slot="title")
            if isinstance(t, tuple) and len(t) == 2:
                return self.type_on(t, t[0].itemcget(t[1], "text"), per="title", slot="title")
            if isinstance(t, tk.Misc):
                return self.type_on(t, str(t.tk.call(t._w, "cget", "-text")), per="title", slot="title")
        except Exception as e:  # noqa: BLE001
            self._log_once(("type_title", type(e).__name__), "fx.motion: type_title raised %r" % (e,))
        return None

    def print_value(self, target, text=None, *, was=None):
        """A newly arrived value prints into its slot - only when what showed before was a placeholder
        (PENDING_WORDS): first arrival, never a revisit. A kit target has drawn the value already and passes `was`
        (its previous text); a plain target is written here (`was` defaults to what it shows now)."""
        try:
            a = _typeable(target, text)
            if a is None:
                return None
            kit = callable(getattr(target, "fx_type_show", None))
            if was is None and not kit:
                was = a.gen() if not (isinstance(target, tuple)) else target[0].itemcget(target[1], "text")
            if not is_pending(was):
                if not kit:
                    a.show(None, False)
                return None
            return self.type_on(target, text, per="value", slot="value")
        except Exception as e:  # noqa: BLE001
            self._log_once(("print_value", type(e).__name__), "fx.motion: print_value raised %r" % (e,))
            return None

    # --- colour ----------------------------------------------------------------------------------------------
    def latch(self, target, prop="bg"):
        """The 110 ms settle: `prop` goes from hot = mix(final, #FFFFFF, .5) to its final colour, linearly. The
        final colour is read now (A5). Runs at FULL and REDUCED (a colour tween), nothing at OFF. Stops without
        writing once the app changed the colour itself. -> the clock entry, or None."""
        try:
            if target is None or self.level() == "off":
                return None
            clk = self._clock()
            if clk is None:
                return None
            acc = _fill_access(target, prop)
            if acc is None:
                return None
            owner, get, put = acc
            if owner is None or not _alive(owner):
                return None
            # ADV1-01: settle a latch already running on this slot FIRST, so the final colour read next is the real
            # one and not that latch's mid-mix (overlapping settles otherwise ratchet the colour towards white)
            clk.finish(owner, ("latch", prop))
            to = get()
            to = "" if to is None else str(to)
            to_hex = _to_hex(owner, to)
            if to_hex is None:
                return None
            hot = _palette.mix(to_hex, WHITE, 0.5)
            st = {"last": str(get()), "dead": False}

            def step(q):
                if st["dead"]:
                    return
                if str(get()) != st["last"]:               # the app repainted: it wins
                    st["dead"] = True
                    return
                put(to if q >= 1.0 else _palette.mix(hot, to_hex, q))
                st["last"] = str(get())

            e = clk.tween(owner, ("latch", prop), LATCH_MS, step, kind="colour")
            if e is None or getattr(e, "dead", False):
                return None
            put(hot)
            st["last"] = str(get())
            self.counts["latch"] += 1
            return e
        except Exception as ex:  # noqa: BLE001
            self._log_once(("latch", type(ex).__name__), "fx.motion: latch raised %r" % (ex,))
            return None

    # --- whole window ----------------------------------------------------------------------------------------
    def power_down(self):
        """The power-down inside the existing 800 ms (main_window keeps after(800, destroy)). FULL: two black
        shutters close (in_quad, 250 ms), a 2 px white beam with a bloom_in glow shrinks to a 6 x 6 dot (200 ms), the
        dot fades to black in five 50 ms steps, then black. Otherwise (REDUCED, OFF, the watchdog degrade): a static
        cover with POWERING DOWN. Both swallow every key and click (clock.block_input) and stay above everything,
        notices and an open confirm included. Once per window."""
        try:
            root = self._root()
            if root is None or not _alive(root):
                return None
            if self._power is not None and any(_alive(w) for w in self._power):
                return None
            clk = self._clock()
            t = self._tones()
            if clk is not None:
                clk.block_input(True)
            if self.level() != "full" or clk is None:
                self._power = self._static_cover(root, t)
                self.counts["power_down"] += 1
                return None
            W = max(1, int(root.winfo_width()))
            H = max(2, int(root.winfo_height()))
            top = tk.Frame(root, bg=BLACK, bd=0, highlightthickness=0, cursor="")
            bot = tk.Frame(root, bg=BLACK, bd=0, highlightthickness=0, cursor="")
            cv = tk.Canvas(root, bg=BLACK, bd=0, highlightthickness=0, cursor="")
            self._power = (top, bot, cv)

            def gone(_e=None, parts=(bot, cv)):
                for w in parts:
                    try:
                        w.destroy()
                    except tk.TclError:
                        pass
            top.bind("<Destroy>", gone, add="+")
            beam_h, glow_px, dot = self._px(2), self._px(2), self._px(6)
            half = max(0, H // 2 - 1)
            items = {}

            def shutters(h):
                for w, kw in ((top, dict(x=0, y=0, relwidth=1.0)), (bot, dict(x=0, rely=1.0, anchor="sw", relwidth=1.0))):
                    if h < 1:
                        w.place_forget()
                    else:
                        w.place(height=h, **kw)
                        w.lift()

            def canvas_on():
                if "beam" not in items:
                    cv.place(x=0, y=0, relwidth=1.0, relheight=1.0)
                    items["glow"] = cv.create_rectangle(0, 0, 0, 0, fill=t.get("bloom_in", WHITE), outline="")
                    items["beam"] = cv.create_rectangle(0, 0, 0, 0, fill=WHITE, outline="")
                    top.place_forget()
                    bot.place_forget()
                tk.Misc.tkraise(cv)              # Canvas.lift is the item raise: the widget's is Misc's

            def step(q):
                ms = q * POWER_MS[-1]
                cx, cy = W / 2.0, H / 2.0
                if ms < POWER_MS[0]:
                    k = ms / float(POWER_MS[0])
                    shutters(int(round(k * k * half)))
                    return
                canvas_on()
                if ms < POWER_MS[1]:
                    k = (ms - POWER_MS[0]) / float(POWER_MS[1] - POWER_MS[0])
                    bw = max(dot, W * (1.0 - k))
                    cv.coords(items["beam"], cx - bw / 2.0, cy - beam_h / 2.0, cx + bw / 2.0, cy + beam_h / 2.0)
                    cv.coords(items["glow"], cx - bw / 2.0 - glow_px, cy - beam_h / 2.0 - glow_px,
                              cx + bw / 2.0 + glow_px, cy + beam_h / 2.0 + glow_px)
                    cv.itemconfigure(items["glow"], state="normal")
                    cv.itemconfigure(items["beam"], state="normal", fill=WHITE)
                    return
                cv.itemconfigure(items["glow"], state="hidden")
                if ms < POWER_MS[2] and q < 1.0:
                    s = min(5, int((ms - POWER_MS[1]) // 50))
                    cv.coords(items["beam"], cx - dot / 2.0, cy - dot / 2.0, cx + dot / 2.0, cy + dot / 2.0)
                    cv.itemconfigure(items["beam"], state="normal", fill=_palette.mix(WHITE, BLACK, s / 5.0))
                    return
                cv.itemconfigure(items["beam"], state="hidden")      # 700 ms on: black

            e = clk.tween(top, "power", POWER_MS[-1], step, kind="spatial")
            if e is None:
                gone()
                try:
                    top.destroy()
                except tk.TclError:
                    pass
                self._power = self._static_cover(root, t)
            else:
                # ADV2-01: a notice / confirm raised after the collapse ended (or after a watchdog / level change
                # finished it) must not stack above the black until the destroy: re-lift the power-down's widget on
                # every tick, as the static cover does (owned by `top`: dies with the window; cancel_power_down
                # cancels it on every part)
                def lift(_now):
                    if cv.winfo_manager():
                        tk.Misc.tkraise(cv)
                    else:
                        for w in (top, bot):
                            if w.winfo_manager():
                                tk.Misc.tkraise(w)
                clk.loop(top, COVER_LIFT_SLOT, COVER_LIFT_MS, lift)
            self.counts["power_down"] += 1
            return None
        except Exception as ex:  # noqa: BLE001
            self._log_once(("power_down", type(ex).__name__), "fx.motion: power_down raised %r" % (ex,))
            return None

    def _static_cover(self, root, t):
        cover = tk.Frame(root, bg=t.get("bg", BLACK), bd=0, highlightthickness=0, cursor="")
        tk.Label(cover, text="POWERING DOWN", bg=t.get("bg", BLACK), fg=t.get("value", WHITE), font=("Consolas", 12),
                 bd=0, padx=0, pady=0).place(relx=0.5, rely=0.5, anchor="center")
        cover.place(x=0, y=0, relwidth=1.0, relheight=1.0)
        cover.lift()
        # ADV1-03: a notice raised later (a toast while the backend stops) must not stack above the cover: re-lift it
        # on every tick while it shows (a loop runs at every level; it moves nothing; it dies with the cover)
        clk = self._clock()
        if clk is not None:
            clk.loop(cover, COVER_LIFT_SLOT, COVER_LIFT_MS, lambda _now, w=cover: tk.Misc.tkraise(w))
        return (cover,)

    def cancel_power_down(self):
        """Take a power-down off again (previews, the harness): its widgets go and input is let through."""
        parts, self._power = self._power, None
        clk = self._clock()
        for w in parts or ():
            if clk is not None:
                try:
                    clk.cancel(w, COVER_LIFT_SLOT)
                except Exception:  # noqa: BLE001
                    pass
            try:
                w.destroy()
            except tk.TclError:
                pass
        clk = self._clock()
        if clk is not None:
            try:
                clk.block_input(False)
            except Exception:  # noqa: BLE001
                pass
        return None

    @property
    def powering_down(self):
        return self._power is not None and any(_alive(w) for w in self._power)

    def boot_pass(self):
        """The one-time power-on pass (first root map, FULL only): a 2 px bright head with a 1 px bloom_in
        afterglow above it, top to bottom over the whole window in 200 ms. Only its 3 px rows take clicks; any
        key, click or navigation finishes it."""
        try:
            if self._booted:
                return None
            self._booted = True
            clk = self._clock()
            root = self._root()
            if clk is None or root is None or self.level() != "full" or not _alive(root):
                return None
            t = self._tones()
            h2, h1 = self._px(2), self._px(1)
            head = tk.Frame(root, bg=t.get("bright", WHITE), bd=0, highlightthickness=0)
            glow = tk.Frame(root, bg=t.get("bloom_in", t.get("bright", WHITE)), bd=0, highlightthickness=0)

            def cleanup():
                if self._boot is not None and self._boot[0] is head:
                    self._boot = None        # only its own pass (a preview run over a running one keeps its own)
                for w in (glow, head):
                    try:
                        w.destroy()
                    except tk.TclError:
                        pass
            head.bind("<Destroy>", lambda _e: (glow.destroy() if _alive(glow) else None), add="+")

            def place(q):
                try:
                    H = int(root.winfo_height())
                except tk.TclError:
                    return
                y = int(round(max(0.0, min(1.0, q)) * max(0, H - h2)))
                glow.place(x=0, y=max(0, y - h1), relwidth=1.0, height=h1)
                head.place(x=0, y=y, relwidth=1.0, height=h2)
                glow.lift()
                head.lift()

            def step(q):
                if q >= 1.0:
                    cleanup()
                    return
                place(q)

            e = clk.tween(head, "boot", BOOT_MS, step, cleanup, kind="spatial", interruptible=True)
            if e is None or getattr(e, "dead", False):
                cleanup()
                return None
            place(0.0)
            self._boot = (head, glow)
            self.counts["boot_pass"] += 1
            return e
        except Exception as ex:  # noqa: BLE001
            self._log_once(("boot_pass", type(ex).__name__), "fx.motion: boot_pass raised %r" % (ex,))
            return None

    def finish_boot(self):
        b = self._boot
        clk = self._clock()
        if b is None or clk is None:
            return None
        try:
            clk.finish(b[0], "boot")
        except Exception:  # noqa: BLE001
            pass
        return None

    # --- B11: the kit's CRT seam ------------------------------------------------------------------------------
    def crt_glass(self, canvas, **spec):
        """The CRT glass of a kit canvas (fx.draw.CrtGlass: the LOADING plate's bloom + scanlines, a screen title's
        scanlines), made through THIS fx and painted once now; None when the fx is not live. The kit reaches every
        effect through fx.get(widget).motion, so under NullFX this call is the no-op namespace's and answers None.
        spec: words= / plate= / frames= / ground= (see CrtGlass). Never raises."""
        try:
            from . import draw as _draw
            return _draw.attach_crt(canvas, fx=self.fx, **spec)
        except Exception as e:  # noqa: BLE001 - an effect never breaks the kit widget asking for it
            self._log_once(("crt_glass", type(e).__name__), "fx.motion: crt_glass raised %r - no CRT there" % (e,))
            return None


# ---------------------------------------------------------------------------------------------- WaitCursor
class WaitCursor(object):
    """WaitCursor(motion, target, cell=None): the block cursor of a real wait (GAP_FX 1.3).

    target:
      * a Label-like widget (a kit StatusLine, a plain Label): a separate cell (a tk.Frame, 5 x 10 approved px, in
        the label's ink) PLACED over the label, CURSOR_GAP px after the drawn text - the label's text is never
        modified, so cget("text") stays the real status. A kit ChatStatusLine keeps the cursor's room free at its
        right end while it shows (set_reserve). It follows the label's re-renders (<Configure>).
      * (canvas, text_item): a rectangle item after the text's bbox.
      * cell=: an existing widget the owner lays out itself (kit.EmptyState's mark): start() / stop() only run its
        blink.
    The cursor is state, not motion: it shows at every level and under the kill switch (no fx: a steady block, no
    timer). It blinks (fx.clock loop, 530 / 530 ms) only while WAIT_BLINK and fx.crt == 2 and motion is not off,
    re-evaluated on fx.* preference changes (B11). start() while running re-places it (the text changed); stop()
    takes it away the instant the wait ends. Never raises Tk errors."""

    CURSOR_W, CURSOR_H, CURSOR_GAP = 5, 10, 6     # approved block_cursor: 5 x 10, 6 px after the text

    def __init__(self, motion=None, target=None, cell=None):
        self.motion = motion
        self.target = target
        self._adopted = cell is not None
        self._cell = cell
        self._item = None
        self._running = False
        self._bound = False
        self._blinking = False
        self._lit = True
        self._ink = None
        self._unsub = None

    def __repr__(self):
        return "<fx.motion.WaitCursor %s>" % ("running" if self._running else "stopped")

    @property
    def running(self):
        return self._running

    @property
    def cursor(self):
        """The cursor cell (a tk.Frame), or None before the first start() (canvas targets: None)."""
        return self._cell

    @property
    def blinking(self):
        return self._blinking

    def _widget(self):
        if isinstance(self.target, tuple) and len(self.target) == 2:
            return self.target[0]
        return self.target if self.target is not None else self._cell

    # --- start / stop --------------------------------------------------------------------------------------
    def start(self):
        self._running = True
        try:
            if self._adopted:
                pass
            elif isinstance(self.target, tuple) and len(self.target) == 2:
                self._show_item()
            else:
                self._reserve(True)
                self._show_cell()
        except tk.TclError:
            return None
        self._sync_blink()
        return None

    def stop(self):
        was = self._running
        self._running = False
        self._sync_blink()
        try:
            if self._adopted:
                pass
            elif isinstance(self.target, tuple) and len(self.target) == 2:
                self._hide_item()
            elif was or self._cell is not None:
                self._hide_cell()
        except tk.TclError:
            pass
        return None

    def refresh(self):
        """Re-place a running cursor (its label re-rendered)."""
        if self._running and not self._adopted:
            try:
                if isinstance(self.target, tuple):
                    self._show_item()
                elif self._cell is not None:
                    self._place_cell()
            except tk.TclError:
                pass

    def destroy(self):
        """stop(), then destroy a cell this cursor made itself (an adopted one is its owner's)."""
        self.stop()
        if not self._adopted and self._cell is not None:
            try:
                self._cell.destroy()
            except tk.TclError:
                pass
            self._cell = None

    # --- Label targets (the B7b SignalAcquiring geometry, moved here) -----------------------------------------
    def _ratio(self):
        try:
            return max(1.0, float(self.target.tk.call("tk", "scaling")) / (96.0 / 72.0))
        except (tk.TclError, ValueError, AttributeError):
            return 1.0

    def _reserve(self, on):
        """A label that can keep room free at its right end (kit.composer.ChatStatusLine's set_reserve) elides its
        text CURSOR_GAP + CURSOR_W px earlier while the cursor stands (B7b ADV1-01). A plain Label: nothing."""
        set_reserve = getattr(self.target, "set_reserve", None)
        if set_reserve is None:
            return
        r = self._ratio()
        try:
            set_reserve(int(round(self.CURSOR_GAP * r)) + int(round(self.CURSOR_W * r)) if on else 0)
        except tk.TclError:
            pass

    def _show_cell(self):
        lab = self.target
        if self._cell is None or not self._cell.winfo_exists():
            r = self._ratio()
            self._cell = tk.Frame(lab.master, width=int(round(self.CURSOR_W * r)),
                                  height=int(round(self.CURSOR_H * r)), bd=0, highlightthickness=0)
            if not self._bound:
                # a resize re-elides the status line: the cursor follows its new end
                lab.bind("<Configure>", self._on_label_configure, add="+")
                self._bound = True
        self._ink = lab.cget("fg")
        self._cell.configure(bg=self._ink)
        self._lit = True
        self._place_cell()

    def _place_cell(self):
        lab, cell = self.target, self._cell
        shown = getattr(lab, "shown", None)          # kit StatusLine: the one displayed line
        n_lines = 1
        if not isinstance(shown, str):
            all_lines = str(lab.cget("text")).split("\n")
            shown, n_lines = all_lines[-1], len(all_lines)
        font = lab.cget("font")
        try:
            text_w = int(lab.tk.call("font", "measure", font, shown))
            inset = (int(float(lab.cget("bd"))) + int(float(lab.cget("highlightthickness")))
                     + int(float(lab.cget("padx"))))
            # B7b fix ADV1-12: a plain Label centres its lines as one block; the cursor goes to the LAST line's middle
            dy = (n_lines - 1) * int(lab.tk.call("font", "metrics", font, "-linespace")) // 2 if n_lines > 1 else 0
        except (tk.TclError, ValueError):
            text_w, inset, dy = 0, 0, 0
        r = self._ratio()
        w = int(round(self.CURSOR_W * r))
        x = inset + text_w + int(round(self.CURSOR_GAP * r))
        width = lab.winfo_width()
        if width > 1:
            # only a guard: a ChatStatusLine keeps the cursor's room free (ADV1-01, _reserve); a plain Label too
            # narrow for its text still keeps the cursor inside it
            x = min(x, max(0, width - inset - w))
        cell.place(in_=lab, x=x, y=dy, rely=0.5, anchor="w")
        cell.lift(lab)

    def _hide_cell(self):
        self._reserve(False)
        cell = self._cell
        if cell is not None:
            try:
                cell.place_forget()
            except tk.TclError:
                pass

    def _on_label_configure(self, _e=None):
        if self._running and self._cell is not None:
            try:
                self._place_cell()
            except tk.TclError:
                pass

    # --- canvas targets -----------------------------------------------------------------------------------
    def _show_item(self):
        cv, text_item = self.target
        bb = cv.bbox(text_item)
        if not bb:
            return
        r = self._ratio() if hasattr(cv, "tk") else 1.0
        w, h, gap = (int(round(v * r)) for v in (self.CURSOR_W, self.CURSOR_H, self.CURSOR_GAP))
        x0 = bb[2] + gap
        cy = (bb[1] + bb[3]) / 2.0
        ink = cv.itemcget(text_item, "fill") or "#FFFFFF"
        self._ink = ink
        if self._item is None or not cv.find_withtag(self._item):
            self._item = cv.create_rectangle(0, 0, 0, 0, fill=ink, outline="", tags=("fx_wait_cursor",))
        cv.coords(self._item, x0, int(round(cy - h / 2.0)), x0 + w, int(round(cy - h / 2.0)) + h)
        cv.itemconfigure(self._item, fill=ink, state="normal")

    def _hide_item(self):
        cv = self.target[0]
        if self._item is not None:
            try:
                cv.delete(self._item)
            except tk.TclError:
                pass
            self._item = None

    # --- blink (B11: WAIT_BLINK on; fx.crt == 2 and motion not off; Q-2: REDUCED blinks as the lab does) --------
    def _fx(self):
        m = self.motion
        fx = getattr(m, "fx", None) if isinstance(m, Motion) else None
        return fx if fx is not None else _fx_of(self._widget())

    def _blink_owner(self):
        if isinstance(self.target, tuple):
            return self.target[0]
        return self._cell

    def _blink_wanted(self):
        if not (WAIT_BLINK and self._running):
            return False
        fx = self._fx()
        if fx is None or not getattr(fx, "enabled", False):
            return False
        try:
            crt = fx.prefs.get("fx.crt") if fx.prefs is not None else None
        except Exception:  # noqa: BLE001
            crt = None
        try:
            lv = fx.level()
        except Exception:  # noqa: BLE001
            lv = "off"
        return crt == 2 and not isinstance(crt, bool) and lv != "off"

    def _paint(self, lit):
        self._lit = lit
        owner = self._blink_owner()
        if owner is None:
            return
        if isinstance(self.target, tuple):
            cv = self.target[0]
            if self._item is not None:
                cv.itemconfigure(self._item, fill=self._ink if lit else cv.cget("bg"))
            return
        if self._ink is None:
            self._ink = owner.cget("bg")
        try:
            ground = owner.master.cget("bg")
        except (tk.TclError, AttributeError):
            ground = self._ink
        owner.configure(bg=self._ink if lit else ground)

    def _toggle(self, _now=None):
        try:
            self._paint(not self._lit)
        except tk.TclError:
            pass

    def _sync_blink(self):
        if not WAIT_BLINK and not self._blinking and self._unsub is None:
            return                                   # the blink switched off (WAIT_BLINK False): no work at all
        try:
            want = self._blink_wanted()
            fx = self._fx()
            clk = getattr(fx, "clock", None) if fx is not None else None
            owner = self._blink_owner()
            if want and not self._blinking and clk is not None and owner is not None:
                if self._adopted and self._ink is None:
                    self._ink = owner.cget("bg")
                if clk.loop(owner, "blink", BLINK_MS, self._toggle) is not None:
                    self._blinking = True
            elif not want and self._blinking:
                self._blinking = False
                if clk is not None and owner is not None:
                    clk.cancel(owner, "blink")
                if owner is not None and _alive(owner):
                    self._paint(True)
            # re-evaluate on fx.* preference changes (fx.crt / fx.motion) while running
            if WAIT_BLINK and self._running and self._unsub is None and fx is not None and owner is not None:
                prefs = getattr(fx, "prefs", None)
                if prefs is not None:
                    self._unsub = prefs.subscribe("fx", lambda _p, _v: self._sync_blink(), owner=owner)
            elif not self._running and self._unsub is not None:
                unsub, self._unsub = self._unsub, None
                unsub()
        except Exception as e:  # noqa: BLE001 - a blink problem never stops the wait cursor
            _log("fx.motion: WaitCursor blink sync raised %r" % (e,))
