"""fx/page08/signal_output.py - Settings 08 SIGNAL OUTPUT (B13; BUILD_PLAN §4 B13, GAP_FX 4.1, spec ST-03; approved
r2_settings_sound): the master switch, the engine's status, the volume and the five category rows with TEST.

    SIGNAL OUTPUT
    [x] Sound cues
        NO AUDIO DEVICE - CUES OFF                        (the status line: only while the output is not ready)
    Volume                                                                  40%
    -------------------------[]----------------------------------------------
    Every cue is synthesised at start-up - nothing is recorded or downloaded. Volume scales the cues themselves;
    Windows' own volume still applies on top. Alt+M mutes or unmutes from any screen.
       [x] NAVIGATION        TEST
       [x] INTERFACE         TEST      (TEST shows on the row under the pointer or holding the keyboard focus)
       [x] TRANSMISSIONS
       [x] SYSTEM
       [x] ALERTS

The categories sit in this section, as the approved frame draws them (GAP_FX 4.1, spec 08.1); the policy rows are
fx/page08/cue_policy.py. The caption follows D-12 (no "on this machine"), with the volume and Alt+M sentences.

* A lamp writes prefs FIRST, then acknowledges: master OFF -> ON auditions load_online, a category OFF -> ON its own
  cue (master on); every ON -> OFF is silent (ST-03). The engine follows the store synchronously (bind_store), so the
  audition already plays at the new settings.
* Volume: the readout follows the drag (the slider's own variable); nothing is written while it moves. A release of
  ANY mouse button (ttk jumps the thumb on buttons 2 / 3 too) or of a slider key schedules ONE commit 180 ms later
  (fx.clock.later, flush-marked, owner the slider): a burst of releases is one commit. The arrow keys are seen on a
  bindtag of the section's own placed before the slider's (the kit slider's arrow bindings end in 'break'). The slider
  losing the focus while it shows an uncommitted value commits it. The commit writes fx.sound.volume (snapped to 5)
  only when it changed, then auditions affirm at the committed gain - never from a closed or minimised window. TEST,
  master ON, leaving the page and the page going away flush a pending commit first (without its audition).
* TEST (a category's representative cue, engine.test) is enabled only while it can play: a running engine that is ready
  or starting, master on, volume above 0. Otherwise the TEST words are gone and the status line says why; master off
  also disables the five lamps (their values kept). A TEST click inverts the word for min(cue duration, 700) ms (one
  fx.clock.later owned by the word); a click while it is inverted is ignored (no restart, no stutter).
* The status line has ONE writer (_sync_status): render_status() of the engine's state and the master / volume. It is
  re-read every 500 ms while page 08 is shown (fx.clock.loop owned by the line; stopped on hide), and on every prefs
  change. The window closed with X (withdrawn) or minimised parks the poll; a Tcl-filtered <Map> line on the toplevel
  (installed at the first park, removed by release()) re-arms it when the window comes back.
* The rows follow the store (page.subscribe("fx.sound")): Alt+M, RESTORE DEFAULTS, a backup restore. An outside volume
  change drops a pending commit (the store wins); any other change leaves a slider being moved alone.
* Read-only page (kill switch): the same rows disabled with the stored values, no TEST, no status line, and the
  caption without the Alt+M sentence (Alt+M is unbound there).
* RESTORE DEFAULTS resets the rows this section shows: master, volume and the five categories.
No after() here: every timer is fx.clock's. Tk thread only.
"""

import tkinter as tk

from ..sound_motion_page import Section, register_section
from . import _sound_common as sc

TITLE = "Signal output"
MASTER = "Sound cues"
VOLUME = "Volume"
CAPTION_BASE = ("Every cue is synthesised at start-up - nothing is recorded or downloaded. Volume scales the cues "
                "themselves; Windows' own volume still applies on top.")
