"""fx.sound.synth - the approved voice: a 1:1 port of the Signal Lab synthesiser (B12, D-23). Pure stdlib.

Ground truth: the Signal Lab page (signal_lab.html, see design archive), the PEAK table and RECIPES (the user
approved every cue "as rendered in the lab", D-23). The lab renders each cue at the audio context's rate (48 kHz on
this machine; 44.1 kHz is the fallback here) into Float32Arrays, removes the mean, normalises the peak to PEAK[name]
dBFS, fades 1 ms at each end, and plays it through a master gain of (volume/50)**1.5 (= 30*log10(v/50) dB). This
module does exactly that:

  * every Float32Array is an array('f') and every JS expression keeps its operand order, so each store rounds to
    float32 the way the lab's did (the noise LCG is the lab's Math.imul generator, bit for bit). Checked against
    the lab's own JS run under Node (V8, the browser's engine): see design archive (the B12 build report, round 1);
  * the one exception, D-23: the user disliked `fault` ("replace with hazard"), so fault is NOT ported:
    CUE_SOURCE = {"fault": "hazard"} and fault renders hazard's recipe at hazard's peak, byte for byte;
  * the transmit_blip variant is ported too (X-16), although the engine uses the default chirp until B14b;
  * the result is quantised to 16-bit PCM with the volume baked in (no OS mixer API) and wrapped as a mono WAV
    image for PlaySoundW (SND_MEMORY).

No device, no thread, no Tk, no file access: render_float()/quantise()/wav_bytes() are functions of their arguments
only and are deterministic (byte-identical on every render). Rendering every cue takes a few hundred ms of CPU, so the
engine renders on its own thread; render_all() sleeps `pause_s` between cues (the spec's mandatory 2 ms yields) and
can be stopped between cues.
"""
import array
import math
import struct
import sys
import time

__all__ = [
    "SAMPLE_RATE", "FALLBACK_RATE", "RATES", "PEAK", "LAB_FAULT_PEAK", "CUE_SOURCE", "CUES", "VARIANTS", "NAMES",
    "DURATION_MS", "RECIPES", "source", "render_float", "quantise", "volume_gain", "wav_bytes", "render_wav",
    "render_all", "peak_dbfs", "dc_lsb",
]

SAMPLE_RATE = 48000
FALLBACK_RATE = 44100
RATES = (SAMPLE_RATE, FALLBACK_RATE)
FULL_SCALE = 32767

# The lab's PEAK table (signal_lab.html, "const PEAK"): dBFS at volume 50. fault is retired (D-23).
PEAK = {
    "route": -26.40, "route_back": -26.09, "denied": -30.99, "affirm": -29.70, "hazard": -27.39,
    "transmit": -33.28, "transmit_blip": -33.28, "lock_on": -27.71, "abort": -35.25, "landed": -26.29,
    "wait_ping": -35.89, "load_start": -26.34, "load_online": -29.19, "power_on": -26.10, "power_off": -27.57,
    "caution": -26.09,
}
LAB_FAULT_PEAK = -25.04            # the lab's value for the retired fault recipe; kept for the record, never used
CUE_SOURCE = {"fault": "hazard"}   # D-23: errors play the hazard recipe (category/priority/min-gap stay fault's)

CUES = ("route", "route_back", "denied", "affirm", "hazard", "transmit", "lock_on", "abort", "landed", "wait_ping",
        "load_start", "load_online", "power_on", "power_off", "caution", "fault")
VARIANTS = ("transmit_blip",)
NAMES = CUES + VARIANTS

# The length of each recipe's output buffer (its outer N(sr, ms)); fault = hazard.
DURATION_MS = {
    "route": 28, "route_back": 28, "denied": 125, "affirm": 104, "hazard": 206, "transmit": 118,
    "transmit_blip": 100, "lock_on": 172, "abort": 140, "landed": 480, "wait_ping": 300, "load_start": 340,
    "load_online": 232, "power_on": 450, "power_off": 600, "caution": 320, "fault": 206,
}

