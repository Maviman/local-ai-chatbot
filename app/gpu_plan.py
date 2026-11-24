"""GPU discovery, engine compatibility and placement planning for the model server (stdlib only).

Nothing here imports server.py: everything that touches the outside world (running a program, reading the model's size) is passed
in, so the logic is tested with simulated devices. Nothing here names a particular GPU either - a "device" is whatever the inference
engine itself calls an accelerator ("CUDA0", "Vulkan1", ...), as listed by `llama-server --list-devices`, and every decision is made
from the numbers the engine and the driver report: memory, free memory, compute capability.

Why this exists. llama.cpp splits a model across every GPU it can see, in proportion to their memory, even when the whole model would
fit on one of them. Measured with the bundled build (b9860), a 6.8 GB model that fits one device was split 3.2 GB / 3.8 GB across
two; `--tensor-split 1,0` (or a single `--device`) puts it all on the first. And for a model too big for one device its own `--fit`
chooses a balanced split (-ts 34,31 for a 16.8 GB model on two equal devices), not "fill the first, spill the rest". The pipelined
layer split also makes every token pass through every device, so splitting early costs speed for nothing. So the program decides
the placement itself, the way a person would: use the best GPU alone if the model fits, and only when it does not, fill that GPU to
its capacity and put the remainder on the next one.

"Best" is the GPU with the most memory unless the user has ordered them otherwise. The engine's own `llama-fit-params` tool is the
oracle for "does it fit": `-fitp on` prints the memory it would allocate on each device for a given split (model, context, compute
buffers) in under a second, without loading anything.
"""
import mmap
import os
import re
import struct
import subprocess

# What llama.cpp leaves free on each device on its own (--fit-target, default 1024 MiB), and a little more of ours: the free-memory
# reading is a snapshot, and a few hundred MiB of drift between it and the load must not push layers onto the CPU.
DEFAULT_FIT_TARGET_MB = 1024
PLAN_SLACK_MB = 256
# --tensor-split shares are whole percent of the model: about two thirds of a layer on a 64-layer model, finer than a layer on any
# smaller one, and a search over it takes 7 probes.
SPLIT_RESOLUTION = 100

PLACEMENT_MODES = ("fill_first", "spread", "single")
DEFAULT_PLACEMENT = "fill_first"

_MiB = 1024 * 1024


# ---------------------------------------------------------------- what the engine and the driver print

# Real lines are under 200 characters and real machines have a handful of GPUs. The patterns below have a lazy `.+?` followed by
# `\s+`, which goes quadratic on a long run of spaces (32,000 of them: 2-4 s with the GIL held), so a longer line is never looked at;
# and a runaway listing cannot make a plan with thousands of devices.
MAX_TOOL_LINE = 500
MAX_DEVICES = 32

_DEVICE_LINE = re.compile(r"^\s*(?P<id>[A-Za-z][\w.\-]*):\s+(?P<name>.+?)\s+\((?P<total>\d+)\s*MiB,\s*(?P<free>\d+)\s*MiB free\)\s*$")


def parse_list_devices(text):
    """The devices in `llama-server --list-devices` output:
        Available devices:
          CUDA0: <name> (12199 MiB, 11036 MiB free)
    -> [{"id": "CUDA0", "name": ..., "total_mb": 12199, "free_mb": 11036}], in the engine's own order."""
    devices, seen = [], set()
    for line in (text or "").splitlines():
        m = _DEVICE_LINE.match(line) if len(line) <= MAX_TOOL_LINE else None
        if m and m["id"] not in seen:                       # an id names one device (it is what --device is given), so a repeat is ignored
            seen.add(m["id"])
            devices.append({"id": m["id"], "name": m["name"].strip(), "total_mb": int(m["total"]), "free_mb": int(m["free"])})
            if len(devices) >= MAX_DEVICES:
                break
    return devices


_SMI_LINE = re.compile(
    r"^\s*(?P<index>\d+)\s*,\s*(?P<name>.+?)\s*,\s*(?P<total>\d+)\s*,\s*(?P<free>\d+)\s*"
    r"(?:,\s*(?P<cc>\d+(?:\.\d+)?|\[?N/A\]?)\s*)?(?:,\s*(?P<uuid>\S+)\s*)?$"
)


