r"""Why does Personal Terminal not start on THIS PC? One run, one report. (Stdlib only; run with the bundled interpreter.)

    Diagnose.bat                                     the usual way: double-click, then send data\diagnostics-<stamp>.txt
    runtime\python\python.exe app\diagnose.py [--no-start-test] [--start-timeout S] [--model NAME.gguf] [--verify-engines]
                                              [--quick] [--root FOLDER] [--out FILE]
    runtime\python\python.exe app\diagnose.py --make-manifest       (developer: record this folder's files as the known-good set)

What it looks at, in order - each part stands alone, so one that fails leaves the rest intact:
  1. the installation's FILES against app\diagnose_manifest.json, the record of a working copy (size and SHA-256 of every file
     under runtime\, bin\ and app\, the launcher and the .bat files; models by size): a file that is missing, truncated, zero-filled
     or otherwise not the one that works is named. This is what "python3.dll is either not designed to run on Windows or it
     contains an error (0xC000012F)" and "could not find the app's runtime files" come down to: a damaged or incomplete copy.
     Every .exe/.dll/.pyd is also checked for a valid program header, every .py compiled, every .gguf checked for its magic;
  2. this PC (Windows, memory, CPU, how long since the last restart, Smart App Control, Defender's controlled folders) and the
     folder it runs from (drive type, file system, free space, mark-of-the-web, can it be written to, can the database be opened);
  3. the bundled Python: does Tk come up, do the app's own modules import;
  4. copies already running: the single-instance lock, the app's processes, who holds ports 8790/8791/8793/8794 - a copy
     stuck from an earlier launch (even one started from ANOTHER drive) makes every later launch exit silently;
  5. the NVIDIA driver and GPUs (nvidia-smi, the driver's DLL, Device Manager's status codes, driver crashes in the System log);
  6. every engine build (bin\ and bin\engines\*): the DLLs it needs, the GPU architectures it was compiled for, whether it runs at
     all (--version) and which GPUs it sees (--list-devices), each GPU's verdict per build (native / compiled on first load / cannot);
  7. a REAL start of the model server on a spare port with a small context (the thing that fails with "WinError 10061"): does it
     come up, does it answer a prompt, and if it dies - the exit code and its log, decoded. If the GPU run fails, a CPU-only run
     of the same build tells the driver/GPU apart from everything else. Skipped with --no-start-test (minutes on a hard disk);
  8. the app's own logs, and the Windows event log entries about the app's programs (crashes, Defender detections).
Nothing is changed, stopped or deleted: the only processes it starts are its own test runs, which it ends itself.
"""
import ctypes
import datetime
import fnmatch
import glob
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

MANIFEST_NAME = "diagnose_manifest.json"
PORTS = (("the app", 8790), ("the model server", 8791), ("the image server", 8793), ("the embedding server", 8794))
MUTEX_NAME = "Local\\LocalAIChatBot-single-instance"
HASH_LIMIT = 8 * 1024 * 1024           # bigger files are checked by size and program header only, unless --verify-engines
ROOT_FILES = ("Local AI ChatBot.exe", "Start Chatbot.bat", "Stop Chatbot.bat", "Diagnose.bat", "README.md")
APP_PROCESS_NAMES = ("pythonw.exe", "python.exe", "llama-server.exe", "sd-server.exe", "ggml-rpc-server.exe", "Local AI ChatBot.exe")
SKIP_DIRS = ("__pycache__",)
MIN_DRIVER_FOR_CUDA13 = 580             # the default build's CUDA 13 runtime needs the R580 driver or newer

# Windows exit codes of a program that died at once (NTSTATUS values; a negative returncode is the same number seen as signed).
EXIT_CODES = {
    0xC0000135: "STATUS_DLL_NOT_FOUND - a DLL it needs is missing",
    0xC000007B: "STATUS_INVALID_IMAGE_FORMAT - a DLL is the wrong kind (32/64-bit) or damaged",
    0xC000012F: "STATUS_INVALID_IMAGE_NOT_MZ - a program file is damaged (not even a valid program header)",
    0xC0000142: "STATUS_DLL_INIT_FAILED - a DLL failed to initialise",
    0xC000001D: "STATUS_ILLEGAL_INSTRUCTION - the CPU lacks an instruction this build uses",
    0xC0000005: "STATUS_ACCESS_VIOLATION - it crashed",
    0xC0000409: "STATUS_STACK_BUFFER_OVERRUN / fail-fast - it stopped on an internal error (a CUDA error often ends this way)",
    0xC0000022: "STATUS_ACCESS_DENIED",
    0xC06D007E: "a delay-loaded DLL was not found",
    0xC06D007F: "a function in a delay-loaded DLL was not found",
    0xE06D7363: "an unhandled C++ exception",
}
# OSError.winerror when a program cannot even be started
WINERRORS = {
    2: "file not found", 3: "path not found",
    5: "access denied - something refuses to run it (an antivirus, Smart App Control, a policy) or the file's permissions",
    193: "not a valid Windows program (damaged, or a 32/64-bit mismatch)",
    225: "Windows Defender (or another antivirus) says the file contains a virus or unwanted software and blocks it",
    1260: "blocked by a group policy / AppLocker rule",
}
DEVICE_MANAGER_CODES = {
    10: "the device cannot start", 12: "not enough resources (a Tesla card often needs 'Above 4G decoding' in the BIOS)",
    22: "the device is disabled", 28: "no driver installed", 31: "Windows cannot load its driver", 43: "the device reported a problem (the driver stopped it)",
    45: "not connected", 48: "the driver is blocked",
}
# What the end of a model server's log usually means (first match wins).
FAILURE_PATTERNS = (
    (r"no kernel image is available|invalid device function|unsupported gpu architecture",
     "a GPU in this PC is too old for this engine build (it has no code for it) - the cuda12 build under bin\\engines\\ covers Pascal cards such as the Tesla P40"),
    (r"CUDA driver version is insufficient|failed to initialize CUDA|cudaGetDeviceCount|no CUDA-capable device|CUDA driver .{0,40}too old",
     "the NVIDIA driver is too old for this engine build (the default build's CUDA 13 runtime needs driver 580 or newer), or it sees no CUDA GPU"),
    (r"out of memory|cudaMalloc failed|failed to allocate .{0,40}(CUDA|buffer)|unable to allocate", "the GPU ran out of memory"),
    (r"couldn't bind HTTP server socket|address already in use|Only one usage of each socket address", "the port is already in use"),
    (r"unknown model architecture|failed to load model|error loading model|llama_model_load: error|invalid magic",
     "the engine could not read this model file (a damaged or incomplete copy, or a model too new for this build)"),
)

# Set by set_root(): the installation being looked at (normally the one this file is part of).
ROOT = DATA_DIR = BIN_DIR = MODELS_DIR = RUNTIME_DIR = MANIFEST_PATH = PY_EXE = None


def set_root(root):
    global ROOT, DATA_DIR, BIN_DIR, MODELS_DIR, RUNTIME_DIR, MANIFEST_PATH, PY_EXE
    ROOT = os.path.abspath(root)
    DATA_DIR = os.path.join(ROOT, "data")
    BIN_DIR = os.path.join(ROOT, "bin")
    MODELS_DIR = os.path.join(ROOT, "models")
    RUNTIME_DIR = os.path.join(ROOT, "runtime", "python")
    MANIFEST_PATH = os.path.join(ROOT, "app", MANIFEST_NAME)
    exe = os.path.join(RUNTIME_DIR, "python.exe")
    PY_EXE = exe if os.path.isfile(exe) else None


set_root(os.path.dirname(APP_DIR))


# ---------------------------------------------------------------------------------------------------- small tools

class Report:
    """Lines go to the console and to the report file as they are made, so a run that dies half-way still leaves what it found."""

    def __init__(self, path):
        self.path = path
        self.flags = []           # things that are wrong (shown again in the summary)
        self.notes = []           # things worth knowing that are not wrong by themselves
        self._f = open(path, "w", encoding="utf-8", buffering=1)

    def line(self, text=""):
        for part in (str(text).splitlines() or [""]):
            self._f.write(part + "\n")
            try:
                print(part)
            except Exception:
                pass

    def section(self, title):
        self.line()
        self.line("=" * 100)
        self.line(title)
        self.line("=" * 100)

    def flag(self, text):
        self.flags.append(text)
        self.line("!! " + text)

    def note(self, text):
        self.notes.append(text)
        self.line("-- " + text)

    def failed(self, what):
        last = traceback.format_exc().strip().splitlines()[-1]
        self.line(f"(the {what} check itself could not be completed: {last})")

    def close(self):
        self._f.close()


def hexcode(code):
    """'0xC0000135 (STATUS_DLL_NOT_FOUND - ...)' for a returncode, however Python reports it."""
    if not isinstance(code, int):
        return str(code)
    unsigned = code & 0xFFFFFFFF
    meaning = EXIT_CODES.get(unsigned)
    text = f"0x{unsigned:08X}" if unsigned > 0xFFFF else str(code)
    return f"{text} ({meaning})" if meaning else text