_TWO_PI = 2 * math.pi              # JS: 2 * Math.PI (the same double)
_NEG_TWO_PI = -2 * math.pi         # JS: -2 * Math.PI
_sin, _exp, _pow, _fmod = math.sin, math.exp, math.pow, math.fmod


# --- the lab's helpers, in the lab's operand order --------------------------------------------------------
def _jround(x):
    """JS Math.round for the non-negative values used here: nearest integer, halves up."""
    r = math.floor(x)
    return int(r + 1 if x - r >= 0.5 else r)


def _N(sr, ms):
    """N(sr, ms): a zeroed Float32Array of max(1, round(sr*ms/1000)) samples."""
    return array.array("f", [0.0]) * max(1, _jround(sr * ms / 1000))


def _edges(a, sr, ms=None):
    """edges(a, sr, ms): linear fade over min(round(sr*(ms||3)/1000), len>>2) samples at both ends."""
    L = len(a)
    n = min(_jround(sr * (ms or 3) / 1000), L >> 2)
    for i in range(n):
        g = i / n
        a[i] *= g
        a[L - 1 - i] *= g
    return a


def _rng(seed):
    """The lab's LCG: s = imul(s, 1664525) + 1013904223 >>> 0; returns s / 2**31 - 1."""
    s = (seed & 0xFFFFFFFF) or 1

    def r():
        nonlocal s
        s = (s * 1664525 + 1013904223) & 0xFFFFFFFF
        return s / 2147483648 - 1
    return r


def _lp1(a, sr, fc):
    x = _exp(_NEG_TWO_PI * fc / sr)
    b = 1 - x
    y = 0.0
    for i in range(len(a)):
        y = b * a[i] + x * y          # y stays a double; only the store rounds (as in the lab)
        a[i] = y
    return a


def _hp1(a, sr, fc):
    x = _exp(_NEG_TWO_PI * fc / sr)
    y = 0.0
    p = 0.0
    for i in range(len(a)):
        v = a[i]
        y = x * (y + v - p)
        p = v
        a[i] = y
    return a


SQ = ((1, 1), (3, 1 / 3), (5, 1 / 5), (7, 1 / 7), (9, 1 / 9))
TRI = ((1, 1), (3, -1 / 9), (5, 1 / 25), (7, -1 / 49))
_SINE = ((1, 1),)


def _tone(sr, ms, f0, f1=0, harm=None, tau=None, am=None, edge=None):
    """tone(sr, ms, f0, f1, {harm, tau, am, edge}): additive partials below 0.42*sr, exponential glide f0 -> f1,
    optional exp(-t*1000/tau) decay and amplitude function am(t), then edges (3 ms unless edge)."""
    n = max(1, _jround(sr * ms / 1000))
    dur = n / sr
    harm = harm or _SINE
    nyq = 0.42 * sr
    f1 = f1 or f0
    glide = f1 != f0
    ratio = f1 / f0
    # Every partial is below nyq for the whole tone when its highest pitch is (with a margin far above any
    # rounding): then the per-sample test is always true and can be skipped without changing a single value.
    all_on = max(f0, f1) * max(k for k, _a in harm) * 1.000001 < nyq
    inc = _TWO_PI * f0 / sr
    out = [0.0] * n
    ph = 0.0
    single = len(harm) == 1 and harm[0] == (1, 1)
    for i in range(n):
        t = i / sr
        if glide:
            f = f0 * _pow(ratio, t / dur)
            ph += _TWO_PI * f / sr
        else:
            f = f0
            ph += inc                     # == 2*PI*f/sr with f == f0, accumulated in the same order
        if single and all_on:
            v = 0.0 + _sin(ph)
        else:
            v = 0.0
            for k, amp in harm:
                if all_on or f * k < nyq:
                    v += amp * _sin(k * ph)
        if tau:
            v *= _exp(-t * 1000 / tau)
        if am is not None:
            v *= am(t)
        out[i] = v
    return _edges(array.array("f", out), sr, edge or 3)