def parse_nvidia_gpus(text):
    """`nvidia-smi --query-gpu=index,name,memory.total,memory.free[,compute_cap[,uuid]] --format=csv,noheader,nounits`
    -> [{"index", "name", "total_mb", "free_mb", "cc"}], cc as an int (6.1 -> 61, 12.0 -> 120) or None."""
    gpus = []
    for line in (text or "").splitlines():
        m = _SMI_LINE.match(line) if len(line) <= MAX_TOOL_LINE else None
        if not m:
            continue
        cc = None
        if m["cc"] and m["cc"][0].isdigit():
            cc = int(round(float(m["cc"]) * 10))
        gpus.append({"index": int(m["index"]), "name": m["name"].strip(), "total_mb": int(m["total"]), "free_mb": int(m["free"]), "cc": cc})
        if len(gpus) >= MAX_DEVICES:
            break
    return gpus


_FIT_MEMORY_ROW = re.compile(r"^\s*(?P<dev>\S+)\s+(?P<model>\d+)\s+(?P<context>\d+)\s+(?P<compute>\d+)\s*$")


def parse_fit_memory(text):
    """`llama-fit-params ... -fitp on` stdout: one row per device, "<device> <model> <context> <compute>" in MiB, plus a "Host" row
    for what stays in system memory -> {"CUDA0": {"model": 6956, "context": 799, "compute": 519}, "Host": {...}}."""
    rows = {}
    for line in (text or "").splitlines():
        m = _FIT_MEMORY_ROW.match(line) if len(line) <= MAX_TOOL_LINE else None
        if m:
            rows[m["dev"]] = {"model": int(m["model"]), "context": int(m["context"]), "compute": int(m["compute"])}
            if len(rows) > MAX_DEVICES:
                break
    return rows


def parse_fit_args(text):
    """`llama-fit-params ...` stdout: the arguments it would have the server run with, e.g. "-c 16384 -ngl 65 -ts 34,31" ->
    {"ngl": 65, "ts": [34.0, 31.0], "overrides": False}. "-ngl -1" means every layer fits on the devices."""
    line = ""
    for candidate in (text or "").splitlines():
        if candidate.strip().startswith("-"):
            line = candidate.strip()
    ngl = re.search(r"(?:^|\s)-ngl\s+(-?\d+)", line)
    ts = re.search(r"(?:^|\s)-ts\s+([\d.,]+)", line)
    return {
        "ngl": int(ngl.group(1)) if ngl else None,
        "ts": [float(x) for x in ts.group(1).split(",") if x] if ts else None,
        "overrides": bool(re.search(r"(?:^|\s)-ot\s", line)),
        "raw": line,
    }


def need_mb(rows, device_id):
    """What the engine says it would put on `device_id`: model + context + compute buffers, MiB (0 for a device that gets nothing)."""
    row = (rows or {}).get(device_id)
    return (row["model"] + row["context"] + row["compute"]) if row else 0


# ---------------------------------------------------------------- which GPUs an engine build can run on

# NVIDIA's fat-binary container: a 16-byte header (magic, version 1, header size 16, payload size) followed by entries, each a 64-byte
# header (kind 1 = PTX / 2 = SASS cubin, ..., the sm number at offset 28) and its payload. A CUDA DLL holds one container per source
# file. Walked here rather than asking the CUDA toolkit, which is not installed on the machines this runs on.
_FATBIN_MAGIC = struct.pack("<IHH", 0xBA55ED50, 1, 16)
_archs_cache = {}


