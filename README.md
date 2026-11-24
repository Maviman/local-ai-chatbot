# Personal Terminal

*(Formerly "Local AI ChatBot". The launcher file `Local AI ChatBot.exe`, the `.bat` files and this folder keep their names, so existing
shortcuts keep working.)*

A fully self-contained, offline AI chatbot. Everything — the model, the app,
your conversation history — lives inside this folder. Copy the folder to a
USB drive or another Windows PC and it just works. No internet needed, no
installation, nothing phones home.

## How to use

Double-click **`Start Chatbot.bat`**.

It starts the model in the background and opens the chat in its own app
window. The status light in the bottom-left corner turns green when the
model has finished loading (~10–30 seconds from an SSD; a big model on a
hard disk can take a few minutes the first time, and the app stays usable
while it loads).

Double-clicking again while it is still starting — or any time it is
already running — just brings the existing window to the front; it never
starts a second copy.

To shut down, click the **⏻ power button** in the bottom-left of the app.
If something ever gets stuck, run `Stop Chatbot.bat`.

## Features

- **Streaming chat** with markdown rendering (tables, code blocks, lists, math notation…)
- **Conversation history** stored in `data/chats.db` (SQLite) — survives
  restarts, searchable with any SQLite tool
- **Folders** to organize chats, and **Projects** — self-contained
  workspaces with their own files, instructions, and images, kept separate
  from your regular chat history
- **Image generation and editing** (Stable Diffusion 1.5, Flux Kontext,
  Qwen-Image-Edit, or an external ComfyUI instance) triggered right from the
  chat — generate an image, then just describe a change in your next
  message to edit it
- **More than one GPU** — with two or more GPUs the app fills the one with
  the most memory first and only spills the model into the next when it
  doesn't fit; you can reorder them, switch one off, and pick which makes
  images (Settings > Advanced > GPUs, see *More than one GPU* below)
- **Drag-and-drop file/image attachments** (text, PDF, DOCX, images), with
  the token cost of each shown before you send
- A live **context-usage meter** with one-click **Compact** to summarize
  older messages instead of hitting the model's context limit
- **Settings** covering the system prompt and sampling, context size, GPU
  layer offload, KV cache type/quantization, batch size, how the model is
  loaded (Automatic / memory-mapped / fully into RAM), mlock, upload limits,
  and appearance (color theme, launch screen)
- **Tool calling** (opt-in, Settings > General) — lets the model call a
  built-in calculator instead of computing arithmetic itself. Off by
  default since not every model's chat template actually supports it.
- **Export** any conversation or single message to a Markdown file
- **Stop button** to cut off a response mid-generation
- **Replies and thoughts are not cut off** — there is no reply limit by
  default (*Settings → Reply limit* is an optional hard stop) and a thinking
  model's thoughts are capped automatically (*Settings → Automatic thinking
  limit*: what the context window can spare) so it always gets to its answer.
  The one real limit is the model's context window: a reply that reaches it
  keeps what was written, ends with a note saying why, and a warning appears.

## What's in the folder

| Path | What it is |
|---|---|
| `Start Chatbot.bat` | Double-click to launch |
| `models/` | Chat models (`.gguf`) |
| `models/image/` | Image-generation models (Stable Diffusion / Flux / Qwen-Image) |
| `models/embedding/` | Embedding model for Project-file search (enables large projects to exceed the raw-context limit) |
| `bin/` | llama.cpp inference engine (CUDA 13 build for NVIDIA GPUs), with the Microsoft Visual C++ runtime DLLs it needs |
| `bin/engines/<name>/` | Extra llama.cpp builds, one folder each — `cuda12` (CUDA 12.4, for older GPUs such as the Tesla P40) ships here; see *More than one GPU* |
| `bin/sd/` | stable-diffusion.cpp inference engine, for image generation |
| `app/` | The backend server (`server.py`) and the native desktop UI (`app/desktop/`) |
| `data/` | Your conversations (`chats.db`), settings, and logs |
| `outputs/exports/` | Markdown exports you've saved from the app |
| `runtime/` | Portable Python 3.12 (runs the app, nothing to install) |

## Swapping the model