def _noise(sr, ms, seed, hp=None, lp=None, tau=None, gain=None, arc=False, edge=None):
    a = _N(sr, ms)
    r = _rng(seed)
    L = len(a)
    for i in range(L):
        a[i] = r()
    if hp:
        _hp1(a, sr, hp)
    if lp:
        _lp1(a, sr, lp)
    for i in range(L):
        t = i / sr
        g = 1 if gain is None else gain
        if tau:
            g *= _exp(-t * 1000 / tau)
        if arc:
            g *= _sin(math.pi * i / L)
        a[i] *= g
    return _edges(a, sr, edge or 1)


def _mix_at(out, src, sr, at_ms, gain=None):
    o = _jround(sr * at_ms / 1000)
    g = 1 if gain is None else gain
    m = min(len(src), len(out) - o)
    if g == 1 and gain is None:
        for i in range(m):
            out[o + i] += src[i]          # src[i] * 1 is exact
    else:
        for i in range(m):
            out[o + i] += src[i] * g
    return out


def _ramp(a, sr, in_ms):
    n = min(len(a), _jround(sr * in_ms / 1000))
    for i in range(n):
        a[i] *= i / n
    return a


def _fm(sr, ms, c0, c1, ratio, index):
    n = max(1, _jround(sr * ms / 1000))
    dur = n / sr
    r = c1 / c0
    pc = 0.0
    pm = 0.0
    out = [0.0] * n
    for i in range(n):
        t = i / sr
        c = c0 * _pow(r, t / dur)
        pc += _TWO_PI * c / sr
        pm += _TWO_PI * c * ratio / sr
        I = index * (1 - t / dur)
        out[i] = _sin(pc + I * _sin(pm)) * _exp(-t * 1000 / 60)
    return _edges(array.array("f", out), sr, 3)


def _hazard_am(t):
    return 0.45 + 0.55 * (1 - _fmod(t * 20, 1))


# --- RECIPES (signal_lab.html "const RECIPES"), fault omitted (D-23) --------------------------------------
# Where the lab calls the same sub-recipe twice (denied's burst, lock_on's and caution's ping, landed's p), the
# arrays are identical, so one render is mixed twice.
def _route(sr, hz):
    o = _N(sr, 28)
    _mix_at(o, _noise(sr, 4, 11, hp=2000, tau=1.2, gain=0.8), sr, 0)
    _mix_at(o, _tone(sr, 24, hz, 0, tau=6), sr, 4)
    return o


def _r_route(sr):
    return _route(sr, 1760)


def _r_route_back(sr):
    return _route(sr, 1320)


def _r_denied(sr):
    o = _N(sr, 125)
    a = _tone(sr, 45, 196, 0, harm=SQ)
    b = _tone(sr, 45, 207.65, 0, harm=SQ)
    for i in range(len(a)):
        a[i] = (a[i] + b[i]) / 2
    burst = _edges(_lp1(a, sr, 2500), sr, 3)
    _mix_at(o, burst, sr, 0)
    _mix_at(o, burst, sr, 80)
    return o


def _r_affirm(sr):
    o = _N(sr, 104)
    h = ((1, 1), (3, 0.15))
    _mix_at(o, _tone(sr, 34, 1318.5, 0, harm=h), sr, 0)
    _mix_at(o, _tone(sr, 60, 1760, 0, harm=h, tau=40), sr, 44)
    return o


def _r_hazard(sr):
    o = _N(sr, 206)
    _mix_at(o, _noise(sr, 3, 23, hp=3000, tau=0.8), sr, 0)
    _mix_at(o, _tone(sr, 200, 987.8, 0, harm=TRI, am=_hazard_am), sr, 6)
    return o


def _squelch(sr):
    return _noise(sr, 25, 31, hp=1500, lp=3500, tau=12, gain=0.7)