ALT_M_SENTENCE = "Alt+M mutes or unmutes from any screen."
CAPTION = CAPTION_BASE + " " + ALT_M_SENTENCE      # the live page; the read-only page (Alt+M unbound) omits Alt+M
TEST = "TEST"
TEST_INVERT_MAX_MS = 700    # TEST shows inverted while its cue plays, at most this long (ST-03); a re-click is ignored
WINDOW_OPEN_STATES = ("normal", "zoomed")   # wm state of an open window (withdrawn = closed by X, iconic = minimised)
LEAF_ENABLED = "fx.sound.enabled"
LEAF_VOLUME = "fx.sound.volume"
CAT_PREFIX = "fx.sound.cat."
COMMIT_MS = 180             # the volume commit after a release (spec ST-03, flush-marked)
STATUS_POLL_MS = 500        # the status line re-read while page 08 is shown
LAMP_INDENT = 22            # the category lamps and the status line sit under the master lamp's text
TEST_COL = 150              # the TEST word 150 px after the category lamp (approved: lamp x 312, TEST x 462)
CAT_GAP = 1                 # between two category rows (approved pitch 28 px: a lamp row is 27 px)
MASTER_TEST_CUE = "load_online"     # master OFF -> ON (ST-03; also what Alt+M's unmute plays)
VOLUME_TEST_CUE = "affirm"          # after a volume commit, at the committed gain
ADJUST_KEYS = ("Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next")    # keys that move the slider


def _fmt_volume(value):
    return "%d%%" % sc.snap(value)


class _CategoryRow(object):
    """One category: its lamp and its TEST word. TEST is always gridded while it can play, drawn in the ground colour
    at rest (so the rows never move and the keyboard can reach it) and in value ink while the pointer is over the row or
    the row holds the keyboard focus (its own hover / focus look is bright). Shown at once on <Enter> / <FocusIn>,
    hidden on <Leave> / <FocusOut> - no timer (X-23)."""

    def __init__(self, section, parent, key, word):
        from ...kit import px
        from ...kit.widgets import TextAction
        page = section.page
        t = page.t
        self.section, self.key, self.word = section, key, word
        self.frame = tk.Frame(parent, bg=t["bg"])
        self.frame.columnconfigure(0, minsize=px(parent, TEST_COL))
        self.var = page.string_var(sc.flag(sc.read(page, CAT_PREFIX + key)))
        self.lamp = sc.lamp(page, self.frame, word, self.var, self._on_lamp)
        self.lamp.grid(row=0, column=0, sticky="w")
        self._ground = t["bg"]
        self._ink = t["value"]
        self.test = TextAction(self.frame, page.theme, text=TEST, command=self._on_test, ground=t["bg"],
                               ink=t["bg"])
        self.test.grid(row=0, column=1, sticky="w")
        self.revealed = False
        self.inverted = False                   # TEST's cue is playing (ST-03 inverse); a re-click is ignored
        self._accent = t.get("bright", t["value"])       # the inverse ground: the phosphor's own bright ink
        for w in (self.frame, self.lamp, self.test):
            w.bind("<Enter>", self._reveal, add="+")
            w.bind("<Leave>", self._maybe_hide, add="+")
        for w in (self.lamp, self.test):
            w.bind("<FocusIn>", self._reveal, add="+")
            w.bind("<FocusOut>", self._maybe_hide, add="+")

    def _on_lamp(self):
        self.section._on_category(self.key, self.var)

    def _on_test(self):
        self.section._on_test(self.key)

    # ---------------------------------------------------------------- the hover / focus reveal
    def _inside(self, e=None):
        """The pointer is over this row, or one of its widgets holds the keyboard focus."""
        try:
            f = self.frame.focus_get()
        except (KeyError, tk.TclError):       # a focus widget tkinter did not make (a popdown)
            f = None
        if f is not None and f in (self.lamp, self.test):
            return True
        try:
            if e is not None and getattr(e, "x_root", None) is not None:
                w = self.frame.winfo_containing(e.x_root, e.y_root)
                while w is not None:
                    if w is self.frame:
                        return True
                    w = w.master
        except (KeyError, tk.TclError):
            pass
        return False

    def _reveal(self, _e=None):
        self._paint(True)

    def _maybe_hide(self, e=None):
        if not self._inside(e):
            self._paint(False)

    def _paint(self, shown):
        self.revealed = bool(shown)
        if self.inverted:                       # the inverse owns the look until it ends
            return
        try:
            self.test.configure(ink=self._ink if shown else self._ground)
        except tk.TclError:
            pass

    # ---------------------------------------------------------------- the TEST inverse (ST-03, ADV1-06)
    def invert(self, ms, clk):
        """TEST inverted (ground bright, ink bg) for min(ms, 700) ms through one fx.clock.later owned by the
        TEST word (cancel-first; a destroyed word drops it silently)."""
        if clk is None or not ms:
            return
        try:
            dur = max(1, min(int(ms), TEST_INVERT_MAX_MS))
            entry = clk.later(self.test, "invert", dur, self.end_invert)
        except Exception:  # noqa: BLE001
            entry = None
        if entry is None:
            return
        self.inverted = True
        try:
            self.test.configure(ground=self._accent, ink=self._ground)
        except tk.TclError:
            pass

    def end_invert(self):
        if not self.inverted:
            return
        self.inverted = False
        try:
            self.test.configure(ground=self._ground)
        except tk.TclError:
            pass
        self._paint(self.revealed)

    # ---------------------------------------------------------------- state
    def set_enabled(self, lamp_ok, test_ok):
        sc.set_state(self.lamp, lamp_ok)
        try:
            if test_ok and not self.test.winfo_manager():
                self.test.grid(row=0, column=1, sticky="w")
            elif not test_ok and self.test.winfo_manager():
                try:
                    focused = self.test.focus_get() is self.test
                except (KeyError, tk.TclError):
                    focused = False
                if focused:
                    self.lamp.focus_set()           # the focus never stays on a word that is about to go
                self.test.grid_remove()
        except tk.TclError:
            pass

    def release(self, clk=None):
        if clk is not None:
            try:
                clk.cancel(self.test, "invert")
            except Exception:  # noqa: BLE001
                pass
        self.inverted = False
        for w in (self.lamp, self.test):
            try:
                w.configure(command=None)
            except tk.TclError:
                pass