def run(argv, timeout, cwd=None, env=None, hidden=True):
    """{'code', 'out', 'err', 'error', 'seconds'} of a program. 'error' says why it could not be run or that it took too long."""
    t = time.time()
    result = {"code": None, "out": "", "err": "", "error": None, "seconds": 0.0}
    try:
        done = subprocess.run(argv, capture_output=True, timeout=timeout, cwd=cwd, env=env,
                              creationflags=subprocess.CREATE_NO_WINDOW if (hidden and os.name == "nt") else 0)
        result["code"] = done.returncode
        result["out"] = done.stdout.decode("utf-8", "replace")
        result["err"] = done.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        result["error"] = f"still running after {timeout} s - ended"
        result["out"] = (e.stdout or b"").decode("utf-8", "replace")
        result["err"] = (e.stderr or b"").decode("utf-8", "replace")
    except OSError as e:
        why = WINERRORS.get(getattr(e, "winerror", None))
        result["error"] = f"cannot be started: {e}" + (f" => {why}" if why else "")
    except Exception as e:
        result["error"] = f"cannot be started: {type(e).__name__}: {e}"
    result["seconds"] = time.time() - t
    return result


def ps(script, timeout=90):
    """A PowerShell snippet's output as text, or a line saying why there is none."""
    res = run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script], timeout)
    if res["error"]:
        return f"(PowerShell {res['error']})"
    text = (res["out"] or "").strip()
    err = (res["err"] or "").strip()
    if not text:
        return "(nothing reported)" + (f" - PowerShell said: {err[:300]}" if err else "")
    return text


def tail(path, n):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as e:
        return [f"(cannot read {path}: {e})"]
    return lines[-n:]


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def pe_header_ok(path):
    """(True, '') when the file has a valid Windows program header (MZ ... PE\\0\\0), else (False, what is wrong)."""
    try:
        with open(path, "rb") as f:
            head = f.read(0x40)
            if len(head) < 0x40:
                return False, f"only {len(head)} bytes"
            if head[:2] != b"MZ":
                return False, "no MZ signature" + (" (the start of the file is all zeros)" if head == bytes(len(head)) else "")
            e_lfanew = struct.unpack_from("<I", head, 0x3C)[0]
            if e_lfanew > 4096:
                return False, "bad PE offset"
            f.seek(e_lfanew)
            if f.read(4) != b"PE\0\0":
                return False, "no PE signature"
        return True, ""
    except OSError as e:
        return False, f"cannot read: {e}"


def file_version(path):
    """'32.0.15.8097' of a Windows program or DLL, or None."""
    try:
        ver = ctypes.WinDLL("version")
        ver.GetFileVersionInfoSizeW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
        ver.GetFileVersionInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        ver.VerQueryValueW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint)]
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return None
        ptr, length = ctypes.c_void_p(), ctypes.c_uint()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)) or length.value < 16:
            return None
        _sig, _sv, ms, ls = struct.unpack("<IIII", ctypes.string_at(ptr.value, 16))
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        return None


def nvidia_driver_from_file_version(v):
    """The NVIDIA driver release ('580.97') hidden in nvcuda.dll's file version ('32.0.15.8097'), or None."""
    try:
        parts = v.split(".")
        digits = f"{int(parts[2]):02d}{int(parts[3]):04d}"
        return f"{digits[1:4]}.{digits[4:]}"
    except Exception:
        return None


def port_open(port, timeout=0.5):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=timeout).close()
        return True
    except OSError:
        return False


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def listening_pid(port, netstat_text):
    """The process listening on a local TCP port, from `netstat -ano -p TCP` output (any Windows language: the state's NAME is not read)."""
    for line in netstat_text.splitlines():
        parts = line.split()
        if (len(parts) >= 5 and parts[0] == "TCP" and parts[1].startswith(("127.0.0.1:", "0.0.0.0:", "[::1]:", "[::]:"))
                and parts[1].rsplit(":", 1)[-1] == str(port) and parts[2].rsplit(":", 1)[-1] == "0" and parts[-1].isdigit()):
            return int(parts[-1])
    return None


def process_image_path(pid):
    try:
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32")
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k.OpenProcess(0x1000, False, int(pid))
        if not h:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wintypes.DWORD(1024)
            return buf.value if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else None
        finally:
            k.CloseHandle(h)
    except Exception:
        return None


def relpath(path):
    return os.path.relpath(path, ROOT).replace("\\", "/")


def zone_identifier(path):
    """The mark-of-the-web zone of a file ('3' = came from the internet), '' when there is none, None when the file system has no such thing."""
    try:
        with open(path + ":Zone.Identifier", "r", encoding="utf-8", errors="replace") as f:
            m = re.search(r"ZoneId\s*=\s*(\d+)", f.read())
            return m.group(1) if m else "?"
    except FileNotFoundError:
        return ""
    except OSError:
        return None


def walk_files(base):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for f in sorted(filenames):
            if not f.endswith(".pyc"):
                yield os.path.join(dirpath, f)


def optional_modules():
    """The app's own helpers when this copy has them (an older copy does not): pe_imports (which DLLs a program needs) and gpu_plan."""
    mods = {}
    for name in ("pe_imports", "gpu_plan"):
        try:
            mods[name] = __import__(name)
        except Exception:
            mods[name] = None
    return mods


# ---------------------------------------------------------------------------------------------------- the manifest