def _scan_fatbins(path):
    sass, ptx, containers = set(), set(), 0
    with open(path, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            pos = 0
            while True:
                pos = mm.find(_FATBIN_MAGIC, pos)
                if pos < 0:
                    break
                pos += 1
                if pos + 16 > len(mm):
                    break
                fat_size = struct.unpack_from("<Q", mm, pos - 1 + 8)[0]
                start, end = pos - 1 + 16, pos - 1 + 16 + fat_size
                cur, entries, ok = start, [], end <= len(mm)
                while ok and cur < end:
                    if cur + 64 > len(mm):
                        ok = False
                        break
                    kind, _unknown, header_size, size = struct.unpack_from("<HHIQ", mm, cur)
                    arch = struct.unpack_from("<I", mm, cur + 28)[0]
                    if kind not in (1, 2) or not (32 <= header_size <= 256) or not (0 < arch < 1000):
                        ok = False
                        break
                    entries.append((kind, arch))
                    cur += header_size + size
                if ok and cur == end:
                    containers += 1
                    for kind, arch in entries:
                        (sass if kind == 2 else ptx).add(arch)
        finally:
            mm.close()
    if not containers:
        return None
    return {"sass": sorted(sass), "ptx": sorted(ptx), "containers": containers}


def cuda_archs(path):
    """The GPU architectures compiled into a ggml-cuda DLL: {"sass": [86, 89, 120], "ptx": [75, 80, 90]} (sm numbers: 86 = 8.6), or
    None when the file is missing or is not one this can read (so: do not filter anything). SASS runs only on the architecture it
    was built for (and later minor versions of the same major); PTX is portable text the driver compiles for any NEWER GPU. Cached
    per file (path, size, modification time): the scan reads the whole DLL, ~0.2 s when it is already in the file cache."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (os.path.abspath(path), st.st_size, st.st_mtime_ns)
    if key not in _archs_cache:
        try:
            _archs_cache[key] = _scan_fatbins(path)
        except (OSError, ValueError, struct.error):
            _archs_cache[key] = None
    return _archs_cache[key]


def arch_support(cc, archs):
    """(runs, why_not) for a GPU of compute capability `cc` (an int: 61 = 6.1) against an engine's compiled architectures. A build
    that was not compiled for a GPU's architecture - and has no PTX old enough for the driver to compile - cannot run kernels on
    it at all (CUDA's "no kernel image is available for execution on the device"), so such a GPU must not be handed any work."""
    if cc is None or not archs or not (archs.get("sass") or archs.get("ptx")):
        return True, ""       # an unknown GPU or an unreadable build: do not rule anything out on a guess
    major, minor = divmod(cc, 10)
    for a in archs.get("sass", ()):
        a_major, a_minor = divmod(a, 10)
        if a_major == major and a_minor <= minor:
            return True, ""
    for p in archs.get("ptx", ()):
        if p <= cc:
            return True, ""
    lowest = min(list(archs.get("sass", ())) + list(archs.get("ptx", ())) or [cc])
    return False, f"its compute capability is {major}.{minor}, older than anything this engine build was compiled for (lowest: {lowest // 10}.{lowest % 10})"


def needs_jit(cc, archs):
    """True when the build has no native code for a GPU of compute capability `cc` but does have PTX the driver can compile for it. It
    works, but the first load spends a while compiling the kernels (cached by the driver afterwards - if its cache is big enough, which
    its 256 MiB default often is not for a program of this size)."""
    if cc is None or not archs or not (archs.get("sass") or archs.get("ptx")):
        return False
    major, minor = divmod(cc, 10)
    if any(a // 10 == major and a % 10 <= minor for a in archs.get("sass", ())):
        return False
    return any(p <= cc for p in archs.get("ptx", ()))


# ---------------------------------------------------------------- engines: the llama.cpp builds that are installed

def discover_engines(bin_dir, exe_name="llama-server.exe"):
    """The engine builds on disk: `bin_dir` itself (named "default") and every `bin_dir/engines/<name>/` that holds a server
    executable, in that order. Several can coexist because one build cannot serve every GPU (a newer CUDA toolkit drops the oldest
    architectures, an older one lacks the newest); the program picks whichever runs on the GPUs the user has."""
    engines = []

    def entry(name, folder):
        exe = os.path.join(folder, exe_name)
        if not os.path.isfile(exe):
            return None
        fit = os.path.join(folder, "llama-fit-params.exe")
        cuda = os.path.join(folder, "ggml-cuda.dll")
        return {"name": name, "dir": folder, "exe": exe, "fit_exe": fit if os.path.isfile(fit) else None, "cuda_dll": cuda if os.path.isfile(cuda) else None}

    first = entry("default", bin_dir)
    if first:
        engines.append(first)
    sub = os.path.join(bin_dir, "engines")
    if os.path.isdir(sub):
        for name in sorted(os.listdir(sub), key=str.lower):
            found = entry(name if name != "default" else "default (engines)", os.path.join(sub, name))
            if found:
                engines.append(found)
    return engines


def choose_engine(engines, devices, preferred=None, devices_by_engine=None):
    """The engine that can run the most memory's worth of the given usable-or-not devices (dicts with total_mb, cc), the first
    listed on a tie - so the default build wins whenever it is good enough. `preferred` (a name from the Settings) wins outright
    when it is installed. Returns (engine or None, {engine name: [device ids it can run]}).

    `devices_by_engine` ({engine name: the devices THAT build lists}) judges each build on what it sees itself, which matters because
    builds differ in what they enumerate: one built for a newer CUDA may not list a GPU that an older build lists and can run (it
    would then be scored on a list the GPU was never in, and the first build would win the tie). Without it every build is judged on
    the one list `devices`."""
    support = {}
    own = {}
    for e in engines:
        archs = cuda_archs(e["cuda_dll"]) if e.get("cuda_dll") else None
        own[e["name"]] = devices_by_engine.get(e["name"], []) if devices_by_engine is not None else devices
        support[e["name"]] = [d["id"] for d in own[e["name"]] if arch_support(d.get("cc"), archs)[0]]
    if preferred and preferred != "auto":
        for e in engines:
            if e["name"] == preferred:
                return e, support
    best, best_score = None, -1
    for e in engines:
        ids = set(support[e["name"]])
        score = sum(d["total_mb"] for d in own[e["name"]] if d["id"] in ids)
        if score > best_score:
            best, best_score = e, score
    return best, support


# ---------------------------------------------------------------- the user's GPUs, in the user's order

def match_driver_gpus(devices, smi_gpus):
    """{device id: the driver's record (index, name, total_mb, free_mb, cc) of the same GPU}, for the engine's CUDA devices the driver
    knows. Matched by name: the k-th CUDA device of a name, in the engine's order, with the k-th GPU of that name in the driver's (two
    identical cards cannot be told apart otherwise; unequal ones always can). Devices of any other backend (Vulkan, a remote RPC one)
    have no match: only the engine knows them."""
    by_name = {}
    for g in sorted(smi_gpus or (), key=lambda g: g.get("index", 0)):
        by_name.setdefault(g["name"], []).append(g)
    taken, out = {}, {}
    for d in sorted((d for d in devices if re.fullmatch(r"CUDA\d+", d["id"])), key=lambda d: int(d["id"][4:])):
        k = taken.get(d["name"], 0)
        taken[d["name"]] = k + 1
        if k < len(by_name.get(d["name"], ())):
            out[d["id"]] = by_name[d["name"]][k]
    return out


def gpu_key(name, total_mb):
    """A stable name for a GPU in the saved settings. The engine's device id ("CUDA1") can change when a card is added or the
    driver reorders them; the name and the memory (rounded to GiB, so a driver's reservation changing by a few MiB does not
    matter) identify the card."""
    return f"{name}|{max(1, round(total_mb / 1024))}G"


def build_inventory(engine_devices, smi_gpus=(), archs=None, order=None, disabled=None):
    """The devices the engine sees, annotated and in priority order:
        id, key, name, total_mb, free_mb, cc (int|None), supported, reason (why not), jit (no native code in this build: the driver
        compiles the kernels on the first load), enabled, usable, position (1-based), label, priority (0-based rank in the user's order
        / by memory before support is weighed - `position` is after it).
    Priority order = the user's `order` (a list of keys) first, then the rest by memory, largest first; ties keep the engine's own
    order; devices the engine build cannot run come last whatever their size. A device is `usable` when it is enabled and the engine
    can run on it."""
    # (settings.json can hold anything - a hand edit, an old version - so a value that is not a list is no list at all)
    order = [k for k in (order if isinstance(order, (list, tuple)) else ()) if isinstance(k, str)]
    disabled = {k for k in (disabled if isinstance(disabled, (list, tuple)) else ()) if isinstance(k, str)}
    cc_of = {}
    for g in smi_gpus or ():
        cc_of.setdefault(g["name"], g.get("cc"))
    seen = {}
    devices = []
    for i, d in enumerate(engine_devices):
        base = gpu_key(d["name"], d["total_mb"])
        seen[base] = seen.get(base, 0) + 1
        key = base if seen[base] == 1 else f"{base}#{seen[base]}"
        cc = cc_of.get(d["name"])
        supported, reason = arch_support(cc, archs)
        devices.append(dict(d, key=key, cc=cc, supported=supported, reason=reason, enabled=key not in disabled, jit=bool(supported and needs_jit(cc, archs)), _engine_index=i))
    rank = {k: i for i, k in enumerate(order)}
    devices.sort(key=lambda d: (0, rank[d["key"]]) if d["key"] in rank else (1, -d["total_mb"], d["_engine_index"]))
    for i, d in enumerate(devices):
        d["priority"] = i       # the user's order (else biggest first) before any engine's support is weighed: the image engine, a different build, follows this
    # A GPU the engine build cannot run is not in the race: it goes to the end (stable, so the rest keep their order), so that "GPU 1"
    # is always the first GPU that can actually be used - the one a plan names - and not a big card that is skipped.
    devices.sort(key=lambda d: not d["supported"])
    for position, d in enumerate(devices, start=1):
        d["position"] = position
        d["label"] = f"GPU {position}"
        d["usable"] = bool(d["enabled"] and d["supported"])
        del d["_engine_index"]
    return devices


# ---------------------------------------------------------------- the plan

def _describe(devices_used, shares, fits, overflow, mode):
    names = [f"{d['label']} ({d['name']})" for d in devices_used]
    if not devices_used:
        return "No GPU is used: every GPU is turned off or unsupported by the engine, so the model runs on the CPU."
    if len(devices_used) == 1:
        if overflow:
            return f"{names[0]} is the only GPU used and the model does not fit in it: the rest runs on the CPU."
        why = "the whole model fits in it" if mode == "fill_first" else "that is the setting"
        return f"Only {names[0]} is used - {why}."
    if mode == "spread":
        return "The model is spread across " + ", ".join(names) + " in proportion to their free memory."
    parts = [f"{d['label']} {s}%" for d, s in zip(devices_used, shares)]
    tail = " The model is larger than the GPUs together: the rest runs on the CPU." if overflow else ""
    return f"{names[0]} is filled first, then " + ", then ".join(names[1:]) + f" ({', '.join(parts)} of the model)." + tail


def plan_placement(devices, mode, probe, extra_first_mb=0, fit_target_mb=DEFAULT_FIT_TARGET_MB, slack_mb=PLAN_SLACK_MB):
    """Which devices the model goes on, and in what proportions. Never raises: whatever a probe does (answer None, raise, return
    something that is not a table of numbers), the plan falls back to "the first device, sizes guessed" - the engine's own --fit then
    decides how much of the model that device holds - and `note` says what went wrong.

    devices   usable devices, in priority order (dicts with id, name, label, total_mb, free_mb)
    mode      "fill_first" (default: the first device alone if the model fits, else fill it and spill to the next),
              "spread" (every device, split by the engine in proportion to free memory - the engine's own default),
              "single" (the first device only)
    probe     probe(device_ids, shares) -> {device_id: {"model", "context", "compute"} MiB, ...} | None
              what the engine would allocate on each device for that split (shares: whole percents, or None for "one device");
              None means the tool could not answer
    extra_first_mb  memory the estimate does not include and the first device must also hold (a vision projector)

    Returns {"mode", "device_ids", "shares" (percent per used device, or None), "fits" (True / False / None = could not tell),
    "overflow" (part of the model must stay on the CPU), "devices" ([{id, label, name, need_mb, cap_mb, share}]), "reason",
    "probes", "estimated" (True when the tool could not answer and sizes were guessed)}."""
    try:
        return _plan_placement(devices, mode, probe, extra_first_mb, fit_target_mb, slack_mb)
    except Exception as e:  # noqa: BLE001 - a plan is an improvement, never a reason for the model not to start
        used = list(devices or ())[:1]
        plan = {"mode": mode, "device_ids": [d.get("id") for d in used], "shares": None, "fits": None, "overflow": False, "probes": 0, "estimated": True,
                "devices": [{"id": d.get("id"), "label": d.get("label", ""), "name": d.get("name", ""), "cap_mb": None, "share": 100, "need_mb": None} for d in used],
                "note": f"planning failed: {e!r}"[:300]}
        plan["reason"] = _describe(used, None, None, False, mode) if all(k in d for d in used for k in ("label", "name")) else "The first GPU is used."
        return plan


def _is_memory_table(rows, ids, shares):
    """Whether a probe's answer is usable: a non-empty table of {"model", "context", "compute"} whole numbers per device that has a row for
    every device that was given a share (a missing row would read as "needs nothing" and make anything fit). Anything else is no answer."""
    if not isinstance(rows, dict) or not rows:
        return False
    for r in rows.values():
        if not isinstance(r, dict) or not all(isinstance(r.get(k), int) and not isinstance(r.get(k), bool) for k in ("model", "context", "compute")):
            return False
    given = shares or [100] + [0] * (len(ids) - 1)
    return all(d in rows for d, s in zip(ids, given) if s)


def _plan_placement(devices, mode, probe, extra_first_mb, fit_target_mb, slack_mb):
    plan = {"mode": mode, "device_ids": [], "shares": None, "fits": None, "overflow": False, "devices": [], "reason": "", "probes": 0, "estimated": False}
    if not devices:
        plan["reason"] = _describe([], None, None, False, mode)
        return plan
    # Two capacities per device. `hard` is what the engine itself would accept (free memory less its own --fit-target): that is the
    # test for "does the whole model fit on this GPU alone", because splitting is the thing to avoid unless it is really needed.
    # `soft` leaves a little more free (the slack) and is what a GPU is FILLED to once a split is needed anyway: free memory is a
    # snapshot, and a few hundred MiB of drift between it and the load should not push layers onto the CPU.
    hard = {d["id"]: max(0, d["free_mb"] - fit_target_mb) for d in devices}
    soft = {d["id"]: max(0, hard[d["id"]] - slack_mb) for d in devices}
    cache = {}

    def ask(ids, shares):
        key = (tuple(ids), tuple(shares) if shares else None)
        if key not in cache:
            plan["probes"] += 1
            try:
                rows = probe(list(ids), list(shares) if shares else None)
            except Exception:  # noqa: BLE001 - the tool failing is "could not tell", like any other failed answer
                rows = None
            cache[key] = rows if _is_memory_table(rows, ids, shares) else None
        return cache[key]

    def first_need(rows):
        return need_mb(rows, devices[0]["id"]) + extra_first_mb

    def finish(used, shares, fits, overflow, rows=None, estimated=False):
        plan.update(device_ids=[d["id"] for d in used], shares=list(shares) if shares and len(used) > 1 else None, fits=fits, overflow=overflow, estimated=estimated)
        plan["devices"] = [
            {"id": d["id"], "label": d["label"], "name": d["name"], "cap_mb": hard[d["id"]], "share": (shares[i] if shares else 100),
             "need_mb": (need_mb(rows, d["id"]) + (extra_first_mb if d is devices[0] else 0)) if rows else None}
            for i, d in enumerate(used)
        ]
        plan["reason"] = _describe(used, shares, fits, overflow, mode)
        return plan

    if mode == "single" or (mode != "spread" and len(devices) == 1):
        rows = ask([devices[0]["id"]], None)
        if rows is None:
            return finish(devices[:1], None, None, False, estimated=True)
        need = first_need(rows)
        fit = need <= hard[devices[0]["id"]]
        return finish(devices[:1], None, fit, not fit, rows)
    if mode == "spread":
        return finish(devices, None, None, False)

    # fill_first with two or more devices. First: does everything fit on the first one?
    ids = [d["id"] for d in devices]
    rows = ask([ids[0]], None)
    if rows is None:
        return finish(devices[:1], None, None, False, estimated=True)
    if first_need(rows) <= hard[ids[0]]:
        return finish(devices[:1], None, True, False, rows)

    # No. Fill each device in turn with as much of the model as it can hold (a binary search over its share, one probe per step),
    # and hand the rest to the next. A probe returns every device's memory at once, so the last device is checked for free.
    n = len(devices)
    shares = [0] * n
    remaining = SPLIT_RESOLUTION
    final_rows = None
    for i in range(n):
        if remaining == 0:
            break
        last = i == n - 1

        def split(share):
            # this device takes `share`, the next one the rest of what is left, nothing for the devices after it
            return shares[:i] + [share] if last else shares[:i] + [share, remaining - share] + [0] * (n - i - 2)

        def holds(share):
            r = ask(ids[: i + 1] if last else ids, split(share))
            if r is None:
                return None
            # the last device is the last resort, so it may be filled to the engine's own limit; the others keep the slack
            limit = hard[ids[i]] if last else soft[ids[i]]
            return need_mb(r, ids[i]) + (extra_first_mb if i == 0 else 0) <= limit, r

        if i > 0 or last:                              # (the first device was already shown not to hold everything)
            everything = holds(remaining)
            if everything is None:
                return finish(devices[:1], None, None, False, estimated=True)
            if everything[0]:
                shares[i], remaining, final_rows = remaining, 0, everything[1]
                break
        if last:
            break                                      # the last device cannot take the rest either: the model is bigger than the GPUs
        lo, hi = 0, remaining - 1                      # largest share of what is left that still fits (0 always does)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            ok = holds(mid)
            if ok is None:
                return finish(devices[:1], None, None, False, estimated=True)
            if ok[0]:
                lo = mid
            else:
                hi = mid - 1
        shares[i] = lo
        remaining -= lo

    if remaining > 0:
        # Everything is full and some of the model is left over: the GPUs together are too small. Fill them all, in proportion to
        # what each can hold; the engine's own --fit moves the rest to the CPU when it starts.
        total = sum(hard.values())
        if total <= 0:
            return finish(devices[:1], None, False, True, rows)
        shares = [int(SPLIT_RESOLUTION * hard[d["id"]] / total) for d in devices]
        shares[0] += SPLIT_RESOLUTION - sum(shares)
        used = [d for d, s in zip(devices, shares) if s > 0]
        used_shares = [s for s in shares if s > 0]
        return finish(used, used_shares, False, True, ask(ids, shares))
    used = [d for d, s in zip(devices, shares) if s > 0]
    used_shares = [s for s in shares if s > 0]
    return finish(used, used_shares, True, False, final_rows)


def launch_args(plan, devices_visible=0):
    """The llama-server arguments that carry out a plan. A plan with one device that is also the only device the engine sees adds
    nothing (the command stays exactly what it was before this existed); no usable device at all means `--device none`."""
    if plan is None:
        return []
    ids = plan.get("device_ids") or []
    if not ids:
        return ["--device", "none"]
    if len(ids) == 1 and devices_visible == 1:
        return []
    args = ["--device", ",".join(ids)]
    if plan.get("shares") and len(ids) > 1:
        args += ["--tensor-split", ",".join(str(s) for s in plan["shares"])]
    return args


# ---------------------------------------------------------------- running the engine's own tools

def run_tool(argv, timeout):
    """(returncode, stdout, stderr) of a program, or None if it could not be run or took too long. No console window flashes up."""
    try:
        done = subprocess.run(
            argv, capture_output=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.returncode, done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace")


def list_engine_devices(server_exe, extra_args=(), runner=run_tool, timeout=20):
    """The devices an engine build sees (`<server> --list-devices`, ~0.3 s), in its order; [] when it sees none, None when it could
    not be asked (no such file, an older build without the option). `extra_args` are passed through (the engine's own --rpc, ...)."""
    out = runner([server_exe, *extra_args, "--list-devices"], timeout)
    if not out:
        return None
    return parse_list_devices(out[1] + "\n" + out[2])


def query_smi_gpus(runner=run_tool, timeout=6):
    """The NVIDIA driver's own view of the GPUs - memory, free memory and compute capability - or [] without an NVIDIA driver.
    Drivers too old to know `compute_cap` are asked again without it."""
    for fields in ("index,name,memory.total,memory.free,compute_cap", "index,name,memory.total,memory.free"):
        out = runner(["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"], timeout)
        if out and out[0] == 0 and "not a valid field" not in out[1]:
            gpus = parse_nvidia_gpus(out[1])
            if gpus:
                return gpus
    return []


def make_fit_probe(fit_exe, model_path, shape_args, extra_args=(), runner=run_tool, timeout=45):
    """The `probe` plan_placement() needs, built on the engine's own `llama-fit-params`: what it would allocate on each device for a
    given split, as the server would be started - `shape_args` are the arguments that decide the sizes (context, cache type, batch
    sizes, flash attention, KV offload), the same ones the real launch gets. Reads the model's metadata only: under a second and no
    GPU memory (measured 0.6-0.9 s on a 20 GB model)."""
    def probe(device_ids, shares):
        argv = [fit_exe, "-m", model_path, *shape_args, *extra_args, "--device", ",".join(device_ids)]
        if shares and len(device_ids) > 1:
            argv += ["-ts", ",".join(str(s) for s in shares)]
        argv += ["-fitp", "on"]
        out = runner(argv, timeout)
        if not out or out[0] != 0:
            return None
        rows = parse_fit_memory(out[1])
        return rows if any(name != "Host" for name in rows) else None
    return probe