Drop any `.gguf` model file into `models/`. The app uses whichever model
you've picked in **Settings > Model**, or the largest `.gguf` it finds if
you haven't chosen one — picking one there reloads it automatically, no
restart needed. Good sources are the GGUF quantizations published on
Hugging Face (e.g. by `unsloth`, `bartowski`, or `ggml-org`). With a 12 GB
GPU, Q4_K_M files up to ~8 GB run fully on the GPU.

Image models work the same way — drop one into `models/image/` and pick it
from **Settings > Image model**.

A project whose files are too large to fit in context normally can't be
chatted with at all. Dropping a small embedding model (e.g.
`nomic-embed-text-v1.5`, a `.gguf` quantization from Hugging Face, ~150 MB)
into `models/embedding/` and restarting the app fixes this automatically —
oversized projects switch from "blocked" to searching their files for the
excerpts most relevant to what you just asked, instead of trying to include
everything at once. No setting to flip; it only ever activates once a
project actually needs it.

## Copying to another PC

A complete installation is this whole folder: `app\`, `bin\` (the engines), `runtime\` (the bundled Python), `models\`, the launcher
`Local AI ChatBot.exe`, the three `.bat` files and this README (`data\` is optional - it is your conversations and settings; the app makes a
fresh one). Nothing is installed on the PC itself. Copy the folder with a tool that reports errors rather than Explorer's drag-and-drop,
which skips or cuts files short without a word when a portable drive hiccups.

The easiest way is `Make Clean Copy.bat`: double-click it, say where the copy should go and whether to take the image generator and any
models along, and it copies exactly what a PC needs (no `data\`, no `outputs\`, no development leftovers), checks every file against the
original, has the copy's own Python compile the app code, and only then gives the folder its name. It never changes or deletes anything
in this folder. To carry the installation as ONE file instead (a USB stick, a share, an e-mail-sized download), double-click
`Make Transport Zip.bat`: it makes the same clean copy, packs it into `dist\PersonalTerminal-transport-<date>.zip` (a `Personal Terminal`
folder plus `START HERE.txt`), reads the zip back entry by entry, and writes a `.sha256.txt` beside it. Without models and with the image
generator it is a few GB; `--models all` or `--no-sd` change that (see the top of `tests\tools\make_transport_zip.py`). The same thing by
hand, in a command prompt:

```
robocopy "G:\software_projects\Local_AI_ChatBot" "E:\Personal Terminal" /E /Z /R:3 /W:5 /XD __pycache__ tests design dist /XF *.log
```

The folder can also be started from a network path (`\\server\share\...`): the launcher uses absolute paths throughout, so that
works, but a model loads over the network at the network's speed - a local disk is the better home for it.

Then, **on the other PC, double-click `Diagnose.bat` once**: it checks every file of the copy against a record of the working one
(`app\diagnose_manifest.json`, made here) and names anything missing, cut short or damaged - and goes on to check the PC itself
(see *Troubleshooting*). An *update package* (`dist\...zip`, see below) is not an installation: it holds the app's code only, and
extracted into an empty folder it cannot run ("Could not find the app's runtime files").

## Updating another installation

Double-click `Build Update Package.bat`. It rebuilds the launcher and makes `dist\PersonalTerminal-update-<date>.zip`: the app's code
(`app\`, the launcher, the three `.bat` files, this README) plus the few small Microsoft Visual C++ runtime DLLs the engines need (a PC without
that runtime cannot start the model server at all). It never carries your conversations (`data\`), models, the interpreter (`runtime\`) or the
engines (`bin\`, the CUDA libraries). On the other PC: close the app, copy its `app\` folder somewhere if you want to be able to go back, then
extract the zip over the installation and allow overwrite (`HOW_TO_UPDATE.txt` in the zip says the same). The package also carries a fresh
`app\diagnose_manifest.json`, so `Diagnose.bat` on the other PC checks against exactly what was packaged.

Before the package is offered it is checked against this folder (`tests\check_update_package.py`): complete and byte-for-byte what is here,
nothing extra, every script compiles, and the package starts from a fresh copy - you will see "Package verified". A package that fails is
deleted. An older GPU's engine build (`bin\engines\cuda12`, 1.4 GB) is not in the package: copy that folder by hand.

## Hardware notes

- Built for an **NVIDIA GPU**: a CUDA 13 build in `bin/` (RTX 20-series or
  newer, recent driver) and a CUDA 12.4 build in `bin/engines/cuda12/` that
  also covers older cards such as the GTX 10-series / Tesla P40. The app asks
  each build which of your GPUs it can run and uses the one that runs the most
  (see *More than one GPU*). On a PC without a suitable NVIDIA GPU the model
  won't start; swap `bin/` for the `win-vulkan` or `win-cpu` build from
  https://github.com/ggml-org/llama.cpp/releases to run there.
- The app itself (UI + history) runs on any Windows 10/11 machine.
- Ports used (localhost only, nothing exposed to the network):
  `8790` (app server), `8791` (chat model), `8793` (image model),
  `8794` (embedding model, when one is installed).
- The app server (`8790`) only answers the app itself: a request has to come
  from this PC's own loopback address, and one that carries a foreign browser
  `Origin` (a web page trying to reach it) is refused.

### More than one GPU

With two or more GPUs, **Settings → Advanced → GPUs** lists them — biggest
memory first — and shows, before you save, where the next model load would
go ("Next load: GPU 1 … is filled first, then GPU 2 … (GPU 1 63 %, GPU 2 37 %
of the model)"). Nothing is tied to particular cards; it works from what the
engine reports.

- **Fill one GPU at a time (the default).** A model that fits the first GPU
  runs entirely on it and the others are not touched. Only a model that does
  not fit is split: the first GPU is filled as far as it will go (leaving
  room for the context and compute buffers), the rest goes on the next, and so
  on. This is worked out with the engine's own memory estimate for the exact
  split, so it matches what really gets loaded. Without this llama.cpp would
  split every model across all GPUs at once, even when one is plenty.
- **Placement.** *Fill one GPU before using the next*, *Spread across all
  enabled GPUs* (in proportion to their free memory), or *Use the first GPU
  only*.
- **Order and Use.** Up / Down set the order the GPUs are filled in; untick
  *Use* to keep a GPU out of it completely (chat and images). The choice is
  remembered per card (by name and size), so it follows the card rather than
  its slot number, and a card you remove keeps its settings for when it
  returns.
- **A GPU the engine can't run** is listed last with the reason and is not
  used. The default build in `bin\` is CUDA 13, which starts at the RTX 20-series;
  a Pascal card (GTX 10-series, Tesla P40/P100) is run by the CUDA 12.4 build
  that ships in `bin\engines\cuda12\` (the official llama.cpp b9860 release).
  Any other build can be added the same way: put a full release folder
  (`llama-server.exe`, `llama-fit-params.exe` and its DLLs) in
  `bin\engines\<any name>\` — a CUDA 12 build covers older *and* newer cards,
  so one build can run all of them.
  The app asks each build which GPUs it sees (builds differ in what they
  list) and picks the one that can run the most GPU memory (the bundled one
  wins ties); **Engine build** in the same section appears when there is more
  than one and can force one. Which generation each card is comes from
  `nvidia-smi` (installed with the NVIDIA driver).
- **A card the build has only portable code for** — an RTX 20-series on the
  bundled build, which carries native code for the RTX 30/40/50-series only —
  works, but its first load compiles the kernels (measured: one to two
  minutes extra on the first answer, then ordinary load times) and the driver
  keeps the result. The GPU list says so. As a precaution the model server is
  given a 4 GiB driver cache (`CUDA_CACHE_MAXSIZE`) whenever a card in the plan
  needs it: the compiled kernels measured 65–116 MiB, well inside the driver's
  default 256 MiB, but a cache that filled up would silently bring the compile
  back on every load. (Measured with a GPU the CUDA 12 build has only portable
  code for; a P40 or 2080 Super is expected to behave the same, unmeasured.) A
  value you set yourself is left alone.
- **Image generation** uses one GPU at a time (the chat model is unloaded
  while an image is made). By default that is the GPU with the most memory (or
  the first in your order) that the *image* engine can run — a separate build
  from the chat one (`bin/sd/`, CUDA 12, which covers Pascal cards), so it can
  be a card marked "not used" for chat; **Image generation GPU** picks
  another.
- Changing the placement, order, a GPU's *Use* box or the engine build
  reloads the model; the other settings in that section only take effect at
  the next start.
- With one GPU (or none) this does nothing: the section stays hidden and the
  model starts with exactly the same command line it always did. If anything
  in the GPU handling fails — or the engine's device list cannot be read — the
  app falls back to that plain launch rather than refuse to start the model
  (it never concludes "no GPU" from a list it could not read).
- The warning that other programs are holding GPU memory (printed at launch)
  uses the driver's own reading for the GPUs the model is placed on; the
  engine's figure is not used for it, because on Windows it ignores memory
  held by other programs.
- To see what was decided: the **Now running** line under the placement shows
  how the loaded model was started, and `data/desktop.log` records the same
  sentence at every launch ("GPU placement: …", plus a note for each GPU that
  was skipped and why).

### Hard disks and model loading

Models are large, and a hard disk (as opposed to an SSD) is slow at the kind of
scattered reads a memory-mapped load produces — it can make everything else that
touches the disk, the app's own database included, stall for as long as the
load lasts. So with **Settings → Advanced → Model loading** left on
**Automatic**, the app reads the model into RAM in one sequential pass when it
lives on a hard disk and fits comfortably in free memory (no more than 60 % of
it), and memory-maps it otherwise (SSD, or too big for RAM). Pick
*Memory-mapped* or *Fully into RAM* to force either; the Advanced tab's live
memory readout says so when the running model was read fully into RAM.

## Look, motion and sound

The look itself (the phosphor colour from *Settings → Appearance*, the plain boxes, the key legend along the bottom
of every screen) is always on. Three effects run on top of it. Each is set on **Settings → Sound & motion** (category
08, or the `8` key on Settings), and a change there applies at once, without SAVE:

- **Motion.** *FULL*: titles, new values and notices type on behind a block cursor, a real wait shows the cursor,
  and the window powers down with a short collapse. *REDUCED*: nothing moves, colour cues only. *OFF*: completely
  static. Until you pick one it follows the Windows setting "Show animations in Windows".
- **CRT texture.** *OFF*, *LOW* or *FULL* (scanlines and a soft glow on titles and plates). *FULL* on a first run.
- **Sound.** Short synthesised cues: a screen change, a message sent, a reply that finishes while you are looking
  elsewhere, an error or a dangerous prompt opening, a model load, shutting down. On, at 40 %, on a first run; the
  volume, the five cue groups, when a finished reply plays, and "quiet when Windows says so" (do not disturb) are on
  the same page. **Alt+M** mutes or unmutes every cue from any screen.

### Turning the effects off

If motion, texture or sound ever misbehave on a PC, or you want none of them, create an empty file named
`fx_disabled` (no extension) in the `data\` folder and start the app again. Motion, the CRT texture and sound are
then off (and so are the timers that drive them); the look stays, and every screen, setting and action works as
before. The Sound & motion page then shows its settings read-only, with a line saying why. Delete the file and
restart to get the effects back; your settings are kept either way. A shortcut launch needs nothing else.

Three environment variables do the same kind of thing for a run started from a command prompt (`set NAME=value`,
then `Start Chatbot.bat` in the same window). They are read when the app starts:

| Variable | Effect |
|---|---|
| `LOCALAI_FX=0` | The same as the `data\fx_disabled` file: no motion, no CRT texture, no sound. |
| `LOCALAI_SFX=0` | Sound only: the sound engine is not started for this run. Motion and CRT stay as set. |
| `LOCALAI_SFX_SILENT=1` | For tests and tools: the sound engine runs and decides every cue as usual, but nothing ever reaches the speakers. Once a run has seen it, that run stays silent. The test runner and the UI harness always set it. |

`0` may also be written `false`, `off` or `no`; for `LOCALAI_SFX_SILENT` any value other than empty, `0`, `false`,
`no` or `off` counts as on.

## Troubleshooting

- **First, for anything that does not start: double-click `Diagnose.bat`** and read (or send) the report it writes to
  `data\diagnostics-<stamp>.txt`. It changes nothing. It checks every file of the installation against the record of a working copy
  (a damaged or incomplete copy is the usual cause of "`python3.dll` is either not designed to run on Windows or it contains an error,
  error status `0xC000012F`" and of the launcher's "Could not find the app's runtime files"), whether a copy of the app is already
  running (even one started from another drive - every later launch then exits silently), who holds the app's ports, the NVIDIA driver
  and GPUs, each engine build's DLLs and whether it runs, and finally starts the model server once on a spare port and asks it a
  question. Lines starting with `!!` are the problems, in order of likelihood. It takes a few minutes (`--no-start-test` skips the
  model-server start; `--quick` keeps only the file, Python and process checks).
- **A box appears instead of the window.** The launcher and the app say what stopped them, in plain words, and each box ends with the
  `Diagnose.bat` line above when that file is in the folder: *"some of its files are not next to this program"* (an incomplete copy, or
  an update package extracted into an empty folder - the missing files are listed); *"stopped right after it started (Windows error
  code 0x...)"* (a program file that is damaged or not a Windows program at all, a DLL Windows could not find, or security software
  blocking the program - the box names which and what to do); *"already running, started from ..."* (a copy of the app is open,
  possibly from another drive: use that window, or run that folder's `Stop Chatbot.bat`); *"cannot write to its data folder"*;
  *"part of the app's code is missing or damaged"*; *"Port 8790 is in use by ..."*.
- **Window opens but status stays "Loading model…" for minutes** — first load
  from a hard drive can be slow (a 12 GB model takes roughly two to three
  minutes cold; subsequent loads are faster once the OS has cached it). The app
  stays responsive meanwhile. Check that a `.gguf` file exists in `models/`, and
  `data/llama-server.log` if it ends in an error instead.
- **No app window opens** — check `data/desktop.log` for errors (once it passes 5 MB it is moved aside as
  `desktop.log.1` at the next launch, so it cannot grow for ever).
- **Model crashed or won't start** — check `data/llama-server.log`. The previous run's log is kept as
  `llama-server.log.1`, so a crash's record survives the restart that follows; each is kept under 50 MB (a server that floods
  its log once wrote 1.3 GB), the oldest part moving to `.1`.
- **"The model server is not running" / `WinError 10061` ("the target machine
  actively refused it")** — nothing is listening where the app talks to the
  model server: it never started, is still loading, or stopped. The app says
  which, with the exit code and what its own log says. The usual causes:
  - *A DLL is missing.* Windows then starts the server and ends it at once
    (exit code `0xC0000135`) with nothing in the log; the app reads the
    engine's import table and names what is missing. The engines need the
    Microsoft Visual C++ runtime, which a clean Windows does not have, so
    `msvcp140.dll`, `vcruntime140.dll` and `vcruntime140_1.dll` (and for
    image generation `vcomp140.dll`, `msvcp140_codecvt_ids.dll`) sit beside
    them — copies of Microsoft's redistributable files. (The image server is
    checked the same way *before* the chat model is unloaded for it, and one
    that ends at once is reported at once instead of after a two-minute wait.)
    If they were removed,
    copy them back or install the *Microsoft Visual C++ Redistributable
    (x64)* from https://aka.ms/vs/17/release/vc_redist.x64.exe. The CUDA part
    also needs the NVIDIA driver (`nvcuda.dll`). An update package
    (`Build Update Package.bat`) carries those few small DLLs along with the
    app's code, but never the engines: `bin\engines\cuda12\`, if a GPU there
    needs that build, has to be copied over by hand.
  - *A GPU the engine build was not made for* (a Pascal card such as the Tesla
    P40 under the CUDA 13 build) — see *More than one GPU*; the `cuda12` build
    in `bin/engines/` runs it.
  - *The port is taken.* A model server left over from an earlier run of this
    chatbot (a crash, or the app force-closed) is stopped automatically
    before a new one starts; any other program holding port `8791` is named in
    the message — close it, or run `Stop Chatbot.bat`.
  - *Out of memory* — lower the GPU layers or the context size in Settings.
  A reply that breaks off because the server died is kept, followed by what
  happened and why. ComfyUI, which this app does not start, is named as "not
  reachable" with the address it tried when it is not running.
- **Want a console with logs?** Run
  `runtime\python\python.exe app\server.py` from a terminal in this folder
  to run just the backend API in the foreground (Ctrl+C to stop) — useful
  for development, but it won't open the native window.

## Development

Regression tests (developer-only; not part of update packages) live in
`tests/`:

```
runtime\python\python.exe tests\run_all.py
```

They run against throwaway copies of the database and settings with fake model
servers, so they never touch your conversations or models (the runner checks
that, too, and fails if a file in `data/`, `models/` or `outputs/` changed).
See `tests/README.md` for what each file covers, and `tests/diagnostics/` for the
tools that reproduce and measure the slow-disk model-load problem.