def make_manifest(out_path=None):
    """Records this folder's files - the known-good set - in app\\diagnose_manifest.json. Everything under runtime\\, bin\\ and app\\
    plus the launcher and the .bat files: size and SHA-256 (files over HASH_LIMIT: SHA-256 too, read once); models\\**\\*.gguf by size
    (they differ per PC, so a model that is not here is fine, one that is here with another size is a bad copy)."""
    out_path = out_path or MANIFEST_PATH
    files, optional = {}, {}
    for base in (RUNTIME_DIR, BIN_DIR, os.path.join(ROOT, "app")):
        if not os.path.isdir(base):
            continue
        for path in walk_files(base):
            if os.path.abspath(path) == os.path.abspath(out_path):
                continue
            st = os.stat(path)
            files[relpath(path)] = {"size": st.st_size, "sha256": sha256_of(path)}
    for name in ROOT_FILES:
        path = os.path.join(ROOT, name)
        if os.path.isfile(path):
            files[name] = {"size": os.path.getsize(path), "sha256": sha256_of(path)}
    if os.path.isdir(MODELS_DIR):
        for path in walk_files(MODELS_DIR):
            if path.lower().endswith(".gguf"):
                optional[relpath(path)] = {"size": os.path.getsize(path)}
    # No timestamp inside: the same folder gives the same file (the packager remakes it on every build), and the file's own date says when.
    manifest = {
        "made_on": platform.node(),
        "python": platform.python_version(),
        "files": files,
        "optional": optional,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
    return manifest


def load_manifest():
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            m = json.load(f)
        if isinstance(m, dict) and isinstance(m.get("files"), dict):
            return m
    except (OSError, ValueError):
        pass
    return None


# ---------------------------------------------------------------------------------------------------- the sections

def sec_header(r, args):
    r.line(f"Personal Terminal diagnostics - {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    r.line(f"Report: {r.path}")
    r.line(f"Installation folder: {ROOT}")
    r.line(f"Run with: {sys.executable} ({platform.python_version()})" + ("" if PY_EXE else "  [this folder has no runtime\\python\\python.exe]"))
    r.line(f"Options: {' '.join(args) or '(none)'}")
    r.line("Send the whole of this file. Lines starting with !! are problems, lines starting with -- are worth knowing.")


def sec_integrity(r, verify_engines):
    r.section("1. The installation's files (against the record of a working copy)")
    manifest = load_manifest()
    present_tops = {name: os.path.isdir(os.path.join(ROOT, name)) for name in ("app", "bin", "runtime", "models", "data")}
    r.line("Folders: " + ", ".join(f"{k} {'present' if v else 'MISSING'}" for k, v in present_tops.items()))
    how_to = "HOW_TO_UPDATE.txt"                 # made by the packager: its presence says the folder came from an update package
    for name in ROOT_FILES + (how_to,):
        path = os.path.join(ROOT, name)
        r.line(f"  {name}: " + (f"{os.path.getsize(path):,} bytes" if os.path.isfile(path) else "missing"))
    if os.path.isfile(os.path.join(ROOT, how_to)) and not (present_tops["runtime"] and present_tops["bin"]):
        r.flag("This folder holds HOW_TO_UPDATE.txt but no runtime\\ or bin\\: it looks like an UPDATE PACKAGE extracted on its own. An update "
               "package carries only the app's code - it has to be extracted over a complete installation (runtime, bin, models), never into an empty folder.")
    elif not present_tops["runtime"]:
        r.flag("runtime\\ is missing: the bundled Python is not here, so nothing can start. Copy the whole runtime\\ folder from a working installation.")
    if not present_tops["bin"]:
        r.flag("bin\\ is missing: no engine (llama-server.exe), so no model can ever load. Copy bin\\ from a working installation.")
    if not present_tops["models"]:
        r.flag("models\\ is missing: there is no model to load.")

    if manifest is None:
        r.note(f"No app\\{MANIFEST_NAME} here (an older copy, or it was not carried over), so files cannot be compared with a working copy; "
               "only the generic checks below run. Copy that file from an up-to-date installation next time, or make one there with "
               "'runtime\\python\\python.exe app\\diagnose.py --make-manifest'.")
    else:
        try:
            manifest_time = os.path.getmtime(MANIFEST_PATH)
        except OSError:
            manifest_time = 0.0
        r.line(f"Manifest: app\\{MANIFEST_NAME} dated {datetime.datetime.fromtimestamp(manifest_time):%Y-%m-%d %H:%M}, made on {manifest.get('made_on')} "
               f"(Python {manifest.get('python')}): {len(manifest['files']):,} files + {len(manifest.get('optional', {})):,} models.")
        # A whole top-level folder that is not here at all was said above in one line (and an update package never carries runtime\ or
        # bin\): its files are not listed one by one.
        absent_trees = {t for t in ("runtime", "bin", "app") if not os.path.isdir(os.path.join(ROOT, t))}
        problems = {"missing": [], "size": [], "content": [], "unreadable": []}        # kind -> [(rel, text, newer than the manifest)]
        same = size_only = 0
        absent_groups = set()
        t = time.time()
        for rel, want in sorted(manifest["files"].items()):
            parts = rel.split("/")
            if parts[0] in absent_trees:
                continue
            # a whole optional folder that is not installed here (an extra engine build, the image server) is one note, not a flood
            group = None
            if parts[0] == "bin" and len(parts) > 2 and parts[1] == "engines":
                group = "/".join(parts[:3])
            elif parts[0] == "bin" and len(parts) > 1 and parts[1] == "sd":
                group = "bin/sd"
            if group and not os.path.isdir(os.path.join(ROOT, *group.split("/"))):
                absent_groups.add(group)
                continue
            path = os.path.join(ROOT, *parts)
            if not os.path.isfile(path):
                problems["missing"].append((rel, rel, False))
                continue
            try:
                st = os.stat(path)
            except OSError as e:
                problems["unreadable"].append((rel, f"{rel} ({e})", False))
                continue
            newer = st.st_mtime > manifest_time + 1
            if st.st_size != want.get("size"):
                problems["size"].append((rel, f"{rel} (is {st.st_size:,} bytes, should be {want.get('size'):,})", newer))
                continue
            if want.get("sha256") and (st.st_size <= HASH_LIMIT or verify_engines):
                try:
                    if sha256_of(path) != want["sha256"]:
                        problems["content"].append((rel, rel, newer))
                        continue
                except OSError as e:
                    problems["unreadable"].append((rel, f"{rel} ({e})", False))
                    continue
            else:
                size_only += 1
            same += 1
        r.line(f"Checked in {time.time() - t:.0f} s: {same:,} as recorded ({size_only:,} big files by size only"
               f"{'' if verify_engines else ' - add --verify-engines to read them in full'}), {len(problems['missing'])} missing, "
               f"{len(problems['size'])} wrong size, {len(problems['content'])} different content, {len(problems['unreadable'])} unreadable.")
        for g in sorted(absent_groups):
            r.note(f"{g}\\ is not installed here (optional: {'an extra engine build' if 'engines' in g else 'the image server'}).")
        # Under app\ a file NEWER than the manifest that differs is code edited after the manifest was made - a development copy, where the
        # manifest is simply older than the files (the packager makes a fresh one): a note. A file OLDER than the manifest that differs, or
        # one that is missing, is what a partly applied update leaves behind: a problem. Under runtime\ and bin\ nothing changes legitimately.
        kinds = (("missing", "MISSING"), ("size", "have the WRONG SIZE (an incomplete or truncated copy)"),
                 ("content", "have DIFFERENT CONTENT from the working copy (damaged, replaced, or edited)"), ("unreadable", "CANNOT BE READ (the drive itself fails to read them)"))
        damaged_trees = set()
        code_trees = ("app", "(root)")                 # the app's code and the root files: what updates change, and what a developer edits
        for key, kind in kinds:
            by_tree = {}
            for rel, text, newer in problems[key]:
                by_tree.setdefault((rel.split("/")[0] if "/" in rel else "(root)", bool(newer)), []).append(text)
            for (tree, newer), names in sorted(by_tree.items()):
                head = ", ".join(names[:25]) + (f" (+{len(names) - 25} more)" if len(names) > 25 else "")
                where = "in the installation folder" if tree == "(root)" else f"under {tree}\\"
                if tree in code_trees and newer:
                    r.note(f"{len(names)} file(s) {where} are newer than the manifest and differ from it - edited after the manifest was made, so the "
                           f"manifest is older than the files (a development copy; the packager makes a fresh manifest, or run --make-manifest): {head}")
                else:
                    r.flag(f"{len(names)} file(s) {where} {kind}: {head}"
                           + (" - these are NEWER than the manifest: if they were changed on purpose, run --make-manifest" if newer else ""))
                    damaged_trees.add("app" if tree in code_trees else tree)
        if "runtime" in damaged_trees:
            r.flag("The bundled Python (runtime\\python) is not the working copy's. Re-copy the whole runtime\\ folder from a working installation. "
                   "If the same files come out damaged again right after a fresh copy, the trouble is this PC's drive, its cable/port, or its memory "
                   "(run chkdsk on the drive and Windows Memory Diagnostic).")
        if "bin" in damaged_trees:
            r.flag("Engine files under bin\\ are not the working copy's - the model server cannot be trusted to start. Re-copy bin\\ from a working installation.")
        if "app" in damaged_trees:
            r.flag("Files under app\\ are missing, or are OLDER versions than the manifest's: an update was applied partially. Re-extract the whole update "
                   "package over this folder (allow overwrite).")
        # models: a copy with another size is cut short
        for rel, want in sorted(manifest.get("optional", {}).items()):
            path = os.path.join(ROOT, *rel.split("/"))
            if os.path.isfile(path):
                size = os.path.getsize(path)
                if size != want.get("size"):
                    r.flag(f"{rel} is {size:,} bytes here but {want.get('size'):,} on the working copy - an incomplete copy of the model.")
        # files here that the record does not know (an older or newer version, or leftovers)
        extra = []
        for base in (RUNTIME_DIR,):
            if os.path.isdir(base):
                for path in walk_files(base):
                    if relpath(path) not in manifest["files"]:
                        extra.append(relpath(path))
        if extra:
            r.note(f"{len(extra)} file(s) under runtime\\ that the working copy does not have: {', '.join(extra[:10])}{' ...' if len(extra) > 10 else ''}")

    # generic checks that need no manifest
    bad_pe = []
    pe_count = 0
    for base in (RUNTIME_DIR, BIN_DIR):
        if os.path.isdir(base):
            for path in walk_files(base):
                if path.lower().endswith((".exe", ".dll", ".pyd")):
                    pe_count += 1
                    ok, why = pe_header_ok(path)
                    if not ok:
                        bad_pe.append(f"{relpath(path)} ({why})")
    for name in ("Local AI ChatBot.exe",):
        path = os.path.join(ROOT, name)
        if os.path.isfile(path):
            pe_count += 1
            ok, why = pe_header_ok(path)
            if not ok:
                bad_pe.append(f"{name} ({why})")
    r.line(f"Program headers: {pe_count:,} .exe/.dll/.pyd files looked at, {len(bad_pe)} damaged.")
    if bad_pe:
        r.flag("Program files that are NOT valid Windows programs (what Windows reports as 'not designed to run on Windows or contains an error', "
               f"0xC000012F / 0xC000007B): {', '.join(bad_pe[:20])}{' ...' if len(bad_pe) > 20 else ''}")
    bad_py, py_count = [], 0
    for base in (os.path.join(ROOT, "app"), os.path.join(RUNTIME_DIR, "Lib")):
        if os.path.isdir(base):
            for path in walk_files(base):
                if path.endswith(".py"):
                    py_count += 1
                    try:
                        with open(path, "rb") as f:
                            compile(f.read(), path, "exec")
                    except Exception as e:
                        bad_py.append(f"{relpath(path)} ({type(e).__name__}: {str(e)[:80]})")
    r.line(f"Python sources: {py_count:,} compiled, {len(bad_py)} broken.")
    if bad_py:
        r.flag(f"Python files that do not compile (a cut-short or damaged copy, or a half-applied update): {', '.join(bad_py[:15])}")
    ggufs = []
    if os.path.isdir(MODELS_DIR):
        for path in walk_files(MODELS_DIR):
            if path.lower().endswith(".gguf"):
                try:
                    with open(path, "rb") as f:
                        magic = f.read(4)
                    ggufs.append((relpath(path), os.path.getsize(path), magic == b"GGUF"))
                except OSError as e:
                    ggufs.append((relpath(path), -1, False))
    if ggufs:
        r.line("Models:")
        for rel, size, ok in ggufs:
            r.line(f"  {rel}: {size / 1048576:,.0f} MB" + ("" if ok else "  <- NOT a GGUF file (damaged or unreadable)"))
        for rel, size, ok in ggufs:
            if not ok:
                r.flag(f"{rel} does not start with the GGUF magic - a damaged or incomplete copy of the model.")
    elif os.path.isdir(MODELS_DIR):
        r.flag("No .gguf model anywhere under models\\.")


def sec_computer(r, quick):
    r.section("2. This PC and the folder the app runs from")
    try:
        rel, ver, _sp, ptype = platform.win32_ver()
        r.line(f"Windows: {platform.system()} {rel} (build {ver}) {ptype}, {platform.machine()}; computer '{platform.node()}', user '{os.environ.get('USERNAME', '?')}'")
        try:
            admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            admin = None
        k = ctypes.WinDLL("kernel32")
        k.GetTickCount64.restype = ctypes.c_ulonglong
        up = k.GetTickCount64() / 1000
        r.line(f"Running as administrator: {admin}; last restart {up / 3600:.1f} hours ago" + (" (over a week: a stuck copy from an earlier launch would still be here)" if up > 7 * 86400 else ""))

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(m)
        if k.GlobalMemoryStatusEx(ctypes.byref(m)):
            r.line(f"Memory: {m.ullTotalPhys / 1048576:,.0f} MB installed, {m.ullAvailPhys / 1048576:,.0f} MB free ({m.dwMemoryLoad}% in use)")
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                cpu = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        except Exception:
            cpu = platform.processor()
        r.line(f"CPU: {cpu}, {os.cpu_count()} logical processors")
    except Exception:
        r.failed("PC")
    try:
        import winreg
        sac = None
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\CI\Policy") as key:
                sac = winreg.QueryValueEx(key, "VerifiedAndReputablePolicyState")[0]
        except OSError:
            pass
        r.line(f"Smart App Control: {({0: 'off', 1: 'ON', 2: 'evaluation mode'}).get(sac, 'not present' if sac is None else sac)}")
        if sac == 1:
            r.flag("Smart App Control is ON. It blocks unsigned programs it does not know - this app's launcher, Python and the engines are unsigned. "
                   "Look for a 'was blocked' notification when launching; Windows Security > App & browser control is where it is turned off (one way, needs a reinstall to turn on again).")
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer") as key:
                r.line(f"SmartScreen (Explorer): {winreg.QueryValueEx(key, 'SmartScreenEnabled')[0]}")
        except OSError:
            r.line("SmartScreen (Explorer): default")
    except Exception:
        r.failed("security settings")
    env_bits = {k: v for k, v in os.environ.items() if k.upper().startswith(("CUDA", "GGML", "LOCALAI", "NVIDIA", "TCL", "TK_")) or k.upper() in ("PYTHONPATH", "PYTHONHOME")}
    if env_bits:
        r.line("Environment variables that matter: " + "; ".join(f"{k}={v[:80]}" for k, v in sorted(env_bits.items())))
    else:
        r.line("Environment variables that matter: none set (good)")
    on_path = [d for d in os.environ.get("PATH", "").split(os.pathsep) if "nvidia" in d.lower() or "cuda" in d.lower()]
    r.line("PATH entries for NVIDIA/CUDA: " + ("; ".join(on_path) if on_path else "none"))

    # the folder
    try:
        drive = os.path.splitdrive(ROOT)[0]
        root_of_drive = drive + "\\" if drive else ROOT
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        k.GetDriveTypeW.restype = ctypes.c_uint
        dtype = {0: "unknown", 1: "no such drive", 2: "REMOVABLE (USB stick / card)", 3: "fixed (internal or external disk)", 4: "NETWORK", 5: "CD/DVD", 6: "RAM disk"}.get(k.GetDriveTypeW(root_of_drive), "?")
        fs_buf, name_buf = ctypes.create_unicode_buffer(261), ctypes.create_unicode_buffer(261)
        serial, maxlen, flags = ctypes.c_uint32(), ctypes.c_uint32(), ctypes.c_uint32()
        k.GetVolumeInformationW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32), ctypes.c_wchar_p, ctypes.c_uint32]
        fs = "?"
        if k.GetVolumeInformationW(root_of_drive, name_buf, 261, ctypes.byref(serial), ctypes.byref(maxlen), ctypes.byref(flags), fs_buf, 261):
            fs = fs_buf.value
        free_avail, total, free_total = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
        k.GetDiskFreeSpaceExW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong)]
        space = f"{free_avail.value / 1073741824:,.1f} GB free of {total.value / 1073741824:,.1f} GB" if k.GetDiskFreeSpaceExW(root_of_drive, ctypes.byref(free_avail), ctypes.byref(total), ctypes.byref(free_total)) else "?"
        r.line(f"Drive {drive or '(none)'}: {dtype}, file system {fs} ('{name_buf.value}'), {space}")
        if fs.upper() in ("FAT32", "FAT", "EXFAT"):
            r.note(f"The folder is on a {fs} drive. It works, but such drives cannot carry Windows' mark-of-the-web (so none of those checks apply) and "
                   "FAT32 cannot hold any file over 4 GB (most models are bigger).")
        if dtype.startswith("NETWORK"):
            r.flag("The folder is on a NETWORK drive: Windows refuses to run some programs from there and model loading would be very slow. Copy it to a local disk.")
        r.line(f"Path length {len(ROOT)} characters" + ("; contains non-ASCII characters" if any(ord(c) > 127 for c in ROOT) else ""))
        if len(ROOT) > 180:
            r.note("A long installation path: paths inside the folder may exceed 260 characters, which some programs cannot open.")
        k.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
        k.GetFileAttributesW.restype = ctypes.c_uint32
        attrs = k.GetFileAttributesW(ROOT)
        if attrs != 0xFFFFFFFF:
            cloudy = attrs & (0x400 | 0x1000 | 0x400000 | 0x40000)       # reparse point, offline, recall on data access, recall on open
            if cloudy:
                r.flag(f"The installation folder is a reparse point / cloud placeholder (attributes 0x{attrs:X}): OneDrive-style 'files on demand' leave empty "
                       "placeholder files that Windows cannot run. Keep the folder out of synced locations or mark it 'Always keep on this device'.")
        # mark of the web on what is run
        marked = []
        for rel in ("Local AI ChatBot.exe", "Start Chatbot.bat", "Stop Chatbot.bat", "runtime/python/pythonw.exe", "runtime/python/python.exe", "bin/llama-server.exe", "bin/ggml-cuda.dll"):
            path = os.path.join(ROOT, *rel.split("/"))
            if os.path.isfile(path):
                z = zone_identifier(path)
                if z:
                    marked.append(f"{rel} (zone {z})")
        if marked:
            r.note("Files marked as coming from the internet/another computer (SmartScreen may show 'Windows protected your PC' - choose More info > Run anyway, "
                   f"or right-click > Properties > Unblock): {', '.join(marked)}")
        else:
            r.line("Mark-of-the-web: none on the launcher, the .bat files, Python or the engine.")
        # can it be written to?
        for label, folder in (("data\\", DATA_DIR), ("the installation folder", ROOT)):
            try:
                os.makedirs(folder, exist_ok=True)
                probe = os.path.join(folder, f".diagnose-write-test-{os.getpid()}")
                with open(probe, "w") as f:
                    f.write("ok")
                os.remove(probe)
                r.line(f"Writing to {label}: ok")
            except OSError as e:
                r.flag(f"Cannot write to {label}: {e}. The app keeps its database, settings and logs in data\\ - a read-only drive, a folder under Defender's "
                       "Controlled Folder Access, or missing permissions stop it before it starts.")
        db = os.path.join(DATA_DIR, "chats.db")
        if os.path.isfile(db):
            try:
                import sqlite3
                con = sqlite3.connect(db, timeout=5)
                try:
                    con.execute("BEGIN IMMEDIATE")
                    con.execute("ROLLBACK")
                    mode = con.execute("PRAGMA journal_mode").fetchone()[0]
                    r.line(f"Database data\\chats.db: opens for writing (SQLite {sqlite3.sqlite_version}, journal {mode}, {os.path.getsize(db):,} bytes)")
                finally:
                    con.close()
            except Exception as e:
                r.flag(f"data\\chats.db cannot be opened for writing: {e}")
        else:
            r.line("Database data\\chats.db: not there yet (a first launch makes it)")
    except Exception:
        r.failed("folder")
    if not quick:
        r.line("Disks and volumes (Windows' own health status):")
        r.line(ps("Get-PhysicalDisk | Select-Object FriendlyName, MediaType, BusType, HealthStatus, OperationalStatus, @{n='SizeGB';e={[int]($_.Size/1GB)}} | Format-Table -AutoSize | Out-String -Width 200", 60))
        r.line(ps("Get-Volume | Where-Object DriveLetter | Select-Object DriveLetter, FileSystemLabel, FileSystem, HealthStatus, @{n='FreeGB';e={[int]($_.SizeRemaining/1GB)}}, @{n='SizeGB';e={[int]($_.Size/1GB)}} | Format-Table -AutoSize | Out-String -Width 200", 60))
        r.line("Defender: controlled folder access and threats found in the last 14 days:")
        r.line(ps("Get-MpPreference | Select-Object EnableControlledFolderAccess, @{n='ProtectedFolders';e={$_.ControlledFolderAccessProtectedFolders -join '; '}} | Format-List | Out-String -Width 250", 60))
        threats = ps("Get-MpThreatDetection -ErrorAction SilentlyContinue | Where-Object { $_.InitialDetectionTime -gt (Get-Date).AddDays(-14) } | Select-Object InitialDetectionTime, ThreatID, @{n='Files';e={$_.Resources -join '; '}}, ActionSuccess | Format-List | Out-String -Width 250", 60)
        r.line(threats)
        if re.search(r"llama|ggml|python|sd-server|Local_AI|LocalAI|Personal", threats, re.IGNORECASE):
            r.flag("Windows Defender has acted on one of this app's files in the last 14 days (see above): a quarantined or deleted engine or runtime file "
                   "stops the app. Restore it from Defender's Protection history and add the installation folder as an exclusion.")