def _r_transmit(sr):
    o = _N(sr, 118)
    _mix_at(o, _squelch(sr), sr, 0)
    _mix_at(o, _fm(sr, 80, 700, 1900, 1.5, 1.2), sr, 32)
    return o


def _r_transmit_blip(sr):
    o = _N(sr, 100)
    _mix_at(o, _squelch(sr), sr, 0)
    _mix_at(o, _tone(sr, 60, 1318.5, 0, tau=25), sr, 32)
    return o


def _r_lock_on(sr):
    o = _N(sr, 172)
    p = _tone(sr, 92, 1568, 0, harm=((1, 1), (2, 0.12)), tau=25)
    _mix_at(o, p, sr, 0)
    _mix_at(o, p, sr, 80)
    return o


def _r_abort(sr):
    o = _N(sr, 140)
    _mix_at(o, _tone(sr, 100, 1400, 500, tau=60), sr, 0)
    _mix_at(o, _noise(sr, 40, 41, lp=1500, tau=15, gain=0.5), sr, 98)
    return o


def _r_landed(sr):
    o = _N(sr, 480)
    p = _tone(sr, 360, 880, 0, harm=((1, 1), (2, 0.25)), tau=90)
    _mix_at(o, p, sr, 0)
    _mix_at(o, p, sr, 120, 0.3)
    return o


def _r_wait_ping(sr):
    return _tone(sr, 300, 1046.5, 0, tau=70)


def _r_load_start(sr):
    o = _N(sr, 340)
    _mix_at(o, _tone(sr, 150, 90, 60, tau=45), sr, 0)
    _mix_at(o, _noise(sr, 3, 51, hp=1500, tau=1), sr, 0)
    _mix_at(o, _ramp(_tone(sr, 220, 110, 220, harm=((1, 1), (2, 0.3)), edge=25), sr, 40), sr, 120, 0.4)
    return o


def _r_load_online(sr):
    o = _N(sr, 232)
    _mix_at(o, _tone(sr, 110, 659.25, 0, tau=60), sr, 0)
    _mix_at(o, _tone(sr, 110, 880, 0, tau=60), sr, 122)
    return o


def _r_power_on(sr):
    o = _N(sr, 450)
    _mix_at(o, _tone(sr, 150, 90, 60, tau=45), sr, 0)
    _mix_at(o, _noise(sr, 3, 61, hp=1500, tau=1), sr, 0)
    _mix_at(o, _ramp(_tone(sr, 260, 110, 220, harm=((1, 1), (2, 0.3)), edge=25), sr, 40), sr, 110, 0.4)
    _mix_at(o, _tone(sr, 60, 1760, 0, tau=15), sr, 388, 0.6)
    return o


def _r_power_off(sr):
    o = _N(sr, 600)
    _mix_at(o, _noise(sr, 3, 71, hp=1500, tau=1), sr, 0)
    _mix_at(o, _tone(sr, 596, 440, 55, tau=260, edge=8), sr, 4)
    return o


def _r_caution(sr):
    o = _N(sr, 320)
    p = _tone(sr, 130, 659.25, 0, harm=TRI)
    _mix_at(o, p, sr, 0)
    _mix_at(o, p, sr, 190)
    return o


RECIPES = {
    "route": _r_route, "route_back": _r_route_back, "denied": _r_denied, "affirm": _r_affirm, "hazard": _r_hazard,
    "transmit": _r_transmit, "transmit_blip": _r_transmit_blip, "lock_on": _r_lock_on, "abort": _r_abort,
    "landed": _r_landed, "wait_ping": _r_wait_ping, "load_start": _r_load_start, "load_online": _r_load_online,
    "power_on": _r_power_on, "power_off": _r_power_off, "caution": _r_caution,
}


# --- public API ---------------------------------------------------------------------------------------------
def source(name):
    """The recipe a cue name renders from (fault -> hazard). KeyError for an unknown name."""
    src = CUE_SOURCE.get(name, name)
    if src not in RECIPES:
        raise KeyError(name)
    return src


