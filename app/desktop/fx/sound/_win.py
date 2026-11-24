"""fx.sound._win - the ONLY module under fx/sound that calls Windows (ctypes). B12, sound engine v1.

Everything that reaches the OS lives here, so the rest of fx/sound stays pure (test_fx_sound pins "no device
symbol outside _win.py" and "no tkinter under fx/sound"). Callers are the engine's single "fx-sound" thread
(never the Tk thread); every public function is cheap, never blocks on the device and never raises.

THE SILENCE RULE (BUILD_PLAN B12, AESTHETIC_SPEC 5.5 "Env"):
    LOCALAI_SFX_SILENT=1 (any value except '', 0, false, no, off) makes every call that could make a noise a
    RECORDED NO-OP: play_memory(), stop() and format_supported() check is_silent() as their FIRST statement,
    before any DLL is loaded or any function is bound, and return what the device path would (success for a valid
    request; play_memory() still says False for a buffer that is not a WAV image). The check is sticky: once
    this process has seen the flag (or force_silent() was called, e.g. by boot(silent=True)) it stays silent
    for the rest of its life, even if the variable is later removed. The latch is taken at import (so a flag
    present when the module loads is never lost to a later environment change). test_fx_sound proves it with recording
    fakes at both the loader and the bound-function level, plus a static "first statement" check.

    Queries that cannot make a sound stay real in silent mode, so a silent engine still sees the machine as it
    is: num_devs() (waveOutGetNumDevs), window_state()/presence() (GetAncestor, IsWindowVisible, IsIconic,
    GetForegroundWindow, GetWindowThreadProcessId) and notification_state() (SHQueryUserNotificationState, the
    DND state).

Device calls:
    play_memory(buf)        PlaySoundW(addr, NULL, SND_MEMORY|SND_ASYNC|SND_NODEFAULT) (flags 0x0007): async,
                            from memory, never the default system sound, never SND_FILENAME, never winsound.
                            Returns the BOOL (False = the buffer was refused; also False on any exception, and
                            for a buffer that is not a RIFF/WAVE image, which never reaches the device).
                            The last KEEP_GENERATIONS buffers are kept alive here, because Windows reads an
                            async memory sound while it plays. make_buffer() appends TAIL_PAD_MS of silence
                            (PlaySoundW drops the last output-latency's worth of every async sound).
    stop(rate)              soft stop: plays 10 ms of zeros async (a hard PlaySound(NULL) blocks 11-38 ms).
    format_supported(rate)  waveOutOpen(NULL, WAVE_MAPPER, fmt, 0, 0, WAVE_FORMAT_QUERY): asks whether the
                            mapper takes 16-bit mono PCM at `rate` WITHOUT opening the device (the 48 kHz ->
                            44.1 kHz fallback decision).

Every DLL is loaded through _load() and every function is bound through _fn(); nothing else in this file touches
ctypes' loaders (pinned by test_fx_sound), so one patch point covers all of them.
"""
import collections
import ctypes
import os
import struct
import threading
import time

__all__ = [
    "SILENT_ENV", "PLAY_FLAGS", "SND_ASYNC", "SND_NODEFAULT", "SND_MEMORY", "WAVE_MAPPER", "WAVE_FORMAT_QUERY",
    "QUNS_NOT_PRESENT", "QUNS_BUSY", "QUNS_RUNNING_D3D_FULL_SCREEN", "QUNS_PRESENTATION_MODE",
    "QUNS_ACCEPTS_NOTIFICATIONS", "QUNS_QUIET_TIME", "QUNS_APP", "DND_STATES", "KEEP_GENERATIONS", "TAIL_PAD_MS",
    "GA_ROOT", "is_silent", "force_silent", "make_buffer", "pad_tail", "wav_image_ok", "play_memory", "stop", "format_supported",
    "num_devs", "window_state", "presence", "notification_state", "dnd_active", "call_log", "counts",
]

SILENT_ENV = "LOCALAI_SFX_SILENT"
_NOT_SILENT_VALUES = ("", "0", "false", "no", "off")

SND_ASYNC = 0x0001
SND_NODEFAULT = 0x0002
SND_MEMORY = 0x0004
PLAY_FLAGS = SND_MEMORY | SND_ASYNC | SND_NODEFAULT          # 0x0007

WAVE_MAPPER = 0xFFFFFFFF                                     # (UINT)-1
WAVE_FORMAT_QUERY = 0x0001
WAVE_FORMAT_PCM = 1
MMSYSERR_NOERROR = 0