def sec_python(r):
    r.section("3. The bundled Python")
    if not PY_EXE:
        r.flag("runtime\\python\\python.exe is not here - nothing below can be tried.")
        return
    r.line(f"Interpreter: {PY_EXE}")
    pth = glob.glob(os.path.join(RUNTIME_DIR, "python*._pth"))
    for p in pth:
        try:
            r.line(f"{os.path.basename(p)}: " + " | ".join(open(p, encoding="utf-8", errors="replace").read().split()))
        except OSError:
            pass
    for name, ver in (("python312.dll", None), ("python3.dll", None), ("_tkinter.pyd", None), ("tcl86t.dll", None), ("tk86t.dll", None), ("vcruntime140.dll", None)):
        path = os.path.join(RUNTIME_DIR, name)
        if os.path.isfile(path):
            ok, why = pe_header_ok(path)
            r.line(f"  {name}: {os.path.getsize(path):,} bytes, version {file_version(path) or '?'}" + ("" if ok else f"  <- DAMAGED ({why})"))
        else:
            r.line(f"  {name}: MISSING")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    tcl = os.path.join(RUNTIME_DIR, "tcl", "tcl8.6")
    tk = os.path.join(RUNTIME_DIR, "tcl", "tk8.6")
    if os.path.isfile(os.path.join(tcl, "init.tcl")):
        env["TCL_LIBRARY"] = tcl
    if os.path.isfile(os.path.join(tk, "tk.tcl")):
        env["TK_LIBRARY"] = tk
    # The embedded Python's ._pth file keeps the current folder off sys.path, so the app's folder is put there by hand (as desktop_main.py does).
    app_on_path = f"import sys; sys.path.insert(0, {os.path.join(ROOT, 'app')!r}); "
    checks = (
        ("Python itself starts", ["-c", "import sys; print(sys.version)"], None),
        ("Tk (the window toolkit) comes up", ["-c", "import tkinter; r = tkinter.Tk(); r.withdraw(); r.update(); r.destroy(); print('tk ok')"], None),
        ("the backend module imports (app\\server.py)", ["-c", app_on_path + "import server; print('server ok')"], os.path.join(ROOT, "app")),
        ("the window module imports (app\\desktop\\main_window.py)", ["-c", app_on_path + "import desktop.main_window; print('window ok')"], os.path.join(ROOT, "app")),
    )
    for label, argv, cwd in checks:
        res = run([PY_EXE, "-B", *argv], 120, cwd=cwd, env=env)
        if res["error"]:
            r.flag(f"{label}: {res['error']}")
        elif res["code"] != 0:
            text = (res["err"] or res["out"]).strip().splitlines()
            r.flag(f"{label}: FAILED (exit {hexcode(res['code'])}) - " + " | ".join(text[-4:]))
        else:
            r.line(f"{label}: ok ({res['seconds']:.1f} s)")