def render_float(name, sr=SAMPLE_RATE):
    """The lab's render(name): the recipe at `sr`, mean removed, peak normalised to PEAK dBFS, 1 ms edges.
    Returns a new array('f') (the lab's Float32Array, full scale 1.0) at volume 50."""
    src = source(name)
    sr = int(sr)
    if sr not in RATES:
        raise ValueError("unsupported sample rate %r" % (sr,))
    a = RECIPES[src](sr)
    mean = 0.0
    for x in a:                        # sequential double sum, as the lab's loop (not sum()/fsum())
        mean += x
    mean /= len(a)
    a = array.array("f", [x - mean for x in a])
    pk = 0.0
    for x in a:
        ax = abs(x)
        if ax > pk:
            pk = ax
    target = _pow(10, PEAK[src] / 20)
    g = target / pk if pk > 0 else 0
    a = array.array("f", [x * g for x in a])
    return _edges(a, sr, 1)


def volume_gain(volume):
    """The lab's master gain for a 0..100 volume: (v/50)**1.5, i.e. 30*log10(v/50) dB; 0 for 0, junk or NaN."""
    try:
        v = float(volume)
    except (TypeError, ValueError):
        return 0.0
    if not (v > 0):                    # also catches NaN
        return 0.0
    return (min(v, 100.0) / 50) ** 1.5


def quantise(floats, volume=50):
    """16-bit PCM (array('h')) of `floats` at `volume` (the trim is baked into the samples; nearest integer,
    halves to even, so the rounding is symmetric). Volume 0 gives all zeros."""
    scale = volume_gain(volume) * FULL_SCALE
    if scale <= 0:
        return array.array("h", bytes(2 * len(floats)))
    lim = FULL_SCALE
    pcm = [round(x * scale) for x in floats]
    if max(pcm, default=0) > lim or min(pcm, default=0) < -lim:      # cannot happen below -6 dBFS; belt and braces
        pcm = [max(-lim, min(lim, s)) for s in pcm]
    return array.array("h", pcm)


def wav_bytes(pcm, sr=SAMPLE_RATE):
    """A complete mono 16-bit PCM WAV image (44-byte header + data, little-endian) for PlaySoundW(SND_MEMORY)."""
    sr = int(sr)
    if sys.byteorder != "little":
        pcm = array.array("h", pcm)
        pcm.byteswap()
    data = pcm.tobytes()
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)
            + b"data" + struct.pack("<I", len(data)) + data)


def render_wav(name, volume=50, sr=SAMPLE_RATE):
    """render_float -> quantise -> wav_bytes in one call."""
    return wav_bytes(quantise(render_float(name, sr), volume), sr)


def render_all(sr=SAMPLE_RATE, names=NAMES, pause_s=0.002, stop=None):
    """{name: array('f')} for every name, each recipe rendered once (fault shares hazard's array; quantise never
    mutates it). Sleeps pause_s between recipes (the spec's mandatory yields, so a Tk thread keeps its beat) and
    returns None as soon as stop() is true between two recipes."""
    out = {}
    done = {}
    first = True
    for name in names:
        src = source(name)
        if src not in done:
            if not first and pause_s:
                time.sleep(pause_s)
            first = False
            if stop is not None and stop():
                return None
            done[src] = render_float(src, sr)
        out[name] = done[src]
    return out


# --- measurement helpers (the render contract; used by the tests and the engine's self-check) -------------
def peak_dbfs(pcm):
    """Peak of 16-bit samples in dBFS (re 32767); -inf for silence."""
    pk = max((abs(s) for s in pcm), default=0)
    return 20 * math.log10(pk / FULL_SCALE) if pk else float("-inf")


def dc_lsb(pcm):
    """Mean of 16-bit samples, in LSB."""
    return (sum(pcm) / len(pcm)) if len(pcm) else 0.0