# QUERY_USER_NOTIFICATION_STATE (shellapi.h)
QUNS_NOT_PRESENT = 1
QUNS_BUSY = 2
QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4
QUNS_ACCEPTS_NOTIFICATIONS = 5
QUNS_QUIET_TIME = 6
QUNS_APP = 7
DND_STATES = frozenset((QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE, QUNS_QUIET_TIME, QUNS_APP))
GA_ROOT = 2                   # GetAncestor: the top-level window of any child HWND

KEEP_GENERATIONS = 2          # the buffer playing now + the one before it (AESTHETIC_SPEC S-01)
STOP_MS = 10
# Sound nav/cut-off fix (2026-10-12): every cue image handed to PlaySoundW carries this much digital silence after its
# last sample. PlaySoundW(SND_ASYNC) closes its wave stream as soon as Windows reports the buffer DONE, which is when
# the data has been handed to the audio engine, not when the speaker has played it: the last tens of ms (the output
# latency) are thrown away. The user heard exactly that: 'route' (28 ms) never, affirm / denied (104 / 125 ms) cut,
# the long load cues (232-340 ms, quiet decaying tails) fine. The pad is what gets thrown away instead. The audible
# samples are unchanged (the lab-approved voice, D-23); fx.sound.engine.TAIL_GUARD_S keeps the one-voice rule in step.
TAIL_PAD_MS = 150
_LOG_MAX = 512

_lock = threading.Lock()
_silent_latched = False
_fns = {}                     # (dll, name) -> bound ctypes function
_keep = collections.deque(maxlen=KEEP_GENERATIONS)
_zero_bufs = {}               # rate -> 10 ms zero WAV buffer (kept alive for the life of the process)
_log = collections.deque(maxlen=_LOG_MAX)
_counts = collections.Counter()


# --- the silence gate -------------------------------------------------------------------------------------
def is_silent():
    """True when nothing may reach the audio device. Reads LOCALAI_SFX_SILENT NOW and latches it: once silent,
    silent for the rest of the process. Never raises (an unreadable environment counts as silent)."""
    global _silent_latched
    if _silent_latched:
        return True
    try:
        v = os.environ.get(SILENT_ENV)
        silent = v is not None and v.strip().lower() not in _NOT_SILENT_VALUES
    except Exception:
        silent = True
    if silent:
        _silent_latched = True
    return silent


def force_silent():
    """Latch silent mode for the rest of this process (boot(silent=True)). There is deliberately no way back."""
    global _silent_latched
    _silent_latched = True


# B12 fix ADV1-03: take the latch NOW, at import, so a process started with the flag stays silent even if its
# environment loses the flag (a helper restoring a pinned environ, os.environ.clear()) before the first device call.
is_silent()


def _record(name, mode, detail=None):
    try:
        _counts[(name, mode)] += 1
        if mode != "query":
            _log.append((time.perf_counter(), name, mode, detail))
    except Exception:
        pass


def call_log():
    """The recorded device / silent / error calls, oldest first: (perf_counter, name, mode, detail)."""
    return list(_log)


def counts():
    """{(name, mode): n} for every call, queries included. mode is 'silent', 'device', 'query' or 'error'."""
    return dict(_counts)


# --- binding (the one patch point) ----------------------------------------------------------------------
def _load(dll):
    """The only place a DLL is loaded."""
    return ctypes.WinDLL(dll)


def _fn(dll, name, argtypes, restype):
    """Bind dll!name once (cached). Raises on failure; callers catch."""
    key = (dll, name)
    f = _fns.get(key)
    if f is None:
        with _lock:
            f = _fns.get(key)
            if f is None:
                f = getattr(_load(dll), name)
                f.argtypes = argtypes
                f.restype = restype
                _fns[key] = f
    return f


def _wintypes():
    from ctypes import wintypes
    return wintypes


class _WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", ctypes.c_ushort), ("nChannels", ctypes.c_ushort),
                ("nSamplesPerSec", ctypes.c_ulong), ("nAvgBytesPerSec", ctypes.c_ulong),
                ("nBlockAlign", ctypes.c_ushort), ("wBitsPerSample", ctypes.c_ushort), ("cbSize", ctypes.c_ushort)]


# --- buffers --------------------------------------------------------------------------------------------
def make_buffer(data, tail_pad_ms=TAIL_PAD_MS):
    """A ctypes char array holding `data` (a whole WAV image), with `tail_pad_ms` of silence appended to a plain PCM
    image (pad_tail; anything else is kept as it is). Pure memory, no device call."""
    data = pad_tail(bytes(data), tail_pad_ms)
    return ctypes.create_string_buffer(data, len(data))