class SignalOutputSection(Section):
    noun = "sound"

    def __init__(self, page, parent):
        from ...kit import px
        self.page = page
        self.parent = parent
        self._px = lambda n: px(parent, n)
        self._commit_pending = False
        self._adjusting = False                 # a button / key is down on the slider
        self._released = False
        self._status_text = None
        t = page.t
        page.title(parent, TITLE)
        # master
        self.master_row = page.option_row(parent)
        self.master_var = page.string_var(sc.flag(sc.read(page, LEAF_ENABLED)))
        self.master = sc.lamp(page, self.master_row, MASTER, self.master_var, self._on_master)
        self.master.grid(row=0, column=0, sticky="w")
        # the status line: packed under the master row only while the output is not ready
        self.status = page.label(parent, "", dim=True)
        self._status_ink = (t["value"], t.get("warn_eff", t["value"]))
        # volume
        self.volume_var = page.string_var(str(sc.snap(sc.read(page, LEAF_VOLUME), 40)))
        self.scale, self.readout = sc.labeled_scale(page, parent, VOLUME, self.volume_var, _fmt_volume)
        # every button moves a ttk slider (1: drag; 2 / 3: ttk::scale::Jump + drag - ADV1-01), so each one adjusts and
        # its release commits
        for b in ("1", "2", "3"):
            self.scale.bind("<ButtonPress-%s>" % b, self._on_adjust, add="+")
            self.scale.bind("<ButtonRelease-%s>" % b, self._schedule_commit, add="+")
        # ADV1-02: the kit slider binds <Left>/<Right>/<Up>/<Down> on the widget itself (returning 'break'), and Tk runs
        # only the most specific binding per tag - a generic <KeyPress> on the widget never sees an arrow. So the
        # "adjusting" hook sits on a tag of its own placed BEFORE the widget's (it returns nothing: the step still runs)
        self._adjust_tag = None
        self._adjust_cmd = None
        try:
            self._adjust_cmd = self.scale.register(self._on_key_adjust_sym)    # deleted with the slider
            self._adjust_tag = "B13VolumeAdjust%x" % id(self)
            self.scale.tk.call("bind", self._adjust_tag, "<KeyPress>", self._adjust_cmd + " %K")
            self.scale.bindtags((self._adjust_tag,) + tuple(self.scale.bindtags()))
        except tk.TclError:
            self.scale.bind("<KeyPress>", self._on_key_adjust, add="+")
        self.scale.bind("<KeyRelease>", self._on_key_release, add="+")
        self.scale.bind("<FocusOut>", self._on_scale_focus_out, add="+")
        # ADV1-05: the read-only page (kill switch) has Alt+M unbound, so its caption does not offer the key
        self.caption = page.caption(parent, CAPTION if page.live else CAPTION_BASE)
        self._map_cmd = None                    # the window's <Map> re-arm while the poll is parked (ADV1-03)
        self._map_script = None
        self._map_path = None
        self._poll_parked = False
        # the five categories
        self.rows = []
        for i, (key, word) in enumerate(sc.CATEGORY_ROWS):
            row = _CategoryRow(self, parent, key, word)
            row.frame.pack(fill="x", padx=(self._px(LAMP_INDENT), 0),
                           pady=(self._px(18 if i == 0 else CAT_GAP), 0))
            self.rows.append(row)
        page.subscribe("fx.sound", self._on_pref, owner=parent)
        self._sync()

    # ---------------------------------------------------------------- reading
    def _engine(self):
        return sc.engine(self.page)

    def _clock(self):
        try:
            return getattr(self.page.fx, "clock", None)
        except Exception:  # noqa: BLE001
            return None

    def _master_on(self):
        return bool(sc.read(self.page, LEAF_ENABLED))

    def _stored_volume(self):
        return sc.snap(sc.read(self.page, LEAF_VOLUME), 0)

    def _can_test(self):
        eng = self._engine()
        return self.page.live and sc.test_ready(sc.running(eng), sc.engine_state(eng), self._master_on(),
                                                self._stored_volume())

    def _window_open(self):
        """The page's window is open on screen: not closed with X (withdrawn) and not minimised. False if unreadable."""
        try:
            return str(self.parent.winfo_toplevel().wm_state()) in WINDOW_OPEN_STATES
        except (tk.TclError, AttributeError):
            return False

    def _tearing(self):
        fx = getattr(self.page, "fx", None)
        return bool(getattr(fx, "_tearing", False) or getattr(fx, "_torn", False))

    # ---------------------------------------------------------------- writing
    def _audition(self, what):
        """engine.test(what) (a cue or a category) at the committed settings; nothing when it cannot play now."""
        if self._released or self._tearing() or not self._can_test():
            return None
        fn = getattr(self._engine(), "test", None)
        if not callable(fn):
            return None
        try:
            return fn(what)
        except Exception:  # noqa: BLE001 - the engine never raises; a double might
            return None

    def _on_master(self):
        page = self.page
        if not page.live or self._released:
            self._sync()
            return
        on = self.master_var.get() == "1"
        self._flush_commit()                    # an audition plays at the committed gain
        changed = page.set_pref(LEAF_ENABLED, on)           # prefs FIRST, then the acknowledgement
        if changed and on:
            self._audition(MASTER_TEST_CUE)
        self._sync()

    def _on_category(self, key, var):
        page = self.page
        if not page.live or self._released or not self._master_on():
            self._sync()
            return
        on = var.get() == "1"
        changed = page.set_pref(CAT_PREFIX + key, on)
        if changed and on:
            self._flush_commit()
            self._audition(key)
        self._sync()

    def _on_test(self, key):
        if not self.page.live or self._released:
            return
        row = next((r for r in self.rows if r.key == key), None)
        if row is not None and row.inverted:    # its cue is still playing: a re-click does not restart it (ADV1-06)
            return
        self._flush_commit()                    # TEST plays at the gain the slider shows
        ms = self._audition(key)
        if row is not None:
            row.invert(ms, self._clock())

    # ---------------------------------------------------------------- the volume commit
    def _on_adjust(self, _e=None):
        if self.page.live:
            self._adjusting = True

    def _on_key_adjust(self, e=None):
        self._on_key_adjust_sym(getattr(e, "keysym", None))

    def _on_key_adjust_sym(self, keysym=None):
        if keysym in ADJUST_KEYS and not self._released:
            self._on_adjust()

    def _on_key_release(self, e=None):
        if getattr(e, "keysym", None) in ADJUST_KEYS:
            self._schedule_commit(e)

    def _on_scale_focus_out(self, e=None):
        if self._adjusting:                     # a key held while the focus left: its release went elsewhere
            self._schedule_commit(e)
            return
        if self._commit_pending or self._released or not self.page.live:
            return
        try:                                    # any other move the slider shows but nobody committed
            shown = sc.snap(self.volume_var.get(), None)
        except tk.TclError:
            shown = None
        if shown is not None and shown != self._stored_volume():
            self._schedule_commit(e)

    def _schedule_commit(self, _e=None):
        self._adjusting = False
        if not self.page.live or self._released:
            return
        self._commit_pending = True
        clk = self._clock()
        entry = None
        if clk is not None:
            try:
                entry = clk.later(self.scale, "commit", COMMIT_MS, self._commit_due, flush=True)
            except Exception:  # noqa: BLE001
                entry = None
        if entry is None:                       # no clock (or it refused): commit now
            self._commit(audition=False)

    def _commit_due(self):
        self._commit(audition=not self._tearing())

    def _cancel_commit(self):
        if not self._commit_pending:
            return
        self._commit_pending = False
        clk = self._clock()
        if clk is not None:
            try:
                clk.cancel(self.scale, "commit")
            except Exception:  # noqa: BLE001
                pass

    def _flush_commit(self):
        """A pending commit runs now (no audition of its own)."""
        if self._commit_pending:
            self._cancel_commit()
            self._commit(audition=False)

    def _commit(self, audition=True):
        self._commit_pending = False
        page = self.page
        if self._released or not page.live:
            return
        try:
            raw = self.volume_var.get()
        except tk.TclError:
            return
        v = sc.snap(raw, self._stored_volume())
        changed = page.set_pref(LEAF_VOLUME, v)
        sc.set_var(self.volume_var, str(v))     # the slider settles on the stored step (37 -> 35)
        if changed and audition and page.shown and self._window_open():     # never from a closed window (ADV1-04)
            self._audition(VOLUME_TEST_CUE)
        self._sync_status()

    # ---------------------------------------------------------------- following the store
    def _on_pref(self, path, _value):
        if self._released:
            return
        if path == LEAF_VOLUME:
            self._cancel_commit()               # a volume set elsewhere (RESTORE, a restore) wins over a pending one
        self._sync()

    def _sync(self):
        """Every row from page.view; the slider is left alone while it is being moved or its commit is pending."""
        if self._released:
            return
        page = self.page
        live = page.live
        master = self._master_on()
        sc.set_var(self.master_var, sc.flag(master))
        if not (self._commit_pending or self._adjusting):
            sc.set_var(self.volume_var, str(self._stored_volume()))
        for row in self.rows:
            sc.set_var(row.var, sc.flag(sc.read(page, CAT_PREFIX + row.key)))
        sc.set_state(self.master, live)
        sc.set_state(self.scale, live)
        test_ok = self._can_test()
        for row in self.rows:
            row.set_enabled(live and master, test_ok)
        self._sync_status()

    def _sync_status(self, *_a):
        """The status line's one writer."""
        if self._released:
            return
        text = ""
        if self.page.live:
            eng = self._engine()
            text = sc.render_status(sc.running(eng), sc.engine_state(eng), self._master_on(), self._stored_volume(),
                                    sc.sfx_env_off())
        if text == self._status_text:
            return
        self._status_text = text
        try:
            if text:
                ink = self._status_ink[1] if text in sc.WARN_STATUSES or text.startswith("SOUND SWITCHED") \
                    else self._status_ink[0]
                self.status.configure(text=text, fg=ink)
                if not self.status.winfo_manager():
                    self.status.pack(fill="x", after=self.master_row, padx=(self._px(LAMP_INDENT), 0),
                                     pady=(self._px(4), 0))
            elif self.status.winfo_manager():
                self.status.pack_forget()
                self.status.configure(text="")
        except tk.TclError:
            pass
        test_ok = self._can_test()
        for row in self.rows:
            try:
                ok_now = bool(row.test.winfo_manager())
            except tk.TclError:
                ok_now = test_ok
            if ok_now != test_ok:
                row.set_enabled(self.page.live and self._master_on(), test_ok)

    def status_text(self):
        """What the status line says now ('' = ready, no line)."""
        return self._status_text or ""

    # ---------------------------------------------------------------- Section hooks
    def defaults(self):
        page = self.page
        out = {LEAF_ENABLED: page.default(LEAF_ENABLED), LEAF_VOLUME: page.default(LEAF_VOLUME)}
        for key, _w in sc.CATEGORY_ROWS:
            out[CAT_PREFIX + key] = page.default(CAT_PREFIX + key)
        return out

    def refresh(self):
        """After RESTORE DEFAULTS: the store wins over a pending volume commit."""
        self._cancel_commit()
        self._adjusting = False
        self._sync()

    def on_show(self):
        self._sync()
        self._start_poll()

    def _start_poll(self):
        self._poll_parked = False
        clk = self._clock()
        if clk is not None and self.page.live and not self._released:
            try:
                clk.loop(self.status, "status", STATUS_POLL_MS, self._poll)
            except Exception:  # noqa: BLE001
                pass

    def _stop_poll(self):
        clk = self._clock()
        if clk is not None:
            try:
                clk.cancel(self.status, "status")
            except Exception:  # noqa: BLE001
                pass

    def _poll(self, *_a):
        """The 500 ms status re-read. A closed (withdrawn) or minimised window parks it - no wakeups while the app sits
        in the background (ADV1-03, ST-03 "withdraw cancels") - until the window is mapped again."""
        if self._released:
            return
        if not self._window_open():
            self._stop_poll()
            self._bind_window_map()
            self._poll_parked = True
            return
        self._sync_status()

    def _bind_window_map(self):
        """bind <top> <Map> {+if {"%W" eq "<top>"} {catch <cmd>}} (fx/__init__'s filtered pattern: children's <Map>
        reach the toplevel's tag too). Installed at the first park and kept until release() (never removed from inside
        its own call); it only re-arms a parked poll."""
        if self._map_cmd is not None:
            return
        try:
            top = self.parent.winfo_toplevel()
            path = str(top)
            cmd = self.status.register(self._on_window_map)      # also deleted with the line
            script = 'if {"%W" eq {' + path + '}} {catch {' + cmd + '}}'
            top.tk.call("bind", path, "<Map>", "+" + script)
            self._map_cmd, self._map_script, self._map_path = cmd, script, path
        except (tk.TclError, AttributeError):
            pass

    def _unbind_window_map(self):
        """Removes exactly its own binding line (other <Map> lines on the toplevel stay) and its Tcl command."""
        cmd, script, path = self._map_cmd, self._map_script, self._map_path
        self._map_cmd = self._map_script = self._map_path = None
        if cmd is None:
            return
        try:
            cur = str(self.status.tk.call("bind", path, "<Map>"))
            if script in cur:
                lines = [ln for ln in cur.split("\n") if ln != script]
                self.status.tk.call("bind", path, "<Map>", "\n".join(lines).strip("\n"))
        except tk.TclError:
            pass
        try:
            self.status.deletecommand(cmd)
        except (tk.TclError, ValueError):
            pass

    def _on_window_map(self):
        """The window came back (deiconify / restore): re-arm a parked poll if 08 is still the shown page."""
        if self._released or not self._poll_parked:
            return
        self._poll_parked = False
        if getattr(self.page, "shown", False):
            self._start_poll()
            self._sync_status()

    def on_hide(self):
        self._stop_poll()
        self._poll_parked = False
        self._flush_commit()
        self._adjusting = False
        for row in self.rows:
            row._paint(False)

    def release(self):
        self.on_hide()
        self._released = True
        self._unbind_window_map()
        clk = self._clock()
        for row in self.rows:
            row.release(clk)
        if self._adjust_tag is not None:        # the slider's own adjust tag (ADV1-02)
            try:
                self.scale.tk.call("bind", self._adjust_tag, "<KeyPress>", "")
            except tk.TclError:
                pass
        for w in (self.master,):
            try:
                w.configure(command=None)
            except tk.TclError:
                pass


register_section("signal", 100, SignalOutputSection)
