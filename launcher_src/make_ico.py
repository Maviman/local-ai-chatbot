"""Regenerates app/ui/images/app_icon.ico from the app's existing PNG icon.

Uses only tkinter (already bundled - Tk 8.6 reads/writes PNG natively) plus
stdlib struct/os - no PIL/Pillow needed. Run with the bundled interpreter:
    runtime\\python\\python.exe launcher_src\\make_ico.py
"""

import os
import struct
import tkinter as tk

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
SRC = os.path.join(_ROOT, "app", "ui", "images", "O5_10_ver5.png")
OUT = os.path.join(_ROOT, "app", "ui", "images", "app_icon.ico")
SIZES = [256, 48, 32, 16]


def build():
    root = tk.Tk()
    root.withdraw()
    src = tk.PhotoImage(file=SRC)
    sw, sh = src.width(), src.height()

    entries = []  # (width, height, png_bytes)
    for target in SIZES:
        factor = max(1, round(sw / target))
        img = src.subsample(factor, factor)
        tmp = os.path.join(_HERE, f"_icon_tmp_{target}.png")
        img.write(tmp, format="png")
        with open(tmp, "rb") as f:
            png_bytes = f.read()
        os.remove(tmp)
        w, h = struct.unpack(">II", png_bytes[16:24])
        entries.append((w, h, png_bytes))
        print(f"target {target} -> actual {w}x{h}, {len(png_bytes)} bytes")

    def size_byte(n):
        return 0 if n >= 256 else n

    header = struct.pack("<HHH", 0, 1, len(entries))
    dir_entries = b""
    image_data = b""
    offset = 6 + 16 * len(entries)
    for w, h, png_bytes in entries:
        dir_entries += struct.pack(
            "<BBBBHHII", size_byte(w), size_byte(h), 0, 0, 1, 32, len(png_bytes), offset
        )
        image_data += png_bytes
        offset += len(png_bytes)

    with open(OUT, "wb") as f:
        f.write(header + dir_entries + image_data)
    print(f"wrote {OUT} ({os.path.getsize(OUT)} bytes, {len(entries)} sizes)")


if __name__ == "__main__":
    build()