def pad_tail(data, ms=TAIL_PAD_MS):
    """`data` (a canonical 44-byte-header PCM WAV image: RIFF / WAVE / 'fmt ' 16 / 'data' at 36) with `ms` of zero
    frames appended and its RIFF and data sizes updated. Anything else (another layout, junk, ms <= 0) comes back
    unchanged. Pure; never raises."""
    try:
        data = bytes(data)
        if (ms <= 0 or len(data) < 44 or data[:4] != b"RIFF" or data[8:16] != b"WAVEfmt "
                or data[36:40] != b"data" or struct.unpack_from("<I", data, 16)[0] != 16
                or struct.unpack_from("<H", data, 20)[0] != WAVE_FORMAT_PCM):
            return data
        rate = struct.unpack_from("<I", data, 24)[0]
        align = struct.unpack_from("<H", data, 32)[0]
        size = struct.unpack_from("<I", data, 40)[0]
        if not (8000 <= rate <= 192000) or not (1 <= align <= 32) or size != len(data) - 44:
            return data
        pad = b"\x00" * (int(rate * ms // 1000) * align)
        size += len(pad)
        return (b"RIFF" + struct.pack("<I", 36 + size) + data[8:40] + struct.pack("<I", size) + data[44:] + pad)
    except Exception:
        return data


def wav_image_ok(buf):
    """True when `buf` (bytes / bytearray / memoryview, or a ctypes char array from make_buffer()) looks like a whole
    RIFF/WAVE image: at least a 44-byte header, 'RIFF' at 0 and 'WAVE' at 8. Pure memory; never raises. Both modes
    of play_memory() answer with it, so a silent run reports a junk buffer exactly as a real one would."""
    try:
        if isinstance(buf, ctypes.Array):
            size, head = ctypes.sizeof(buf), bytes(buf[:12])
        elif isinstance(buf, (bytes, bytearray, memoryview)):
            mv = memoryview(buf)
            size, head = mv.nbytes, bytes(mv)[:12] if mv.nbytes >= 12 else b""
        else:
            return False
        return size >= 44 and head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    except Exception:
        return False


def _zero_wav(rate, ms=STOP_MS):
    n = max(1, int(rate * ms // 1000))
    pcm = b"\x00\x00" * n
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, WAVE_FORMAT_PCM, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


# --- device calls (gated) -------------------------------------------------------------------------------
def play_memory(buf):
    """Start async playback of the WAV image in `buf` (from make_buffer(); bytes are wrapped). True when Windows
    accepted it; False for a buffer that is not a WAV image (refused before the device) or when Windows refused it.
    Silent mode: a recorded no-op that answers as the device path would for the buffer's shape (True for a WAV
    image, False for junk), with no device call. Never raises."""
    if is_silent():
        ok = wav_image_ok(buf)
        _record("PlaySoundW", "silent", (_size_of(buf), ok))
        return ok
    try:
        if not wav_image_ok(buf):
            _record("PlaySoundW", "error", "not a WAV image: %r" % (type(buf).__name__,))
            return False
        if not isinstance(buf, ctypes.Array):
            buf = make_buffer(buf)
        _keep.append(buf)
        wt = _wintypes()
        f = _fn("winmm", "PlaySoundW", [ctypes.c_void_p, wt.HMODULE, wt.DWORD], wt.BOOL)
        ok = bool(f(ctypes.addressof(buf), None, PLAY_FLAGS))
        _record("PlaySoundW", "device", (len(buf), ok))
        return ok
    except Exception as e:
        _record("PlaySoundW", "error", repr(e))
        return False


def stop(rate=48000):
    """Soft stop: replace whatever plays with STOP_MS of zeros (async). Silent mode: recorded no-op, True."""
    if is_silent():
        _record("PlaySoundW:stop", "silent", rate)
        return True
    try:
        rate = int(rate)
        buf = _zero_bufs.get(rate)
        if buf is None:
            buf = _zero_bufs[rate] = make_buffer(_zero_wav(rate), 0)
        wt = _wintypes()
        f = _fn("winmm", "PlaySoundW", [ctypes.c_void_p, wt.HMODULE, wt.DWORD], wt.BOOL)
        ok = bool(f(ctypes.addressof(buf), None, PLAY_FLAGS))
        _record("PlaySoundW:stop", "device", ok)
        return ok
    except Exception as e:
        _record("PlaySoundW:stop", "error", repr(e))
        return False


def format_supported(rate, bits=16, channels=1):
    """Does the wave mapper accept PCM at `rate`? A WAVE_FORMAT_QUERY open: the device is NOT opened.
    Silent mode: recorded no-op, True. Any failure: False."""
    if is_silent():
        _record("waveOutOpen:query", "silent", rate)
        return True
    try:
        rate, bits, channels = int(rate), int(bits), int(channels)
        align = channels * bits // 8
        fmt = _WAVEFORMATEX(WAVE_FORMAT_PCM, channels, rate, rate * align, align, bits, 0)
        wt = _wintypes()
        f = _fn("winmm", "waveOutOpen",
                [ctypes.c_void_p, wt.UINT, ctypes.POINTER(_WAVEFORMATEX), ctypes.c_size_t, ctypes.c_size_t, wt.DWORD],
                wt.UINT)
        rc = f(None, WAVE_MAPPER, ctypes.byref(fmt), 0, 0, WAVE_FORMAT_QUERY)
        _record("waveOutOpen:query", "device", (rate, rc))
        return rc == MMSYSERR_NOERROR
    except Exception as e:
        _record("waveOutOpen:query", "error", repr(e))
        return False


def _size_of(buf):
    try:
        return len(buf)
    except Exception:
        return None


# --- queries (cannot make a sound; real in silent mode) -------------------------------------------------
def num_devs():
    """waveOutGetNumDevs(): the number of wave output devices (0.017 ms). 0 on any failure."""
    try:
        wt = _wintypes()
        n = int(_fn("winmm", "waveOutGetNumDevs", [], wt.UINT)())
        _record("waveOutGetNumDevs", "query")
        return max(0, n)
    except Exception as e:
        _record("waveOutGetNumDevs", "error", repr(e))
        return 0


def window_state(hwnd):
    """{'visible', 'iconic', 'foreground', 'foreground_own_process'} for the app window, or None when hwnd is
    falsy or the calls fail. Any HWND of the window works (Tk's inner winfo_id() or the wm_frame() frame): it is
    resolved to its top-level root first (GetAncestor GA_ROOT), because IsIconic is only true on the top level.
    foreground_own_process: the foreground window belongs to this process (one of the app's own dialogs or windows
    has the focus)."""
    try:
        h = int(hwnd or 0)
    except Exception:
        return None
    if h <= 0:
        return None
    try:
        wt = _wintypes()
        h = int(_fn("user32", "GetAncestor", [wt.HWND, wt.UINT], wt.HWND)(h, GA_ROOT) or h)
        visible = bool(_fn("user32", "IsWindowVisible", [wt.HWND], wt.BOOL)(h))
        iconic = bool(_fn("user32", "IsIconic", [wt.HWND], wt.BOOL)(h))
        fg = _fn("user32", "GetForegroundWindow", [], wt.HWND)() or 0
        own = False
        if fg and fg != h:
            pid = wt.DWORD(0)
            _fn("user32", "GetWindowThreadProcessId", [wt.HWND, ctypes.POINTER(wt.DWORD)], wt.DWORD)(
                fg, ctypes.byref(pid))
            own = int(pid.value) == os.getpid()
        _record("presence", "query")
        return {"visible": visible, "iconic": iconic, "foreground": fg == h, "foreground_own_process": own}
    except Exception as e:
        _record("presence", "error", repr(e))
        return None


def presence(hwnd):
    """(hidden, away) for the app window, given any of its HWNDs (AESTHETIC_SPEC 5.5): hidden = not visible (the
    window was closed, i.e. withdrawn); away = minimised, or the foreground is neither this window nor another
    window of this process (an own dialog in front still counts as present). None when unknown (no window attached
    yet, a bad handle, or the calls failed)."""
    st = window_state(hwnd)
    if st is None:
        return None
    hidden = not st["visible"]
    away = st["iconic"] or not (st["foreground"] or st["foreground_own_process"])
    return hidden, away


def notification_state():
    """SHQueryUserNotificationState(): a QUNS_* value (1..7), or 0 when unknown (0.065 ms)."""
    try:
        s = ctypes.c_int(0)
        hr = _fn("shell32", "SHQueryUserNotificationState", [ctypes.POINTER(ctypes.c_int)], ctypes.c_long)(
            ctypes.byref(s))
        _record("SHQueryUserNotificationState", "query")
        return int(s.value) if hr == 0 else 0
    except Exception as e:
        _record("SHQueryUserNotificationState", "error", repr(e))
        return 0


def dnd_active(state=None):
    """True when Windows says do-not-disturb (QUNS 2 busy, 3 D3D full screen, 4 presentation, 6 quiet time,
    7 full-screen Store app). QUNS_NOT_PRESENT (1) is not DND: the engine treats it as away."""
    if state is None:
        state = notification_state()
    return state in DND_STATES
