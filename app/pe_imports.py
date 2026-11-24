"""Which DLLs does a Windows program need, and which of them are missing? (stdlib only; Windows executables and DLLs.)

Why this exists: when a program's DLL is missing, Windows does not start it at all - the process exits at once with code 0xC0000135
(STATUS_DLL_NOT_FOUND), the program never gets to write a word to its log, and nothing ever listens on the port it was going to open. To
the app that looks like "the model server did not answer" (WinError 10061, connection refused) with an empty log and no clue. The engine
needs the Microsoft Visual C++ runtime (msvcp140.dll, vcruntime140.dll, vcruntime140_1.dll), which comes with Windows only if some other
program installed it: a clean PC does not have it. Reading the import table (a documented, fixed format) names what is missing.

Windows looks for a DLL in the program's own folder first, then in the system folders and along PATH; the "api-ms-win-*" / "ext-ms-*"
names are virtual (the loader maps them to real system DLLs) and are never missing files.
"""
import os
import struct

_MAX_NAMES = 600          # a sane import table is a few dozen names: a damaged or hostile file cannot make this walk long


def pe_imports(path):
    """The DLL names a PE file imports at load time (the ordinary import table; delay-loaded imports do not stop a program from
    starting and are not included) in the order listed, or None if the file is not a readable PE file. Never raises."""
    try:
        with open(path, "rb") as f:
            data = f.read()
        if data[:2] != b"MZ":
            return None
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            return None
        _machine, nsec, _stamp, _symtab, _nsym, optsize, _chars = struct.unpack_from("<HHIIIHH", data, pe + 4)
        opt = pe + 24
        magic = struct.unpack_from("<H", data, opt)[0]
        directories = opt + (112 if magic == 0x20B else 96)          # PE32+ (64-bit) or PE32
        import_rva, _size = struct.unpack_from("<II", data, directories + 8)
        if not import_rva:
            return []
        sections = []
        table = opt + optsize
        for i in range(min(nsec, 96)):
            vsize, va, rsize, raw = struct.unpack_from("<IIII", data, table + 8 + 40 * i)
            sections.append((va, max(vsize, rsize), raw))

        def offset(rva):
            for va, size, raw in sections:
                if va <= rva < va + size:
                    return raw + (rva - va)
            return None

        names = []
        at = offset(import_rva)
        while at is not None and len(names) < _MAX_NAMES:
            _orig_thunk, _stamp2, _chain, name_rva, _thunk = struct.unpack_from("<IIIII", data, at)
            if not name_rva:
                break
            name_at = offset(name_rva)
            if name_at is None:
                break
            end = data.index(b"\0", name_at)
            names.append(data[name_at:end].decode("ascii", "replace"))
            at += 20
        return names
    except (OSError, struct.error, ValueError, IndexError):
        return None


def _system_dirs():
    windir = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    dirs = [os.path.join(windir, "System32"), windir]
    dirs += [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    return dirs


def missing_dependencies(path, system_dirs=None, max_depth=4):
    """{dll name: the file that imports it} for every DLL `path` needs, directly or through the DLLs next to it, that is nowhere Windows
    would look: not in the program's own folder, not in the system folders, not on PATH. {} when nothing is missing - and on any
    system or file this cannot read, where it says nothing rather than guess. DLLs found in the program's own folder are followed
    (their own needs count); the system's are not (they are the system's business)."""
    if os.name != "nt":
        return {}
    base = os.path.dirname(os.path.abspath(path))
    system_dirs = _system_dirs() if system_dirs is None else system_dirs
    missing, seen = {}, set()

    def visit(file, depth):
        for name in pe_imports(file) or ():
            low = name.lower()
            if low in seen:
                continue
            seen.add(low)
            if low.startswith(("api-ms-win-", "ext-ms-")):
                continue
            local = os.path.join(base, name)
            if os.path.isfile(local):
                if depth < max_depth:
                    visit(local, depth + 1)
            elif not any(os.path.isfile(os.path.join(d, name)) for d in system_dirs):
                missing[name] = os.path.basename(file)

    visit(path, 0)
    return missing


def is_vc_runtime(name):
    """Whether a DLL name belongs to the Microsoft Visual C++ runtime (installed by the "Visual C++ Redistributable", not bundled)."""
    low = name.lower()
    return low.startswith(("vcruntime", "msvcp", "vcomp", "concrt", "ucrtbase")) or low in ("vcruntime140.dll", "vcruntime140_1.dll")