def sec_processes(r, state):
    r.section("4. Copies of the app already running, and the ports")
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.restype = ctypes.c_void_p
        k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k.CreateMutexW(None, False, MUTEX_NAME)
        held = ctypes.get_last_error() == 183
        if h:
            k.CloseHandle(h)
        state["mutex_held"] = held
        if held:
            r.flag("The app's single-instance lock is HELD: a copy of Personal Terminal is running on this PC right now (its window may be hidden, or it may be "
                   "stuck loading). Every new launch - from ANY drive or folder - just pokes that copy and exits without a word. End it (pythonw.exe in Task "
                   "Manager, or the Stop Chatbot.bat of the folder it was started from - see the process list below for which) or restart the PC.")
        else:
            r.line("Single-instance lock: free (no copy of the app is running).")
    except Exception:
        r.failed("single-instance lock")
    r.line("Processes of this app on this PC (any folder):")
    names = ", ".join(f"'{n}'" for n in APP_PROCESS_NAMES)
    listing = ps(f"Get-CimInstance Win32_Process | Where-Object {{ @({names}) -contains $_.Name }} | Select-Object ProcessId, ParentProcessId, Name, "
                 "@{n='Started';e={$_.CreationDate}}, ExecutablePath, @{n='CommandLine';e={ if ($_.CommandLine) { $_.CommandLine.Substring(0, [Math]::Min(300, $_.CommandLine.Length)) } }} "
                 "| Format-List | Out-String -Width 400", 60)
    if listing.startswith("(PowerShell"):
        res = run(["tasklist", "/FO", "CSV", "/NH"], 30)
        rows = [line for line in res["out"].splitlines() if any(line.lower().startswith(f'"{n.lower()}"') for n in APP_PROCESS_NAMES)]
        listing += "\n" + ("\n".join(rows) if rows else "(none per tasklist)")
    r.line(listing if listing.strip() and not listing.startswith("(nothing") else "(none)")
    own = [line for line in listing.splitlines() if "ExecutablePath" in line and ROOT.lower() in line.lower()]
    other = [line for line in listing.splitlines() if "ExecutablePath" in line and line.split(":", 1)[-1].strip() and ROOT.lower() not in line.lower()]
    if other:
        r.note(f"App processes running from ANOTHER folder/drive: {len(other)} - " + "; ".join(x.split(":", 1)[-1].strip() for x in other[:4]) +
               ". A copy on another drive holds the lock and the ports just the same, and that drive's Stop Chatbot.bat only stops its own.")
    r.line("Ports:")
    net = run(["netstat", "-ano", "-p", "TCP"], 30)
    for label, port in PORTS:
        open_ = port_open(port)
        pid = listening_pid(port, net["out"]) if open_ else None
        path = process_image_path(pid) if pid else None
        who = f"process {pid} ({path or 'path not readable'})" if pid else ("someone (pid not readable)" if open_ else "")
        r.line(f"  {port} ({label}): " + (f"IN USE by {who}" if open_ else "free"))
        state[f"port_{port}"] = (open_, pid, path)
        if open_ and port == 8790:
            r.flag(f"Port 8790 is in use by {who}: the app's own port. A launch that finds it taken says 'already running' in data\\desktop.log and exits silently. "
                   "If that process is a stuck earlier copy, end it or restart the PC; if it is another program, it must give the port up.")
        if open_ and port == 8791 and not (path and os.path.normcase(os.path.abspath(path)).startswith(os.path.normcase(ROOT) + os.sep)):
            r.flag(f"Port 8791 (the model server's) is in use by {who}, which is not this folder's model server. The new model server cannot bind it "
                   "and exits at once - 'WinError 10061' follows. End that process (a leftover of a copy on another drive: that drive's Stop Chatbot.bat).")


def sec_gpu(r, state, mods, quick):
    r.section("5. The NVIDIA driver and the GPUs")
    smi = shutil.which("nvidia-smi")
    r.line(f"nvidia-smi: {smi or 'not on PATH (no NVIDIA driver, or an unusual install)'}")
    gpus = []
    if smi:
        res = run(["nvidia-smi"], 30)
        if res["error"]:
            r.flag(f"nvidia-smi {res['error']} - the driver is not answering (a hung driver: restart the PC).")
        else:
            r.line("\n".join(res["out"].splitlines()[:28]))
            if res["code"] != 0:
                r.flag(f"nvidia-smi failed (exit {hexcode(res['code'])}): {(res['err'] or res['out']).strip()[:300]}")
        q = run(["nvidia-smi", "--query-gpu=index,name,driver_version,memory.total,memory.used,memory.free,compute_cap,pstate,pcie.link.gen.current", "--format=csv"], 20)
        if not q["error"] and q["code"] == 0:
            r.line(q["out"].strip())
            for line in q["out"].strip().splitlines()[1:]:
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 7:
                    try:
                        cc = int(round(float(parts[6]) * 10)) if parts[6][0].isdigit() else None
                    except ValueError:
                        cc = None
                    gpus.append({"index": parts[0], "name": parts[1], "driver": parts[2], "total": parts[3], "free": parts[5], "cc": cc})
        else:
            q2 = run(["nvidia-smi", "--query-gpu=index,name,driver_version,memory.total,memory.used,memory.free", "--format=csv"], 20)
            if not q2["error"] and q2["code"] == 0:
                r.line(q2["out"].strip())
                for line in q2["out"].strip().splitlines()[1:]:
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 6:
                        gpus.append({"index": parts[0], "name": parts[1], "driver": parts[2], "total": parts[3], "free": parts[5], "cc": None})
    state["gpus"] = gpus
    driver = None
    if gpus:
        try:
            driver = float(gpus[0]["driver"].split(".")[0])
        except (ValueError, IndexError):
            driver = None
    nvcuda = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvcuda.dll")
    if os.path.isfile(nvcuda):
        v = file_version(nvcuda)
        derived = nvidia_driver_from_file_version(v) if v else None
        r.line(f"nvcuda.dll (the CUDA driver): present, file version {v or '?'}" + (f" = driver {derived}" if derived else ""))
        if driver is None and derived:
            try:
                driver = float(derived.split(".")[0])
            except ValueError:
                pass
    else:
        r.flag("C:\\Windows\\System32\\nvcuda.dll is MISSING: no NVIDIA driver is installed (or it is broken). The engines' GPU support cannot load; "
               "the model would run on the CPU only. Install the NVIDIA driver.")
    state["driver"] = driver
    if driver is not None and driver < MIN_DRIVER_FOR_CUDA13:
        r.flag(f"The NVIDIA driver is release {driver:.0f}. The default engine build (bin\\) is a CUDA 13 build, which needs driver {MIN_DRIVER_FOR_CUDA13} or newer - "
               "with this driver its GPU support fails to initialise and the model runs on the CPU (or the server dies). Either update the driver or use the "
               "cuda12 engine build under bin\\engines\\ (Settings > Advanced > GPUs > engine), which works with older drivers.")
    for g in gpus:
        if g["cc"] is not None and g["cc"] < 70:
            r.note(f"{g['name']} is compute capability {g['cc'] // 10}.{g['cc'] % 10} (Pascal or older): the default CUDA 13 engine build has no code for it at all "
                   "- the cuda12 build under bin\\engines\\ is needed for it - and NVIDIA's 580 driver series is the last with support for such cards.")
    if not quick:
        r.line("Display adapters per Windows (Device Manager status):")
        adapters = ps("Get-CimInstance Win32_VideoController | Select-Object Name, DriverVersion, DriverDate, Status, ConfigManagerErrorCode, "
                      "@{n='AdapterRAM_MB';e={[int]($_.AdapterRAM/1MB)}}, PNPDeviceID | Format-List | Out-String -Width 250", 60)
        r.line(adapters)
        for m in re.finditer(r"ConfigManagerErrorCode\s*:\s*(\d+)", adapters):
            code = int(m.group(1))
            if code:
                r.flag(f"A display adapter has Device Manager error code {code} ({DEVICE_MANAGER_CODES.get(code, 'see Device Manager')}): the driver is not running "
                       "for it. Nothing can use that GPU until Device Manager shows it healthy.")
        r.line("Driver trouble in the System event log (last 7 days: nvlddmkm, display driver resets):")
        events = ps("$since=(Get-Date).AddDays(-7); "
                    "$a=@(Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='nvlddmkm'; StartTime=$since} -MaxEvents 20 -ErrorAction SilentlyContinue); "
                    "$b=@(Get-WinEvent -FilterHashtable @{LogName='System'; Id=4101,14; StartTime=$since} -MaxEvents 20 -ErrorAction SilentlyContinue | Where-Object { $_.ProviderName -match 'Display|nvlddmkm' }); "
                    "($a + $b) | Sort-Object TimeCreated -Descending | Select-Object -First 12 TimeCreated, Id, ProviderName, "
                    "@{n='Msg';e={ $t=($_.Message -replace '\\s+',' '); $t.Substring(0,[Math]::Min(300,$t.Length)) }} | Format-List | Out-String -Width 250", 120)
        r.line(events)
        if "nvlddmkm" in events or "4101" in events:
            r.note("The NVIDIA driver has logged resets/errors in the last 7 days - a driver that keeps stopping takes the model server with it.")


def sec_engines(r, state, mods, quick):
    r.section("6. The engine builds (llama.cpp) and the image server")
    pe = mods.get("pe_imports")
    gp = mods.get("gpu_plan")
    builds = []
    exe = os.path.join(BIN_DIR, "llama-server.exe")
    builds.append(("default (bin\\)", BIN_DIR, exe))
    eng = os.path.join(BIN_DIR, "engines")
    if os.path.isdir(eng):
        for name in sorted(os.listdir(eng), key=str.lower):
            builds.append((f"{name} (bin\\engines\\{name}\\)", os.path.join(eng, name), os.path.join(eng, name, "llama-server.exe")))
    state["builds"] = []
    gpus = state.get("gpus") or []
    for label, folder, server_exe in builds:
        r.line(f"--- {label}")
        info = {"label": label, "dir": folder, "exe": server_exe, "ok": False, "devices": None, "broken": False}
        state["builds"].append(info)
        if not os.path.isfile(server_exe):
            r.flag(f"{label}: llama-server.exe is missing.")
            info["broken"] = True
            continue
        files = {n.lower() for n in os.listdir(folder)}
        parts = []
        for pattern, what in (("cudart64_*.dll", "CUDA runtime"), ("cublas64_*.dll", "cuBLAS"), ("cublaslt64_*.dll", "cuBLASLt")):
            hits = [n for n in files if fnmatch.fnmatch(n, pattern)]
            parts.append(f"{what}: {', '.join(sorted(hits)) if hits else 'MISSING'}")
        vc = [n for n in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll") if n in files]
        parts.append(f"VC++ runtime beside it: {', '.join(vc) if vc else 'none (needs the system one)'}")
        parts.append("ggml-cuda.dll: " + ("present" if "ggml-cuda.dll" in files else "MISSING (a CPU-only build)"))
        r.line("  " + "; ".join(parts))
        ok, why = pe_header_ok(server_exe)
        if not ok:
            r.flag(f"{label}: llama-server.exe is damaged ({why}).")
            info["broken"] = True
            continue
        if pe is not None:
            try:
                missing = pe.missing_dependencies(server_exe)
                if missing:
                    r.flag(f"{label}: llama-server.exe needs DLLs that are nowhere on this PC: {', '.join(sorted(missing))} (needed by "
                           f"{', '.join(sorted(set(missing.values())))}). Windows ends it at once with exit code 0xC0000135 and an empty log. "
                           + ("Install the Microsoft Visual C++ Redistributable (x64) from https://aka.ms/vs/17/release/vc_redist.x64.exe, or copy those DLLs next to it."
                              if all(pe.is_vc_runtime(n) for n in missing) else "Put them in the engine's folder (nvcuda.dll comes with the NVIDIA driver)."))
                    info["broken"] = True
                else:
                    r.line("  DLLs it needs: all found")
                cuda_dll = os.path.join(folder, "ggml-cuda.dll")
                if os.path.isfile(cuda_dll):
                    gap = pe.missing_dependencies(cuda_dll)
                    if gap:
                        r.flag(f"{label}: its GPU support (ggml-cuda.dll) needs DLLs that are missing: {', '.join(sorted(gap))} - the model would run on the CPU only. "
                               "(nvcuda.dll = the NVIDIA driver; cudart/cublas = the CUDA runtime that belongs in the engine's folder.)")
            except Exception:
                r.failed("DLL dependency")
        else:
            r.line("  (app\\pe_imports.py is not in this copy, so which DLLs it needs was not read)")
        archs = None
        if gp is not None and os.path.isfile(os.path.join(folder, "ggml-cuda.dll")):
            try:
                archs = gp.cuda_archs(os.path.join(folder, "ggml-cuda.dll"))
                if archs:
                    fmt = lambda xs: ", ".join(f"{a // 10}.{a % 10}" for a in xs)
                    r.line(f"  Compiled for GPUs: native code for {fmt(archs.get('sass', []))}; portable code (compiled by the driver on first load) down to "
                           f"{fmt(sorted(archs.get('ptx', []))[:1]) or 'none'}")
            except Exception:
                r.failed("architecture")
        if quick:
            continue
        res = run([server_exe, "--version"], 60)
        if res["error"]:
            r.flag(f"{label}: llama-server.exe {res['error']}")
            info["broken"] = True
            continue
        text = (res["err"] + res["out"]).strip().splitlines()
        if res["code"] != 0:
            r.flag(f"{label}: llama-server.exe --version ended with exit code {hexcode(res['code'])}" + (f" - {' | '.join(text[-3:])}" if text else " (nothing printed)"))
            info["broken"] = True
            continue
        r.line(f"  Runs: yes ({' | '.join(text[:2]) if text else 'no version text'})")
        info["ok"] = True
        ld = run([server_exe, "--list-devices"], 120)
        if ld["error"]:
            r.flag(f"{label}: llama-server.exe --list-devices {ld['error']} - initialising the GPU hangs (a stuck driver: restart the PC).")
            continue
        out = (ld["out"] + "\n" + ld["err"]).strip()
        shown = [line for line in out.splitlines() if line.strip()]
        r.line("  --list-devices: " + (" | ".join(shown[:8]) if shown else "(nothing printed)") + (f" [exit {hexcode(ld['code'])}]" if ld["code"] else ""))
        devices = gp.parse_list_devices(out) if gp is not None else [{"id": m.group(1), "name": m.group(2)} for m in re.finditer(r"^\s*(\w+\d+):\s*(.+?)\s*\(", out, re.M)]
        info["devices"] = devices
        if not devices:
            r.flag(f"{label}: lists NO GPU - the engine sees no CUDA device from this PC (driver too old for this build, no driver, missing CUDA DLLs, or the GPU "
                   "is in error in Device Manager). The model would run on the CPU only." + ("" if ld["code"] == 0 else f" (exit code {hexcode(ld['code'])})"))
        else:
            r.line("  GPUs it sees: " + "; ".join(f"{d['id']} = {d['name']}" + (f" ({d.get('total_mb')} MiB, {d.get('free_mb')} MiB free)" if d.get("total_mb") else "") for d in devices))
        if archs and gp is not None and gpus:
            for g in gpus:
                if g["cc"] is None:
                    continue
                runs, why = gp.arch_support(g["cc"], archs)
                if not runs:
                    r.flag(f"{label}: cannot run on {g['name']} - {why}. If the model server is started with this build while that GPU is in use, "
                           "it dies on 'no kernel image is available' (exit 0xC0000409) - the cause of 'WinError 10061' on such a PC. "
                           "The app's Settings > Advanced > GPUs marks such a GPU 'not used' and prefers a build that can run it (the cuda12 build).")
                elif gp.needs_jit(g["cc"], archs):
                    r.note(f"{label}: {g['name']} has no native code in this build - the driver compiles the kernels on the first load (1-2 minutes extra, once).")
                else:
                    r.line(f"  {g['name']}: native code in this build")
    sd = os.path.join(BIN_DIR, "sd", "sd-server.exe")
    r.line("--- image server (bin\\sd\\sd-server.exe)")
    if os.path.isfile(sd):
        ok, why = pe_header_ok(sd)
        line = f"  present, {os.path.getsize(sd):,} bytes" + ("" if ok else f" - DAMAGED ({why})")
        if pe is not None and ok:
            try:
                missing = pe.missing_dependencies(sd)
                line += f"; DLLs it needs: {'all found' if not missing else 'MISSING ' + ', '.join(sorted(missing))}"
                if missing:
                    r.flag(f"sd-server.exe needs DLLs that are missing: {', '.join(sorted(missing))} - image generation cannot start (chat is not affected).")
            except Exception:
                pass
        r.line(line)
    else:
        r.line("  not installed (image generation unavailable; chat is not affected)")


def diagnose_log(text, code):
    for pattern, what in FAILURE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return what
    if isinstance(code, int) and EXIT_CODES.get(code & 0xFFFFFFFF):
        return EXIT_CODES[code & 0xFFFFFFFF]
    return None


def pick_model(args_model):
    if args_model:
        path = args_model if os.path.isabs(args_model) else os.path.join(MODELS_DIR, args_model)
        return path if os.path.isfile(path) else None
    try:
        with open(os.path.join(DATA_DIR, "settings.json"), "r", encoding="utf-8") as f:
            active = json.load(f).get("active_model")
    except Exception:
        active = None
    candidates = [p for p in glob.glob(os.path.join(MODELS_DIR, "*.gguf")) if os.path.isfile(p)]
    if not candidates:
        return None
    # the smallest model is the quickest honest test of "does the server come up on this PC"; the active one only if it is the smallest
    smallest = min(candidates, key=os.path.getsize)
    if active and os.path.isfile(os.path.join(MODELS_DIR, active)) and os.path.getsize(os.path.join(MODELS_DIR, active)) <= os.path.getsize(smallest) * 1.5:
        return os.path.join(MODELS_DIR, active)
    return smallest


def start_test(r, build, model, timeout, cpu_only=False):
    """Starts this build's llama-server on a spare port with a small context, waits for it, asks it one question, stops it.
    Returns 'ok', 'died', 'timeout' or 'cannot'."""
    port = free_port()
    stamp = datetime.datetime.now().strftime("%H%M%S")
    log_dir = os.path.dirname(os.path.abspath(r.path))          # next to the report (data\ in the ordinary case)
    log_path = os.path.join(log_dir, f"diagnostics-start-{re.sub(r'[^A-Za-z0-9]+', '_', build['label'].split(' ')[0])}{'-cpu' if cpu_only else ''}-{stamp}.log")
    cmd = [build["exe"], "-m", model, "--host", "127.0.0.1", "--port", str(port), "-c", "2048", "--parallel", "1", "--jinja", "--no-webui",
           "--flash-attn", "on", "-b", "512", "-ub", "512"]
    if cpu_only:
        cmd += ["--device", "none"]
    r.line(f"  Starting ({'CPU only' if cpu_only else 'GPU as the engine chooses'}): {os.path.basename(build['exe'])} -m {os.path.basename(model)} --port {port} -c 2048 ... "
           f"(log: {log_path})")
    t0 = time.time()
    try:
        log = open(log_path, "w", encoding="utf-8", errors="replace")
    except OSError as e:
        r.line(f"  (cannot write the log file: {e})")
        log = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError as e:
        why = WINERRORS.get(getattr(e, "winerror", None))
        r.flag(f"{build['label']}: the model server cannot be started at all: {e}" + (f" => {why}" if why else ""))
        return "cannot"
    outcome = "timeout"
    last_said = t0
    listening_at = None
    try:
        while time.time() - t0 < timeout:
            if proc.poll() is not None:
                outcome = "died"
                break
            # The port opens as soon as the program is up; /health answers 503 until the model is loaded, then 200 {"status":"ok"}.
            if port_open(port, 0.3):
                if listening_at is None:
                    listening_at = time.time()
                    r.line(f"  Listening after {listening_at - t0:.0f} s; loading the model ...")
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
                        health = resp.read().decode("utf-8", "replace").strip()
                    if '"ok"' in health:
                        outcome = "ok"
                        r.line(f"  /health: {health[:200]}")
                        break
                except Exception:
                    pass                     # 503 (still loading) or a dropped connection: keep waiting
            if time.time() - last_said >= 20:
                last_said = time.time()
                r.line(f"    ... {time.time() - t0:.0f} s, still loading (a cold model on a hard disk can take minutes)")
            time.sleep(0.5)
        if outcome == "ok":
            r.line(f"  Model loaded after {time.time() - t0:.0f} s. Asking it one question ...")
            try:
                body = json.dumps({"prompt": "The capital of France is", "n_predict": 8, "temperature": 0}).encode()
                req = urllib.request.Request(f"http://127.0.0.1:{port}/completion", data=body, headers={"Content-Type": "application/json"})
                t1 = time.time()
                with urllib.request.urlopen(req, timeout=max(120, timeout)) as resp:
                    answer = json.loads(resp.read().decode("utf-8", "replace"))
                timings = answer.get("timings") or {}
                r.line(f"  Answer in {time.time() - t1:.1f} s: {answer.get('content', '')!r}"
                       + (f" ({timings.get('predicted_per_second', 0):.1f} tokens/s)" if timings.get("predicted_per_second") else ""))
                r.line(f"  {build['label']}: WORKS on this PC" + (" (CPU only)" if cpu_only else ""))
            except Exception as e:
                time.sleep(1)
                if proc.poll() is not None:
                    outcome = "died"           # reported below, with the exit code and the log: a GPU kernel problem shows here first
                    r.line(f"  The model server came up but DIED while answering ({type(e).__name__}).")
                else:
                    outcome = "no_answer"
                    r.flag(f"{build['label']}: the model server came up but did not answer a prompt: {type(e).__name__}: {e}")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    pass
        if log is not subprocess.DEVNULL:
            log.close()
    log_text = "\n".join(tail(log_path, 40)) if log is not subprocess.DEVNULL else ""
    if outcome == "died" and proc.returncode is not None:
        code = proc.returncode
        why = diagnose_log(log_text, code)
        r.flag(f"{build['label']}: the model server DIED after {time.time() - t0:.0f} s with exit code {hexcode(code)}"
               + (f" - most likely {why}" if why else "") + ". Its last log lines:")
        r.line("    " + "\n    ".join(log_text.splitlines()[-25:]) if log_text.strip() else "    (the log is EMPTY: it died before printing anything - a missing DLL or a damaged file)")
    elif outcome == "timeout":
        r.flag(f"{build['label']}: the model server was still not listening after {timeout} s (ended). On a slow disk a large model can simply take longer: "
               "rerun with --start-timeout 900, or pick a small model with --model. Its last log lines:")
        r.line("    " + "\n    ".join(log_text.splitlines()[-15:]) if log_text.strip() else "    (empty log)")
    return outcome


def sec_start_test(r, state, args):
    r.section("7. A real start of the model server (the thing that fails with 'WinError 10061')")
    if args.get("no_start_test"):
        r.line("Skipped (--no-start-test).")
        return
    model = pick_model(args.get("model"))
    if not model:
        r.flag("No model to test with (no .gguf in models\\, or the --model given is not there).")
        return
    r.line(f"Model: {os.path.basename(model)} ({os.path.getsize(model) / 1048576:,.0f} MB)")
    if state.get("port_8791", (False,))[0]:
        r.note("A model server is already listening on 8791 (the app's, or a leftover) - it holds GPU memory, so this test has less to work with than a fresh launch would.")
    builds = [b for b in state.get("builds", []) if not b.get("broken") and os.path.isfile(b["exe"])][:2]
    if not builds:
        r.flag("No usable engine build to test (see section 6).")
        return
    timeout = int(args.get("start_timeout") or 300)
    for b in builds:
        outcome = start_test(r, b, model, timeout)
        state.setdefault("start_results", {})[b["label"]] = outcome
        if outcome in ("died", "timeout") and os.path.isfile(os.path.join(b["dir"], "ggml-cuda.dll")):
            r.line("  The GPU run failed; trying the same build on the CPU only, to tell the GPU/driver apart from everything else:")
            cpu = start_test(r, b, model, timeout, cpu_only=True)
            state["start_results"][b["label"] + " (CPU only)"] = cpu
            if cpu == "ok":
                r.flag(f"{b['label']}: works on the CPU but not on the GPU - the problem is the GPU side: the NVIDIA driver, a GPU this build cannot run on "
                       "(see section 5 and 6), or GPU memory already taken by something else.")


def sec_logs(r):
    r.section("8. The app's own logs")
    for name, n in (("desktop.log", 150), ("llama-server.log", 120), ("sd-server.log", 60)):
        path = os.path.join(DATA_DIR, name)
        if os.path.isfile(path):
            st = os.stat(path)
            r.line(f"--- data\\{name} (last {n} lines; {st.st_size:,} bytes, last written {datetime.datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M})")
            lines = tail(path, n)
            r.line("\n".join(lines) if lines else "(empty)")
            if name == "desktop.log":
                text = "\n".join(lines)
                if "already running - showing its window" in text:
                    r.note("desktop.log shows launches that found a copy 'already running' - the silent exit described in section 4.")
                if "Traceback" in text:
                    r.note("desktop.log holds Python tracebacks (an error inside the app) - the last one is what to read first.")
        else:
            r.line(f"--- data\\{name}: not there" + (" (the app has never got as far as starting - or data\\ was not copied)" if name == "desktop.log" else ""))
    settings = os.path.join(DATA_DIR, "settings.json")
    if os.path.isfile(settings):
        try:
            with open(settings, "r", encoding="utf-8") as f:
                s = json.load(f)
            keys = ("active_model", "active_mmproj", "engine", "gpu_placement", "gpu_order", "gpu_disabled", "image_gpu", "context_size", "max_parallel_chats",
                    "gpu_layers_auto", "gpu_layers", "kv_cache_type", "kv_offload", "batch_size", "model_load_mode", "mlock_model", "settings_migration_version")
            r.line("--- data\\settings.json (what matters for launching): " + ", ".join(f"{k}={s.get(k)!r}" for k in keys if k in s))
            if s.get("active_model") and not os.path.isfile(os.path.join(MODELS_DIR, str(s.get("active_model")))):
                r.note(f"settings.json names active_model {s.get('active_model')!r}, which is not in models\\ here - the app falls back to the largest model it finds.")
        except Exception as e:
            r.flag(f"data\\settings.json cannot be read as JSON ({e}) - the app then starts with default settings.")
    if os.path.isdir(DATA_DIR):
        rows = []
        for name in sorted(os.listdir(DATA_DIR)):
            path = os.path.join(DATA_DIR, name)
            if os.path.isfile(path):
                st = os.stat(path)
                rows.append(f"{name} ({st.st_size:,} B, {datetime.datetime.fromtimestamp(st.st_mtime):%m-%d %H:%M})")
        r.line("--- data\\: " + (", ".join(rows) if rows else "(empty)"))


def sec_eventlog(r):
    r.section("9. Windows event log entries about the app's programs (last 7 days)")
    r.line("Application log - crashes (1000), Windows Error Reporting (1001), .NET (1026) naming the app's programs:")
    r.line(ps("$since=(Get-Date).AddDays(-7); Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000,1001,1026; StartTime=$since} -MaxEvents 300 -ErrorAction SilentlyContinue "
              "| Where-Object { $_.Message -match 'llama-server|pythonw|python\\.exe|python3|sd-server|Local AI ChatBot|Personal Terminal|ggml|LocalAI|Local_AI' } "
              "| Select-Object -First 12 TimeCreated, Id, ProviderName, @{n='Msg';e={ $t=($_.Message -replace '\\s+',' '); $t.Substring(0,[Math]::Min(700,$t.Length)) }} "
              "| Format-List | Out-String -Width 300", 150))
    r.line("Windows Defender log - detections and actions (1116-1119) in the last 14 days:")
    d = ps("Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-Windows Defender/Operational'; Id=1116,1117,1118,1119; StartTime=(Get-Date).AddDays(-14)} -MaxEvents 40 -ErrorAction SilentlyContinue "
           "| Select-Object TimeCreated, Id, @{n='Msg';e={ $t=($_.Message -replace '\\s+',' '); $t.Substring(0,[Math]::Min(500,$t.Length)) }} | Format-List | Out-String -Width 300", 120)
    r.line(d)
    if re.search(r"llama|ggml|python|sd-server|Local_AI|LocalAI|Personal Terminal", d, re.IGNORECASE):
        r.flag("Windows Defender detected/acted on one of this app's files (see above) - restore it from Defender's Protection history and exclude the installation folder.")


def sec_summary(r, state):
    r.section("Summary")
    if r.flags:
        r.line(f"{len(r.flags)} problem(s) found - the first ones are usually the cause, the later ones often follow from them:")
        for i, f in enumerate(r.flags, 1):
            r.line(f"  {i}. {f}")
    else:
        r.line("No problem found by these checks. If the app still does not start here, send this report anyway - section 8 (the logs) and the "
               "exact words of any message on screen are the next clues.")
    if r.notes:
        r.line(f"{len(r.notes)} note(s):")
        for n in r.notes:
            r.line(f"  - {n}")
    results = state.get("start_results") or {}
    if results:
        r.line("Model server start test: " + "; ".join(f"{k}: {v}" for k, v in results.items()))
    r.line()
    r.line(f"Report written to: {r.path}")


def parse_args(argv):
    args = {"no_start_test": False, "start_timeout": None, "model": None, "verify_engines": False, "quick": False, "root": None, "out": None, "make_manifest": False}
    it = iter(argv)
    for a in it:
        if a == "--no-start-test":
            args["no_start_test"] = True
        elif a == "--start-timeout":
            args["start_timeout"] = int(next(it))
        elif a == "--model":
            args["model"] = next(it)
        elif a == "--verify-engines":
            args["verify_engines"] = True
        elif a == "--quick":
            args["quick"] = True
        elif a == "--root":
            args["root"] = next(it)
        elif a == "--out":
            args["out"] = next(it)
        elif a == "--make-manifest":
            args["make_manifest"] = True
        elif a in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        else:
            print(f"unknown option {a!r}\n{__doc__}")
            sys.exit(2)
    return args


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    if args["root"]:
        set_root(args["root"])
    if args["make_manifest"]:
        t = time.time()
        m = make_manifest()
        print(f"Wrote {MANIFEST_PATH}: {len(m['files']):,} files, {len(m['optional']):,} models, {time.time() - t:.0f} s.")
        return 0
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = args["out"]
    if not out:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            out = os.path.join(DATA_DIR, f"diagnostics-{stamp}.txt")
            open(out, "a").close()
        except OSError:
            out = os.path.join(tempfile.gettempdir(), f"PersonalTerminal-diagnostics-{stamp}.txt")
    r = Report(out)
    state = {}
    mods = optional_modules()
    sections = (
        ("header", lambda: sec_header(r, argv)),
        ("files", lambda: sec_integrity(r, args["verify_engines"])),
        ("PC and folder", lambda: sec_computer(r, args["quick"])),
        ("Python", lambda: sec_python(r)),
        ("processes", lambda: sec_processes(r, state)),
        ("GPU", lambda: sec_gpu(r, state, mods, args["quick"])),
        ("engines", lambda: sec_engines(r, state, mods, args["quick"])),
        ("start test", lambda: sec_start_test(r, state, args)),
        ("logs", lambda: sec_logs(r)),
    )
    if not args["quick"]:
        sections += (("event log", lambda: sec_eventlog(r)),)
    for name, fn in sections:
        try:
            fn()
        except SystemExit:
            raise
        except Exception:
            r.line()
            r.line(f"(section '{name}' failed: {traceback.format_exc().strip().splitlines()[-1]})")
    try:
        sec_summary(r, state)
    finally:
        r.close()
    return 1 if r.flags else 0


if __name__ == "__main__":
    sys.exit(main())
