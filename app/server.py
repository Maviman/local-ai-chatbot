"""
Personal Terminal (formerly Local AI ChatBot) — portable, offline chat server.

Responsibilities:
  * Spawn llama-server (llama.cpp) with the model found in ../models
  * Serve the local REST API used by the native tkinter GUI (app/desktop_main.py)
  * Persist conversations to ../data/chats.db (SQLite)
  * Proxy streaming chat completions to llama-server

Uses only the Python standard library, plus the vendored pure-Python
`pypdf` under app/vendor_py (no compiled/pip-installed dependencies),
so it runs on the bundled embeddable Python with no installed packages.
"""

import ast
import base64
import csv
import gc
import http.client
import io
import json
import logging
import math
import mimetypes
import operator
import os
import queue
import re
import secrets
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import traceback
import uuid
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor_py"))
import pypdf  # noqa: E402  (vendored, pure Python, no dependencies on Python 3.12)

# gpu_plan.py (GPU discovery, engine compatibility, placement planning) sits next to this file. The embedded runtime's ._pth keeps a
# script's own folder off sys.path, so running server.py directly (the headless mode) would not find it without this.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)
import gpu_plan  # noqa: E402
import pe_imports  # noqa: E402  (which DLLs a Windows program needs and which are missing: names why a model server dies at once)

logging.getLogger("pypdf").setLevel(logging.ERROR)

APP_PORT = 8790
LLAMA_PORT = 8791
LLAMA_URL = f"http://127.0.0.1:{LLAMA_PORT}"
SD_PORT = 8793
SD_URL = f"http://127.0.0.1:{SD_PORT}"
# The embedding server runs the same llama-server.exe binary as chat (in
# --embedding mode against a small dedicated model) as a 3rd concurrent
# subprocess - see start_embed_server().
EMBED_PORT = 8794
EMBED_URL = f"http://127.0.0.1:{EMBED_PORT}"
# The model servers (llama-server for chat, the same binary for embeddings) are started with this key (--api-key) and
# refuse any request without it; it is made fresh for every launch of this app and never written anywhere. Before,
# any web page open in a browser on this PC could talk to the model server on its port (its CORS answer reflected
# the origin, and it checked no Host). Every request below carries it through llama_headers(); llama-server keeps
# /health public. sd-server (the image server) has no key option in this build.
LLAMA_API_KEY = secrets.token_hex(16)


def llama_headers(extra=None):
    """The headers every request to a model server carries: the per-launch key, plus `extra`."""
    headers = {"Authorization": "Bearer " + LLAMA_API_KEY}
    if extra:
        headers.update(extra)
    return headers

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.path.join(ROOT, "models")
MMPROJ_DIR = os.path.join(MODELS_DIR, "mmproj")
SD_MODELS_DIR = os.path.join(MODELS_DIR, "image")
FLUX_COMPONENTS_DIR = os.path.join(SD_MODELS_DIR, "flux-components")
EMBED_MODELS_DIR = os.path.join(MODELS_DIR, "embedding")
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "chats.db")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
LLAMA_LOG_PATH = os.path.join(DATA_DIR, "llama-server.log")
SD_LOG_PATH = os.path.join(DATA_DIR, "sd-server.log")          # the image server's own output (before: thrown away, so a failure had no reason)
# The model server's log is kept under this size (the part past it goes on in llama-server.log, the part before it becomes
# llama-server.log.1): one run once wrote a 1.3 GB log - see pump_process_log.
LLAMA_LOG_MAX_BYTES = 50 * 1024 * 1024
# Duplicates app/desktop/theme.py's own THEME_PATH constant (same file,
# computed independently on each side since server.py has zero imports from
# app/desktop/* and that boundary is worth keeping) - only used by
# handle_backup/handle_restore below, which need to read/write it without
# importing the desktop package. Keep the filename in sync with theme.py if
# it ever changes.
THEME_PATH = os.path.join(DATA_DIR, "desktop_theme.json")
LLAMA_EXE = os.path.join(ROOT, "bin", "llama-server.exe")
SD_EXE = os.path.join(ROOT, "bin", "sd", "sd-server.exe")
DEFAULT_OUTPUTS_DIR = os.path.join(ROOT, "outputs")

DEFAULT_SETTINGS = {
    "system_prompt": (
        "You are a helpful, thoughtful assistant. Be warm and conversational. "
        "Give thorough answers to complex questions and concise answers to simple ones. "
        "Use markdown formatting when it aids readability."
    ),
    "temperature": 0.7,
    # These three match llama-server's own built-in defaults (see
    # `llama-server --help`) so introducing them doesn't silently change
    # existing users' generation behavior until they touch a slider.
    "min_p": 0.05,
    "top_p": 0.95,
    "top_k": 40,
    "context_size": 16384,
    # How many chat replies llama-server generates at once (its --parallel
    # slot count). Each slot gets the FULL configured context_size - see
    # start_llama()'s comment for why this multiplies -c rather than
    # dividing it, unlike the earlier design this replaced.
    "max_parallel_chats": 1,
    "active_model": None,
    "outputs_dir": None,
    "confirm_save_location": False,
    "active_mmproj": None,
    "active_image_model": None,
    # A user-configured external tool/site for image generation - the
    # desktop app's image-gen action opens this rather than running a local
    # model in-process (see the project's image-gen redesign decision).
    "image_gen_link": None,
    # "local" (stable-diffusion.cpp, in-process, managed by this app) or
    # "comfyui" (an externally-run ComfyUI instance this app only talks to
    # over HTTP - never launched/stopped by this app, unlike sd-server, so
    # none of the chat-model-swap/residency logic applies to it).
    "image_backend": "local",
    "comfyui_url": "http://127.0.0.1:8188",
    # Path to a workflow exported from ComfyUI's UI as "API format" JSON.
    # comfyui_prompt_marker is a literal placeholder string the user puts in
    # that workflow's positive-prompt text field - substituted with the real
    # prompt before submitting. Deliberately not "smart" node-type/graph
    # detection (e.g. guessing which CLIPTextEncode node is "the" positive
    # prompt) - workflows vary too much for a heuristic to be trustworthy
    # sight-unseen, and a literal marker the user places themselves is both
    # simpler and cannot silently substitute into the wrong field.
    "comfyui_workflow_path": None,
    "comfyui_prompt_marker": "__PROMPT__",
    # The response-length control - see RESPONSE_LENGTH_PROMPTS: what the model
    # is TOLD about length. It shapes a reply; it never cuts one off (it used to
    # also set a hard token cap per level, which ended long answers and long
    # thoughts mid-sentence - see max_reply_tokens). Kept under the old
    # "verbosity" key so existing settings.json files carry over without a
    # migration.
    "verbosity": "normal",
    # A hard limit on ONE reply, thinking included, in tokens. 0 = none, the
    # default: a reply then ends when the model finishes it, or when the context
    # window is full - never at an arbitrary number. A limit is for someone who
    # wants one, e.g. to stop a model that never finishes: it is cut off there,
    # which is flagged to the user.
    #
    # (llama.cpp's --context-shift - keep generating past a full window by
    # dropping the oldest tokens - was tried and is NOT used: measured on the
    # bundled build, every installed model reports "KV cache shifting is not
    # supported for this context" with this app's configuration (sliding-window
    # attention and hybrid/recurrent models); Gemma only shifts with --swa-full,
    # which costs several GB of VRAM at a 16k context, and the hybrids cannot at
    # all. The flag would have been ignored on every model.)
    "max_reply_tokens": 0,
    # Off by default now that DRY (below) is on. repeat_penalty works per
    # *token*, so on a long answer it progressively pushes the model off its
    # own vocabulary - including the ordinary connective words and structural
    # punctuation any sustained prose needs - which is a direct cause of the
    # drift-into-waffle failure mode. DRY penalizes repeated *sequences*
    # instead, which is what actually wants suppressing. Running both at once
    # double-penalizes; leave this off unless DRY alone isn't holding.
    "repeat_penalty_enabled": False,
    "repeat_penalty": 1.1,
    # DRY ("Don't Repeat Yourself") sampler. 0.8/1.75/2 are llama.cpp's own
    # documented starting values. dry_penalty_last_n is pinned to -1 (the
    # whole context) in sampling_params rather than exposed - a short window
    # defeats the point, since the loops worth catching in a long answer are
    # exactly the ones that repeat something said thousands of tokens ago.
    "dry_multiplier": 0.8,
    "dry_base": 1.75,
    "dry_allowed_length": 2,
    # Both default to off. presence_penalty is the one worth reaching for
    # when a model keeps circling the same few ideas (it's a flat one-time
    # penalty per already-used token, so unlike repeat_penalty it doesn't
    # compound with response length); some model cards ask for it explicitly.
    "presence_penalty": 0.0,
    "frequency_penalty": 0.0,
    # --- reasoning-model thinking controls (see model_thinking_capability) ---
    # All three are inert on a model whose template has no thinking, and the
    # Settings screen greys them out in that case rather than hiding them, so
    # it stays obvious *why* they don't apply.
    #
    # "auto" leaves the template's own default alone; "on"/"off" pass
    # enable_thinking through chat_template_kwargs. Only meaningful for
    # think-tag models - harmony templates have no off switch and use
    # thinking_effort below instead.
    "thinking_mode": "auto",
    # harmony (gpt-oss) only. "medium" is that template's own documented
    # default, so this changes nothing until the user moves it.
    "thinking_effort": "medium",
    # True: thinking may use up to THINKING_CONTEXT_SHARE of the context window
    # (less if a reply limit is set: half of it), so a long think can never
    # eat the whole window and leave no room for the answer. See
    # thinking_budget_tokens.
    "thinking_budget_auto": True,
    # Used when thinking_budget_auto is off. 0 means "no limit" and is
    # translated to -1 on the command line - never passed through as a literal
    # 0, which this llama.cpp build documents as "immediate end" but actually
    # treats as unlimited-with-no-answer (verified: budget 0 produced 1284
    # chars of thinking and zero content).
    "thinking_budget": 1024,
    "max_upload_mb": 20,
    "max_file_tokens": 25_000,          # absolute per-file sanity cap, independent of the combined budget below
    "project_content_budget_pct": 0.5,  # fraction of the effective per-slot context reserved for project content
    # When true, start_llama() omits -ngl entirely and lets llama.cpp's own
    # --fit (on by default since well before the bundled build 9860) size the
    # offload against its real allocator. That is strictly better than this
    # file's safe_gpu_layers heuristic, which has to guess at compute-buffer
    # growth and can only ever undershoot to stay safe. Measured 2026-08-20 on
    # the 12GB RTX 5070, at the same 16384 context: Qwen3.6-27B-UD-IQ2_XXS ran
    # 4.98 tok/s under safe_gpu_layers' -ngl 41 (8391 MiB used, GPU 12% busy)
    # vs 43.92 tok/s letting --fit choose (11604 MiB, GPU 72%) - 8.8x, with
    # 3.2GB of VRAM simply going unused in the first case. Qwythos-9B-MTP,
    # which already fit entirely in VRAM either way, went 51.3 -> 84.6 tok/s
    # for 98 MiB more, purely because safe_gpu_layers caps -ngl at block_count
    # and so never offloads the output/embedding tensors.
    #
    # gpu_layers below stays the manual override (used only when this is
    # false), and stays an int so the /api/settings contract and the Advanced
    # VRAM meter don't have to deal with a union type.
    "gpu_layers_auto": True,
    "gpu_layers": 99,
    # --- more than one GPU (the logic lives in gpu_plan.py) ---
    # "fill_first": the first GPU in the order below is used alone when the whole model fits in it; only when it does not is it
    # filled to capacity and the rest put on the next one (llama.cpp's own default splits across every GPU at once, even when one
    # is enough, and every token then passes through all of them). "spread": every enabled GPU, split by llama.cpp in proportion
    # to free memory. "single": the first GPU only. Does nothing on a machine with one GPU.
    "gpu_placement": "fill_first",
    # The GPUs in the order they are used, as keys (gpu_plan.gpu_key: name and memory). One not listed follows by memory, largest
    # first, so an empty list means "biggest first". Keys of GPUs that are no longer installed are ignored.
    "gpu_order": [],
    # Keys of GPUs that must not be used.
    "gpu_disabled": [],
    # Which installed llama.cpp build runs the model: "auto" (whichever can run the most of the GPUs; the one in bin\ unless another
    # in bin\engines\<name>\ does better) or the folder name of one.
    "engine": "auto",
    # The GPU image generation runs on: "auto" (the first usable GPU in the order above) or a key. Applies the next time the image
    # engine starts.
    "image_gpu": "auto",
    "kv_offload": True,
    "kv_cache_type": "q8_0",
    # llama.cpp's own built-in defaults (2048 logical / 512 physical) -
    # batch_size drives both -b and -ub (see start_llama()), so the default
    # here must match those exactly or every fresh install would silently
    # get different prompt-processing VRAM behavior than before this setting
    # existed.
    "batch_size": 2048,
    # How llama-server reads the model file: "auto" (read it fully into RAM
    # when it sits on a spinning disk and fits comfortably, memory-map it
    # otherwise), "mmap" (always memory-map) or "ram" (always read it fully
    # into RAM). Replaces the old boolean disable_mmap - see
    # resolve_model_load() for the measurements behind the automatic choice.
    "model_load_mode": "auto",
    "mlock_model": False,
    # Opt-in, not opt-out: confirmed via a real spike that chat-template
    # tool-call support is genuinely model-dependent (one of three bundled
    # models tested returned real structured tool_calls, one fell back to
    # its own different plain-text convention) - defaulting this on would
    # silently change generation behavior for models never verified to
    # handle it well.
    "tools_enabled": False,
    # The document tool (create_document - see the tool-calling section): offered to the model whenever this is on,
    # independently of the calculator switch above. On by default: it is the one tool a chat is for ("make a system
    # prompt for ..." becomes a document card), and a model without tool-call support simply never calls it.
    "document_tool_enabled": True,
    # The folder the document viewer's SAVE AS dialog opens in (the last one used); None until a first save.
    "document_save_dir": None,
}

KV_CACHE_TYPES = {"f16", "q8_0", "q4_0"}
# Approximate bytes-per-element for each supported KV cache quantization —
# used only for the rough VRAM-fit estimate shown in Advanced Settings, not
# passed to llama.cpp (which takes the string type directly).
KV_CACHE_BYTES = {"f16": 2.0, "q8_0": 1.0625, "q4_0": 0.5625}

# What the model is actually told about length. The old three-level version
# of this said only "Provide thorough, comprehensive responses" at the top
# end, which asks for length without asking for structure - an open
# invitation to pad, restate, and trail off, which is the reported failure
# mode. Every level past "normal" now pairs the request for length with the
# constraints that keep a long answer on the rails: plan first, cover each
# point once, and stop when done rather than filling a quota.
RESPONSE_LENGTH_PROMPTS = {
    "concise": (
        "Keep responses short and focused — a few sentences unless more detail is "
        "explicitly requested."
    ),
    "normal": "",
    "detailed": (
        "Give a thorough answer: cover the angles that matter and include concrete "
        "examples or reasoning where they earn their place. Work out what the answer "
        "needs before you start writing, and make each point once — don't restate an "
        "earlier point in different words later, and don't close with a summary that "
        "just repeats the body."
    ),
    "long": (
        "This calls for a long, sustained answer. Plan the sections it needs before "
        "you start, then write them in order.\n"
        "- Each section must carry something the earlier ones didn't. If you find "
        "yourself rephrasing a point you already made, move on to the next one instead.\n"
        "- Go deep rather than wide: develop each point properly instead of listing "
        "many shallow ones.\n"
        "- Cut filler. No restating the question back, no throat-clearing about what "
        "you're about to cover, no closing summary that repeats the body.\n"
        "- Stop when the last planned section is done. Length is a budget, not a "
        "target — finishing early is fine, and better than padding."
    ),
}

# (There used to be a hard token cap per level here - 1,024 / 4,096 / 8,192 /
# 16,384 - as a backstop against a model that never stops. With no way to
# continue, everything past it was lost: long answers and, worse, long
# thoughts - a reasoning model could spend the whole cap thinking and answer
# with nothing - ended mid-sentence at a number nobody chose. The levels now
# only change what the model is told, and a limit exists only if the user sets
# one: max_reply_tokens.)

# Order matters - the desktop Settings screen renders this as a slider and
# indexes into it, so appending is safe but reordering is not.
RESPONSE_LENGTH_LEVELS = ["concise", "normal", "detailed", "long"]

# One-time captioning prompt for project images (see caption_image()) - the
# resulting text is what actually gets injected into every chat turn in that
# project, not the image itself, so it needs to be detailed enough to stand
# in for the image on its own.
CAPTION_PROMPT = (
    "Describe this image in detail for future reference in a conversation. "
    "Include any text visible in the image verbatim. Be thorough but factual."
)


def sampling_params(settings):
    """Model-level sampling parameters shared by chat and regenerate requests.

    llama-server's /v1/chat/completions accepts llama.cpp's own sampler
    fields alongside the OpenAI-compatible ones, so DRY and the penalties go
    in the same body as temperature/top_p - no separate endpoint needed.
    Verified against the bundled build 9860, which lists --dry-multiplier,
    --presence-penalty and --frequency-penalty in its --help."""
    params = {
        "temperature": settings.get("temperature", 0.7),
        "repeat_penalty": (
            settings.get("repeat_penalty", 1.1)
            if settings.get("repeat_penalty_enabled", False) else 1.0
        ),
        "min_p": settings.get("min_p", 0.05),
        "top_p": settings.get("top_p", 0.95),
        "top_k": settings.get("top_k", 40),
        "presence_penalty": settings.get("presence_penalty", 0.0),
        "frequency_penalty": settings.get("frequency_penalty", 0.0),
    }
    # Omitted unless the user set a limit: llama-server's own default is "until the model is done" (-n -1).
    reply_limit = response_max_tokens(settings)
    if reply_limit is not None:
        params["max_tokens"] = reply_limit
    dry_multiplier = float(settings.get("dry_multiplier", 0.8) or 0.0)
    if dry_multiplier > 0:
        params.update({
            "dry_multiplier": dry_multiplier,
            "dry_base": settings.get("dry_base", 1.75),
            "dry_allowed_length": int(settings.get("dry_allowed_length", 2)),
            # -1 = the whole context (see the DEFAULT_SETTINGS note).
            "dry_penalty_last_n": -1,
        })
    # Not a sampler, but it belongs to the same per-turn request and this is
    # the one place both chat entry points (handle_chat and
    # handle_edit_message) already share. Omitted entirely rather than sent
    # empty when the model's template has no thinking knob.
    template_kwargs = thinking_template_kwargs(settings)
    if template_kwargs:
        params["chat_template_kwargs"] = template_kwargs
    return params


def thinking_template_kwargs(settings, model_path=None):
    """chat_template_kwargs for this turn, or {} when the active model's
    template has no thinking knob. Per-request, so changing these never
    reloads the model."""
    cap = model_thinking_capability(model_path or find_model())
    if cap["control"] == "enable_thinking":
        mode = settings.get("thinking_mode", "auto")
        if mode in ("on", "off"):
            return {"enable_thinking": mode == "on"}
        return {}
    if cap["control"] == "reasoning_effort":
        effort = settings.get("thinking_effort", "medium")
        if effort == "max":
            effort = "high"                  # gpt-oss has three levels; its top one is the most it can do
        if effort in (THINKING_STYLES["harmony"]["efforts"] or []):
            return {"reasoning_effort": effort}
    return {}


# The share of one chat's context window the automatic thinking budget may use; the rest is for the prompt and the answer.
THINKING_CONTEXT_SHARE = 0.7
# Thinking effort on a think-tags model (which has no effort knob of its own, only on / off): the share of the context
# window its automatic budget may use. "medium" (the default) is the old fixed share, so nothing changes until the user moves
# it; "max" takes everything but THINKING_ANSWER_RESERVE tokens.
THINKING_EFFORT_SHARE = {"low": 0.25, "medium": THINKING_CONTEXT_SHARE, "high": 0.8, "max": 1.0}
THINKING_EFFORT_LEVELS = ("low", "medium", "high", "max")
THINKING_ANSWER_RESERVE = 2048        # tokens of the window never given to thinking: the answer needs room


def thinking_budget_tokens(settings, model_path=None):
    """The --reasoning-budget value to launch llama-server with, or None to
    leave the flag off entirely (model can't think, or the user asked for no
    limit). Never returns 0 - see the thinking_budget default's note."""
    cap = model_thinking_capability(model_path or find_model())
    # budget_enforced, not just supports: on a style llama.cpp can't actually
    # hold to a budget, passing the flag would do nothing except make
    # Response length a restart-triggering setting. See THINKING_STYLES.
    if not cap["supports"] or not cap["budget_enforced"]:
        return None
    if settings.get("thinking_budget_auto", True):
        # "Automatic": as much thinking as the context window can spare while the
        # answer still has room - THINKING_CONTEXT_SHARE of it. A model that thinks
        # without any limit can spend the WHOLE window on its thoughts and never
        # reach an answer (the window is a hard stop), so a forced end of the
        # thinking at that point turns "no answer at all" into "the thought was
        # cut off, and the answer follows". It used to be half of a fixed reply
        # cap - 2,048 tokens at the default - which cut ordinary long thoughts
        # short while leaving most of the window unused. With a reply limit set,
        # thinking also never gets more than half of that (reproduced live: a
        # 300-token cap on Qwythos-9B was spent entirely on thinking and
        # returned an empty reply).
        try:
            context = int(settings.get("context_size", 16384) or 16384)
        except (TypeError, ValueError):
            context = 16384
        effort = settings.get("thinking_effort", "medium")
        share = THINKING_EFFORT_SHARE.get(effort, THINKING_EFFORT_SHARE["medium"])
        budget = int(context * share)
        if share > THINKING_CONTEXT_SHARE and budget > THINKING_ANSWER_RESERVE and context - budget < THINKING_ANSWER_RESERVE:
            budget = context - THINKING_ANSWER_RESERVE
        reply_limit = response_max_tokens(settings)
        if reply_limit is not None:
            budget = min(budget, reply_limit // 2)
        return max(128, budget)
    budget = int(settings.get("thinking_budget", 1024) or 0)
    return budget if budget > 0 else None


def response_max_tokens(settings):
    """The hard limit on one reply (thinking included), in tokens, or None for
    no limit - the default. Only a limit the user set (max_reply_tokens)
    exists; the response-length level never caps anything."""
    raw = settings.get("max_reply_tokens")
    if isinstance(raw, bool):            # a stray true is not a one-token limit
        return None
    try:
        limit = int(raw or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    return limit if limit > 0 else None


def describe_cut_off(reply_limit, generated, settings, model_path=None):
    """Why a reply that llama-server ended with finish_reason "length" was cut
    short: {"reason": "limit" | "context", "generated": tokens it produced,
    "limit": the reply limit sent with the request or None, "context": the
    context size one chat gets, "max_context": the most the model was trained
    for, or None}. "limit" only when a limit was sent and the reply reached it
    (the token count comes from the stream, so a few tokens of slack);
    otherwise the context window filled up."""
    try:
        context = int(settings.get("context_size", 16384) or 16384)
    except (TypeError, ValueError):
        context = 16384
    generated = int(generated or 0)
    # a few tokens of slack for the stream's own count, in proportion for a small limit
    reached = bool(reply_limit) and generated >= int(reply_limit) - min(8, int(reply_limit) // 20)
    max_context = None
    if model_path:
        try:
            max_context = gguf_context_length(model_path)
        except Exception:
            max_context = None
    return {"reason": "limit" if reached else "context", "generated": generated,
            "limit": int(reply_limit) if reply_limit else None, "context": context,
            "max_context": max_context if isinstance(max_context, int) and max_context > context else None}


def cut_off_note(cut, has_answer, has_thoughts):
    """The italic line a reply that was cut short carries. It is streamed live
    and saved with the reply, so the reply says so after a reload as well - a
    reply that just stops mid-sentence, with no word of why, is what this
    replaces."""
    if cut["reason"] == "server":
        message = cut.get("message") or "See the end of data\\llama-server.log for why."
        if has_answer:
            return f"\n\n_(Reply interrupted: the connection to the model server ended before the reply was complete. {message})_"
        if has_thoughts:
            return f"_(The connection to the model server ended while it was still thinking, so there is no answer. Its thoughts so far are saved under Thoughts. {message})_"
        return f"_(The connection to the model server ended before it wrote anything. {message})_"
    if cut["reason"] == "limit":
        why = f"the reply limit of {cut['limit']:,} tokens was reached"
        fix = "Raise or remove the reply limit in Settings, or ask it to continue."
    else:
        why = f"the context window ({cut['context']:,} tokens) filled up"
        room = f" (this model allows up to {cut['max_context']:,})" if cut.get("max_context") else ""
        fix = f"Compact the conversation, start a new chat or raise the context size in Settings{room}, then ask it to continue."
    if has_answer:
        return f"\n\n_(Reply cut off: {why}. {fix})_"
    if has_thoughts:
        return f"_(The model never got to its answer: {why} while it was still thinking. Its thoughts are saved under Thoughts. {fix})_"
    return f"_(No reply: {why}. {fix})_"

# Ceiling on a single conversation's unsent composer draft. Generous for
# anything anyone would actually type into a chat box - a paste larger than
# this belongs as a file attachment, which has its own (larger) budget. Exists
# only so a runaway paste can't push an unbounded blob into every autosave.
MAX_DRAFT_CHARS = 100_000

MAX_IMAGE_BYTES = 10 * 1024 * 1024
DOWNLOAD_CHUNK = 4 * 1024 * 1024

# Mirrors app/desktop/chat_view.py's IMAGE_EXTENSIONS/UNSUPPORTED_IMAGE_EXTENSIONS -
# needed server-side too since project-image uploads are a raw multipart stream
# (app/desktop/api_client.py's _stream_upload), not routed through the chat
# drop handler that does this filtering client-side.
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp"}
UNSUPPORTED_IMAGE_EXTENSIONS = {".heic", ".heif", ".webp", ".tif", ".tiff"}
_IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".bmp": "image/bmp",
}

llama_proc = None
llama_start_error = None
llama_restart_lock = threading.Lock()
# VRAM snapshot taken immediately *before* the current llama-server was
# spawned, so "used" at that instant is by definition all foreign - other
# programs, not us. This is the only moment the two are separable: once
# llama-server is up, query_process_vram_mb() can't attribute VRAM per-process
# on consumer/WDDM GPUs (see its docstring), so a card-wide reading can no
# longer tell "our model" from "someone else's job". Keeping the launch-time
# figure is what lets the UI say *why* offload came out small, instead of
# silently falling back to CPU and reading as "the app can't load this model".
# None when no model has been started this session, or when nvidia-smi is
# unavailable. See FOREIGN_VRAM_WARN_MB.
llama_launch_vram = None
# A3: how the model file compares with the memory this PC could give it at the last launch (free RAM + free VRAM):
# {"too_large", "model_gb", "memory_gb", "ram_gb", "vram_gb"} from model_fit, kept after a crash so the UI can still say it.
llama_launch_fit = None
# A1: a model server that is up (its port answers) but has stopped answering its health check is "hung", not
# "starting" - see llama_health. A hung verdict needs HUNG_AFTER_S of failed probes after at least one ok.
HUNG_AFTER_S = 20
_health_fail_times = []
_health_ok_seen = False
# How the current llama-server was told to read the model file: into_ram True
# means --no-mmap. Set by start_llama(); reported by /api/vram-status so the UI
# can say what "Automatic" actually decided.
llama_launch_load = {"into_ram": False}
# conv_id -> llama-server slot id (0..max_parallel_chats()-1) for every chat
# reply currently generating - replaces the old single `chat_active` bool now
# that more than one can run at once. Every read/write of this dict, or of
# the slot-id assignment derived from it, must hold active_chats_lock so two
# near-simultaneous /api/chat requests can't both pass the "is a slot free"
# check and grab the same slot id.
active_chats = {}
active_chats_lock = threading.Lock()
# conv_ids with a generate_conversation_title() background thread still in
# flight - lets the client poll for "is the title still being generated"
# instead of guessing with a fixed delay (see generate_conversation_title
# and its /api/conversations/{id}/title-status route).
title_pending = set()
title_pending_lock = threading.Lock()
sd_proc = None
sd_start_error = None        # why start_sd_server() could not start the image server, when it could not (None otherwise)

# Sentinels for the desktop GUI's main-thread event loop (e.g. a second
# launch attempt asking the already-running instance to re-show its window).
gui_events = queue.Queue()

download_progress = {}
download_progress_lock = threading.Lock()

image_gen_status = {"active": False, "stage": None, "error": None, "cancelled": False}
image_gen_lock = threading.Lock()

# Left True after a generate/edit job succeeds, instead of immediately
# restoring the chat model - lets a burst of back-to-back generate/edit
# calls (a Grok-style "make it nighttime", "now add a cat" chain) skip the
# unload/reload cycle entirely and reuse the already-warm sd-server.
# Confirmed live (2026-08-03): a second request to an already-running
# sd-server process skips its lazy first-use tensor-loading/conversion step
# entirely (the dominant cost on a cold start under VRAM pressure), though
# per-request text encoding/sampling cost is unaffected - the win scales
# with how many image ops happen in a row before the next real chat message.
# All three of these are guarded by image_gen_lock, same as image_gen_status.
image_model_resident = False
sd_resident_model_path = None
last_image_activity = 0.0
# How long the image model is left resident with no new generate/edit before
# the idle-restore watcher (see _idle_restore_watcher) restores the chat
# model on its own - otherwise a walked-away-from session would leave chat
# unusable indefinitely.
IDLE_RESTORE_SECONDS = 300

# handle_compact() makes its own (non-streaming) request to llama-server -
# unlike chat/image-gen, it previously had no re-entrancy guard of its own,
# only checking active_chats/image_gen_status at entry. Confirmed live: since
# that request blocks the handler thread without ever touching active_chats,
# and a client that gives up waiting doesn't cancel the server-side request
# (no streaming connection to detect the disconnect through, unlike chat),
# repeated/retried compact calls for the same conversation silently stacked
# into multiple concurrent summarization requests all hitting llama-server
# at once. compact_active makes it a full peer of any_chat_active()/
# image_gen_status["active"] in the same mutual-exclusion scheme.
compact_active = False
compact_lock = threading.Lock()


def any_chat_active():
    return bool(active_chats)


# ---------------------------------------------------------------- settings

def sanitize_stored_settings(stored):
    """(clean copy, [keys dropped]) of a settings dict that came from disk or from
    a backup. A value of the wrong kind for its setting (see
    settings_type_problem) is dropped, so that setting falls back to its
    default instead of poisoning every reader - a list in active_model made each
    request that joins it onto a path fail, and the file survives restarts."""
    clean, dropped = {}, []
    for key, value in stored.items():
        if key in DEFAULT_SETTINGS and settings_type_problem(key, value):
            dropped.append(key)
        else:
            clean[key] = value
    return clean, dropped


def _read_settings_file():
    """settings.json parsed, or None when there is nothing usable to read. A
    reader that opens the file at the instant save_settings() renames its
    replacement over it gets "Access is denied" on Windows; that used to fall
    straight into "no file -> all defaults", so one request in a thousand saw
    DEFAULT settings (measured: 100 of 168,425 reads during 1,500 saves) - and
    a settings write that did its load-modify-save on such a read saved the
    defaults back over the user's real settings. A mid-replace failure is
    transient (microseconds), so it is retried; only a file that is really
    absent or really unreadable gives the defaults."""
    for attempt in range(10):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except PermissionError:
            pass  # mid-replace (or a scanner has it for a moment): try again
        except FileNotFoundError:
            if not os.path.exists(SETTINGS_PATH + ".tmp"):
                return None  # genuinely absent (a fresh install)
        except (OSError, ValueError):
            return None
        time.sleep(0.003)
    return None


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    stored = _read_settings_file()
    if stored is None:
        return settings
    # Valid JSON that is not an object ([], 5, null) used to reach dict.update(),
    # where all but a few shapes raised TypeError out of every caller - this is
    # on the path of every request, so one such file made the whole server 500.
    if isinstance(stored, dict):
        settings.update(sanitize_stored_settings(stored)[0])
    return settings


_settings_write_lock = threading.Lock()


def save_settings(settings):
    # Atomic write (temp file + os.replace, which is an atomic rename on
    # both POSIX and Windows within the same filesystem) - a plain
    # open(path, "w") leaves a window where a concurrent load_settings()
    # (e.g. from another request thread, or start_llama()'s own re-read
    # right after this call) can observe a truncated/partial file and
    # silently fall back to all-defaults (see load_settings()'s bare
    # except), which reads exactly like "my change didn't save".
    tmp_path = SETTINGS_PATH + ".tmp"
    # One writer at a time: two saves sharing the one temp name would interleave
    # their bytes into it and rename the result over the real file.
    with _settings_write_lock:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        # On Windows os.replace() fails with "Access is denied" while any thread
        # has the destination open for reading - and load_settings() opens it on
        # every request, so a Save raced with ordinary traffic could surface as a
        # 500 (28% of saves failed with 4 busy readers). A read holds the file
        # for microseconds, so a short bounded retry is enough.
        for attempt in range(100):
            try:
                os.replace(tmp_path, SETTINGS_PATH)
                return
            except PermissionError:
                if attempt == 99:
                    raise
                time.sleep(0.02)


_theme_write_lock = threading.Lock()


def save_theme_bytes(raw):
    """Writes data\\desktop_theme.json whole and atomically - its own temp file next to it, then os.replace, with the same bounded
    PermissionError retry as save_settings. A restore used to write it in place with a plain open(): the desktop's own preference
    writer (fx/prefs.py), which replaces the file atomically and re-reads it whenever its stat changed, could then see a half-written
    file. The content is still treated as opaque - written exactly as it came. Raises OSError when it cannot be written (the temp
    file is removed first)."""
    tmp_path = THEME_PATH + ".srv.tmp"
    with _theme_write_lock:
        try:
            with open(tmp_path, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(100):
                try:
                    os.replace(tmp_path, THEME_PATH)
                    return
                except PermissionError:
                    if attempt == 99:
                        raise
                    time.sleep(0.02)
        except BaseException:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise


# Bumped whenever migrate_settings() below grows a new step. Stored in
# settings.json so each step runs at most once per install.
SETTINGS_MIGRATION_VERSION = 2


def migrate_settings():
    """One-time carry-over of existing settings.json files onto changed
    defaults. Called once from main(), not from load_settings() - that one is
    on the hot path (every request re-reads settings) and must never write.

    Only rewrites values a *previous version of this app* wrote or defaulted;
    anything the user has since chosen for themselves survives, because each
    step runs at most once and every control involved stays editable
    afterwards."""
    settings = load_settings()
    version = settings.get("settings_migration_version", 0)
    if version >= SETTINGS_MIGRATION_VERSION:
        return
    changed = []
    if version < 1:
        # Note there is deliberately no gpu_layers_auto step here. Existing
        # files predate the key entirely, so load_settings()'s merge over
        # DEFAULT_SETTINGS already hands them Auto - and a step that forced it
        # on would also stamp on the choice of anyone who had since turned it
        # off, which is the one thing this function must never do. (The old
        # gpu_layers number stays in the file untouched, as the manual
        # override.)
        #
        # Turning DRY on while leaving repeat_penalty at 1.1 would penalize
        # repetition twice over, which degrades long answers rather than
        # helping them - the exact problem this pair of changes exists to fix.
        # Unlike gpu_layers_auto this one does need an explicit step, because
        # the key already exists in every settings.json written before today
        # and would otherwise shadow the new default forever.
        if settings.get("repeat_penalty_enabled", False):
            settings["repeat_penalty_enabled"] = False
            changed.append("repeat penalty -> off (DRY sampler takes over)")
    if version < 2 and "disable_mmap" in settings:
        # The boolean became the three-way model_load_mode. Someone who had
        # ticked "force into RAM" keeps exactly that; everyone else (False was
        # simply the default) moves to Automatic.
        settings["model_load_mode"] = "ram" if settings.get("disable_mmap") else "auto"
        del settings["disable_mmap"]
        changed.append(f"model loading -> {settings['model_load_mode']} (replaces 'disable mmap')")
    settings["settings_migration_version"] = SETTINGS_MIGRATION_VERSION
    save_settings(settings)
    if changed:
        print("settings migrated: " + "; ".join(changed))


# ---------------------------------------------------------------- outputs

def outputs_root():
    settings = load_settings()
    return settings.get("outputs_dir") or DEFAULT_OUTPUTS_DIR


def outputs_subdir(category):
    path = os.path.join(outputs_root(), category)
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------- database

# handle_reset_data() deletes and recreates chats.db on disk (rather than
# DELETE-ing rows) so a factory reset can never miss a table the way the old
# hardcoded DELETE-per-table list did. db_reset_event pauses new checkouts
# during the delete/recreate window (and swap_db_file() retires the pool
# first, see _db_pool_close_all) to keep other threads from racing the swap;
# db_reset_lock just keeps two concurrent reset requests from stepping on
# each other.
db_reset_event = threading.Event()
db_reset_event.set()
db_reset_lock = threading.Lock()
# The thread currently inside swap_db_file(). While db_reset_event is cleared
# it alone may still check out connections - it has to, to build the new
# file's schema before anyone else is let back in (see swap_db_file).
_db_swapper_thread = None

# ---- why connections are pooled and long-lived (don't "simplify" this back)
#
# This app runs from an ordinary spinning disk on at least one machine
# (5400 rpm, ~90 MB/s). A cold model load makes llama.cpp's mmap +
# PrefetchVirtualMemory pile thousands of reads into that disk's queue, and
# any *durable* write to the same volume then waits behind all of them -
# seconds to minutes. The old db() opened a brand-new connection per call and
# let refcounting close it, which meant every request created the -wal/-shm
# files, and closing the last connection ran a checkpoint (fsync) and deleted
# them again. Those file operations stalled behind the load, and while one was
# stuck every other SQLite call in the process stalled with it (reproduced:
# a 72 s freeze of the whole UI; measured with a pure-Python canary thread
# that the GIL was NOT held - only SQLite calls blocked).
#
# What a persistent connection changes, measured on that same saturated disk:
# reads never stall (max 0.2 ms), and a WAL commit costs ~0.01 ms because
# synchronous=NORMAL never fsyncs a commit in WAL mode. So:
#   * connections are created once, kept open for the life of the process and
#     checked out per `with db() as conn:` block (no per-request file
#     creation/deletion, no close-time checkpoint);
#   * wal_autocheckpoint is OFF - the automatic checkpoint would run an fsync
#     inside whichever request thread happened to commit - and a maintenance
#     thread checkpoints instead (start_db_maintenance), at times the disk
#     isn't being hammered, where blocking only blocks that thread;
#   * the pool grows on demand and never makes a caller wait for a connection
#     (a pool that *did* made readers queue behind one stalled writer);
#   * busy timeout is 60 s, not Python's 5 s: a writer stalled in I/O makes
#     other writers wait, and "database is locked" would be a worse outcome;
#   * the -wal file is pre-grown at startup (_db_prealloc_wal). Even with
#     persistent connections, a commit that has to EXTEND the file stalls
#     behind the load (NTFS allocation/metadata work waits on the same
#     queue): 22 s of writes on a saturated disk lost 16 s to stalls with a
#     growing WAL and 3 s with a pre-grown one, at the full write rate.
DB_BUSY_TIMEOUT_S = 60
DB_POOL_MAX_IDLE = 16
DB_POOL_PREWARM = 4
# journal_size_limit caps how big the -wal *file* may stay after a checkpoint
# resets it - generous, so the pre-grown file is never truncated back down.
DB_WAL_SIZE_LIMIT_BYTES = 64 * 1024 * 1024
DB_WAL_PREALLOC_MB = 16
# Per-connection page cache (SQLite's default is 2 MB): keeps hot pages in
# this process so a read doesn't have to ask the disk while it is busy.
DB_CACHE_KIB = 8192
DB_MAINTENANCE_INTERVAL_S = 60

_db_pool_lock = threading.Lock()
_db_pool_idle = []
_db_pool_out = 0
_db_pool_generation = 0
_db_pool_drained = threading.Condition(_db_pool_lock)


def _open_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=DB_BUSY_TIMEOUT_S, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute(f"PRAGMA journal_size_limit={DB_WAL_SIZE_LIMIT_BYTES}")
    conn.execute(f"PRAGMA cache_size=-{DB_CACHE_KIB}")
    return conn


def _db_release(conn, generation):
    global _db_pool_out
    with _db_pool_lock:
        keep = generation == _db_pool_generation and len(_db_pool_idle) < DB_POOL_MAX_IDLE
        if keep:
            _db_pool_idle.append(conn)
            _db_pool_out -= 1
            if _db_pool_out == 0:
                _db_pool_drained.notify_all()
            return
    # Either the pool was retired while this connection was out (a DB file
    # swap) or the idle list is full - close it rather than hand a stale file
    # handle to the next caller. It is closed BEFORE it stops counting as
    # checked out: _db_pool_close_all() reads "nothing out" as "no file handle
    # left", and a swap that went ahead on that word while this connection was
    # still open had SQLite refuse the journal-mode switch ("database is
    # locked") - measured as 1 restore in ~6 under traffic ending in a 500.
    try:
        conn.close()
    except sqlite3.Error:
        pass
    finally:
        with _db_pool_lock:
            _db_pool_out -= 1
            if _db_pool_out == 0:
                _db_pool_drained.notify_all()


class _PooledDb:
    """What db() returns. ``with db() as conn:`` yields the real sqlite3
    Connection and behaves exactly like ``with conn:`` - commit on a clean exit,
    rollback on an exception - and then hands the connection back to the pool
    instead of leaving it to be garbage-collected. Every call site in this file
    uses the ``with`` form; a bare ``db()`` that is never exited would leak a
    checked-out connection (and stall swap_db_file waiting for it)."""

    __slots__ = ("_conn", "_generation")

    def __init__(self, conn, generation):
        self._conn = conn
        self._generation = generation

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        conn = self._conn
        try:
            if exc_type is None:
                conn.commit()
            else:
                conn.rollback()
        except sqlite3.Error:
            # A failed commit leaves the transaction open; it must not reach
            # the next caller still half-done.
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            if exc_type is None:
                raise
        finally:
            _db_release(conn, self._generation)
        return False


def db():
    global _db_pool_out
    swapper = threading.get_ident() == _db_swapper_thread
    while True:
        if not swapper:
            db_reset_event.wait()
        with _db_pool_lock:
            if not swapper and not db_reset_event.is_set():
                continue  # a file swap began between wait() and here
            generation = _db_pool_generation
            conn = _db_pool_idle.pop() if _db_pool_idle else None
            _db_pool_out += 1
            break
    if conn is None:
        try:
            conn = _open_db_connection()
        except BaseException:
            with _db_pool_lock:
                _db_pool_out -= 1
                if _db_pool_out == 0:
                    _db_pool_drained.notify_all()
            raise
    return _PooledDb(conn, generation)


def _db_pool_close_all(wait_s=30.0):
    """Retires the whole pool: idle connections close now, checked-out ones
    close as they come back (their generation no longer matches). Waits up to
    wait_s for in-flight `with db()` blocks to finish; returns False if some
    were still out. Callers that are about to replace or delete chats.db must
    have cleared db_reset_event first so nothing new can be checked out."""
    global _db_pool_generation
    with _db_pool_lock:
        _db_pool_generation += 1
        idle = list(_db_pool_idle)
        _db_pool_idle.clear()
        drained = _db_pool_drained.wait_for(lambda: _db_pool_out == 0, timeout=wait_s)
    for conn in idle:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    return drained


def _db_prealloc_wal(conn, mb=DB_WAL_PREALLOC_MB):
    """Grows the -wal file to ~mb now, while the disk is idle, so later commits
    overwrite space that already exists instead of extending the file (see the
    pooling comment above db()). Done by writing an oversized *uncommitted*
    transaction through a deliberately tiny page cache - SQLite spills the
    dirty pages into the WAL as it goes - and then rolling it back, so the
    database itself is untouched and nothing is left in it (a committed scratch
    table would permanently bloat chats.db with free pages). Best-effort: any
    failure just means the first writes are a little slower under load."""
    wal_path = DB_PATH + "-wal"
    try:
        if os.path.exists(wal_path) and os.path.getsize(wal_path) >= mb * 1024 * 1024:
            return
        conn.execute("PRAGMA cache_size=-64")
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE _wal_grow (x)")
            conn.execute("INSERT INTO _wal_grow VALUES (zeroblob(?))", (mb * 1024 * 1024,))
        finally:
            conn.rollback()
            conn.execute(f"PRAGMA cache_size=-{DB_CACHE_KIB}")
    except sqlite3.Error as e:
        print(f"NOTE: could not pre-size the database write-ahead log ({e})")


def _db_pool_prewarm(count=DB_POOL_PREWARM):
    """Opens the pool's first connections now, while the disk is still idle -
    at startup that is before llama-server begins loading a model, which is
    when creating -wal/-shm files and opening handles is cheap - and pre-sizes
    the write-ahead log."""
    conns = [db() for _ in range(count)]
    for c in conns:
        c.__enter__()
    _db_prealloc_wal(conns[0]._conn)
    for c in conns:
        c.__exit__(None, None, None)


def checkpoint_db(mode="PASSIVE"):
    """Folds the WAL back into chats.db. Uses its own throwaway connection so
    the fsync this performs can only ever block the calling thread, never a
    request. Never the last connection open (the pool always holds some), so
    closing it doesn't trigger a second, close-time checkpoint."""
    conn = sqlite3.connect(DB_PATH, timeout=DB_BUSY_TIMEOUT_S)
    try:
        conn.execute(f"PRAGMA wal_checkpoint({mode})")
    finally:
        conn.close()


def _chat_model_is_loading():
    proc = llama_proc
    if proc is None or proc.poll() is not None:
        return False
    return llama_health().get("status") == "starting"


def _db_maintenance_loop():
    # The -wal file is pre-grown, so its size says nothing about how much is
    # waiting to be checkpointed - just do it on a timer. A PASSIVE checkpoint
    # is cheap when there is little to copy, never blocks readers or writers,
    # and is also what lets SQLite rewind the log to reuse its pre-grown space.
    while True:
        time.sleep(DB_MAINTENANCE_INTERVAL_S)
        # A model load is what saturates the disk; the WAL growing a little
        # meanwhile is harmless, and a checkpoint would only add to the pile.
        if _chat_model_is_loading():
            continue
        try:
            checkpoint_db("PASSIVE")
        except sqlite3.Error:
            pass


def start_db_maintenance():
    """Starts the WAL-checkpointing daemon thread. Call once per process,
    right after init_db() - both entry points do."""
    threading.Thread(target=_db_maintenance_loop, daemon=True).start()


def close_db(timeout=5.0):
    """Graceful end of life for the pool: checkpoint the WAL into chats.db and
    close every connection. Bounded on purpose - a checkpoint ends in an fsync
    that can stall behind a busy disk, and skipping it is safe (the WAL is
    simply replayed the next time the database opens)."""
    def work():
        try:
            checkpoint_db("TRUNCATE")
        except sqlite3.Error:
            pass
        _db_pool_close_all(wait_s=2.0)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT 'New chat',
                -- Unsent composer text. See the ALTER TABLE below; declared
                -- here too so a fresh DB matches a migrated one.
                draft TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                -- See the ALTER TABLE below for what `reasoning` holds and why
                -- it's separate from content; declared here too so a fresh DB
                -- and a migrated one end up with the same shape.
                reasoning TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                instructions TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            CREATE TABLE IF NOT EXISTS project_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                content TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                added_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            CREATE TABLE IF NOT EXISTS project_file_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_file_id INTEGER NOT NULL REFERENCES project_files(id) ON DELETE CASCADE,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                token_count INTEGER NOT NULL,
                embedding BLOB NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            CREATE TABLE IF NOT EXISTS project_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            CREATE TABLE IF NOT EXISTS project_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                data BLOB NOT NULL,
                caption TEXT NOT NULL,
                caption_tokens INTEGER NOT NULL,
                size_bytes INTEGER NOT NULL,
                added_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            CREATE TABLE IF NOT EXISTS folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            CREATE TABLE IF NOT EXISTS project_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                sort_order REAL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            CREATE TABLE IF NOT EXISTS conversation_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                content TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                added_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            CREATE TABLE IF NOT EXISTS message_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                data BLOB NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            -- Superseded assistant replies for a regenerated turn - the ones
            -- the "[<] 2/3 [>]" control flips back to. Deliberately holds only
            -- the INACTIVE variants: the live `messages` row always carries
            -- whichever version is currently active, so every other query in
            -- this file (build_chat_messages_payload, handle_compact, exports,
            -- token counting, /thoughts) keeps working untouched and can never
            -- accidentally feed the model two versions of its own answer.
            -- `position` is that variant's 1-based slot in the carousel;
            -- messages.version_pos (see the ALTER TABLE below) says which slot
            -- the live row itself occupies, so slots 1..N are always exactly
            -- covered by the archived rows plus the live one.
            CREATE TABLE IF NOT EXISTS message_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                content TEXT NOT NULL,
                reasoning TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_message_versions_message_id
                ON message_versions(message_id, position);
            -- Documents the model writes with the create_document tool (see
            -- tool_create_document). message_id is NULL only while the reply
            -- that made it is still streaming (filled in by _stream_and_save
            -- once the reply is saved); version_id works as for message_images
            -- (NULL = part of the live version of that reply). char_offset is
            -- how much reply text came before the call, so the card can sit at
            -- that point of the reply; version counts repeats of one filename
            -- in a conversation ("v2").
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                message_id INTEGER,
                version_id INTEGER,
                title TEXT NOT NULL,
                filename TEXT NOT NULL,
                content TEXT NOT NULL,
                size INTEGER NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                char_offset INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_documents_message_id ON documents(message_id);
            CREATE INDEX IF NOT EXISTS idx_documents_conversation_id ON documents(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_message_images_message_id ON message_images(message_id);
            CREATE INDEX IF NOT EXISTS idx_project_files_project_id ON project_files(project_id);
            CREATE INDEX IF NOT EXISTS idx_project_file_chunks_file_id ON project_file_chunks(project_file_id);
            CREATE INDEX IF NOT EXISTS idx_project_file_chunks_project_id ON project_file_chunks(project_id);
            CREATE INDEX IF NOT EXISTS idx_project_images_project_id ON project_images(project_id);
            CREATE INDEX IF NOT EXISTS idx_project_memory_project_id ON project_memory(project_id);
            CREATE INDEX IF NOT EXISTS idx_conversation_files_conversation_id ON conversation_files(conversation_id);
            """
        )
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(conversations)").fetchall()]
        if "project_id" not in cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN project_id INTEGER REFERENCES projects(id)")
        if "folder_id" not in cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN folder_id INTEGER REFERENCES folders(id)")
        if "pinned" not in cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        if "sort_order" not in cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN sort_order REAL")
        if "compacted_summary" not in cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN compacted_summary TEXT")
        if "compacted_through_id" not in cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN compacted_through_id INTEGER NOT NULL DEFAULT 0")
        if "draft" not in cols:
            # Composer text the user typed but hasn't sent. Lives on the
            # conversation row so it inherits every delete path (chat delete,
            # project delete, Reset app data) and rides along in backups,
            # rather than needing its own table taught about each of those.
            # Deliberately not part of any message: it has never been sent, is
            # never included in the model payload, and must not affect
            # updated_at (typing a note shouldn't reorder the chat list).
            conn.execute("ALTER TABLE conversations ADD COLUMN draft TEXT")
        project_cols = [r["name"] for r in conn.execute("PRAGMA table_info(projects)").fetchall()]
        if "sort_order" not in project_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN sort_order REAL")
        if "group_id" not in project_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN group_id INTEGER REFERENCES project_groups(id)")
        conv_files_cols = [r["name"] for r in conn.execute("PRAGMA table_info(conversation_files)").fetchall()]
        if "content_tokens" not in conv_files_cols:
            conn.execute("ALTER TABLE conversation_files ADD COLUMN content_tokens INTEGER")
        project_files_cols = [r["name"] for r in conn.execute("PRAGMA table_info(project_files)").fetchall()]
        if "content_tokens" not in project_files_cols:
            conn.execute("ALTER TABLE project_files ADD COLUMN content_tokens INTEGER")
        project_memory_cols = [r["name"] for r in conn.execute("PRAGMA table_info(project_memory)").fetchall()]
        if "content_tokens" not in project_memory_cols:
            conn.execute("ALTER TABLE project_memory ADD COLUMN content_tokens INTEGER")
        message_cols = [r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
        if "reasoning" not in message_cols:
            # A reasoning model's `reasoning_content` stream for this turn, kept
            # so the Thoughts panel survives a restart instead of living only in
            # the desktop client's memory. Deliberately NOT part of `content`
            # and never fed back to the model on later turns - reasoning models
            # are trained to see their own prior thinking discarded, and
            # build_chat_messages_payload only ever reads `content`.
            conn.execute("ALTER TABLE messages ADD COLUMN reasoning TEXT")
        if "version_pos" not in message_cols:
            # Which slot of this message's version carousel the live row is
            # currently showing (see message_versions above). 1 for every
            # message that has never been regenerated, which is why the
            # default makes existing rows correct without a backfill pass.
            conn.execute("ALTER TABLE messages ADD COLUMN version_pos INTEGER NOT NULL DEFAULT 1")
        message_image_cols = [r["name"] for r in conn.execute("PRAGMA table_info(message_images)").fetchall()]
        if "version_id" not in message_image_cols:
            # Which archived version this image belongs to, or NULL for "part
            # of whichever version is live right now" - so flipping back to a
            # regenerated-away image turn brings its picture back with it.
            # NULL for every pre-existing row, which is exactly right: before
            # versioning existed, every image belonged to the live message.
            # Deliberately left hanging off message_id as well, so all the
            # existing "DELETE FROM message_images WHERE message_id..." cleanup
            # paths still sweep up archived versions' images for free.
            conn.execute("ALTER TABLE message_images ADD COLUMN version_id INTEGER")
        # These two indexes reference columns added via ALTER TABLE just above, so
        # they must run after that block (a fresh conversations table has neither
        # column yet at the point the CREATE TABLE script above runs).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_project_id ON conversations(project_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_folder_id ON conversations(folder_id)")
        # A document whose reply never landed (the app died between the tool call and the saved reply) has no card
        # to live in. Swept here, which only ever runs with no turn in flight: start-up, and a file swap (restore /
        # reset), which refuses while a chat is active.
        conn.execute("DELETE FROM documents WHERE message_id IS NULL")
    # Startup (and the end of a file swap) is the cheapest moment to open the
    # pool's first connections - see the pooling comment above db().
    _db_pool_prewarm()


def _leave_wal_mode():
    """Folds the write-ahead log into chats.db and switches the file back to the
    rollback journal, so that SQLite itself - under its own locking - drops the
    -wal/-shm files rather than leaving that to a raw unlink. Returns False only
    if another connection kept the file locked through every retry.

    A connection that is still being closed elsewhere makes the switch fail with
    "database is locked" (SQLite does not wait for that one), so that is retried
    briefly. The connection used here is always closed: it used to be leaked
    when a pragma raised, which held chats.db open for the rest of the swap and
    turned a transient lock into a 6-second failure of the whole operation.
    Any other error (a corrupt file, say) is not this function's business - a
    wipe or restore is exactly how someone recovers from that - so it returns
    True and lets the swap proceed."""
    for _attempt in range(20):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
            return True
        except sqlite3.Error as e:
            if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                return True
            time.sleep(0.05)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
    return False


def _retry_on_permission_error(action):
    """Runs action() up to 20 times, 0.1 s apart, while Windows reports the file
    as in use. Returns True once it succeeds."""
    for _attempt in range(20):
        try:
            action()
            return True
        except PermissionError:
            time.sleep(0.1)
    return False


def swap_db_file(new_db_path=None):
    """Safely replaces chats.db on disk - shared by handle_reset_data's "wipe
    everything" path (new_db_path=None, just deletes and recreates an empty
    schema) and handle_restore (new_db_path points at an already-validated
    replacement file to move into place). Caller must hold db_reset_lock.
    Returns an error string on failure, else None - always runs init_db() on
    success, which for restore also applies any ALTER TABLE migrations a
    backup taken on an older app version still needs.

    In WAL mode on Windows, a prior connection's mapping of the -shm file can
    keep the main .db file locked for os.remove() even after that connection
    has gone out of scope and nothing else in this process is using it -
    confirmed by reproducing the failure live (2 of 3 runs) with the
    checkpoint-first step (_leave_wal_mode) removed, then 8 of 8 clean with it
    back. gc.collect() clears any connection only kept alive by a reference
    cycle.

    Order matters when something goes wrong: the stale -wal/-shm go first and
    the step that actually replaces chats.db is the LAST thing done, so a
    failure at any point leaves the user's current database in place. (It used
    to delete chats.db first, so a later failure could lose the live data and
    not install the new file either.)

    The whole thing - including init_db() on the new file - happens with
    db_reset_event cleared, and this thread is the only one let through
    (_db_swapper_thread). Re-opening the gate first, as this used to, let a
    request that arrived in between query a file with no tables yet ("no such
    table: conversations")."""
    global _db_swapper_thread
    db_reset_event.clear()
    _db_swapper_thread = threading.get_ident()
    error = None
    try:
        # The pool's persistent connections are what keep chats.db and the
        # -shm mapping open, so they have to go first. Nothing new can be
        # checked out any more (the event is cleared above); in-flight
        # `with db()` blocks are short and close themselves on release.
        if not _db_pool_close_all():
            print("WARNING: a database connection was still in use during a file swap - continuing")
        gc.collect()
        if not _leave_wal_mode():
            error = "could not reset database — file still in use"
        for path in (DB_PATH + "-wal", DB_PATH + "-shm"):
            if error:
                break
            if os.path.exists(path) and not _retry_on_permission_error(lambda p=path: os.remove(p)):
                error = "could not reset database — file still in use"
        if not error:
            if new_db_path is not None:
                # Atomic replacement: there is never a moment with no chats.db.
                installed = _retry_on_permission_error(lambda: os.replace(new_db_path, DB_PATH))
            else:
                installed = (not os.path.exists(DB_PATH)) or _retry_on_permission_error(lambda: os.remove(DB_PATH))
            if not installed:
                error = "could not reset database — file still in use"
        if not error:
            init_db()
    finally:
        _db_swapper_thread = None
        db_reset_event.set()
    return error


# ---------------------------------------------------------------- llama-server

def find_model():
    if not os.path.isdir(MODELS_DIR):
        return None
    ggufs = [f for f in os.listdir(MODELS_DIR) if f.lower().endswith(".gguf")]
    if not ggufs:
        return None
    settings = load_settings()
    active = settings.get("active_model")
    if active and active in ggufs:
        return os.path.join(MODELS_DIR, active)
    # No (valid) active model chosen — fall back to the largest one.
    ggufs.sort(key=lambda f: os.path.getsize(os.path.join(MODELS_DIR, f)), reverse=True)
    return os.path.join(MODELS_DIR, ggufs[0])


_GGUF_SCALAR_FORMATS = {
    0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2),
    4: ("<I", 4), 5: ("<i", 4), 6: ("<f", 4), 7: ("<?", 1),
    10: ("<Q", 8), 11: ("<q", 8), 12: ("<d", 8),
}


def _gguf_read_string(f):
    (length,) = struct.unpack("<Q", f.read(8))
    return f.read(length).decode("utf-8", errors="replace")


def _gguf_read_value(f, vtype):
    if vtype == 8:  # string
        return _gguf_read_string(f)
    if vtype == 9:  # array
        (elem_type,) = struct.unpack("<I", f.read(4))
        (count,) = struct.unpack("<Q", f.read(8))
        return [_gguf_read_value(f, elem_type) for _ in range(count)]
    fmt, size = _GGUF_SCALAR_FORMATS[vtype]
    return struct.unpack(fmt, f.read(size))[0]


def gguf_read_metadata(path, predicate):
    """Scan a .gguf file's metadata header without loading the model, collecting every
    key/value pair for which predicate(key) is true. Returns {} if the file can't be
    read as GGUF (missing, truncated, wrong magic, etc.) — never raises."""
    found = {}
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF":
                return {}
            (version,) = struct.unpack("<I", f.read(4))
            if version < 2:
                return {}  # ancient format, not worth supporting
            (_tensor_count,) = struct.unpack("<Q", f.read(8))
            (kv_count,) = struct.unpack("<Q", f.read(8))
            for _ in range(kv_count):
                key = _gguf_read_string(f)
                (vtype,) = struct.unpack("<I", f.read(4))
                value = _gguf_read_value(f, vtype)
                if predicate(key):
                    found[key] = value
    except (OSError, struct.error, UnicodeDecodeError, KeyError):
        return {}
    return found


# Caches the full metadata dict per (path, mtime) so the many small gguf_* getters
# below (block_count, kv_head_dim, sliding_window, context_length, ...) don't each
# re-scan the same file from disk — a single settings/VRAM-estimate request used to
# trigger 3-4 full re-parses of the same GGUF header. Excludes tokenizer.* keys
# (vocab/merges arrays, which can be tens of thousands of entries) since no caller
# needs them and decoding them would bloat both the scan and the cache.
_gguf_metadata_cache = {}


def _gguf_cached_metadata(path):
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    cached = _gguf_metadata_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    full = gguf_read_metadata(path, lambda k: not k.startswith("tokenizer."))
    _gguf_metadata_cache[path] = (mtime, full)
    return full


def _first_numeric(meta, predicate=None):
    for key, value in meta.items():
        if predicate is not None and not predicate(key):
            continue
        if isinstance(value, (int, float)):
            return int(value)
    return None


def gguf_context_length(path):
    """A model's trained context length (<arch>.context_length). None if undetermined."""
    return _first_numeric(_gguf_cached_metadata(path), lambda k: k.endswith(".context_length"))


def gguf_embedding_length(path):
    """A text model's embedding dimension (<arch>.embedding_length) — used to check
    whether a vision (mmproj) file is compatible with it. None if undetermined."""
    return _first_numeric(_gguf_cached_metadata(path), lambda k: k.endswith(".embedding_length"))


def gguf_mmproj_projection_dim(path):
    """A vision projector (mmproj) file's output embedding dimension. llama.cpp refuses
    to pair a model and mmproj whose dimensions don't match — checking this ourselves
    lets us reject the pairing with a clear message instead of crashing the model server."""
    return _first_numeric(_gguf_cached_metadata(path), lambda k: k == "clip.vision.projection_dim")


def gguf_block_count(path):
    """A model's transformer layer count (<arch>.block_count) — used with the file
    size to roughly estimate per-layer VRAM cost. None if undetermined."""
    return _first_numeric(_gguf_cached_metadata(path), lambda k: k.endswith(".block_count"))


def gguf_kv_head_dim(path):
    """The per-token KV cache width in elements: head_count_kv * key_length, i.e. the
    actual (possibly grouped-query-attention-reduced) K or V vector size the model
    stores per layer per token. Prefers the model's explicitly declared key_length
    over deriving it from embedding_length/head_count, since several architectures
    (e.g. Gemma) use a head dimension that isn't embedding_length/head_count. None
    if any piece is missing."""
    meta = _gguf_cached_metadata(path)
    head_count = next((v for k, v in meta.items() if k.endswith(".attention.head_count")), None)
    head_count_kv = next((v for k, v in meta.items() if k.endswith(".attention.head_count_kv")), None)
    key_length = next((v for k, v in meta.items() if k.endswith(".attention.key_length")), None)
    embedding_length = next((v for k, v in meta.items() if k.endswith(".embedding_length")), None)
    if not head_count:
        return None
    head_count_kv = head_count_kv or head_count
    if key_length:
        head_dim = key_length
    elif embedding_length:
        head_dim = embedding_length / head_count
    else:
        return None
    return head_count_kv * head_dim


# Cached separately from _gguf_cached_metadata, which deliberately excludes
# every tokenizer.* key (vocab/merges arrays are enormous). The chat template
# is a single string of a few KB, so it's cheap to keep - but only worth
# reading for the handful of callers below, not on every metadata lookup.
_gguf_chat_template_cache = {}


def gguf_chat_template(path):
    """A model's Jinja chat template, or "" if it declares none."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    cached = _gguf_chat_template_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    found = gguf_read_metadata(path, lambda k: k == "tokenizer.chat_template")
    template = found.get("tokenizer.chat_template") or ""
    _gguf_chat_template_cache[path] = (mtime, template)
    return template


# The three thinking styles seen across the model families this app runs, and
# what each one's chat template actually lets you control. Established by
# reading the templates of every installed model plus live probes against
# llama-server build 9860 (2026-08-21):
#
#   harmony     gpt-oss. Emits <|channel|>analysis/commentary/final. Its
#               template takes `reasoning_effort` (low/medium/high) - there
#               is no off switch, the analysis channel always exists. This is
#               also the only style that can resume thinking *after* answer
#               content has started (channels can alternate), which think-tag
#               models cannot.
#   think_tags  Qwen3.5/3.6 and friends. Emits <think>...</think> strictly
#               before the answer. Template takes `enable_thinking` (bool),
#               and passing False genuinely suppresses thinking entirely
#               (verified: 0 chars reasoning_content, direct answer).
#   None        No thinking of any kind (e.g. gemma3).
#
# Both knobs are chat-template variables, so they travel per request in
# chat_template_kwargs and never need a model reload. The token *budget* is
# the exception - --reasoning-budget is launch-only, a per-request
# "reasoning_budget" field is silently ignored (verified: 1438 chars of
# thinking with budget=80 in the body).
THINKING_STYLES = {
    "harmony": {
        "label": "Harmony channels",
        "control": "reasoning_effort",
        "efforts": ["low", "medium", "high"],
        "can_interleave": True,
        # --reasoning-budget does not bind on this style. Measured 2026-08-21
        # on gpt-oss-20B: launched with --reasoning-budget 100 at effort=high,
        # it still produced 13,129 characters (~3,200 tokens) of thinking and
        # never reached an answer. The budget works by injecting the
        # end-of-thinking tag, which only exists in think-tag templates -
        # harmony switches channels instead, so there's nothing to inject.
        # Passing the flag anyway would be a promise the UI can't keep (and
        # would make Response length restart the server for no benefit), so
        # thinking_budget_tokens returns None for this style. reasoning_effort
        # is the real lever here: low vs high measured 200 vs 18,375 chars.
        "budget_enforced": False,
    },
    "think_tags": {
        "label": "Think tags",
        "control": "enable_thinking",
        "efforts": None,
        "can_interleave": False,
        # Verified on Qwythos-9B: --reasoning-budget 80 capped thinking at
        # ~325 chars and the answer still arrived.
        "budget_enforced": True,
    },
}


def model_thinking_capability(path):
    """What kind of thinking this model supports and which control it takes.
    Detected from the chat template rather than the architecture, because
    that's what actually governs the behavior - two models can share an
    architecture and be finetuned onto different templates."""
    off = {"supports": False, "style": None, "label": "No thinking",
           "control": None, "efforts": None, "can_interleave": False,
           "budget_enforced": False}
    if not path or not os.path.isfile(path):
        return off
    template = gguf_chat_template(path)
    if not template:
        return off
    if "<|channel|>" in template and "analysis" in template:
        style = "harmony"
    elif "<think>" in template or "</think>" in template:
        style = "think_tags"
    else:
        return off
    spec = THINKING_STYLES[style]
    # A template can declare the style without exposing the knob (a hard-coded
    # fork rather than a variable) - report the knob only if it's really there,
    # so the UI never offers a control that would silently do nothing.
    control = spec["control"] if spec["control"] in template else None
    return {
        "supports": True,
        "style": style,
        "label": spec["label"],
        "control": control,
        "efforts": spec["efforts"] if control == "reasoning_effort" else None,
        "can_interleave": spec["can_interleave"],
        "budget_enforced": spec["budget_enforced"],
    }


def gguf_kv_cache_layers(path):
    """How many of a model's layers actually hold a KV cache.

    For a plain transformer that's every layer, but hybrid linear-attention
    models (Qwen3.5/Qwen3-Next and friends, `general.architecture = qwen35` /
    `qwen35moe`) interleave a handful of real attention layers among many
    SSM/Gated-DeltaNet ones, and only the attention layers cache K/V - the
    recurrent layers carry a small fixed-size state instead, which doesn't
    grow with context. `<arch>.full_attention_interval = 4` means every 4th
    layer is full attention, so a 64-block model caches on 16 layers, not 64.

    Assuming all of them (which estimate_vram_mb did until 2026-08-20)
    overstates the KV cache 4x on exactly these models - 1632 MiB on the
    27B/64-block Qwen3.6 at a 16384 context - and that inflation feeds
    straight into safe_gpu_layers, which then strands GPU layers on the CPU
    to make room for a cache that was never going to be allocated."""
    block_count = gguf_block_count(path)
    if not block_count:
        return None
    interval = _first_numeric(
        _gguf_cached_metadata(path), lambda k: k.endswith(".full_attention_interval")
    )
    if not interval or interval <= 1:
        return block_count
    return max(1, block_count // interval)


def gguf_sliding_window(path):
    """A model's local-attention window size (<arch>.attention.sliding_window), if it
    uses one (e.g. Gemma 2/3's interleaved local/global attention). Most layers in
    these models only need this much KV cache, not the full context size — ignoring
    that would badly overestimate VRAM needs for exactly these models. None if the
    model doesn't declare one."""
    return _first_numeric(_gguf_cached_metadata(path), lambda k: k.endswith(".attention.sliding_window"))


def query_vram_mb():
    """Total/free VRAM in MiB via nvidia-smi, summed over the GPUs the program may use, or None on
    non-NVIDIA machines / if the driver tools aren't on PATH (e.g. a Vulkan/CPU build per the
    README's portability notes) — every caller must treat that as 'estimate unavailable', not an
    error. With one GPU this is that GPU's own figures, as it always was. With several (the old
    version asked for one line and failed on two, so every VRAM feature quietly switched off) it is
    the sum over the GPUs that are enabled and that the engine build can run on - the memory the
    model can actually be spread over - and "gpus" lists each one for the live meter."""
    try:
        gpus = gpu_plan.query_smi_gpus(runner=run_engine_tool)
        if not gpus:
            return None
        wanted = None
        inv = gpu_inventory(cached_only=True) if len(gpus) > 1 else None
        if inv:
            wanted = {}
            for d in inv["devices"]:
                if d["usable"]:
                    wanted[d["name"]] = wanted.get(d["name"], 0) + 1
        counted = []
        for g in gpus:
            use = wanted is None or wanted.get(g["name"], 0) > 0
            if wanted is not None and use:
                wanted[g["name"]] -= 1
            counted.append(dict(g, in_use=use))
        mine = [g for g in counted if g["in_use"]] or counted
        return {
            "total_mb": sum(g["total_mb"] for g in mine),
            "free_mb": sum(g["free_mb"] for g in mine),
            "gpus": [{"index": g["index"], "name": g["name"], "total_mb": g["total_mb"], "free_mb": g["free_mb"], "in_use": g["in_use"]} for g in counted],
        }
    except Exception:
        return None       # whatever went wrong, "unavailable" - never an error: callers include the one that starts the model


# ---------------------------------------------------------------- GPUs and engine builds (the logic lives in gpu_plan.py)

# Extra arguments handed to every engine tool and to llama-server itself. Empty in normal use: the end-to-end diagnostic
# (tests/diagnostics/multigpu_e2e.py) points it at an RPC device to emulate a second GPU on a one-GPU machine.
LLAMA_EXTRA_ARGS = []
# Gathering the facts costs a few processes (the driver query, `--list-devices` once or twice, ~0.3-1 s in all), and the Settings screen
# asks often, so they are cached; a launch always asks afresh.
GPU_FACTS_TTL_S = 15.0
_gpu_facts = {"value": None}
_gpu_facts_lock = threading.Lock()
_gpu_plan_cache = {}
_gpu_plan_lock = threading.Lock()
# The placement the running llama-server was started with (see start_llama), for the Settings screen.
llama_launch_plan = None
# Each GPU's free memory (by key) as the engine reported it just before that launch - i.e. with the model not yet loaded.
llama_launch_free = {}
# Memory the vision projector takes on the first GPU beyond what llama-fit-params can see (it does not accept --mmproj), as a
# multiple of the file's size.
MMPROJ_MEMORY_FACTOR = 1.1


def run_engine_tool(argv, timeout):
    """gpu_plan.run_tool, behind a module-level name so a test can stand a fake engine in for the real programs."""
    return gpu_plan.run_tool(argv, timeout)


def gather_gpu_facts(preferred_engine):
    """The raw facts about the GPUs and the installed engine builds: which builds exist, what the driver reports, what the chosen
    build sees. {"engines", "smi", "engine", "support", "devices", "archs", "preferred", "at"}; "devices" is None when no build could
    be run at all (nothing is known: the caller launches exactly as before), and [] when builds ran but none lists a device."""
    engines = gpu_plan.discover_engines(os.path.dirname(LLAMA_EXE), os.path.basename(LLAMA_EXE))
    facts = {"engines": engines, "smi": gpu_plan.query_smi_gpus(runner=run_engine_tool), "engine": None, "support": {}, "devices": None,
             "archs": None, "preferred": preferred_engine, "at": time.time()}
    # Every build is asked for its own list - one process each, ~0.3 s: builds differ in what they enumerate (one made for a newer CUDA
    # may not list a GPU that an older build lists and can run), so each is judged on what IT sees. A build that runs but lists nothing
    # says nothing about the GPUs - it may have none to offer, or print them in a form this code cannot read (a newer llama.cpp changing
    # its output) - so nothing is concluded from the silence.
    own, any_ran = {}, False
    facts["sees"] = {}                            # build -> how many devices it lists; None = it could not be run at all
    facts["missing"] = {}                         # build -> the DLLs its GPU support cannot find
    for e in engines:
        found = gpu_plan.list_engine_devices(e["exe"], LLAMA_EXTRA_ARGS, runner=run_engine_tool)
        any_ran = any_ran or found is not None
        facts["sees"][e["name"]] = None if found is None else len(found)
        # DLLs this build's GPU support needs and cannot find (its CUDA runtime, the driver's nvcuda.dll ...): the usual reason a build
        # starts but lists no GPU
        facts["missing"][e["name"]] = sorted(pe_imports.missing_dependencies(e["cuda_dll"]), key=str.lower) if e.get("cuda_dll") else []
        if found:
            own[e["name"]] = found
    if not any_ran:
        return facts                              # no engine could be asked at all: nothing is known
    cc_of = {g["name"]: g.get("cc") for g in facts["smi"]}
    seen = {name: [dict(d, cc=cc_of.get(d["name"])) for d in devs] for name, devs in own.items()}
    engine, support = gpu_plan.choose_engine(engines, [], preferred_engine, devices_by_engine=seen)
    facts["engine"], facts["support"] = engine, support
    # [] (as opposed to None) = engines ran and the chosen one lists no device. The plan is then "none" - the model starts exactly as the
    # engine would on its own - and above all NOT "--device none", which would turn a GPU that works into CPU-only inference.
    facts["devices"] = own.get(engine["name"], []) if engine else []
    facts["archs"] = gpu_plan.cuda_archs(engine["cuda_dll"]) if engine and engine.get("cuda_dll") else None
    return facts


def gpu_inventory(settings=None, force=False, cached_only=False):
    """The GPUs the engine sees, annotated and in the user's priority order (gpu_plan.build_inventory), with the engine builds:
    {"devices", "engine", "engines", "support", "smi", "at"} - or None when there is nothing to go on (no engine build lists devices),
    in which case everything proceeds as it did before there was any GPU handling. `cached_only` never spawns a process: polling
    code uses it and gets None until some caller has gathered the facts."""
    settings = settings if settings is not None else load_settings()
    preferred = settings.get("engine") if isinstance(settings.get("engine"), str) else "auto"
    facts = _gpu_facts["value"]
    fresh = facts is not None and facts["preferred"] == preferred and time.time() - facts["at"] < GPU_FACTS_TTL_S
    if cached_only:
        fresh = facts is not None and facts["preferred"] == preferred
    if force or not fresh:
        if cached_only:
            return None
        with _gpu_facts_lock:
            facts = _gpu_facts["value"]
            if force or facts is None or facts["preferred"] != preferred or time.time() - facts["at"] >= GPU_FACTS_TTL_S:
                facts = _gpu_facts["value"] = gather_gpu_facts(preferred)
    if not facts or facts["devices"] is None:
        return None
    devices = gpu_plan.build_inventory(facts["devices"], facts["smi"], facts["archs"], order=settings.get("gpu_order"), disabled=settings.get("gpu_disabled"))
    return {"devices": devices, "engine": facts["engine"], "engines": facts["engines"], "support": facts["support"], "smi": facts["smi"], "at": facts["at"],
            "sees": facts.get("sees", {}), "missing": facts.get("missing", {})}


def llama_shape_args(settings, total_context_size, kv_cache_type):
    """The launch arguments that decide how much memory the model needs - the same ones start_llama() passes - for the fit tool."""
    batch_size = int(settings.get("batch_size", 2048))
    args = ["-c", str(total_context_size), "-fa", "on", "-b", str(batch_size), "-ub", str(min(batch_size, 512))]
    if kv_cache_type in KV_CACHE_TYPES and kv_cache_type != "f16":
        args += ["--cache-type-k", kv_cache_type, "--cache-type-v", kv_cache_type]
    if not settings.get("kv_offload", True):
        args.append("--no-kv-offload")
    return args


def estimator_probe(model_path, total_context_size, kv_cache_type, kv_offload):
    """gpu_plan's `probe` built on this file's own rough VRAM estimate, for an engine build that has no llama-fit-params (or when it
    fails): what the model would take on each device for a split, from estimate_vram_mb. Far less exact - the reason the fit tool is
    preferred - but good enough to tell "fits on one GPU" from "does not"."""
    blocks = gguf_block_count(model_path)
    est = estimate_vram_mb(model_path, (blocks or 0) + 1, total_context_size, kv_cache_type, kv_offload) if blocks else None

    def probe(device_ids, shares):
        if est is None:
            return None
        shares = shares or [100] + [0] * (len(device_ids) - 1)
        total = sum(shares) or 1
        return {dev: {"model": round((est["high"] - VRAM_OVERHEAD_MB) * s / total), "context": 0, "compute": VRAM_OVERHEAD_MB if s else 0}
                for dev, s in zip(device_ids, shares)}
    return probe


def _free_after_unload(devices):
    """The devices as they will be once the running model is gone. Changing a GPU setting restarts the model - stop, then start - so
    the next load begins with the memory the running one holds free again, and the "Next load" preview must plan for that. On some
    systems the engine's free figure drops by what the running model occupies (a plan from it would say "no longer fits"), so no device
    is taken to have less free than it had just before the running model was launched."""
    proc = llama_proc
    if not llama_launch_free or proc is None or proc.poll() is not None:
        return devices
    return [dict(d, free_mb=max(d["free_mb"], min(d["total_mb"], llama_launch_free.get(d["key"], 0)))) for d in devices]


def compute_gpu_plan(settings, model_path, mmproj_path=None, inv=None, force_inventory=False, preview=False):
    """(plan, inventory) for the model under these settings, or (None, None) when there is nothing to plan with. The plan decides
    which GPUs get the model and in what proportions (gpu_plan.plan_placement); it is cached on everything it depends on (the model,
    the size-deciding settings, each usable GPU's free memory to 256 MiB), so asking again costs nothing. May take several seconds
    the first time, when the model does not fit on the first GPU alone (a handful of 0.7 s fit-tool probes) - never call it from the
    Tk thread. `preview`: the plan for the NEXT load (Settings, before saving) while a model may be loaded."""
    inv = inv or gpu_inventory(settings, force=force_inventory)
    if not inv:
        return None, None
    if not inv["devices"]:
        return None, inv       # the engines ran but no GPU is known (none installed, or a listing this code cannot read): no plan, never "--device none"
    mode = settings.get("gpu_placement")
    mode = mode if mode in gpu_plan.PLACEMENT_MODES else gpu_plan.DEFAULT_PLACEMENT
    usable = [d for d in inv["devices"] if d["usable"]]
    if preview:
        usable = _free_after_unload(usable)
    if len(inv["devices"]) == 1 and len(usable) == 1:
        # One GPU and the engine can use it: nothing to decide and nothing to pass (the command is exactly what it always was).
        d = usable[0]
        return {"mode": mode, "device_ids": [d["id"]], "shares": None, "fits": None, "overflow": False, "estimated": False, "probes": 0,
                "devices": [{"id": d["id"], "label": d["label"], "name": d["name"], "cap_mb": d["free_mb"], "share": 100, "need_mb": None}],
                "reason": f"Only {d['label']} ({d['name']}) is installed."}, inv
    kv_cache_type = settings.get("kv_cache_type", "q8_0")
    total_context = int(settings.get("context_size", 16384)) * max_parallel_chats(settings)
    shape = llama_shape_args(settings, total_context, kv_cache_type)
    extra_first_mb = int(os.path.getsize(mmproj_path) / (1024 * 1024) * MMPROJ_MEMORY_FACTOR) if mmproj_path and os.path.isfile(mmproj_path) else 0
    try:
        stamp = (model_path, os.path.getsize(model_path), int(os.path.getmtime(model_path)))
    except OSError:
        return None, inv
    engine = inv["engine"]
    key = (engine["name"] if engine else None, stamp, tuple(shape), mode, extra_first_mb // 64, tuple((d["id"], d["free_mb"] // 256) for d in usable))
    with _gpu_plan_lock:
        if key in _gpu_plan_cache:
            return _gpu_plan_cache[key], inv
        fallback = estimator_probe(model_path, total_context, kv_cache_type, settings.get("kv_offload", True))
        if engine and engine.get("fit_exe"):
            fit = gpu_plan.make_fit_probe(engine["fit_exe"], model_path, shape, LLAMA_EXTRA_ARGS, runner=run_engine_tool)

            def probe(ids, shares):
                return fit(ids, shares) or fallback(ids, shares)
        else:
            probe = fallback
        plan = gpu_plan.plan_placement(usable, mode, probe, extra_first_mb=extra_first_mb)
        if len(_gpu_plan_cache) > 64:
            _gpu_plan_cache.clear()
        _gpu_plan_cache[key] = plan
    return plan, inv


def prepare_gpu_launch(settings, model_path, total_context_size, kv_cache_type, mmproj_path):
    """(engine executable, extra llama-server arguments, plan, inventory) for a launch. Any trouble at all - no engine build lists
    devices, the tools fail, an unexpected error - gives the plain old launch (the default executable, no arguments, no plan):
    GPU placement is an improvement and must never be the reason a model fails to start."""
    try:
        inv = gpu_inventory(settings, force=True)
        if not inv:
            return LLAMA_EXE, [], None, None
        plan, inv = compute_gpu_plan(settings, model_path, mmproj_path, inv=inv)
        exe = inv["engine"]["exe"] if inv["engine"] else LLAMA_EXE
        return exe, gpu_plan.launch_args(plan, devices_visible=len(inv["devices"])), plan, inv
    except Exception:
        print("WARNING: GPU placement failed, launching without it:")
        traceback.print_exc()
        return LLAMA_EXE, [], None, None


def gpu_config_signature(settings):
    """What the GPU settings amount to for a launch: the placement mode, the GPUs that would be used in order, and the engine build.
    Two settings dicts with the same signature start the model in the same way, so a change that does not alter it (reordering
    GPUs that are not there, say) does not need the model reloaded - which on a hard disk is minutes."""
    inv = gpu_inventory(settings)
    if not inv:
        return None
    mode = settings.get("gpu_placement")
    return (mode if mode in gpu_plan.PLACEMENT_MODES else gpu_plan.DEFAULT_PLACEMENT,
            tuple(d["id"] for d in inv["devices"] if d["usable"]), inv["engine"]["name"] if inv["engine"] else None)


def vram_for_plan(inv, plan):
    """{"total_mb", "free_mb"} summed over the devices a plan uses, or None when there is no plan. It is what the launch bookkeeping
    works from: the foreign-memory warning and the budget for a fixed layer count. So it is the DRIVER's reading of each GPU where
    there is one (nvidia-smi: what is really free) - the engine's own figure on Windows ignores the memory other programs hold (measured:
    "11036 MiB free" with 4 GB in use elsewhere, and still 11036 with the model loaded) - and the engine's figure only for a device
    the driver tool does not know (another vendor, a remote device)."""
    if not inv or not plan or not plan.get("device_ids"):
        return None
    by_id = {d["id"]: d for d in inv["devices"]}
    used = [by_id[i] for i in plan["device_ids"] if i in by_id]
    if not used:
        return None
    driver = gpu_plan.match_driver_gpus(inv["devices"], inv.get("smi"))
    total = free = 0
    for d in used:
        g = driver.get(d["id"])
        total += g["total_mb"] if g else d["total_mb"]
        free += g["free_mb"] if g else d["free_mb"]
    return {"total_mb": total, "free_mb": free}


def image_engine_plan(settings, inv=None):
    """(device, backend, env) for the image engine: the GPU it will run on, the value for sd-server's --backend ("cuda1") and extra
    environment for it - or (None, None, None) to leave the choice to the engine (a machine with one GPU, or nothing known about the
    GPUs). The same on/off as the chat model, restricted to the GPUs the IMAGE engine's own build can run (it is a different build, with
    its own architectures), in the user's priority order - not the chat engine's, which sorts a card it cannot run last although it may be
    the biggest and one this build runs - and `image_gpu` in Settings picks one explicitly. By default that is the GPU with the most
    memory: the chat model is unloaded while an image is made, so the two never compete for it.

    The index in "cudaN" must be the image engine's own numbering, which it cannot list and which can differ from the chat engine's (the
    two runtimes may not enumerate the same cards). So where the driver knows the card, the image engine is started with
    CUDA_DEVICE_ORDER=PCI_BUS_ID - CUDA's numbering then equals nvidia-smi's - and N is the driver's index for it."""
    inv = inv or gpu_inventory(settings)
    if not inv or len(inv["devices"]) < 2:
        return None, None, None
    archs = gpu_plan.cuda_archs(os.path.join(os.path.dirname(SD_EXE), "ggml-cuda.dll"))
    ok = sorted((d for d in inv["devices"] if d["enabled"] and d["id"].startswith("CUDA") and gpu_plan.arch_support(d.get("cc"), archs)[0]),
                key=lambda d: d.get("priority", d["position"]))
    if not ok:
        return (None, "cpu", None) if all(not d["enabled"] for d in inv["devices"]) else (None, None, None)
    want = settings.get("image_gpu")
    pick = next((d for d in ok if d["key"] == want), None) or ok[0]
    driver = gpu_plan.match_driver_gpus(inv["devices"], inv.get("smi")).get(pick["id"])
    if driver is not None and "CUDA_VISIBLE_DEVICES" not in os.environ and "CUDA_DEVICE_ORDER" not in os.environ:
        return pick, f"cuda{driver['index']}", {"CUDA_DEVICE_ORDER": "PCI_BUS_ID"}
    return pick, "cuda" + pick["id"][len("CUDA"):], None


def image_engine_pick(settings, inv=None):
    """(device, backend) of image_engine_plan, for what only reports the choice."""
    pick, backend, _ = image_engine_plan(settings, inv)
    return pick, backend


def public_gpu(d):
    """One GPU as the API reports it. `image_ok`: whether the image engine's build can run on it."""
    image_archs = gpu_plan.cuda_archs(os.path.join(os.path.dirname(SD_EXE), "ggml-cuda.dll"))
    return {"id": d["id"], "key": d["key"], "name": d["name"], "total_mb": d["total_mb"], "free_mb": d["free_mb"],
            "cc": (f"{d['cc'] // 10}.{d['cc'] % 10}" if d.get("cc") else None), "supported": d["supported"], "reason": d["reason"],
            "enabled": d["enabled"], "usable": d["usable"], "position": d["position"], "label": d["label"], "jit": bool(d.get("jit")),
            "image_ok": d["id"].startswith("CUDA") and gpu_plan.arch_support(d.get("cc"), image_archs)[0]}


def gpus_payload(settings, refresh=False):
    """The body of GET /api/gpus: the GPUs in priority order, the engine builds, and the placement the running model was started with."""
    inv = gpu_inventory(settings, force=refresh)
    proc = llama_proc
    running = proc is not None and proc.poll() is None
    mode = settings.get("gpu_placement")
    base = {"available": inv is not None, "placement": mode if mode in gpu_plan.PLACEMENT_MODES else gpu_plan.DEFAULT_PLACEMENT, "running": running,
            "plan": llama_launch_plan if running else None, "devices": [], "engine": None, "engines": []}
    if not inv:
        return base
    base["devices"] = [public_gpu(d) for d in inv["devices"]]
    base["engine"] = inv["engine"]["name"] if inv["engine"] else None
    base["engines"] = [{"name": e["name"], "runs": inv["support"].get(e["name"], []), "sees": inv.get("sees", {}).get(e["name"]),
                        "missing": inv.get("missing", {}).get(e["name"], [])} for e in inv["engines"]]
    picked, backend = image_engine_pick(settings, inv)
    base["image_gpu"] = {"key": picked["key"] if picked else None, "backend": backend}
    return base


def query_process_vram_mb(pid):
    """This specific process's own VRAM usage in MiB via nvidia-smi's
    per-process compute-apps query — distinct from query_vram_mb()'s
    card-wide total, since other GPU-using apps (browser, desktop
    compositor) share the same card. Returns None if nvidia-smi is
    unavailable, the pid isn't currently a listed compute app, or (measured
    2026-08-04 on an RTX 5070 in the default WDDM driver mode) the driver
    simply doesn't report it — nvidia-smi listed every single process on
    the machine with used_memory=[N/A], not just this one, which is a known
    nvidia-smi limitation on consumer/WDDM GPUs rather than anything
    specific to this process. Callers must treat None as "isolate this
    process's share unavailable, fall back to the card-wide total" rather
    than "nothing is using VRAM"."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            timeout=5,
        )
        for line in out.decode().strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 2 and parts[0] == str(pid):
                return int(parts[1])
        return None
    except Exception:
        return None


def query_process_ram_mb(pid):
    """This specific process's own working-set RAM usage in MiB via
    tasklist (no psutil - this app stays dependency-free). Returns None if
    the pid isn't currently running."""
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            timeout=5,
        )
        line = out.decode().strip()
        if not line or line.startswith("INFO:"):
            return None
        fields = next(csv.reader([line]))
        mem_str = fields[4].replace(",", "").replace(" K", "").strip()
        return int(mem_str) / 1024
    except Exception:
        return None


def available_ram_mb():
    """Physical RAM that can be handed to a new allocation right now, in MiB
    (GlobalMemoryStatusEx; includes the file cache, which Windows gives back on
    demand). None if it can't be read - callers must treat that as "unknown",
    never as "plenty"."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return status.ullAvailPhys / (1024 * 1024)
    except Exception:
        return None


_spinning_disk_cache = {}


def path_is_on_spinning_disk(path):
    """True if `path` lives on a drive that pays a seek penalty (a hard disk),
    False for SSD/NVMe, None if Windows can't say (network share, some USB
    bridges, non-Windows) - callers must treat None as "not known to be
    spinning". Asks the volume itself (IOCTL_STORAGE_QUERY_PROPERTY /
    StorageDeviceSeekPenaltyProperty); needs no elevation. Cached per drive."""
    if os.name != "nt":
        return None
    drive = os.path.splitdrive(os.path.abspath(path))[0].upper()
    if len(drive) != 2 or drive[1] != ":":
        return None  # UNC path or something else we can't open as a volume
    if drive in _spinning_disk_cache:
        return _spinning_disk_cache[drive]
    result = None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        k32.DeviceIoControl.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
        ]
        handle = k32.CreateFileW(f"\\\\.\\{drive}", 0, 0x1 | 0x2, None, 3, 0, None)  # no access rights needed
        if handle not in (None, ctypes.c_void_p(-1).value):
            try:
                class STORAGE_PROPERTY_QUERY(ctypes.Structure):
                    _fields_ = [("PropertyId", wintypes.DWORD), ("QueryType", wintypes.DWORD),
                                ("AdditionalParameters", ctypes.c_ubyte * 1)]

                class DEVICE_SEEK_PENALTY_DESCRIPTOR(ctypes.Structure):
                    _fields_ = [("Version", wintypes.DWORD), ("Size", wintypes.DWORD),
                                ("IncursSeekPenalty", ctypes.c_ubyte)]

                query = STORAGE_PROPERTY_QUERY(7, 0)  # StorageDeviceSeekPenaltyProperty, PropertyStandardQuery
                out = DEVICE_SEEK_PENALTY_DESCRIPTOR()
                returned = wintypes.DWORD()
                if k32.DeviceIoControl(
                    handle, 0x002D1400, ctypes.byref(query), ctypes.sizeof(query),
                    ctypes.byref(out), ctypes.sizeof(out), ctypes.byref(returned), None,
                ):
                    result = bool(out.IncursSeekPenalty)
            finally:
                k32.CloseHandle(handle)
    except Exception:
        result = None
    _spinning_disk_cache[drive] = result
    return result


MODEL_LOAD_MODES = ("auto", "mmap", "ram")
# Automatic only loads a model fully into RAM when it fits in this share of the
# RAM that is free at launch; the rest is headroom for the context/compute
# buffers, the OS and everything else the user has open.
MODEL_RAM_LOAD_MAX_FRACTION = 0.6
# When Settings asks for a RAM load outright: a model bigger than this share of the RAM that is free cannot be loaded that way at all
# (llama.cpp fails to allocate it and the server dies), so that one load is memory-mapped instead and the log says so.
MODEL_RAM_LOAD_FORCED_MAX_FRACTION = 0.9


def resolve_model_load(model_path, settings):
    """Decides how llama-server should read the model file: returns
    (load_into_ram, note) where load_into_ram means --no-mmap and note is a
    one-line explanation for the log (or None when there's nothing to say).

    Why this exists: llama.cpp memory-maps the model and then asks Windows to
    prefetch it, which on a spinning disk puts thousands of reads into the
    drive's queue at once. Measured on this app's own 5400 rpm drive: a queue
    of ~9,200 outstanding requests vs ~20 with --no-mmap (same load time), and
    while it lasts everything else that touches that drive - this app's own
    database commits, other programs - waits behind it for seconds to minutes.
    Reading the file in order into RAM instead keeps the queue short. On an SSD
    there's no such problem and mmap's instant warm start is the better deal,
    and a model too big to fit in free RAM has to be memory-mapped anyway."""
    mode = settings.get("model_load_mode", "auto")
    if mode == "ram":
        # Asked for outright - unless it cannot fit in the RAM that is free right now, when the load would simply fail: that one
        # load is memory-mapped instead, with a note; the setting itself is kept, and the next load asks again.
        size_mb = os.path.getsize(model_path) / (1024 * 1024)
        free_mb = available_ram_mb()
        if free_mb is not None and size_mb > free_mb * MODEL_RAM_LOAD_FORCED_MAX_FRACTION:
            return False, (
                f"NOTE: Settings asks for {os.path.basename(model_path)} to be read fully into RAM, but it is {size_mb:,.0f} MB and "
                f"only {free_mb:,.0f} MB of RAM is free right now, so it would not load that way - it is memory-mapped instead this "
                f"time (the setting is kept)."
            )
        return True, None
    if mode == "mmap":
        return False, None
    if path_is_on_spinning_disk(model_path) is not True:
        return False, None
    drive = os.path.splitdrive(os.path.abspath(model_path))[0]
    size_mb = os.path.getsize(model_path) / (1024 * 1024)
    free_mb = available_ram_mb()
    if free_mb is None or size_mb > free_mb * MODEL_RAM_LOAD_MAX_FRACTION:
        return False, (
            f"NOTE: {drive} is a spinning disk, so loading {os.path.basename(model_path)} will keep it "
            f"busy and slow other programs down until it finishes - it is too large to read fully into "
            f"RAM ({size_mb:,.0f} MB model, "
            f"{'unknown' if free_mb is None else f'{free_mb:,.0f} MB'} free), so it is memory-mapped instead."
        )
    return True, (
        f"NOTE: {drive} is a spinning disk - reading {os.path.basename(model_path)} fully into RAM "
        f"({size_mb:,.0f} MB, {free_mb:,.0f} MB free) instead of memory-mapping it, which keeps the "
        f"drive responsive for the rest of the system while it loads."
    )


# VRAM set aside for CUDA context, compute buffers, and image-model swap headroom —
# a rough constant, not derived from anything measurable without instrumenting llama.cpp.
VRAM_OVERHEAD_MB = 700


def estimate_vram_mb(model_path, gpu_layers, context_size, kv_cache_type, kv_offload=True):
    """Rough estimate of VRAM (in MiB) the chat model would need for the given settings:
    (fraction of the model's weights offloaded to GPU) + (KV cache at the given
    quantization) + a fixed overhead. This is a heuristic for the Advanced Settings
    meter — llama.cpp's own allocator is the real authority — but it's grounded in the
    model's actual on-disk size and GGUF-declared shape rather than a guess.

    kv_offload=False means llama.cpp is launched with --no-kv-offload, which keeps
    the whole KV cache in system RAM instead of VRAM — so the KV term is skipped
    entirely rather than just reduced, matching what's actually happening on the GPU.
    Confirmed by direct measurement 2026-08-04: at a fixed 35000 context, VRAM usage
    scaled with gpu_layers alone (~215 MB/layer, matching per_layer_mb) with no
    extra cost attributable to context size once kv_offload was off.

    Returns {"low": mb, "high": mb} — "high" assumes every layer needs a full
    context_size worth of KV cache, "low" assumes every layer only needs a sliding
    local-attention window (see gguf_sliding_window). For models without a sliding
    window these are equal. Models like Gemma 2/3 interleave a few full-context
    layers among many sliding-window ones, so real usage sits toward "low" — without
    knowing the exact per-architecture ratio, a single-point estimate would either
    consistently overshoot (as measured against Gemma 3 12B: this formula predicted
    ~13.6 GB when the model actually loaded fine with 1.7 GB of VRAM to spare) or
    require guessing that ratio, so a range is reported instead of false precision."""
    block_count = gguf_block_count(model_path)
    if not block_count:
        return None
    file_size_mb = os.path.getsize(model_path) / (1024 * 1024)
    # +1 lumps embedding/output/norm tensors in as one extra layer-equivalent share.
    per_layer_mb = file_size_mb / (block_count + 1)
    # block_count + 1, not block_count: llama.cpp offloads the output/embedding
    # tensors too once -ngl exceeds the layer count, and that extra share is
    # worth real throughput - measured 2026-08-20 on Qwythos-9B-MTP, 51.3 ->
    # 84.6 tok/s for 98 MiB. Capping here at block_count (as this did until
    # then) made that configuration unreachable however much VRAM was free,
    # because safe_gpu_layers clamps against whatever this function will cost.
    offloaded_layers = min(int(gpu_layers), block_count + 1)
    weight_mb = per_layer_mb * offloaded_layers + VRAM_OVERHEAD_MB

    if not kv_offload:
        return {"low": weight_mb, "high": weight_mb}

    kv_head_dim = gguf_kv_head_dim(model_path)
    kv_bytes_per_elem = KV_CACHE_BYTES.get(kv_cache_type, 2.0)
    if not kv_head_dim:
        return {"low": weight_mb, "high": weight_mb}
    # Not block_count - hybrid linear-attention models only cache on their
    # attention layers. See gguf_kv_cache_layers.
    kv_layers = gguf_kv_cache_layers(model_path) or block_count

    def kv_mb_for(window):
        # 2x for K and V.
        return (window * kv_layers * kv_head_dim * 2 * kv_bytes_per_elem) / (1024 * 1024)

    high = weight_mb + kv_mb_for(context_size)
    sliding_window = gguf_sliding_window(model_path)
    if sliding_window and sliding_window < context_size:
        low = weight_mb + kv_mb_for(sliding_window)
    else:
        low = high
    return {"low": low, "high": high}


# Real, reproducible failure mode this guards against (2026-08-04): gpu_layers
# in settings.json is one global value reused across every model regardless
# of size. Switch from a model it was tuned for to a much bigger one and
# llama.cpp's own memory-fit check aborts rather than degrading — but the
# process doesn't always exit cleanly, leaving it holding several GB of
# VRAM/RAM indefinitely while never answering /health, which reads as (and
# contributes to) the whole machine hanging. Better to undershoot layers
# (slower inference, CPU-offloaded remainder) than hit that path again.
#
# 512 was measured insufficient (2026-08-04, kv_offload=False probe on a
# 27B/64-block model): estimate_vram_mb's linear per-layer model doesn't
# capture how compute-buffer overhead grows as more layers execute on GPU —
# real usage outran the estimate by ~1.5GB right at the edge (35 real layers
# safe with 1.2GB to spare, 36 estimated-safe but only ~215-308MB free in
# reality, measured twice). 768 still wasn't enough margin either: a second
# live run landed back on the unsafe 36 because query_vram_mb()'s reading
# fluctuates run-to-run by ~150-300MB even with nothing else changing, and
# that alone was enough to cross the boundary. This is still a flat
# heuristic layered on top of a linear model that understates compute-buffer
# growth at high layer counts — not a real fix for either source of error,
# just a bigger buffer against both of them at once.
VRAM_SAFETY_MARGIN_MB = 1536

# How much foreign VRAM use (see llama_launch_vram) counts as "something else
# is holding your GPU" rather than ordinary desktop overhead. A Windows
# desktop with a browser open idles around 0.5-1.5 GB on this hardware, so
# 2048 clears that without needing a per-machine baseline.
#
# Measured 2026-08-30, and the reason this exists: an unrelated training job
# was holding 5.5-11 GB of a 12 GB card (fluctuating, so each launch sampled a
# different figure - which is why data/desktop.log shows the same model clamped
# to 23, then 22, then 10 across consecutive runs). With ~1 GB free, llama.cpp
# correctly offloaded *zero* layers and the 27B ran pure CPU at 0.52 tok/s.
# Nothing anywhere said why: the app just looked broken. The card was never
# the problem the user thought it was.
FOREIGN_VRAM_WARN_MB = 2048


def safe_gpu_layers(model_path, requested_layers, context_size, kv_cache_type, kv_offload=True, vram=None):
    """Clamps requested_layers down to a value estimate_vram_mb predicts will
    actually fit in currently-free VRAM for this specific model + context.
    Returns requested_layers unchanged if VRAM or GGUF metadata can't be
    read (matches estimate_vram_mb's own "estimate unavailable" behavior —
    e.g. non-NVIDIA/Vulkan-CPU setups per the README's portability notes),
    since there's nothing safe to clamp *to* in that case. `vram` is the
    {"total_mb", "free_mb"} to clamp against - start_llama passes the memory
    of the GPUs it is about to use; by default the driver is asked."""
    vram = vram or query_vram_mb()
    block_count = gguf_block_count(model_path)
    if vram is None or not block_count:
        return requested_layers
    budget_mb = vram["free_mb"] - VRAM_SAFETY_MARGIN_MB
    # block_count + 1 so a fully-offloaded model can also put its
    # output/embedding tensors on the GPU (see estimate_vram_mb).
    layers = max(0, min(int(requested_layers), block_count + 1))
    while layers > 0:
        estimate = estimate_vram_mb(model_path, layers, context_size, kv_cache_type, kv_offload)
        # estimate_vram_mb's "high" assumes every layer needs a full-context
        # KV cache — the safe (worst-case) figure to clamp against, since
        # undershooting the clamp reopens exactly the abort/hang this exists
        # to prevent, while overshooting it only costs some spare VRAM.
        if estimate is None or estimate["high"] <= budget_mb:
            break
        layers -= 1
    return layers


def build_multipart(fields, files):
    """fields: dict[str, str]; files: list of (field_name, filename, content_type, data bytes).
    Returns (body_bytes, content_type_header_value). No external deps (no requests library)."""
    boundary = f"----LocalAIChatBotBoundary{int(time.time() * 1000)}"
    parts = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    for field_name, filename, content_type, data in files:
        header = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field_name}"; '
            f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'
        )
        parts.append(header.encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# Recognizes a "WIDTHxHEIGHT"-shaped dimension request inside a chat-typed
# image prompt ("...the dimensions should be 500x1000") so it can be pulled
# out and passed as sd-server's own "size" parameter instead of being left
# as confusing literal text for the diffusion model to try to render.
# Confirmed live (2026-08-03) against sd-server's actual OpenAI-compat
# /v1/images/generations endpoint: it honors "size":"WxH" (the standard
# OpenAI DALL-E convention) but silently ignores separate "width"/"height"
# fields despite /sdcpp/v1/capabilities documenting those as the *native*
# defaults schema - "size" is the one that actually works over this
# endpoint. Bounds are well inside the server's own reported limits
# (64-4096) - a naive huge request could overflow the 12GB VRAM budget this
# app already has to manage carefully (see the VRAM-cliff history).
IMAGE_DIMENSIONS_RE = re.compile(r"(\d{2,4})\s*[xX×]\s*(\d{2,4})")
MIN_IMAGE_DIMENSION = 256
MAX_IMAGE_DIMENSION = 1536


def extract_image_dimensions(prompt):
    """Returns ("WxH", cleaned_prompt) if the prompt contains a dimension
    request, else (None, prompt) unchanged."""
    m = IMAGE_DIMENSIONS_RE.search(prompt)
    if not m:
        return None, prompt
    w = max(MIN_IMAGE_DIMENSION, min(MAX_IMAGE_DIMENSION, int(m.group(1))))
    h = max(MIN_IMAGE_DIMENSION, min(MAX_IMAGE_DIMENSION, int(m.group(2))))
    cleaned = re.sub(r"\s+", " ", prompt[: m.start()] + prompt[m.end() :]).strip()
    return f"{w}x{h}", cleaned or prompt


# Recognizes the chat model hallucinating a fake "I generated/edited an
# image" claim during a *normal* chat reply - confirmed live (2026-08-03):
# once a real generate/edit exchange exists in a conversation's history, an
# unrelated follow-up that doesn't match the chat-side intent regexes falls
# through to the chat model, which - having just seen a real "Generated an
# image for: X" turn in its own context - completes the same *structural*
# pattern on the next turn with a fabricated success claim and an invented
# filename, even though no image was actually created that turn. Rewording
# the context placeholder (see build_chat_messages_payload) only partly
# helped - the model adapted and mimicked the new wording too. This phrase
# is otherwise "reserved": generate_image_worker/edit_image_worker are the
# only legitimate source of a message starting with it.
FAKE_IMAGE_CLAIM_RE = re.compile(r"^\s*(?:generated an image for|edited the image for)\s*:", re.IGNORECASE)
# How many characters of real answer content to hold back before deciding -
# comfortably longer than either reserved phrase above, short enough that
# holding back this many characters (not tokens) at typical output speeds is
# imperceptible for the ~100% of ordinary replies that never trip the check.
FAKE_IMAGE_CLAIM_LOOKAHEAD_CHARS = 40


def _ensure_image_model_loaded(model):
    """Shared by generate_image_worker/edit_image_worker: if the requested
    image model is already resident (see image_model_resident) from a prior
    call in the same burst, skips straight to "generating" and reuses the
    still-running sd-server - confirmed live (2026-08-03) this skips
    sd-server's own lazy first-use tensor-loading/conversion step, the
    dominant cost on a cold start under VRAM pressure. Otherwise runs the
    original full unload-chat/load-image/wait-for-health sequence. Raises
    on failure, same as the code it replaced."""
    global image_model_resident, sd_resident_model_path
    if image_model_resident and sd_resident_model_path == model:
        image_gen_status["stage"] = "generating"
        return
    if image_model_resident:
        # Resident, but for a different model than this request wants (e.g.
        # the active image model changed) - tear down before loading afresh.
        # handle_set_active_image_model() normally refuses this while
        # resident, so this is a defensive fallback, not the common path.
        stop_sd_server()
        image_model_resident = False
        sd_resident_model_path = None
    problem = image_server_blocker()
    if problem:
        raise RuntimeError(problem)          # before the chat model is unloaded: an image server that cannot start is no use to anyone
    image_gen_status["stage"] = "unloading_chat_model"
    with llama_restart_lock:
        stop_llama()
    image_gen_status["stage"] = "loading_image_model"
    start_sd_server()
    deadline = time.time() + 120
    started_at = time.time()
    cpu_retry = False
    ready = False
    while time.time() < deadline:
        if image_gen_status["cancelled"]:
            raise RuntimeError("cancelled")
        if sd_health() is not None:
            ready = True
            break
        proc = sd_proc
        if proc is None or proc.poll() is not None:
            if proc is not None and not cpu_retry and time.time() - started_at < 60:
                # it ended within a minute: one more try with no graphics card in the picture (a build that does not know this card)
                cpu_retry = True
                print("image server stopped at once - trying once on the CPU")
                start_sd_server(force_cpu=True)
                deadline = time.time() + 600
                continue
            raise RuntimeError(image_server_stopped_message(proc) + (" It was also tried on the CPU." if cpu_retry else ""))       # not going to come up: do not wait
        time.sleep(1)
    if not ready:
        raise RuntimeError("image model server did not become ready in time")
    image_gen_status["stage"] = "generating"


def edit_image_worker(conv_id, source_bytes, source_mime, prompt, ref_images=None):
    global image_model_resident, sd_resident_model_path, last_image_activity
    try:
        model = find_image_model()
        _ensure_image_model_loaded(model)
        size, prompt = extract_image_dimensions(prompt)
        ext = "png" if "png" in source_mime else "jpg"
        files = [("image", f"source.{ext}", source_mime, source_bytes)]
        for i, (ref_mime, ref_bytes) in enumerate(ref_images or []):
            ref_ext = "png" if "png" in ref_mime else "jpg"
            files.append(("ref_images", f"ref{i}.{ref_ext}", ref_mime, ref_bytes))
        fields = {"prompt": prompt, "response_format": "b64_json"}
        if size:
            fields["size"] = size
        body, content_type = build_multipart(fields, files)
        req = urllib.request.Request(
            SD_URL + "/v1/images/edits",
            data=body,
            headers={"Content-Type": content_type},
        )
        # Confirmed live: on this hardware, most of the wall-clock time here
        # is tensor loading/conversion (T5-XXL etc.), not sampling itself -
        # a cold-cache run measured ~300s end to end for a plain generation,
        # right at the old timeout's edge, causing intermittent spurious
        # "timed out" failures on a request that would otherwise have
        # succeeded. 600s leaves real margin above the observed worst case.
        with urllib.request.urlopen(req, timeout=600) as resp:
            result = json.loads(resp.read().decode())
        image_bytes = base64.b64decode(result["data"][0]["b64_json"])

        # Left resident (not restored) for a fast follow-up edit/generate -
        # see image_model_resident's own comment. Only a real chat message
        # (ensure_chat_model_available()) or the idle watcher swaps back.
        image_model_resident = True
        sd_resident_model_path = model
        last_image_activity = time.time()

        ref_note = f" (with {len(ref_images)} reference image{'s' if len(ref_images) != 1 else ''})" if ref_images else ""
        with db() as conn:
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'user', ?)",
                (conv_id, f"Edit image{ref_note}: {prompt}"),
            )
            cur = conn.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'assistant', ?)",
                (conv_id, f"Edited the image for: {prompt}"),
            )
            new_message_id = cur.lastrowid
            conn.execute(
                "INSERT INTO message_images (message_id, filename, mime_type, data) VALUES (?, ?, ?, ?)",
                (new_message_id, "edited.png", "image/png", image_bytes),
            )
            conn.execute(
                "UPDATE conversations SET updated_at=datetime('now','localtime') WHERE id=?",
                (conv_id,),
            )
        image_gen_status["error"] = None
    except Exception as e:
        image_gen_status["error"] = "cancelled" if image_gen_status["cancelled"] else str(e)
        try:
            stop_sd_server()
            image_model_resident = False
            sd_resident_model_path = None
            restart_chat_model_if_down()
        except Exception:
            pass
    finally:
        image_gen_status["active"] = False
        image_gen_status["stage"] = None


def generate_image_worker(conv_id, prompt):
    global image_model_resident, sd_resident_model_path, last_image_activity
    try:
        model = find_image_model()
        _ensure_image_model_loaded(model)
        size, prompt = extract_image_dimensions(prompt)
        payload_dict = {"prompt": prompt, "n": 1, "response_format": "b64_json"}
        if size:
            payload_dict["size"] = size
        payload = json.dumps(payload_dict).encode()
        req = urllib.request.Request(
            SD_URL + "/v1/images/generations",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        # See the matching comment in edit_image_worker - 600s for the same reason.
        with urllib.request.urlopen(req, timeout=600) as resp:
            result = json.loads(resp.read().decode())
        image_bytes = base64.b64decode(result["data"][0]["b64_json"])

        # Left resident (not restored) for a fast follow-up edit/generate -
        # see image_model_resident's own comment. Only a real chat message
        # (ensure_chat_model_available()) or the idle watcher swaps back.
        image_model_resident = True
        sd_resident_model_path = model
        last_image_activity = time.time()

        with db() as conn:
            conv = conn.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
            # Insert the prompt as a user turn first so the conversation keeps
            # valid user/assistant alternation for any future chat messages
            # (Gemma's chat template rejects an assistant-first history).
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'user', ?)",
                (conv_id, f"Generate an image: {prompt}"),
            )
            cur = conn.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'assistant', ?)",
                (conv_id, f"Generated an image for: {prompt}"),
            )
            new_message_id = cur.lastrowid
            conn.execute(
                "INSERT INTO message_images (message_id, filename, mime_type, data) VALUES (?, ?, ?, ?)",
                (new_message_id, "generated.png", "image/png", image_bytes),
            )
            conn.execute(
                "UPDATE conversations SET updated_at=datetime('now','localtime') WHERE id=?",
                (conv_id,),
            )
            if conv is not None and conv["title"] == "New chat":
                title = re.sub(r"\s+", " ", prompt)[:60]
                conn.execute("UPDATE conversations SET title=? WHERE id=?", (title, conv_id))
        image_gen_status["error"] = None
    except Exception as e:
        image_gen_status["error"] = "cancelled" if image_gen_status["cancelled"] else str(e)
        try:
            stop_sd_server()
            image_model_resident = False
            sd_resident_model_path = None
            restart_chat_model_if_down()
        except Exception:
            pass
    finally:
        image_gen_status["active"] = False
        image_gen_status["stage"] = None


def _comfyui_substitute(node, marker, prompt):
    """Recursively replaces every occurrence of `marker` inside a ComfyUI
    API-format workflow (any string value, at any depth) with the real
    prompt text - see comfyui_prompt_marker's own comment in DEFAULT_SETTINGS
    for why this is a literal placeholder the user places themselves rather
    than heuristically guessing which node is "the" positive prompt."""
    if isinstance(node, dict):
        return {k: _comfyui_substitute(v, marker, prompt) for k, v in node.items()}
    if isinstance(node, list):
        return [_comfyui_substitute(v, marker, prompt) for v in node]
    if isinstance(node, str):
        return node.replace(marker, prompt)
    return node


def comfyui_render(prompt):
    """Runs the configured ComfyUI workflow for `prompt` and returns the picture's bytes. Raises RuntimeError with a sentence a person can act on.
    Shared by the chat path (comfyui_generate_worker) and the Images tab (gallery_generate_worker)."""
    settings = load_settings()
    base_url = (settings.get("comfyui_url") or "").rstrip("/")
    workflow_path = settings.get("comfyui_workflow_path") or ""
    marker = settings.get("comfyui_prompt_marker") or "__PROMPT__"
    prompt_id = None
    try:
        image_gen_status["stage"] = "generating"
        with open(workflow_path, "r", encoding="utf-8") as f:
            workflow = json.load(f)
        if marker not in json.dumps(workflow):
            raise RuntimeError(
                f'the workflow file has no "{marker}" placeholder in any field - '
                "put it in the positive prompt text node, then re-export"
            )
        workflow = _comfyui_substitute(workflow, marker, prompt)

        submit_req = urllib.request.Request(
            base_url + "/prompt",
            data=json.dumps({"prompt": workflow, "client_id": uuid.uuid4().hex}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(submit_req, timeout=30) as resp:
                submitted = json.loads(resp.read().decode())
        except OSError as e:          # URLError (refused: nothing listening) and the plain connection errors urllib lets through (dropped)
            if is_connection_failure(e):
                # "<urlopen error [WinError 10061] No connection could be made because the target machine actively refused it>" says
                # nothing a user can act on. ComfyUI is not started by this app: it has to be running already.
                raise RuntimeError(f"ComfyUI is not reachable at {base_url}. It has to be running already - start it, or correct the "
                                   "ComfyUI address, then try again.") from e
            raise
        prompt_id = submitted.get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"ComfyUI rejected the workflow: {submitted.get('node_errors') or submitted}")

        # ComfyUI generation time varies enormously by workflow/model/steps -
        # this deadline is generous on purpose, same reasoning as sd-server's
        # own 600s HTTP timeout elsewhere in this file.
        deadline = time.time() + 600
        history = None
        while time.time() < deadline:
            if image_gen_status["cancelled"]:
                raise RuntimeError("cancelled")
            try:
                with urllib.request.urlopen(base_url + f"/history/{prompt_id}", timeout=10) as resp:
                    all_history = json.loads(resp.read().decode())
            except Exception:
                all_history = {}
            if prompt_id in all_history:
                history = all_history[prompt_id]
                break
            time.sleep(2)
        if history is None:
            raise RuntimeError("ComfyUI did not finish generating in time")

        image_ref = None
        for node_output in (history.get("outputs") or {}).values():
            images = node_output.get("images") or []
            if images:
                image_ref = images[0]
                break
        if image_ref is None:
            status_str = ((history.get("status") or {}).get("status_str")) or "no image in the workflow's output"
            raise RuntimeError(f"ComfyUI finished without producing an image: {status_str}")

        view_qs = urllib.parse.urlencode(
            {
                "filename": image_ref["filename"],
                "subfolder": image_ref.get("subfolder", ""),
                "type": image_ref.get("type", "output"),
            }
        )
        with urllib.request.urlopen(base_url + "/view?" + view_qs, timeout=60) as resp:
            return resp.read()
    except Exception:
        if prompt_id and image_gen_status["cancelled"]:
            # Best-effort - remove it from the queue if still waiting, and
            # interrupt it if it's the one currently running. Not fatal if
            # ComfyUI is unreachable here; the job just keeps running
            # orphaned server-side (this app was never managing that process
            # to begin with, unlike sd-server, so there's no stronger lever).
            try:
                cancel_req = urllib.request.Request(
                    base_url + "/queue",
                    data=json.dumps({"delete": [prompt_id]}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(cancel_req, timeout=5)
            except Exception:
                pass
            try:
                interrupt_req = urllib.request.Request(base_url + "/interrupt", data=b"")
                urllib.request.urlopen(interrupt_req, timeout=5)
            except Exception:
                pass
        raise


def comfyui_generate_worker(conv_id, prompt):
    """Generate-image path for the "comfyui" image_backend - an externally
    run ComfyUI instance this app only ever talks to over HTTP, never
    launches/stops (unlike sd-server), so none of generate_image_worker's
    chat-model-swap/residency logic applies here: the chat model is left
    running throughout. Writes to the DB using the exact same message
    shape generate_image_worker uses, so the rest of the app (transcript
    rendering, edit-chaining's _last_image_by_conv gate, etc.) works
    unchanged regardless of which backend actually produced the image."""
    global last_image_activity
    try:
        image_bytes = comfyui_render(prompt)
        last_image_activity = time.time()
        with db() as conn:
            conv = conn.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
            # Insert the prompt as a user turn first so the conversation keeps
            # valid user/assistant alternation for any future chat messages
            # (Gemma's chat template rejects an assistant-first history) -
            # same reasoning as generate_image_worker.
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'user', ?)",
                (conv_id, f"Generate an image: {prompt}"),
            )
            cur = conn.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'assistant', ?)",
                (conv_id, f"Generated an image for: {prompt}"),
            )
            new_message_id = cur.lastrowid
            conn.execute(
                "INSERT INTO message_images (message_id, filename, mime_type, data) VALUES (?, ?, ?, ?)",
                (new_message_id, "generated.png", "image/png", image_bytes),
            )
            conn.execute(
                "UPDATE conversations SET updated_at=datetime('now','localtime') WHERE id=?",
                (conv_id,),
            )
            if conv is not None and conv["title"] == "New chat":
                title = re.sub(r"\s+", " ", prompt)[:60]
                conn.execute("UPDATE conversations SET title=? WHERE id=?", (title, conv_id))
        image_gen_status["error"] = None
    except Exception as e:
        image_gen_status["error"] = "cancelled" if image_gen_status["cancelled"] else str(e)
    finally:
        image_gen_status["active"] = False
        image_gen_status["stage"] = None


# ---- the Images tab: pictures made from the tab itself, not inside a chat. Each is a PNG plus a small JSON of how it was made, in outputs\generated\
# (no database table: the files are the gallery, and a person can open the folder).

GALLERY_CATEGORY = "generated"
GALLERY_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,120}\.png")
GALLERY_LIMITS = {"width": (256, 1536), "height": (256, 1536), "steps": (1, 100), "cfg_scale": (0.0, 30.0)}


def gallery_clean_params(body, settings):
    """The request body of POST /api/images/generate -> (params, problem). Unknown keys are ignored on purpose (a newer tab may send more)."""
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        return None, "write a prompt first"
    if len(prompt) > 4000:
        return None, "the prompt is too long (4000 characters at most)"
    p = {"prompt": prompt, "negative_prompt": str(body.get("negative_prompt") or "").strip()[:2000]}
    for key, (lo, hi) in GALLERY_LIMITS.items():
        raw = body.get(key)
        if raw in (None, ""):
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None, f"{key} must be a number"
        p[key] = max(lo, min(hi, val))
    for key in ("width", "height"):
        if key in p:
            p[key] = int(p[key] // 8 * 8)
    if "steps" in p:
        p["steps"] = int(p["steps"])
    try:
        seed = int(body.get("seed", -1))
    except (TypeError, ValueError):
        return None, "seed must be a whole number"
    p["seed"] = seed if seed >= 0 else -1
    backend = body.get("backend") or settings.get("image_backend") or "local"
    p["backend"] = "comfyui" if backend == "comfyui" else "local"
    return p, None


def gallery_extra_args(p, kind):
    """The sd-server per-request settings, sent inside the prompt in the tag its own server reads (<sd_cpp_extra_args>{json}</...>)."""
    extra = {}
    if p.get("negative_prompt"):
        extra["negative_prompt"] = p["negative_prompt"]
    if p.get("seed", -1) >= 0:
        extra["seed"] = p["seed"]
    sample = {}
    if "steps" in p:
        sample["sample_steps"] = p["steps"]
    if "cfg_scale" in p:
        # an editing (Flux-style) model is guided by "distilled guidance" and wants the ordinary cfg left at 1
        sample["guidance"] = {"txt_cfg": 1.0, "distilled_guidance": p["cfg_scale"]} if kind == "edit" else {"txt_cfg": p["cfg_scale"]}
    if sample:
        extra["sample_params"] = sample
    return extra


def gallery_save(image_bytes, p, model_name):
    """Writes the PNG and its JSON into the gallery folder; returns the PNG's file name."""
    folder = outputs_subdir(GALLERY_CATEGORY)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = f"img-{stamp}"
    n = 1
    while os.path.exists(os.path.join(folder, stem + ".png")):
        n += 1
        stem = f"img-{stamp}-{n}"
    meta = {k: v for k, v in p.items()}
    meta.update({"model": model_name, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "bytes": len(image_bytes)})
    with open(os.path.join(folder, stem + ".json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    with open(os.path.join(folder, stem + ".png"), "wb") as f:
        f.write(image_bytes)
    return stem + ".png"


def gallery_generate_worker(p):
    """The Images tab's job: the same image lock, status and cancel as a chat generation, but the result is a file in the gallery folder."""
    global image_model_resident, sd_resident_model_path, last_image_activity
    try:
        if p["backend"] == "comfyui":
            image_bytes = comfyui_render(p["prompt"])
            model_name = "ComfyUI"
        else:
            model = find_image_model()
            _ensure_image_model_loaded(model)
            kind = image_model_kind(model)[0]
            prompt = p["prompt"]
            extra = gallery_extra_args(p, kind)
            if extra:
                prompt += "<sd_cpp_extra_args>" + json.dumps(extra) + "</sd_cpp_extra_args>"
            payload_dict = {"prompt": prompt, "n": 1, "response_format": "b64_json"}
            if "width" in p or "height" in p:
                payload_dict["size"] = f"{int(p.get('width', 512))}x{int(p.get('height', 512))}"
            req = urllib.request.Request(SD_URL + "/v1/images/generations", data=json.dumps(payload_dict).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=600) as resp:
                result = json.loads(resp.read().decode())
            image_bytes = base64.b64decode(result["data"][0]["b64_json"])
            image_model_resident = True
            sd_resident_model_path = model
            model_name = os.path.basename(model)
        last_image_activity = time.time()
        gallery_save(image_bytes, p, model_name)
        image_gen_status["error"] = None
    except Exception as e:
        image_gen_status["error"] = "cancelled" if image_gen_status["cancelled"] else str(e)
        if p["backend"] != "comfyui":
            try:
                stop_sd_server()
                image_model_resident = False
                sd_resident_model_path = None
                restart_chat_model_if_down()
            except Exception:
                pass
    finally:
        image_gen_status["active"] = False
        image_gen_status["stage"] = None


# ---- conversation titles
#
# A title is a nicety generated after the first reply; a reply the user is
# waiting for always comes first. Measured on a real model (Qwythos-9B Q4_K_M):
#   * with thinking on (what this used to do) a title took 100-190 generated
#     tokens (1.4-2.4 s here, minutes on the big partly-offloaded models); with
#     thinking off it takes 10 tokens (0.27 s) - so thinking is switched off
#     (title_generation_params);
#   * it did NOT disturb the conversation's cached prompt (the follow-up reused
#     716 cached tokens with or without a title request in between), so sharing
#     the slot costs nothing there;
#   * but a client that disconnects from a NON-streaming request does not stop
#     llama-server generating it (the slot stayed busy for the full 10 s of a
#     900-token generation), whereas a disconnect during a STREAMING one frees
#     the slot in 0.14 s. So the title request is streamed, and a title still
#     running when a reply needs the model is abandoned on the spot
#     (cancel_pending_titles) rather than making that reply queue behind it.
title_jobs = {}  # conv_id -> TitleJob of every title request in flight; guarded by title_pending_lock
TITLE_TIMEOUT_S = 300
# A title that gave way to a reply (the user sent the next message before it was done) used to be lost for good: the chat kept its
# truncated first message as its name. It is remembered here and asked for again once that next reply has landed and the model is
# free - up to TITLE_RETRY_LIMIT times, then it is let go. conv_id -> (user_text, assistant_text, times already asked again);
# guarded by title_pending_lock.
title_retry = {}
TITLE_RETRY_LIMIT = 2


def take_title_retry(conv_id):
    """(user_text, assistant_text) of a title abandoned earlier for this conversation, to be asked for again now - or None when
    nothing was abandoned, or it has been tried TITLE_RETRY_LIMIT times already (then it is forgotten)."""
    with title_pending_lock:
        entry = title_retry.get(conv_id)
        if not entry:
            return None
        user_text, assistant_text, tries = entry
        if tries >= TITLE_RETRY_LIMIT:
            del title_retry[conv_id]
            return None
        title_retry[conv_id] = (user_text, assistant_text, tries + 1)
        return user_text, assistant_text


class TitleJob:
    """One title request that can be abandoned from another thread: cancel()
    sets a flag and shuts the request's socket down, which wakes the thread
    blocked on llama-server and - the request being streamed - makes llama-server
    drop the task and free the slot."""

    def __init__(self, conv_id=None, user_text=None, assistant_text=None):
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._conn = None
        # What the request is about, so that one abandoned for a reply can be asked for again later (cancel_pending_titles).
        self.conv_id, self.user_text, self.assistant_text = conv_id, user_text, assistant_text

    def attach(self, conn):
        with self._lock:
            self._conn = conn
            if self.cancelled.is_set():
                self._shut(conn)

    def cancel(self):
        self.cancelled.set()
        with self._lock:
            if self._conn is not None:
                self._shut(self._conn)

    @staticmethod
    def _shut(conn):
        try:
            conn.sock.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass


def start_title_generation(conv_id, user_text, assistant_text):
    job = TitleJob(conv_id, user_text, assistant_text)
    with title_pending_lock:
        title_pending.add(conv_id)
        title_jobs[conv_id] = job
    threading.Thread(
        target=generate_conversation_title, args=(conv_id, user_text, assistant_text, job), daemon=True
    ).start()


def cancel_pending_titles():
    """Called when something is about to need the model for real (a reply, a
    compaction, an image job): any title still being generated gives way. The
    conversation keeps the placeholder title it was given from its first
    message for now - the title is asked for again once the next reply has
    landed (take_title_retry), up to TITLE_RETRY_LIMIT times."""
    with title_pending_lock:
        jobs = list(title_jobs.values())
        # Remembered right here, before the reply even starts, so that the reply's end finds it whatever the abandoned
        # thread is still doing. An entry already there keeps its count of tries.
        for job in jobs:
            if job.conv_id is not None and job.conv_id not in title_retry:
                title_retry[job.conv_id] = (job.user_text, job.assistant_text, 0)
    for job in jobs:
        job.cancel()


def title_generation_params():
    """(chat_template_kwargs, max_tokens) for the title request. A title never
    needs reasoning, so where the model's template has a switch for it, it is
    used. gpt-oss style templates can't be switched off, only turned down to
    "low", and a model that thinks with no switch at all still needs room for the
    preamble - those keep the old 300-token allowance (confirmed live that 20
    tokens was consumed entirely by the preamble, leaving content empty and the
    rename silently skipped). A model that doesn't think needs only a few."""
    cap = model_thinking_capability(find_model())
    if cap["control"] == "enable_thinking":
        return {"enable_thinking": False}, 64
    if cap["control"] == "reasoning_effort":
        return {"reasoning_effort": "low"}, 300
    return ({}, 300) if cap["supports"] else ({}, 64)


def request_title_text(payload, job):
    """Streams the title request and returns the text it produced, or None if
    `job` was cancelled first (or the connection was cut because of that)."""
    if job.cancelled.is_set():
        return None
    parsed = urllib.parse.urlsplit(LLAMA_URL)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=TITLE_TIMEOUT_S)
    try:
        conn.connect()
        job.attach(conn)
        conn.request(
            "POST", "/v1/chat/completions", body=json.dumps(payload).encode(),
            headers=llama_headers({"Content-Type": "application/json"}),
        )
        resp = conn.getresponse()
        if resp.status != 200:
            raise RuntimeError(f"llama-server answered {resp.status}: {resp.read()[:200]!r}")
        parts = []
        for raw_line in resp:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            for choice in chunk.get("choices", []):
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    parts.append(piece)
        return None if job.cancelled.is_set() else "".join(parts)
    except (OSError, http.client.HTTPException):
        if job.cancelled.is_set():
            return None
        raise
    finally:
        conn.close()


def generate_conversation_title(conv_id, user_text, assistant_text, job=None):
    """Best-effort background enhancement: ask the model for a short title based
    on the first exchange. Failures are silent — the conversation keeps the
    truncated-message fallback title that was already set synchronously."""
    job = job or TitleJob()
    done = False                # a title was written (or there was nothing to write)
    try:
        template_kwargs, max_tokens = title_generation_params()
        payload = {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Generate a short, specific title (3-6 words) summarizing the "
                        "conversation below. Reply with ONLY the title text — no quotes, "
                        "no trailing punctuation, no explanation."
                    ),
                },
                {
                    "role": "user",
                    "content": f"User: {(user_text or '')[:500]}\n\nAssistant: {(assistant_text or '')[:500]}",
                },
            ],
            "stream": True,
            "temperature": 0.3,
            "max_tokens": max_tokens,
        }
        if template_kwargs:
            payload["chat_template_kwargs"] = template_kwargs
        raw_title = request_title_text(payload, job)
        if raw_title is None:
            return  # gave way to a reply that needed the model; the placeholder title stays
        raw_title = raw_title.strip()
        # Defends against a model whose chat template doesn't get llama-server's
        # native reasoning_content split, so a <think> preamble lands directly in
        # content instead - confirmed no such split/stripping exists anywhere else
        # in this file, and no --reasoning-format flag is passed at launch, so this
        # is the only place that would ever see the leak. An unclosed <think> (ran
        # out of max_tokens mid-reasoning) keeps only what precedes it; a closed
        # one is dropped entirely.
        title = raw_title
        if re.search(r"<think>", title, re.IGNORECASE) and not re.search(r"</think>", title, re.IGNORECASE):
            title = re.split(r"<think>", title, maxsplit=1, flags=re.IGNORECASE)[0]
        title = re.sub(r"<think>.*?</think>", "", title, flags=re.DOTALL | re.IGNORECASE)
        title = title.strip().strip("\"'").strip()
        title = re.sub(r"\s+", " ", title).rstrip(".!?")[:60]
        if title:
            # Only replaces the placeholder (see _begin_chat_turn): a name the
            # user typed in the meantime is theirs and stays.
            placeholder = re.sub(r"\s+", " ", user_text or "")[:60] or "Image"
            with db() as conn:
                conn.execute(
                    "UPDATE conversations SET title=? WHERE id=? AND title=?", (title, conv_id, placeholder)
                )
        else:
            print(
                f"WARNING: title generation for conversation {conv_id} produced an empty "
                f"title after stripping (raw content: {raw_title!r})"
            )
        done = True
    except Exception:
        if not job.cancelled.is_set():
            print(f"WARNING: title generation failed for conversation {conv_id}:")
            traceback.print_exc()
    finally:
        with title_pending_lock:
            # Only the NEWEST job for this conversation clears its pending mark: an abandoned one that finishes late
            # (a slow machine) must not discard the mark a retried job registered meanwhile (/title-status read "not
            # pending" while the retry still ran).
            newest = title_jobs.get(conv_id)
            if newest is None or newest is job:
                title_pending.discard(conv_id)
            if newest is job:
                del title_jobs[conv_id]
            if done or not job.cancelled.is_set():
                # Written, empty, or failed for a reason the next reply would not cure: nothing to ask again. (One that gave
                # way to a reply was remembered by cancel_pending_titles and is asked for again once that reply has landed.)
                title_retry.pop(conv_id, None)


def caption_image(data, mime_type):
    """One-time, synchronous captioning call for a project image (see
    handle_upload_project_image): the returned text is what actually gets
    injected into every future chat turn in that project, in place of the
    image itself - so unlike generate_conversation_title's best-effort
    swallow-all-failures style, this call's failures must propagate to the
    caller (a synchronous upload request the user is waiting on), not be
    silently absorbed."""
    b64 = base64.b64encode(data).decode("ascii")
    payload = {
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": CAPTION_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
            ]},
        ],
        "stream": False,
        "temperature": 0.3,
        "max_tokens": 600,
    }
    req = urllib.request.Request(
        LLAMA_URL + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers=llama_headers({"Content-Type": "application/json"}),
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        result = json.loads(resp.read().decode())
    caption = result["choices"][0]["message"]["content"].strip()
    if not caption:
        raise ValueError("the vision model returned an empty description")
    return caption


def find_mmproj():
    settings = load_settings()
    active = settings.get("active_mmproj")
    if not active:
        return None
    path = os.path.join(MMPROJ_DIR, active)
    return path if os.path.isfile(path) else None


_WINDOWS_DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"] + [f"COM{i}" for i in range(1, 10)] + [f"LPT{i}" for i in range(1, 10)]
)


def safe_filename(name):
    """A bare file name that is safe to create under one of the app's folders, or
    None. Besides stripping any directory part this refuses what Windows cannot
    store or treats specially: control characters (a NUL used to make open()
    raise ValueError), the characters <>:"|?*, names ending in a dot or space,
    and the reserved device names (CON, NUL, COM1 ... - "NUL.gguf" would
    swallow an upload without writing a byte, "CON.gguf" would write to the
    console) with or without an extension."""
    name = os.path.basename(str(name or "").strip())
    if not name or name in (".", "..") or "/" in name or "\\" in name or len(name) > 255:
        return None
    if any(ord(ch) < 32 or ch in '<>:"|?*' for ch in name) or name.endswith((".", " ")):
        return None
    if name.split(".")[0].rstrip(" ").upper() in _WINDOWS_DEVICE_NAMES:
        return None
    return name


# ---------------------------------------------------------------- export

EXPORT_MIME = {"json": "application/json", "txt": "text/plain", "md": "text/markdown"}


def export_filename(title, fmt, suffix=None):
    safe = re.sub(r"[^\w\-]+", "_", title or "conversation").strip("_")[:60] or "conversation"
    ts = time.strftime("%Y%m%d-%H%M%S")
    parts = [safe] + ([suffix] if suffix else []) + [ts]
    return "-".join(parts) + "." + fmt


def build_conversation_export(conv, messages, fmt):
    if fmt == "json":
        content = json.dumps(
            {
                "title": conv["title"],
                "created_at": conv["created_at"],
                "messages": [
                    {"role": m["role"], "content": m["content"], "created_at": m["created_at"]}
                    for m in messages
                ],
            },
            indent=2,
        )
    elif fmt == "txt":
        lines = [conv["title"], ""]
        for m in messages:
            who = "User" if m["role"] == "user" else "Assistant"
            lines.append(f"{who}: {m['content']}")
            lines.append("")
        content = "\n".join(lines)
    else:
        lines = [f"# {conv['title']}", ""]
        for m in messages:
            who = "**User:**" if m["role"] == "user" else "**Assistant:**"
            lines.append(who)
            lines.append("")
            lines.append(m["content"])
            lines.append("")
            lines.append("---")
            lines.append("")
        content = "\n".join(lines)
    return content, EXPORT_MIME[fmt]


def build_system_prompt(conn, conv, settings, query_text=None):
    """query_text: the current turn's user message (or edited/regenerated-
    for text - see handle_chat/handle_edit_message), used only if this
    project's stored files exceed their raw-inclusion budget and retrieval
    needs something to rank chunks against - see the "Reference files"
    branch below. None is fine (e.g. handle_context_usage's informational
    call, which has no "current turn" to speak of) - retrieval still runs,
    just ranked against an empty query, which is a worse but not broken
    fallback."""
    system_content = settings["system_prompt"]
    extra_notes = [
        RESPONSE_LENGTH_PROMPTS.get(settings.get("verbosity", "normal"), ""),
    ]
    extra_notes = [n for n in extra_notes if n]
    if extra_notes:
        system_content = system_content + "\n\n" + "\n".join(extra_notes)
    if conv["project_id"]:
        proj = conn.execute("SELECT * FROM projects WHERE id=?", (conv["project_id"],)).fetchone()
        if proj is not None:
            files = conn.execute(
                "SELECT filename, content FROM project_files WHERE project_id=? ORDER BY added_at",
                (conv["project_id"],),
            ).fetchall()
            memory = conn.execute(
                "SELECT content FROM project_memory WHERE project_id=? ORDER BY created_at",
                (conv["project_id"],),
            ).fetchall()
            images = conn.execute(
                "SELECT filename, caption FROM project_images WHERE project_id=? ORDER BY added_at",
                (conv["project_id"],),
            ).fetchall()
            parts = [system_content, f"--- Project: {proj['name']} ---"]
            if proj["instructions"].strip():
                parts.append(proj["instructions"].strip())
            if files:
                # project_content_tokens_used sums files+images+memory
                # together (the combined budget this whole check is against)
                # - so images/memory alone can push this over even for a
                # small file set. Retrieval only ever helps the files portion
                # (images/memory always stay fully raw-dumped, in both
                # branches below) - a known, accepted limit, not a bug: still
                # strictly better than the unconditional hard block this
                # replaced, since it now at least tries instead of refusing
                # outright.
                used = project_content_tokens_used(conn, conv["project_id"])
                budget = project_content_token_budget(settings)
                if used <= budget:
                    file_blocks = "\n\n".join(f"### {f['filename']}\n{f['content']}" for f in files)
                    parts.append("Reference files:\n" + file_blocks)
                else:
                    excerpts = []
                    if ensure_project_chunks(conn, conv["project_id"]):
                        excerpts = retrieve_relevant_chunks(conn, conv["project_id"], query_text)
                    if excerpts:
                        excerpt_blocks = "\n\n".join(
                            f"### {e['filename']} (excerpt)\n{e['content']}" for e in excerpts
                        )
                        parts.append(
                            "Reference files (this project's stored files exceed the full-inclusion "
                            "budget - showing only the excerpts most relevant to this turn, not "
                            "complete files):\n" + excerpt_blocks
                        )
                    # Embedding server unreachable or nothing retrieved -
                    # skip file content entirely rather than dumping
                    # (potentially oversized) raw content; instructions/
                    # images/memory below are unaffected either way.
            if images:
                image_blocks = "\n\n".join(f"### {i['filename']}\n{i['caption']}" for i in images)
                parts.append(
                    "Reference images (AI-generated descriptions - you are not shown the images "
                    "directly, only these descriptions):\n" + image_blocks
                )
            if memory:
                notes = "\n".join(f"- {m['content']}" for m in memory)
                parts.append("Persistent notes to remember:\n" + notes)
            system_content = "\n\n".join(parts)
    else:
        files = conn.execute(
            "SELECT filename, content FROM conversation_files WHERE conversation_id=? ORDER BY added_at",
            (conv["id"],),
        ).fetchall()
        if files:
            file_blocks = "\n\n".join(f"### {f['filename']}\n{f['content']}" for f in files)
            system_content = system_content + "\n\nReference files:\n" + file_blocks
    if conv["compacted_summary"]:
        system_content += (
            "\n\n--- Summary of earlier conversation (older turns were compacted to save "
            "context space; treat this as ground truth for what was discussed/decided "
            "before) ---\n" + conv["compacted_summary"]
        )
    return system_content


def build_chat_messages_payload(conn, conv, current_message_id=None, exclude_message_id=None):
    """Builds {role, content} dicts for every message in a conversation, from the
    earliest not yet folded into conv['compacted_summary'] onward (see build_system_prompt).

    The message matching current_message_id (if any) has its attached images
    resolved into real multimodal content parts; every other message with
    images gets a text placeholder instead, since re-sending every past
    image on every subsequent turn would compound processing time (llama.cpp
    multimodal benchmarks show ~15s per image regardless of size).

    exclude_message_id omits one message from the payload without deleting it -
    used by the regenerate path, where the reply being replaced must stay in
    the DB (it becomes an archived version) but obviously must not be shown to
    the model as an already-given answer to the very prompt it's re-answering.
    """
    rows = conn.execute(
        "SELECT id, role, content FROM messages WHERE conversation_id=? AND id > ? ORDER BY id",
        (conv["id"], conv["compacted_through_id"] or 0),
    ).fetchall()
    if exclude_message_id is not None:
        rows = [m for m in rows if m["id"] != exclude_message_id]
    msg_ids = [m["id"] for m in rows]
    images_by_message = {}
    if msg_ids:
        placeholders = ",".join("?" * len(msg_ids))
        # version_id IS NULL - only the images belonging to whichever version
        # of a regenerated turn is currently active (see message_versions).
        img_rows = conn.execute(
            f"SELECT message_id, filename, mime_type, data FROM message_images "
            f"WHERE message_id IN ({placeholders}) AND version_id IS NULL ORDER BY id",
            msg_ids,
        ).fetchall()
        for r in img_rows:
            images_by_message.setdefault(r["message_id"], []).append(r)
    result = []
    for m in rows:
        images = images_by_message.get(m["id"], [])
        if not images:
            result.append({"role": m["role"], "content": m["content"]})
            continue
        if current_message_id is not None and m["id"] == current_message_id:
            parts = []
            if m["content"]:
                parts.append({"type": "text", "text": m["content"]})
            for img in images:
                b64 = base64.b64encode(img["data"]).decode("ascii")
                parts.append({"type": "image_url", "image_url": {"url": f"data:{img['mime_type']};base64,{b64}"}})
            result.append({"role": m["role"], "content": parts})
        else:
            names = ", ".join(img["filename"] for img in images)
            # Deliberately framed as an out-of-band system note, not natural
            # sentence phrasing tacked onto the message - confirmed live
            # (2026-08-03) that the old "{content}\n\n[Image(s) attached: X]"
            # format was convincing enough for the chat model to mimic
            # verbatim on a later unrelated turn (a follow-up that didn't
            # trigger a real generate/edit call got a hallucinated reply in
            # this exact shape, with no image actually created) - it read
            # too much like something the assistant itself would say to
            # confirm success. "[SYSTEM: ...]" reads as plainly not the
            # assistant's own voice, which a model has much less reason to
            # reproduce as its own turn.
            note = f"[SYSTEM: this turn included image attachment(s) not shown as text here - {names}]"
            placeholder = f"{m['content']}\n\n{note}" if m["content"] else note
            result.append({"role": m["role"], "content": placeholder})
    return result


def message_version_state(conn, message_id):
    """(version_pos, version_count) for one message - the count being the live
    `messages` row plus each of its archived variants. (None, None) if the row
    is gone."""
    row = conn.execute("SELECT version_pos FROM messages WHERE id=?", (message_id,)).fetchone()
    if row is None:
        return None, None
    archived = conn.execute(
        "SELECT COUNT(*) AS n FROM message_versions WHERE message_id=?", (message_id,)
    ).fetchone()["n"]
    return row["version_pos"], archived + 1


def archive_and_replace_version(conn, message_id, content, reasoning):
    """Files message_id's current content/reasoning (plus whatever images are
    attached to it) away into message_versions, then writes `content` into that
    same row as a new version appended to the end of the carousel.

    Returns (message_id, version_pos, version_count), or (None, None, None) if
    the row no longer exists. Everything happens on one connection/transaction,
    so no reader can catch the moment where the old text has been archived but
    the new text isn't in place yet - which would briefly show the same reply
    twice in the carousel.
    """
    row = conn.execute(
        "SELECT content, reasoning, version_pos FROM messages WHERE id=?", (message_id,)
    ).fetchone()
    if row is None:
        return None, None, None
    archived_positions = [
        r["position"] for r in conn.execute(
            "SELECT position FROM message_versions WHERE message_id=?", (message_id,)
        ).fetchall()
    ]
    # Slots 1..N are always exactly the archived positions plus the live row's,
    # so the newcomer lands at N+1. Derived from the positions themselves
    # rather than from a row count or insertion order, both of which stop
    # tracking reality once switch_message_version() starts trading slots.
    next_pos = max(archived_positions + [row["version_pos"]]) + 1
    cur = conn.execute(
        "INSERT INTO message_versions (message_id, position, content, reasoning) VALUES (?, ?, ?, ?)",
        (message_id, row["version_pos"], row["content"], row["reasoning"]),
    )
    conn.execute(
        "UPDATE message_images SET version_id=? WHERE message_id=? AND version_id IS NULL",
        (cur.lastrowid, message_id),
    )
    # The reply's document cards (create_document) are filed away with it, like its images.
    conn.execute(
        "UPDATE documents SET version_id=? WHERE message_id=? AND version_id IS NULL",
        (cur.lastrowid, message_id),
    )
    conn.execute(
        "UPDATE messages SET content=?, reasoning=?, version_pos=? WHERE id=?",
        (content, reasoning, next_pos, message_id),
    )
    return message_id, next_pos, len(archived_positions) + 2


def switch_message_version(conn, message_id, position):
    """Makes archived version `position` the live one: the two simply trade
    places, so the carousel keeps exactly one copy of each variant and the
    `messages` row always holds whichever is on screen. Returns the new
    version_pos, or None if the message or that slot doesn't exist.
    """
    row = conn.execute(
        "SELECT content, reasoning, version_pos FROM messages WHERE id=?", (message_id,)
    ).fetchone()
    if row is None:
        return None
    if position == row["version_pos"]:
        return position  # already the live one - clicking through to it again is a no-op
    target = conn.execute(
        "SELECT id, content, reasoning FROM message_versions WHERE message_id=? AND position=?",
        (message_id, position),
    ).fetchone()
    if target is None:
        return None
    # Both image sets are resolved to explicit ids BEFORE anything is written.
    # Doing the re-pointing as two UPDATE..WHERE statements instead would be
    # wrong in either order: whichever ran second would also sweep up the rows
    # the first one had just moved into its own WHERE clause.
    outgoing_images = [
        r["id"] for r in conn.execute(
            "SELECT id FROM message_images WHERE message_id=? AND version_id IS NULL", (message_id,)
        ).fetchall()
    ]
    incoming_images = [
        r["id"] for r in conn.execute(
            "SELECT id FROM message_images WHERE message_id=? AND version_id=?",
            (message_id, target["id"]),
        ).fetchall()
    ]
    conn.execute(
        "UPDATE message_versions SET content=?, reasoning=?, position=? WHERE id=?",
        (row["content"], row["reasoning"], row["version_pos"], target["id"]),
    )
    conn.execute(
        "UPDATE messages SET content=?, reasoning=?, version_pos=? WHERE id=?",
        (target["content"], target["reasoning"], position, message_id),
    )
    for image_id in outgoing_images:
        conn.execute("UPDATE message_images SET version_id=? WHERE id=?", (target["id"], image_id))
    for image_id in incoming_images:
        conn.execute("UPDATE message_images SET version_id=NULL WHERE id=?", (image_id,))
    # Document cards ride the carousel the same way as images (resolved to ids first, for the same reason).
    outgoing_docs = [
        r["id"] for r in conn.execute(
            "SELECT id FROM documents WHERE message_id=? AND version_id IS NULL", (message_id,)
        ).fetchall()
    ]
    incoming_docs = [
        r["id"] for r in conn.execute(
            "SELECT id FROM documents WHERE message_id=? AND version_id=?", (message_id, target["id"])
        ).fetchall()
    ]
    for doc_id in outgoing_docs:
        conn.execute("UPDATE documents SET version_id=? WHERE id=?", (target["id"], doc_id))
    for doc_id in incoming_docs:
        conn.execute("UPDATE documents SET version_id=NULL WHERE id=?", (doc_id,))
    return position


def build_message_export(conv, message, fmt):
    if fmt == "json":
        content = json.dumps(
            {
                "conversation_title": conv["title"],
                "role": message["role"],
                "content": message["content"],
                "created_at": message["created_at"],
            },
            indent=2,
        )
    else:
        content = message["content"]
    return content, EXPORT_MIME[fmt]


class ExtractionError(Exception):
    pass


# Limits on what an uploaded document may cost to read. The upload size limit
# (max_upload_mb) applies to the COMPRESSED file, and a .docx is a zip: a 0.3 MB
# one whose document.xml expands to 100 MB took 5.7 s to parse, holding the GIL
# the whole time - in this process, which also runs the window. A PDF is bounded
# by pypdf's own 75 MB-per-stream decompression cap, but not in page count.
# None of these is near what a real document needs (a 500-page novel is ~3 MB of
# docx XML and ~1.5 MB of text): they only stop a crafted file.
MAX_DOCX_XML_BYTES = 32 * 1024 * 1024
MAX_PDF_PAGES = 5000
MAX_PDF_TEXT_CHARS = 4_000_000
PDF_EXTRACT_SECONDS = 60


def extract_pdf_text(data):
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                raise ExtractionError("this PDF is password-protected and can't be read")
        started = time.monotonic()
        pages, total = [], 0
        for index, page in enumerate(reader.pages):
            if index >= MAX_PDF_PAGES:
                raise ExtractionError(f"this PDF has more than {MAX_PDF_PAGES:,} pages - too long to read")
            if time.monotonic() - started > PDF_EXTRACT_SECONDS:
                raise ExtractionError("reading this PDF is taking too long - it may be damaged or unusually complex")
            text = page.extract_text() or ""
            total += len(text)
            if total > MAX_PDF_TEXT_CHARS:
                raise ExtractionError("this PDF contains too much text to read")
            pages.append(text)
    except ExtractionError:
        raise
    except Exception as e:
        raise ExtractionError(f"could not read this PDF ({e})")
    return "\n\n".join(pages).strip()


DOCX_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def extract_docx_text(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            size = z.getinfo("word/document.xml").file_size
            if size > MAX_DOCX_XML_BYTES:
                raise ExtractionError(
                    f"this Word file is too large to read (its text part is {size // (1024 * 1024)} MB "
                    f"uncompressed; the limit is {MAX_DOCX_XML_BYTES // (1024 * 1024)} MB)"
                )
            xml_bytes = z.read("word/document.xml")
    except ExtractionError:
        raise
    except (zipfile.BadZipFile, KeyError):
        raise ExtractionError("could not read this Word file (only modern .docx is supported, not .doc)")
    try:
        root = ET.fromstring(xml_bytes)
    except (ET.ParseError, RecursionError):
        raise ExtractionError("could not parse this Word document's contents")
    paragraphs = []
    for p in root.iter(DOCX_W_NS + "p"):
        texts = [t.text or "" for t in p.iter(DOCX_W_NS + "t")]
        paragraphs.append("".join(texts))
    return "\n".join(paragraphs).strip()


def extract_upload_text(filename, raw):
    ext = os.path.splitext(filename)[1].lower()
    try:
        if ext == ".pdf":
            return extract_pdf_text(raw)
        if ext == ".docx":
            return extract_docx_text(raw)
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ExtractionError(
            "only plain-text, .pdf, or .docx files are supported (not .doc/.rtf or other binary formats)"
        )


def normalize_extracted_text(content):
    """Trim trailing whitespace per line and collapse runs of blank lines to one.
    PDF/DOCX extraction routinely leaves long runs of blank lines and trailing
    spaces that cost real tokens without carrying any information; this is a
    lossless cleanup that never touches indentation or non-blank content."""
    lines = []
    blank_run = 0
    for line in content.split("\n"):
        line = line.rstrip()
        if line == "":
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0
        lines.append(line)
    return "\n".join(lines).strip()


def model_fit(model_path, vram):
    """A3: does the model file fit in the memory this PC can give it - the RAM free now plus the VRAM free at launch?
    {"too_large", "model_gb", "memory_gb", "ram_gb", "vram_gb"} (whole GB with one decimal), or None when neither
    reading is available. Shown by Home and Settings > Advanced; before, the only word of it was a NOTE in the log."""
    try:
        model_mb = os.path.getsize(model_path) / (1024 * 1024)
    except OSError:
        return None
    ram_mb = available_ram_mb()
    vram_mb = vram.get("free_mb") if isinstance(vram, dict) else None
    if ram_mb is None and vram_mb is None:
        return None
    memory_mb = (ram_mb or 0) + (vram_mb or 0)
    return {"too_large": model_mb > memory_mb, "model_gb": round(model_mb / 1024, 1), "memory_gb": round(memory_mb / 1024, 1),
            "ram_gb": round((ram_mb or 0) / 1024, 1), "vram_gb": round((vram_mb or 0) / 1024, 1)}


def start_llama():
    """Returns the actual gpu_layers value it launched with (which may
    differ from whatever the caller's own `settings` dict/HTTP response
    already committed to, if safe_gpu_layers had to clamp it here) - every
    caller that reports settings back to a client must use this return
    value rather than assume its own pre-restart snapshot still matches,
    or the client ends up caching a value that was never actually true.
    Returns None if no model could be found (nothing started), or the string
    "auto" when offload sizing was handed to llama.cpp's --fit and there is
    therefore no number this process could report honestly."""
    global llama_proc, llama_start_error, llama_launch_vram, llama_launch_plan, llama_launch_free, llama_log_pump
    global llama_launch_fit, _health_fail_times, _health_ok_seen
    llama_start_error = None
    llama_launch_fit = None
    _health_fail_times, _health_ok_seen = [], False        # A1: a new launch starts its health history afresh
    model = find_model()
    if model is None:
        llama_start_error = ("no .gguf model found in the models folder. Add one in Settings > Model, or copy a .gguf file into the "
                             "models folder next to this app, then restart the app.")
        print("ERROR:", llama_start_error)
        return None
    # Before anything is measured or planned: a leftover server holds GPU memory that would make the free-memory readings wrong, and its
    # port would stop the new one from starting (see free_model_server_port).
    port_problem = free_model_server_port()
    if port_problem:
        llama_start_error = port_problem
        llama_proc = None
        print("ERROR:", llama_start_error)
        return None
    settings = load_settings()
    context_size = settings.get("context_size", 16384)
    kv_cache_type = settings.get("kv_cache_type", "q8_0")
    # int(): the desktop's GPU-layers control is a ttk.Scale (a DoubleVar), and
    # older builds saved its raw float, so existing settings.json files can hold
    # e.g. 22.0 - or a genuinely fractional 22.856. safe_gpu_layers coerces
    # internally either way, but without this the *log line* below reports a
    # fractional layer count, which is meaningless and reads as a bug.
    requested_layers = int(settings.get("gpu_layers", 99))
    kv_offload = settings.get("kv_offload", True)
    parallel = max_parallel_chats(settings)
    # llama-server's -c is its TOTAL context budget, divided evenly across
    # --parallel slots - to give every chat the full user-configured
    # context_size regardless of how many slots exist, -c must be that
    # total, not the per-chat figure. This is why VRAM clamping below is
    # against total_context_size, not context_size: the real KV cache cost
    # (and thus what can safely fit) scales with the actual -c value.
    total_context_size = context_size * parallel
    # Which engine build and which GPUs, and in what proportions (gpu_plan.py). Decided here, before anything is spawned, because
    # the free memory it plans against - and llama_launch_vram below - must be a reading taken while everything in use is still
    # someone else's. On a one-GPU machine this adds nothing to the command; a failure anywhere in it gives the plain old launch.
    mmproj = find_mmproj()
    gpu_exe, gpu_args, gpu_plan_result, gpu_inv = prepare_gpu_launch(settings, model, total_context_size, kv_cache_type, mmproj)
    llama_launch_free = {d["key"]: d["free_mb"] for d in gpu_inv["devices"]} if gpu_inv else {}
    if gpu_plan_result is not None:
        llama_launch_plan = dict(gpu_plan_result, engine=(gpu_inv["engine"]["name"] if gpu_inv and gpu_inv["engine"] else None), args=list(gpu_args))
        print(f"GPU placement: {gpu_plan_result['reason']}" + (f" Engine build: {llama_launch_plan['engine']}." if llama_launch_plan["engine"] not in (None, "default") else ""))
        for d in (gpu_inv["devices"] if gpu_inv else []):
            if not d["supported"]:
                print(f"NOTE: {d['label']} ({d['name']}) is not used - {d['reason']}.")
    else:
        llama_launch_plan = None
        if gpu_inv is not None and not gpu_inv["devices"]:
            print("NOTE: the engine reported no GPU (or its device list could not be read), so no GPU placement is applied - "
                  "the model starts the way the engine would on its own.")
    for e in (gpu_inv["engines"] if gpu_inv else []):
        sees = gpu_inv.get("sees", {}).get(e["name"], -1)
        if e["name"] != "default" and sees == 0:
            print(f"NOTE: engine build '{e['name']}' starts but lists no GPU, so it is not used - its CUDA runtime DLLs (cudart, cublas) may be missing from its folder.")
        elif e["name"] != "default" and sees is None:
            print(f"NOTE: engine build '{e['name']}' could not be started, so it is not used.")
    # A GPU this build has no native code for runs through the driver's JIT: the first load compiles the kernels and the driver keeps the
    # result. Measured (an RTX 5070 under the CUDA 12 build, which has only portable code for it): one to two minutes extra on the first
    # answer, then ordinary 7-23 s loads, the compiled kernels taking 65-116 MiB - so the driver's default 256 MiB cache suffices. The model
    # server alone is still given the most the driver takes (4 GiB), and only when a GPU in the plan needs it, as a precaution: a cache
    # that filled up would drop kernels silently, and the compile would come back on every load.
    launch_env = None
    planned_ids = set(gpu_plan_result["device_ids"]) if gpu_plan_result else set()
    jit_gpus = [d for d in (gpu_inv["devices"] if gpu_inv else []) if d["id"] in planned_ids and d.get("jit")]
    if jit_gpus:
        print("NOTE: " + ", ".join(f"{d['label']} ({d['name']})" for d in jit_gpus) + " has no native code in this engine build: the first "
              "load compiles its kernels (this can take a few minutes) and the driver keeps the result for later loads.")
        if "CUDA_CACHE_MAXSIZE" not in os.environ:
            launch_env = dict(os.environ, CUDA_CACHE_MAXSIZE=str(4 * 1024 ** 3))
    # Snapshot free VRAM *before* spawning, while everything in use is still
    # someone else's - see llama_launch_vram. Whatever is free at this instant
    # is the entire budget both safe_gpu_layers and llama.cpp's own --fit get
    # to work with, so it's also the honest explanation for a small offload.
    # Summed over the GPUs the plan uses (the engine's own figures, which also
    # works for a GPU nvidia-smi knows nothing about); the driver's figure when
    # there is no plan.
    llama_launch_vram = vram_for_plan(gpu_inv, gpu_plan_result) or query_vram_mb()
    llama_launch_fit = model_fit(model, llama_launch_vram)
    if llama_launch_vram is not None:
        foreign_mb = llama_launch_vram["total_mb"] - llama_launch_vram["free_mb"]
        llama_launch_vram["foreign_used_mb"] = foreign_mb
        model_mb = os.path.getsize(model) / (1024 * 1024)
        if foreign_mb >= FOREIGN_VRAM_WARN_MB:
            print(
                f"WARNING: {foreign_mb:,.0f} MB of your {llama_launch_vram['total_mb']:,} MB of GPU memory is "
                f"already in use by other programs - only {llama_launch_vram['free_mb']:,} MB is free "
                f"for {os.path.basename(model)}. Expect fewer layers on the GPU and slower replies; "
                f"close the other GPU program and restart the model to get that memory back."
            )
        if model_mb > llama_launch_vram["total_mb"]:
            # Not a contention problem and no setting can fix it - the weights
            # alone are bigger than all the GPU memory in use, so some layers always run on
            # the CPU. Said plainly here because the symptom (slow replies) is
            # identical to the contention case above and invites the same
            # fruitless round of settings-tweaking.
            print(
                f"NOTE: {os.path.basename(model)} is {model_mb:,.0f} MB but the GPU memory in use is only "
                f"{llama_launch_vram['total_mb']:,} MB in total, so it can never fit entirely on the "
                f"GPU - the remainder runs on the CPU at any setting. A smaller quantization is the "
                f"only way to change that."
            )
    auto_layers = settings.get("gpu_layers_auto", True)
    if auto_layers:
        # Leaving -ngl off is what arms llama.cpp's --fit: it only adjusts
        # arguments that weren't set explicitly. Everything else below stays
        # pinned, so --fit has exactly one free variable and can't quietly
        # shrink the user's context or batch size to make something fit.
        gpu_layers = "auto"
    else:
        gpu_layers = safe_gpu_layers(
            model, requested_layers, total_context_size, kv_cache_type, kv_offload, vram=llama_launch_vram
        )
        if gpu_layers != requested_layers:
            print(
                f"gpu_layers clamped from {requested_layers} to {gpu_layers} to fit free VRAM "
                f"for {os.path.basename(model)} (see safe_gpu_layers)"
            )
            settings["gpu_layers"] = gpu_layers
            save_settings(settings)
    # A DLL the engine needs but cannot find stops it from starting at all: the process dies at once (exit code 0xC0000135) without a word
    # in its log, nothing ever listens on its port, and the first anyone knew was a chat failing with "WinError 10061 - connection
    # refused". The engine's folder is checked first, so the reason is stated instead.
    missing_now = describe_missing_dlls(pe_imports.missing_dependencies(gpu_exe))
    if missing_now:
        llama_start_error = f"{os.path.basename(gpu_exe)} cannot start: {missing_now[0]}. {missing_now[1]}"
        llama_proc = None
        print("ERROR:", llama_start_error)
        return None
    backend_gap = describe_missing_dlls(
        pe_imports.missing_dependencies(os.path.join(os.path.dirname(gpu_exe), "ggml-cuda.dll")), subject="the GPU support of this engine build cannot load")
    if backend_gap:
        print(f"NOTE: {backend_gap[0]}, so the model would run on the CPU, slowly. {backend_gap[1]}")
    cmd = [
        gpu_exe,
        "-m", model,
        "--host", "127.0.0.1",
        "--port", str(LLAMA_PORT),
        "--api-key", LLAMA_API_KEY,         # per-launch key: only this app can talk to it (see llama_headers)
        "-c", str(total_context_size),
        "--parallel", str(parallel),
        "--jinja",
        "--no-webui",
        "--flash-attn", "on",
    ]
    if not auto_layers:
        cmd += ["-ngl", str(gpu_layers)]
    # The extra arguments go first, where the planning tools got them: llama.cpp checks --device against the devices it knows WHILE
    # it parses the command line, and --rpc is what adds remote ones - "--device RPC0 ... --rpc host:port" is "invalid device: RPC0".
    cmd += LLAMA_EXTRA_ARGS
    # Which GPUs, and the split between them, from the plan above (nothing at all on a one-GPU machine). With the layer count left
    # to --fit (Auto) it still adjusts that to what really fits; the split itself is ours.
    cmd += gpu_args
    # Launch-only: a per-request "reasoning_budget" field is accepted and then
    # silently ignored by this build, so this is the only place it can be set.
    # That's also why changing it (or, with thinking_budget_auto on, changing
    # the response length it's derived from) counts as a restart-triggering
    # setting in the /api/settings handler.
    budget = thinking_budget_tokens(settings, model)
    if budget is not None:
        cmd += ["--reasoning-budget", str(budget)]
    if not settings.get("kv_offload", True):
        cmd.append("--no-kv-offload")
    if kv_cache_type in KV_CACHE_TYPES and kv_cache_type != "f16":
        # Quantizing the KV cache (vs. the f16 default) roughly halves its VRAM
        # footprint for ~lossless quality at q8_0 — the single biggest lever for
        # fitting more context (or more model) in a fixed VRAM budget. Requires
        # flash attention, which is pinned on above.
        cmd += ["--cache-type-k", kv_cache_type, "--cache-type-v", kv_cache_type]
    # -b (logical batch) governs how many tokens get queued for prompt
    # processing; -ub (physical/micro batch, must be <= -b) is what actually
    # sizes the GPU compute buffer, so it's the more direct VRAM lever - but
    # exposing two raw llama.cpp terms to the settings UI is more knobs than
    # the single "prompt batch size" tradeoff is worth. Deriving -ub from the
    # same value (capped at llama.cpp's own 512 default) keeps one slider
    # meaningful in both directions: raising it past 512 only grows the
    # logical queue (more throughput, -ub stays capped), lowering it below
    # 512 shrinks both together (less peak VRAM during prompt eval).
    batch_size = int(settings.get("batch_size", 2048))
    cmd += ["-b", str(batch_size), "-ub", str(min(batch_size, 512))]
    load_into_ram, load_note = resolve_model_load(model, settings)
    if load_note:
        print(load_note)
    llama_launch_load["into_ram"] = load_into_ram
    if load_into_ram:
        # Reads the whole model into a regular RAM allocation up front instead
        # of memory-mapping it - no per-layer disk I/O during inference (at the
        # cost of committing the model's full size in RAM immediately; mmap'd
        # pages can otherwise be evicted/paged by the OS under memory
        # pressure), and, the reason Automatic picks it on a hard disk, an
        # ordinary sequential read at load time instead of the thousands of
        # simultaneous paging reads mmap + prefetch produces. See
        # resolve_model_load().
        cmd.append("--no-mmap")
    if settings.get("mlock_model", False):
        # Pins whatever's currently resident (mmap'd or fully loaded) in
        # physical RAM so the OS can never swap it out - trades a hard RAM
        # commitment for consistent performance under system memory pressure.
        cmd.append("--mlock")
    if mmproj:
        cmd += ["--mmproj", mmproj]
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW
    os.makedirs(DATA_DIR, exist_ok=True)
    # The server's output comes through this process into data\llama-server.log, kept under LLAMA_LOG_MAX_BYTES (pump_process_log).
    # Handed the file to write itself, as it used to be, a server that floods its log (one run wrote 1.3 GB) fills the disk, and a
    # file the child holds open cannot be cut short from here. The previous run's log is kept as llama-server.log.1: after a crash
    # the start that follows would otherwise have erased the only record of it.
    rotate_log(LLAMA_LOG_PATH)
    llama_proc = subprocess.Popen(cmd, creationflags=flags, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **({"env": launch_env} if launch_env else {}))
    llama_log_pump = pump_process_log(llama_proc, LLAMA_LOG_PATH, LLAMA_LOG_MAX_BYTES)
    print(f"llama-server started (pid {llama_proc.pid}) with model {os.path.basename(model)}")
    return gpu_layers


llama_log_pump = None          # the thread copying the running model server's output into its log (None with no server, or in tests whose stand-in has no pipe)


def rotate_log(path, limit=0):
    """Moves `path` aside as `path`.1 (replacing an older .1) when it holds more than `limit` bytes - with the default 0, whenever it
    holds anything. True when it was moved. Never raises: a log another program has open (a reader of its tail) stays where it is and
    is tried again at the next opportunity."""
    try:
        if os.path.getsize(path) <= limit:
            return False
        os.replace(path, path + ".1")
        return True
    except OSError:
        return False


def pump_process_log(proc, path, limit):
    """Copies a child's stdout (a pipe) into the file `path` from a daemon thread, flushing each line so a reader of the file's tail
    (the load overlay) sees it at once, and keeps the file under `limit` bytes: once a line would take it past, the file becomes
    `path`.1 (the older .1 is dropped) and a fresh one is begun - so the log holds the newest `limit` bytes plus up to `limit` before
    them, whatever the server prints. The pipe is drained whatever happens to the file (a pipe nobody reads blocks the child at a few
    KB): a log that cannot be written is not written. Returns the thread, or None when the child has no pipe (a stand-in in tests)."""
    stream = getattr(proc, "stdout", None)
    if stream is None:
        return None

    def pump():
        out, size = None, 0
        try:
            out = open(path, "ab")
            size = out.tell()
        except OSError:
            out = None
        try:
            for chunk in iter(lambda: stream.readline(65536), b""):     # a line, or 64 KB of one that never ends
                if out is None:
                    continue
                try:
                    if size + len(chunk) > limit and size > 0:
                        out.close()
                        out = None
                        rotate_log(path)                                # a failed move (the file is open elsewhere) just delays it
                        out = open(path, "ab")
                        size = out.tell()
                    out.write(chunk)
                    out.flush()
                    size += len(chunk)
                except OSError:
                    out = None
        finally:
            for f in (stream, out):
                try:
                    if f is not None:
                        f.close()
                except Exception:
                    pass

    thread = threading.Thread(target=pump, name="llama-log", daemon=True)
    try:
        thread.start()
    except Exception as e:                   # no thread to read the pipe: the child must not be left to block on it
        print(f"WARNING: the model server's log is not being written ({e})")
        try:
            stream.close()
        except Exception:
            pass
        return None
    return thread


def stop_llama():
    global llama_proc
    # The model is going away (switch, settings restart, image job, shutdown):
    # a title request waiting on it would only fail noisily when it dies.
    cancel_pending_titles()
    proc = llama_proc
    llama_proc = None
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            # kill() (SIGKILL/TerminateProcess) doesn't itself block until
            # the OS has actually finished tearing the process down - without
            # waiting here, start_llama() right after this call could spawn
            # the replacement process before the old one's VRAM has been
            # reclaimed. A plain wait() (no timeout) is safe: a killed
            # process can't ignore SIGKILL the way it could ignore
            # terminate(), so this isn't waiting on anything that can hang
            # the way the graceful path could.
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                print(f"WARNING: llama-server pid {proc.pid} did not exit even after kill()")


def read_llama_crash_tail(max_chars=2000):
    """Best-effort tail of the last llama-server run's output, shown when the
    process has exited unexpectedly (e.g. an incompatible model/mmproj pairing)."""
    try:
        with open(LLAMA_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = [l for l in f.readlines() if l.strip()]
    except OSError:
        return "llama-server exited unexpectedly (no log available)"
    tail = "".join(lines[-15:]).strip()
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail or "llama-server exited unexpectedly (empty log)"


# What a model server that died (or never got going) usually means, read from the tail of its own log: (pattern, what happened, what to do).
# First match wins, most specific first.
LLAMA_FAILURE_CAUSES = (
    (r"no kernel image is available|invalid device function|unsupported gpu architecture",
     "a GPU in this computer is too old for this engine build (it has no code for that GPU)",
     "In Settings > Advanced > GPUs look for a GPU marked 'not used', and put a llama.cpp build that supports it under bin\\engines\\ "
     "(the CUDA 12 build covers Pascal cards such as the Tesla P40) - or switch that GPU off."),
    (r"CUDA driver version is insufficient|failed to initialize CUDA|cudaGetDeviceCount|no CUDA-capable device|CUDA driver .{0,40}too old",
     "the NVIDIA driver is too old for this engine build, or it sees no CUDA GPU",
     "Update the NVIDIA driver, or put a CUDA 12 build of llama.cpp under bin\\engines\\ (it works with older drivers)."),
    (r"out of memory|cudaMalloc failed|failed to allocate .{0,40}(CUDA|buffer)|unable to allocate",
     "the GPU ran out of memory",
     "Lower the context size or the number of parallel chats in Settings, or close other programs that use the GPU, then restart the model."),
    (r"couldn't bind HTTP server socket|address already in use|Only one usage of each socket address",
     "another program is already using the model server's port (8791)",
     "An earlier copy of the model server may still be running: look for llama-server.exe in Task Manager (or run Stop Chatbot.bat)."),
    (r"unknown model architecture|failed to load model|error loading model|llama_model_load: error|invalid magic",
     "the engine could not read this model",
     "The file may be incomplete or damaged, or too new for this engine build."),
)
# Windows exit codes of a process that died at once: code -> (what happened, what to do)
LLAMA_EXIT_CODES = {
    0xC0000135: ("a DLL the model server needs was not found",
                 "A llama.cpp build placed under bin\\engines\\ needs its CUDA runtime DLLs (cudart, cublas) in the same folder as llama-server.exe."),
    0xC000007B: ("a DLL the model server needs is the wrong kind (32/64-bit) or damaged", "Re-copy the engine folder."),
    0xC000001D: ("the CPU lacks an instruction set this build needs", "Use a llama.cpp build made for older CPUs."),
    0xC0000005: ("the model server crashed (memory access violation)", "See the end of data\\llama-server.log."),
    0xC0000409: ("the model server stopped on an internal error", "See the end of data\\llama-server.log."),
}


def describe_missing_dlls(missing, subject="the model server cannot start", program="llama-server.exe"):
    """(what happened, what to do) for {dll name: importer} from pe_imports.missing_dependencies, or None when nothing is missing.
    `subject`: what the missing DLLs stop from working ("the GPU support of this engine build cannot load"); `program`: the executable
    whose folder they belong in."""
    if not missing:
        return None
    names = ", ".join(sorted(missing, key=str.lower))
    if all(pe_imports.is_vc_runtime(n) for n in missing):
        return (f"the Microsoft Visual C++ runtime is not installed on this PC (missing: {names})",
                "Install the 'Microsoft Visual C++ Redistributable 2015-2022 (x64)' from Microsoft (https://aka.ms/vs/17/release/vc_redist.x64.exe), "
                f"or copy those DLLs (from C:\\Windows\\System32 on a PC that has the runtime) into the folder of {program}.")
    where = "a llama.cpp build under bin\\engines\\ needs its CUDA runtime DLLs there; " if program == "llama-server.exe" else ""
    return (f"{subject} because these DLLs are missing: {names}",
            f"Put them in the same folder as {program} ({where}nvcuda.dll comes with the NVIDIA driver).")


def diagnose_llama_failure(log_tail, returncode=None, exe=None):
    """(what happened, what to do) for a model server that died or never got going, from the end of its log, its exit code and - when it
    died at once with nothing in the log - the DLLs its executable needs; or None when none of them says anything useful. The log is
    trusted first: it names the real cause, where the exit code only says that it died."""
    text = log_tail if isinstance(log_tail, str) else ""
    for pattern, what, todo in LLAMA_FAILURE_CAUSES:
        if re.search(pattern, text, re.IGNORECASE):
            return what, todo
    code = (returncode & 0xFFFFFFFF) if isinstance(returncode, int) else None
    if exe and (code == 0xC0000135 or (code is not None and not text.strip())):
        found = describe_missing_dlls(pe_imports.missing_dependencies(exe))
        if found:
            return found
    if code is not None:
        known = LLAMA_EXIT_CODES.get(code)
        if known:
            return known
    return None


def llama_exe_of(proc):
    """The executable a Popen was started with, or None."""
    args = getattr(proc, "args", None)
    if isinstance(args, (list, tuple)) and args:
        return str(args[0])
    return args if isinstance(args, str) else None


def is_connection_failure(exc):
    """True for "nothing is listening / the connection was dropped" - what a model server that is down, restarting or has just died looks
    like to a client (WinError 10061 refused, 10054 reset, 10053 aborted) - as opposed to an answer, however bad, or a slow server."""
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ConnectionError):
        return True
    return getattr(reason, "winerror", None) in (10061, 10054, 10053)


DOWN_MESSAGE_MAX = 500      # what the desktop shows of a backend error (api_client._parse_error caps it there)


def model_server_down_message(include_log=True):
    """Why the model server cannot be reached right now, in words: what a refused connection ("WinError 10061" - nothing is listening
    on its port) comes down to. Says whether it is still loading, was never started, or stopped - and if it stopped, what its own log
    and exit code say about why and what to do about it."""
    if llama_start_error is not None:
        return f"The model server did not start: {llama_start_error}"
    proc = llama_proc
    if proc is None:
        return "The model server is not running. Pick a model in Settings, or restart the app."
    code = proc.poll()
    if code is None:
        return "The model server is still starting (it is loading the model) - try again in a moment."
    tail = read_llama_crash_tail()
    exit_text = f" (exit code 0x{code & 0xFFFFFFFF:X})" if isinstance(code, int) and code else ""
    found = diagnose_llama_failure(tail, code, llama_exe_of(proc))
    message = f"The model server stopped running{exit_text}."
    if found:
        message += f" Most likely {found[0]}. {found[1]}"
    # The chat bubble shows at most DOWN_MESSAGE_MAX characters of an error (api_client._parse_error): the log excerpt
    # is whole lines from the end that fit after the actionable sentence, never a fragment cut mid-line (it used
    # to append 900 characters and the bubble cut them).
    below = " Its last output is below; data\\llama-server.log has the rest."
    excerpt = ""
    if include_log and tail:
        room = DOWN_MESSAGE_MAX - len(message) - (0 if found else len(below)) - 2
        kept = []
        for line in reversed(tail.splitlines()):
            if sum(len(k) + 1 for k in kept) + len(line) > room:
                break
            kept.insert(0, line)
        excerpt = "\n".join(kept).strip()
    if not found:
        message += below if excerpt else " See the end of data\\llama-server.log for why."
    if excerpt:
        message += "\n\n" + excerpt
    return message


def server_stopped_reason():
    """One or two sentences for "the model server stopped": what its log and exit code say it was and what to do about it, or where to
    look. Waits briefly for the process to be gone first - a dropped connection can be seen a moment before the exit itself."""
    proc = llama_proc
    if proc is not None:
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
    code = proc.poll() if proc is not None else None
    if proc is not None and code is None:
        return "The model server is still running, so the connection was lost or it stopped answering - see the end of data\\llama-server.log."
    found = diagnose_llama_failure(read_llama_crash_tail() if code is not None else "", code, llama_exe_of(proc))
    exit_text = f" (exit code 0x{code & 0xFFFFFFFF:X})" if isinstance(code, int) and code else ""
    if found:
        return f"The model server stopped{exit_text}. Most likely {found[0]}. {found[1]}"
    return f"The model server stopped{exit_text}. See the end of data\\llama-server.log for why."


def port_accepts_connections(port, timeout=0.5):
    """Is anything listening on this local port? Exists because on Windows a
    connect() to a CLOSED loopback port takes ~2 s to be refused (measured
    2.04 s; 4 s for "localhost", which tries IPv6 first) regardless of how
    short the caller's urlopen timeout is - urllib only applies it to the
    connect once the OS gives up. A raw connect with its own short timeout
    returns in `timeout` for a closed port and in a few ms for an open one."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=timeout).close()
        return True
    except OSError:
        return False


def listening_pid(port):
    """The id of the process listening on this local TCP port (from netstat), or None when nobody is or it cannot be told."""
    out = run_engine_tool(["netstat", "-ano", "-p", "TCP"], 10)
    for line in (out[1].splitlines() if out else ()):
        parts = line.split()
        # Proto, local address, foreign address, state, pid. A listener is the line whose foreign address is 0.0.0.0:0 - the state's own
        # name is not read (netstat prints it translated: ABHÖREN, À L'ÉCOUTE ... - and the pid is the last word, whatever the state's length).
        if (len(parts) >= 5 and parts[0] == "TCP" and parts[1].startswith(("127.0.0.1:", "0.0.0.0:", "[::1]:", "[::]:"))
                and parts[1].rsplit(":", 1)[-1] == str(port) and parts[2].rsplit(":", 1)[-1] == "0" and parts[-1].isdigit()):
            return int(parts[-1])
    return None


def process_image_path(pid):
    """The full path of the program a process is running, or None (Windows only; None when it cannot be asked)."""
    if os.name != "nt" or not pid:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32")          # its own instance: the argument types set below must not change anyone else's calls
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, int(pid))              # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            return buf.value if kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else None
        finally:
            kernel.CloseHandle(handle)
    except Exception:
        return None


LEFTOVER_STOP_WAIT = 10        # seconds a model server left over from an earlier run is given to let go of its port once it has been stopped


def free_model_server_port():
    """None when the model server may be started: its port is free, has just been freed, or whoever holds it cannot be told (the launch
    then goes ahead as it always did, and a refused bind shows in the server's own log, which diagnose_llama_failure reads). Otherwise a
    sentence saying why the model server cannot start.

    A program already listening on the port is, most often, a llama-server left behind by an earlier run of this chatbot - a crash, or a
    force-kill of the app that does not take its child with it - and it still holds the model's GPU memory. The new server could not bind
    the port and would exit at once, while the app went on talking to the old one (with whatever model, context and GPU split IT was
    started with). A llama-server running from THIS folder is that leftover and is stopped (what Stop Chatbot.bat does); any other
    program is named and left alone. This process itself (the test servers) is never in the way."""
    if not port_accepts_connections(LLAMA_PORT):
        return None
    try:
        pid = listening_pid(LLAMA_PORT)
        path = process_image_path(pid) if pid else None
    except Exception:
        return None             # a probe that goes wrong must never keep the model from starting
    if not pid or pid == os.getpid():
        return None
    # realpath on both: one folder is spelled several ways (an 8.3 short name in %TEMP%-style paths, a drive made with subst, a junction)
    here = os.path.normcase(os.path.realpath(ROOT)) + os.sep
    ours = bool(path) and os.path.basename(path).lower() == "llama-server.exe" and os.path.normcase(os.path.realpath(path)).startswith(here)
    if not ours:
        who = f"{os.path.basename(path)} (process {pid})" if path else f"a program (process {pid})"
        return (f"Port {LLAMA_PORT} is already in use by {who}, so the model server cannot start. Close that program, or - if it is a leftover "
                "of this chatbot - run Stop Chatbot.bat.")
    print(f"NOTE: a model server from an earlier run (process {pid}) was still running and holding port {LLAMA_PORT} and its GPU memory; stopping it.")
    run_engine_tool(["taskkill", "/F", "/PID", str(pid)], 15)
    deadline = time.time() + LEFTOVER_STOP_WAIT
    while time.time() < deadline:
        if not port_accepts_connections(LLAMA_PORT, timeout=0.3):
            return None
        time.sleep(0.3)
    return f"A model server left over from an earlier run (process {pid}) would not stop and still holds port {LLAMA_PORT}. Run Stop Chatbot.bat."


def llama_health():
    """{"status": ok | starting | hung | error, ...}. "error" carries "error" (why) and "reason": "start" (it never
    started: no model, a DLL, its port) or "stopped" (it ran and died). A1: "hung" = the process is alive and its port
    answers, but /health has not answered (timed out, reset) for HUNG_AFTER_S after at least one ok - before, that
    read "starting" for ever (Home: "Loading..."). A 503 from llama-server itself ("loading model") is "starting"."""
    global _health_ok_seen
    if llama_start_error is not None:
        return {"status": "error", "error": llama_start_error, "reason": "start"}
    if llama_proc is not None and llama_proc.poll() is not None:
        return {"status": "error", "error": model_server_down_message(), "reason": "stopped"}
    # llama-server only binds its port after it has loaded its own DLLs, which
    # on a cold hard disk (~700 MB of CUDA libraries) takes several seconds.
    # Asking the closed port directly cost this call - and every /api/health
    # request, and Home's 2 s poll behind it, which then reported the whole
    # backend as offline - a full 2 s per attempt for no information.
    if not port_accepts_connections(LLAMA_PORT):
        return {"status": "starting"}
    try:
        with urllib.request.urlopen(urllib.request.Request(LLAMA_URL + "/health", headers=llama_headers()), timeout=2) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError:
        return {"status": "starting"}            # 503 "loading model": up, and says so itself
    except Exception:
        now = time.time()
        _health_fail_times.append(now)
        del _health_fail_times[:-10]
        if _health_ok_seen and len(_health_fail_times) >= 3 and now - _health_fail_times[0] >= HUNG_AFTER_S:
            return {"status": "hung", "error": "The model server is running but has not answered for "
                                               f"{int(now - _health_fail_times[0])} s - it may be stuck. Press RESTART to start it again."}
        return {"status": "starting"}
    if isinstance(result, dict) and result.get("status") == "ok":
        _health_ok_seen = True
        _health_fail_times.clear()
    return result


def _restore_chat_model_now():
    """The actual (slow) swap-back work: tear down a resident image model and
    bring the chat model back up, waiting for it to be genuinely healthy this
    time - closes a pre-existing gap where start_llama() alone was always
    fire-and-forget with nothing confirming the new process was actually
    ready to serve requests. Callers own the image_gen_status/image_gen_lock
    bookkeeping around this; this function just does the swap. Returns an
    error string on failure, None on success."""
    global image_model_resident, sd_resident_model_path
    stop_sd_server()
    with llama_restart_lock:
        start_llama()
    # A cold multi-GB model on a hard disk legitimately takes minutes to load
    # (measured: ~245 s for 20 GB), so the old 120 s here failed healthy
    # reloads; a load that genuinely fails reports "error" and ends the wait at
    # once, so the long cap only ever applies to one that is making progress.
    deadline = time.time() + 15 * 60
    ready = False
    while time.time() < deadline:
        status = llama_health().get("status")
        if status == "ok":
            ready = True
            break
        if status == "error":
            break
        time.sleep(1)
    image_model_resident = False
    sd_resident_model_path = None
    return None if ready else "chat model did not become ready in time"


def ensure_chat_model_available():
    """Called from handle_chat()/handle_edit_message()/handle_compact() (and
    the idle-restore watcher) before they assume llama-server is up. If the
    image model is currently resident (left warm after a generate/edit for
    fast follow-up chaining - see image_model_resident), swaps the chat model
    back in synchronously, blocking the caller until it's confirmed ready.
    Reuses image_gen_status/image_gen_lock and the existing
    "restoring_chat_model" stage (generate_image_worker/edit_image_worker's
    own final step already used this same label) so every other place in
    this file that checks image_gen_status["active"] as a busy signal keeps
    working completely unmodified. Returns an error string on failure, None
    on success (including the common no-op case where nothing was resident)."""
    with image_gen_lock:
        if not image_model_resident:
            return None
        if image_gen_status["active"] or compact_active:
            return "busy — try again in a moment"
        image_gen_status["active"] = True
        image_gen_status["stage"] = "restoring_chat_model"
        image_gen_status["error"] = None
    try:
        return _restore_chat_model_now()
    finally:
        image_gen_status["active"] = False
        image_gen_status["stage"] = None


def count_tokens(text):
    """Exact token count from the currently-loaded model's own tokenizer (llama-server's
    /tokenize endpoint), not an estimate. Returns None if the model server isn't reachable."""
    if not text:
        return 0
    try:
        req = urllib.request.Request(
            LLAMA_URL + "/tokenize",
            data=json.dumps({"content": text}).encode(),
            headers=llama_headers({"Content-Type": "application/json"}),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        return len(data.get("tokens", []))
    except Exception:
        return None


def max_parallel_chats(settings=None):
    """How many chat replies llama-server generates at once (its --parallel
    slot count) - a real user setting, floored at 1. See start_llama() for
    how this multiplies -c rather than dividing a fixed context_size, so
    raising this costs proportionally more VRAM instead of shrinking every
    chat's budget (the earlier design this replaced did the latter, with no
    way to opt out even for someone who never runs two chats at once)."""
    settings = settings or load_settings()
    return max(1, int(settings.get("max_parallel_chats", 1)))


def effective_slot_context_tokens(settings=None):
    """Each chat slot gets the FULL configured context_size - start_llama()
    launches llama-server with -c set to context_size * max_parallel_chats
    precisely so this never has to divide. Every caller needing "what does
    ONE chat actually get" must go through this, not read context_size
    directly, in case that changes again later."""
    settings = settings or load_settings()
    return max(1, int(settings.get("context_size", 16384)))


def project_content_token_budget(settings=None):
    """Combined ceiling for a project's files+images+memory (all injected
    into every chat turn - see build_system_prompt): a fraction
    (project_content_budget_pct) of the real per-slot context. Self-
    corrects with context_size/model changes instead of drifting out of
    sync the way flat char/token ceilings would."""
    settings = settings or load_settings()
    pct = float(settings.get("project_content_budget_pct", 0.5))
    return max(1, int(effective_slot_context_tokens(settings) * pct))


def project_content_tokens_used(conn, project_id):
    """Cheap SQL aggregate over already-cached token columns (content_tokens
    / caption_tokens) - NOT a fresh count_tokens() HTTP call - safe to run
    on every chat/regenerate request as a generation-time safety net
    without adding latency."""
    row = conn.execute(
        "SELECT "
        "(SELECT COALESCE(SUM(content_tokens), 0) FROM project_files WHERE project_id=?) + "
        "(SELECT COALESCE(SUM(caption_tokens), 0) FROM project_images WHERE project_id=?) + "
        "(SELECT COALESCE(SUM(content_tokens), 0) FROM project_memory WHERE project_id=?) AS total",
        (project_id, project_id, project_id),
    ).fetchone()
    return row["total"]


def get_active_slot_progress(slot_id):
    """Best-effort peek at llama-server's own /slots endpoint (its native
    monitoring API, distinct from the OpenAI-compat chat endpoint) for one
    specific slot's mid prompt-eval progress. Confirmed live (2026-07-30)
    that n_prompt_tokens_processed climbs in real time while a slot digests a
    prompt. Now that up to max_parallel_chats() chats can generate at once
    (each pinned to its own slot via the id_slot request param - see
    _stream_and_save), slot_id disambiguates which one a given conversation's
    progress request is asking about. Returns None if llama-server isn't
    reachable or that slot isn't currently processing."""
    try:
        with urllib.request.urlopen(urllib.request.Request(LLAMA_URL + "/slots", headers=llama_headers()), timeout=2) as resp:
            slots = json.loads(resp.read().decode())
    except Exception:
        return None
    for slot in slots:
        if slot.get("id") == slot_id and slot.get("is_processing"):
            return slot.get("n_prompt_tokens_processed")
    return None


def image_model_kind(path):
    """(kind, reason) for an image model file: "text2image" (makes a picture from a prompt), "edit" (changes a picture; it can also make one from
    text, but that is not what it is for) or "unsupported" (this app cannot start it at all). Decided from the file name."""
    name = os.path.basename(path).lower()
    if "qwen" in name and "image" in name:
        return "unsupported", "it needs a separate language-model file and a VAE file that this app does not have (Qwen-Image)"
    if "kontext" in name or "edit" in name:
        return "edit", "it edits pictures (it can make one from text too, but slowly)"
    return "text2image", ""


def image_model_problem(path):
    """A sentence saying why this image model cannot be used, or None."""
    kind, reason = image_model_kind(path)
    if kind != "unsupported":
        return None
    return f"{os.path.basename(path)} cannot be used: {reason}. Choose another image model in Settings > Image model (stable-diffusion-v1-5 works)."


def find_image_model():
    if not os.path.isdir(SD_MODELS_DIR):
        return None
    candidates = [
        f for f in os.listdir(SD_MODELS_DIR) if f.lower().endswith((".gguf", ".safetensors", ".ckpt"))
    ]
    if not candidates:
        return None
    settings = load_settings()
    active = settings.get("active_image_model")
    if active and active in candidates:
        return os.path.join(SD_MODELS_DIR, active)
    # No (valid) active image model chosen. Never "the largest file": that was a 13 GB editing model this app cannot launch, which died at once with
    # no explanation. The smallest model that makes a picture from text comes first (SD 1.5-class), then an editing model; one that cannot be started at
    # all is never picked on its own.
    ranked = []
    for f in candidates:
        path = os.path.join(SD_MODELS_DIR, f)
        kind, _why = image_model_kind(path)
        if kind == "unsupported":
            continue
        ranked.append((0 if kind == "text2image" else 1, os.path.getsize(path), f))
    if not ranked:
        return None
    ranked.sort()
    return os.path.join(SD_MODELS_DIR, ranked[0][2])


def is_flux_model(path):
    """Flux-family models (incl. Kontext) need separate --clip_l/--t5xxl/--vae
    files instead of a single -m checkpoint. Detected via GGUF's own declared
    architecture, with a filename check as a cheap fallback."""
    arch = gguf_architecture(path)
    if arch:
        return "flux" in arch.lower()
    return "flux" in os.path.basename(path).lower()


def gguf_architecture(path):
    value = _gguf_cached_metadata(path).get("general.architecture")
    return value if isinstance(value, str) else None


def find_flux_components():
    """Locate the shared clip_l/t5xxl/vae files Flux-family models need, by
    filename convention inside models/image/flux-components/. Returns None if
    the folder is missing or any piece can't be found."""
    if not os.path.isdir(FLUX_COMPONENTS_DIR):
        return None
    files = os.listdir(FLUX_COMPONENTS_DIR)

    def first_match(*prefixes):
        for f in files:
            if any(f.lower().startswith(p) for p in prefixes):
                return os.path.join(FLUX_COMPONENTS_DIR, f)
        return None

    clip_l = first_match("clip_l")
    t5xxl = first_match("t5xxl")
    vae = first_match("ae.", "vae", "flux_vae", "flux-vae")
    if clip_l and t5xxl and vae:
        return {"clip_l": clip_l, "t5xxl": t5xxl, "vae": vae}
    return None


def start_sd_server(force_cpu=False):
    global sd_proc, sd_start_error
    sd_start_error = None
    model = find_image_model()
    if model is None:
        sd_start_error = f"no usable image model in {SD_MODELS_DIR} (put stable-diffusion-v1-5 or a Flux model there)"
        print("ERROR: no usable image model in", SD_MODELS_DIR)
        return
    problem = image_model_problem(model)
    if problem:
        sd_start_error = problem
        print("ERROR:", problem)
        return
    cmd = [
        SD_EXE,
        "-l", "127.0.0.1",
        "--listen-port", str(SD_PORT),
    ]
    if is_flux_model(model):
        components = find_flux_components()
        if components is None:
            sd_start_error = f"this model needs clip_l/t5xxl/vae files in {FLUX_COMPONENTS_DIR} but one or more is missing"
            print(
                "ERROR: this model needs clip_l/t5xxl/vae files in", FLUX_COMPONENTS_DIR,
                "but one or more is missing",
            )
            return
        cmd += [
            "--diffusion-model", model,
            "--clip_l", components["clip_l"],
            "--t5xxl", components["t5xxl"],
            "--vae", components["vae"],
            "--clip-on-cpu",
            "--diffusion-fa",
            # sd-server's OpenAI-compat edit endpoint doesn't accept a per-request
            # strength override, so the server-wide default matters: Kontext's
            # reference-image conditioning anchors to the source so strongly that
            # the built-in 0.75 default produces near-identical no-op "edits".
            "--strength", "0.85",
        ]
    else:
        cmd += ["-m", model]
    # Which GPU, on a machine with several (nothing is added with one): see image_engine_pick. Verified against the bundled build:
    # `--backend cuda0` starts and serves, combines with --clip-on-cpu in either order, and a device that does not exist ("cuda9")
    # is refused with "backend 'cuda9' was not found" - so only ever a device the engine itself lists.
    try:
        picked, backend, sd_env = image_engine_plan(load_settings())
    except Exception:
        picked, backend, sd_env = None, None, None
    if backend:
        cmd += ["--backend", backend]
        print(f"image engine: using {picked['label'] + ' (' + picked['name'] + ')' if picked else 'the CPU'} (--backend {backend})")
    if force_cpu:
        # the retry after an immediate exit: no --backend choice and no CUDA device visible, so the engine runs on the CPU
        cmd = [a for i, a in enumerate(cmd) if a != "--backend" and (i == 0 or cmd[i - 1] != "--backend")]
        sd_env = dict(sd_env or {}, CUDA_VISIBLE_DEVICES="-1")
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW
    if not force_cpu:
        rotate_log(SD_LOG_PATH)
    try:
        with open(SD_LOG_PATH, "ab") as log:         # the command first (which arguments were used), then whatever the server prints
            log.write((("--- retry on the CPU ---" if force_cpu else "--- start ---") + " command: " + " ".join(cmd) + chr(10)).encode("utf-8", "replace"))
    except OSError:
        pass
    sd_proc = subprocess.Popen(cmd, creationflags=flags, cwd=os.path.dirname(SD_EXE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               **({"env": dict(os.environ, **sd_env)} if sd_env else {}))
    pump_process_log(sd_proc, SD_LOG_PATH, LLAMA_LOG_MAX_BYTES)
    print(f"sd-server started (pid {sd_proc.pid}) with model {os.path.basename(model)}" + (" on the CPU" if force_cpu else ""))


def stop_sd_server():
    global sd_proc
    if sd_proc and sd_proc.poll() is None:
        sd_proc.terminate()
        try:
            sd_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            sd_proc.kill()
    sd_proc = None


def image_server_blocker():
    """Why the image server cannot even be started - a sentence - or None. Looked at BEFORE the chat model is unloaded to make room for
    it: a server that dies at once (a missing DLL, on a PC without the Visual C++ runtime) used to cost the chat model, then a two-minute
    wait, then "did not become ready in time" - the same silent death as the model server's, with the same refused connection behind it."""
    found = describe_missing_dlls(pe_imports.missing_dependencies(SD_EXE), subject="the image server cannot start", program="sd-server.exe")
    if not found:
        return None
    what = found[0][0].upper() + found[0][1:] if found[0].startswith("the image server") else f"The image server cannot start: {found[0]}"
    return f"{what}. {found[1]}"


SD_FAILURE_CAUSES = (
    ("get sd version from file failed", "The image server could not recognise this model file (it is not a checkpoint in a format this build can read)."),
    ("out of memory", "The image server ran out of memory loading this model: try a smaller model, or close programs that use the graphics card."),
    ("no kernel image", "This image-server build has no code for this graphics card."),
    ("cuda error", "A graphics-card (CUDA) error stopped the image server."),
)


def sd_log_tail(max_chars=500):
    """The last lines the image server printed (its log), as one short string; '' when there is none."""
    try:
        with open(SD_LOG_PATH, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 8192))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("---")]
    tail = " | ".join(lines[-6:])
    return tail[-max_chars:]


def image_server_stopped_message(proc):
    """What a job waiting for the image server to come up is told when it is not going to: it never started (the reason start_sd_server
    recorded), or it started and ended at once. Waiting longer cannot help in either case. The server keeps no log of its own."""
    if proc is None:
        return "The image server did not start: " + (sd_start_error or "its program could not be launched.")
    code = proc.poll()
    exit_text = f" (exit code 0x{code & 0xFFFFFFFF:X})" if isinstance(code, int) and code else ""
    found = describe_missing_dlls(pe_imports.missing_dependencies(SD_EXE), subject="the image server cannot start", program="sd-server.exe")
    if found:
        return f"The image server stopped right after it started{exit_text}. Most likely {found[0]}. {found[1]}"
    tail = sd_log_tail()
    low = tail.lower()
    cause = next((sentence for needle, sentence in SD_FAILURE_CAUSES if needle in low), "")
    if tail:
        return f"The image server stopped right after it started{exit_text}. {cause + ' ' if cause else ''}Its last output: {tail} (full log: data{os.sep}sd-server.log)"
    return f"The image server stopped right after it started{exit_text} and printed nothing; the command it was started with is on the first line of data{os.sep}sd-server.log."


def restart_chat_model_if_down():
    """After an image job that failed: bring the chat model back - unless it never went away (a failure before it was unloaded)."""
    with llama_restart_lock:
        proc = llama_proc
        if proc is None or proc.poll() is not None:
            start_llama()


def sd_health():
    if not port_accepts_connections(SD_PORT):
        return None  # see port_accepts_connections: a closed port costs ~2 s to ask directly
    try:
        with urllib.request.urlopen(SD_URL + "/sdcpp/v1/capabilities", timeout=2) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


# ---------------------------------------------------------------- embeddings / RAG
#
# Retrieval-augmented injection for Project files, engaged only once a
# project's raw-dumped content would exceed its context budget (see
# _check_project_content_budget/build_system_prompt) - under budget, every
# file still gets injected in full on every turn, unchanged from before this
# section existed. Chunking/embedding is lazy (first retrieval-triggering
# turn only, see ensure_project_chunks), so a project that never crosses the
# budget never touches any of this or depends on the embedding server being
# up at all.

RAG_TOP_K = 6
RAG_CHUNK_TARGET_TOKENS = 300
RAG_CHUNK_OVERLAP_TOKENS = 50

embed_proc = None


def find_embed_model():
    if not os.path.isdir(EMBED_MODELS_DIR):
        return None
    ggufs = [f for f in os.listdir(EMBED_MODELS_DIR) if f.lower().endswith(".gguf")]
    if not ggufs:
        return None
    # No "active model" selection setting for this, unlike models/ - this
    # directory is single-purpose (embedding only). If more than one .gguf
    # somehow ends up here, pick consistently rather than arbitrarily.
    ggufs.sort(key=lambda f: os.path.getsize(os.path.join(EMBED_MODELS_DIR, f)), reverse=True)
    return os.path.join(EMBED_MODELS_DIR, ggufs[0])


def start_embed_server():
    """Runs the same llama-server.exe binary used for chat, in --embedding
    mode against a small dedicated model, as a 3rd concurrent subprocess on
    its own port. Unlike sd-server (which the chat model is unloaded to make
    room for), this must stay up *alongside* the chat model - retrieval needs
    to run on every turn of an over-budget project's conversation, not just
    when nothing else is using the GPU. -ngl 0 (CPU) is deliberate, not a
    placeholder: a model this small costs only tens of milliseconds per
    embedding call on CPU, and keeping it off the GPU avoids stacking its
    VRAM footprint on top of the already-resident chat model for no
    meaningful latency benefit. --embd-normalize 2 (L2) is pinned explicitly
    rather than relying on llama-server's own default, since
    retrieve_relevant_chunks below assumes normalized vectors (a plain dot
    product standing in for cosine similarity - confirmed live during model
    selection that this server's default already normalizes, but pinning it
    here means that assumption can't silently break if that default ever
    changes upstream). No error if no model asset is present - RAG then just
    stays unavailable and _check_project_content_budget's existing hard-block
    behavior for over-budget projects is unchanged, so this is safe to call
    even before models/embedding/ has anything in it."""
    global embed_proc
    model = find_embed_model()
    if model is None:
        print("ERROR: no embedding model configured in", EMBED_MODELS_DIR, "- RAG will stay unavailable")
        return
    cmd = [
        LLAMA_EXE,
        "-m", model,
        "--host", "127.0.0.1",
        "--port", str(EMBED_PORT),
        "--api-key", LLAMA_API_KEY,
        "--embedding",
        "--pooling", "mean",
        "--embd-normalize", "2",
        "-ngl", "0",
        "-c", "2048",
    ]
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW
    embed_proc = subprocess.Popen(cmd, creationflags=flags, cwd=os.path.dirname(LLAMA_EXE))
    print(f"embed-server started (pid {embed_proc.pid}) with model {os.path.basename(model)}")


def stop_embed_server():
    global embed_proc
    proc = embed_proc
    embed_proc = None
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def embed_health():
    if not port_accepts_connections(EMBED_PORT):
        return False  # see port_accepts_connections: a closed port costs ~2 s to ask directly
    try:
        with urllib.request.urlopen(urllib.request.Request(EMBED_URL + "/health", headers=llama_headers()), timeout=2) as resp:
            return json.loads(resp.read().decode()).get("status") == "ok"
    except Exception:
        return False


def embed_texts(texts):
    """Calls the embedding server's OpenAI-compatible /v1/embeddings for a
    batch of strings at once. Returns a list of embedding vectors (each a
    list of floats) in the same order as `texts`, or None if the embedding
    server isn't reachable or errors. Explicitly re-sorts the response by
    each item's own "index" field rather than trusting positional order -
    that field exists in the API spec precisely because a batch response
    isn't guaranteed to come back in request order."""
    try:
        req = urllib.request.Request(
            EMBED_URL + "/v1/embeddings",
            data=json.dumps({"input": texts}).encode(),
            headers=llama_headers({"Content-Type": "application/json"}),
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        by_index = sorted(data["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in by_index]
    except Exception:
        return None


_WHITESPACE = re.compile(r"\s")


def _window_oversize_paragraph(text, size, overlap):
    """Splits one paragraph longer than `size` characters into windows of at
    most `size`, each starting `overlap` characters before the previous one
    ended. Each window is cut back to a natural boundary - a line break,
    sentence end, comma or space - when one falls in the window's second half,
    and only cut mid-word when the text has no boundary at all (e.g. one long
    unbroken run of characters)."""
    pieces = []
    start, n = 0, len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            window = text[start:end]
            for sep in ("\n", ". ", "! ", "? ", "; ", ", ", " "):
                idx = window.rfind(sep)
                if idx > size * 0.5:
                    end = start + idx + len(sep)
                    break
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= n:
            break
        nxt = max(end - overlap, start + 1)  # step back by the overlap, but always move forward
        # Start the next window on a clean boundary - a line start if there is
        # one in the overlap, else a word start - not mid-word or mid-line.
        line_start = text.find("\n", nxt, end)
        if line_start != -1:
            nxt = line_start + 1
        else:
            gap = _WHITESPACE.search(text, nxt, end)
            if gap:
                nxt = gap.end()
        start = nxt
    return pieces


def chunk_text(content, target_tokens=RAG_CHUNK_TARGET_TOKENS, overlap_tokens=RAG_CHUNK_OVERLAP_TOKENS):
    """Splits content into overlapping paragraph-aware chunks for embedding.
    Packs whole paragraphs (blank-line-separated) greedily up to
    ~target_tokens, estimated cheaply via chars/4 (avoiding one exact-
    tokenize HTTP round-trip per paragraph here - ensure_project_chunks
    re-tokenizes each finished chunk for its stored token_count instead),
    carrying the last overlap_tokens worth of the previous chunk's trailing
    paragraphs forward into the next one so a fact sitting near a chunk
    boundary still shows up whole in at least one chunk. A single paragraph
    longer than target_tokens on its own is windowed (see
    _window_oversize_paragraph) instead of kept whole: it used to be kept whole
    on the assumption that project files are prose, but source code, CSV, logs,
    JSON and a lot of PDF-extracted text have no blank lines at all - one
    "paragraph" the size of the entire file - and a chunk that big made
    retrieval hand the whole over-budget file straight back to the prompt."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", content) if p.strip()]
    if not paragraphs:
        return []
    target_chars = target_tokens * 4
    overlap_chars = overlap_tokens * 4
    chunks = []
    current = []
    current_chars = 0
    for para in paragraphs:
        if len(para) > target_chars:
            # Too big to pack with its neighbours: flush what's pending, emit it
            # as its own overlapping windows, and start fresh after it.
            if current:
                chunks.append("\n\n".join(current))
                current, current_chars = [], 0
            chunks.extend(_window_oversize_paragraph(para, target_chars, overlap_chars))
            continue
        if current and current_chars + len(para) > target_chars:
            chunks.append("\n\n".join(current))
            carry, carry_chars = [], 0
            for p in reversed(current):
                if carry_chars >= overlap_chars:
                    break
                carry.insert(0, p)
                carry_chars += len(p)
            current, current_chars = carry, carry_chars
        current.append(para)
        current_chars += len(para)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def ensure_project_chunks(conn, project_id):
    """Lazily chunks+embeds any project_files that don't have chunks yet.
    Only ever called from build_system_prompt's over-budget branch, so a
    project that stays under its raw-dump budget never reaches this and
    never depends on the embedding server being up. Returns True if every
    file that needed chunking got embedded successfully, False if the
    embedding server was unreachable partway through (callers must then
    treat retrieval as unavailable, not silently proceed with a partial
    chunk set).

    Each file's network work - one embedding call plus a tokenizer call per
    chunk, which for a big project is hundreds of HTTP round trips - is done
    BEFORE anything is written, and the rows then go in with one short commit
    per file. Doing it the other way round (insert as you go, commit when the
    caller finishes) held the database's single write lock across all of those
    calls, which stalled every other write in the app for as long as the
    chunking took (measured: an unrelated write waited 1.7 s behind a small
    project; a large one is minutes). The caller therefore must not have a
    write transaction of its own open when it calls this - the commit here
    would end it."""
    files = conn.execute(
        "SELECT id, content FROM project_files WHERE project_id=? "
        "AND id NOT IN (SELECT DISTINCT project_file_id FROM project_file_chunks WHERE project_id=?)",
        (project_id, project_id),
    ).fetchall()
    for f in files:
        pieces = chunk_text(f["content"])
        if not pieces:
            continue
        vectors = embed_texts(pieces)
        if vectors is None:
            return False
        rows = []
        for i, (piece, vector) in enumerate(zip(pieces, vectors)):
            token_count = count_tokens(piece)
            if token_count is None:
                token_count = len(piece) // 4
            packed = struct.pack(f"<{len(vector)}f", *vector)
            rows.append((f["id"], project_id, i, piece, token_count, packed))
        conn.executemany(
            "INSERT INTO project_file_chunks "
            "(project_file_id, project_id, chunk_index, content, token_count, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    return True


def retrieve_relevant_chunks(conn, project_id, query_text, top_k=RAG_TOP_K):
    """Ranks every chunk stored for this project against query_text by
    cosine similarity - a plain dot product, since embed_texts' vectors are
    L2-normalized (start_embed_server pins --embd-normalize 2), and pure
    Python rather than numpy: brute-force over dozens-to-low-hundreds of
    chunks (the expected scale for a single project's files) is entirely
    adequate without adding a dependency. Returns [] if the embedding server
    is unreachable or the project has no chunks yet (e.g. only images/memory
    - not files - pushed it over budget, which retrieval can't help with,
    see build_system_prompt)."""
    query_vectors = embed_texts([query_text or ""])
    if not query_vectors:
        return []
    query_vec = query_vectors[0]
    rows = conn.execute(
        "SELECT c.content, c.embedding, f.filename FROM project_file_chunks c "
        "JOIN project_files f ON f.id = c.project_file_id WHERE c.project_id=?",
        (project_id,),
    ).fetchall()
    if not rows:
        return []
    scored = []
    for row in rows:
        vec = struct.unpack(f"<{len(row['embedding']) // 4}f", row["embedding"])
        score = sum(a * b for a, b in zip(query_vec, vec))
        scored.append((score, row["filename"], row["content"]))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [{"filename": fn, "content": content} for _score, fn, content in scored[:top_k]]


def start_idle_restore_watcher():
    """Starts the daemon thread that automatically restores the chat model
    if the image model has been left resident (see image_model_resident)
    with no new generate/edit for IDLE_RESTORE_SECONDS - otherwise walking
    away from an image-editing session would leave chat unusable
    indefinitely with nothing prompting the restore. Must be called exactly
    once per process; both entry points (server.py's own main() and
    desktop_main.py) call this right after init_db()."""
    threading.Thread(target=_idle_restore_watcher, daemon=True).start()


def _idle_restore_watcher():
    while True:
        time.sleep(15)
        with image_gen_lock:
            idle_too_long = (
                image_model_resident and not image_gen_status["active"] and not compact_active
                and not any_chat_active() and (time.time() - last_image_activity) > IDLE_RESTORE_SECONDS
            )
        if idle_too_long:
            ensure_chat_model_available()


def download_model_worker(url, filename, dest_dir=None, progress_key=None):
    dest_dir = dest_dir or MODELS_DIR
    progress_key = progress_key or filename
    tmp_path = os.path.join(dest_dir, filename + ".part")
    final_path = os.path.join(dest_dir, filename)
    try:
        os.makedirs(dest_dir, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "LocalAIChatBot"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length", 0) or 0)
            with download_progress_lock:
                download_progress[progress_key] = {"downloaded": 0, "total": total, "done": False, "error": None}
            downloaded = 0
            with open(tmp_path, "wb") as f:
                while True:
                    chunk = resp.read(DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    with download_progress_lock:
                        download_progress[progress_key]["downloaded"] = downloaded
        os.replace(tmp_path, final_path)
        with download_progress_lock:
            download_progress[progress_key]["done"] = True
    except Exception as e:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        with download_progress_lock:
            download_progress[progress_key] = {"downloaded": 0, "total": 0, "done": True, "error": str(e)}


# ---------------------------------------------------------------- tool calling
#
# Confirmed via a real spike (2026-08-07, throwaway scratchpad script, not
# kept) that this app's bundled llama-server.exe genuinely returns
# structured tool_calls (not just plain-text mimicry) for at least one
# bundled model - not every model's chat template supports the OpenAI-style
# `tools` schema (Gemma's own template uses a different ```tool_code
# convention instead and never emits structured tool_calls at all), so this
# is opt-in (settings["tools_enabled"], default False) rather than always-on.
#
# Only one tool exists so far: a calculator. It's implemented via Python's
# ast module, restricted to numeric literals and +-*/ // % ** (and unary
# +/-) - never a raw eval() of model-controlled input. This is the one
# genuinely new execution surface introduced by this whole feature-gaps
# project and is worth being careful with even for a first tool.

MAX_TOOL_ROUND_TRIPS = 1  # a single tool-call-then-answer cycle per turn, not a full agentic loop


def partial_json_string(raw, key):
    """The value of the string field `key` in a JSON object that may still be streaming in (tool-call arguments
    arrive in fragments), once that string is complete - None before that, or when the field is not a string."""
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"((?:[^"\\]|\\.)*)"', raw or "")
    if not m:
        return None
    try:
        return json.loads('"' + m.group(1) + '"')
    except ValueError:
        return None

_SAFE_AST_OPERATORS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


# Exponentiation is the one operator that can turn seven characters into hours of
# work: `9**9**9` is a 369-million-digit integer, computed in C while holding the
# GIL - which froze the whole app (this server runs in the same process as the
# window) until it finished or ran out of memory (measured: still running after
# 20 s). A result may have at most this many bits (~30,000 digits - far beyond
# anything a person asks a calculator for).
MAX_CALC_RESULT_BITS = 100_000


def _bounded_pow(base, exponent):
    if isinstance(base, int) and isinstance(exponent, int) and exponent > 0 and abs(base) > 1:
        if exponent * abs(base).bit_length() > MAX_CALC_RESULT_BITS:
            raise ValueError("result would be too large")
    return operator.pow(base, exponent)


def _safe_eval_ast_node(node):
    """Recursively evaluates a parsed arithmetic expression, allowing only
    numeric constants and the operators in _SAFE_AST_OPERATORS - anything
    else (names, calls, attribute access, subscripts, comprehensions,
    string/list/dict literals, ...) hits the final raise and is rejected
    outright rather than attempted. No path through this function ever
    reaches Python's own eval()/exec()."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise ValueError("only numeric constants are allowed")
    if isinstance(node, ast.BinOp):
        op = _SAFE_AST_OPERATORS.get(type(node.op))
        if op is None:
            raise ValueError(f"operator {type(node.op).__name__} is not allowed")
        if op is operator.pow:
            op = _bounded_pow
        return op(_safe_eval_ast_node(node.left), _safe_eval_ast_node(node.right))
    if isinstance(node, ast.UnaryOp):
        op = _SAFE_AST_OPERATORS.get(type(node.op))
        if op is None:
            raise ValueError(f"operator {type(node.op).__name__} is not allowed")
        return op(_safe_eval_ast_node(node.operand))
    raise ValueError(f"expression contains a disallowed construct: {type(node).__name__}")


def tool_calculate(expression=None, **_ignored):
    """calculate tool implementation - see AVAILABLE_TOOLS for the schema
    the model sees. Always returns a string (the "content" of the tool
    result message sent back to the model), including for error cases -
    letting the model see and react to "error: ..." rather than silently
    swallowing a bad call."""
    if not isinstance(expression, str) or not expression.strip():
        return "error: 'expression' must be a non-empty string"
    if len(expression) > 200:
        return "error: expression too long"
    try:
        tree = ast.parse(expression, mode="eval")
        result = _safe_eval_ast_node(tree.body)
    except ZeroDivisionError:
        return "error: division by zero"
    except SyntaxError:
        return "error: not a valid arithmetic expression"
    except Exception as e:
        return f"error: could not evaluate expression ({e})"
    try:
        return str(result)
    except ValueError:
        # Python refuses to print an integer of more than 4,300 digits.
        return "error: the result is too large to show"


# ---- the document tool (create_document): the model writes a Markdown / plain-text document that the chat shows as
# a card (opened in a viewer, saved, downloaded). Stored in the `documents` table, attached to the reply that made it.
# The content is untrusted model output: stored and later shown as text, never interpreted or executed; the file name
# is only a suggestion, run through safe_document_filename before anything sees it.
DOCUMENT_MAX_CHARS = 2_000_000           # the context window ends a document long before this; a hard stop all the same
DOCUMENT_TITLE_MAX = 200
DOCUMENT_FILENAME_STEM_MAX = 80
DOCUMENT_EXTENSIONS = (".md", ".txt")
_DOCUMENT_RESERVED_STEMS = {"CON", "PRN", "AUX", "NUL", "CLOCK$"} | {f"{p}{n}" for p in ("COM", "LPT") for n in "0123456789"}
_DOCUMENT_BAD_CHARS_RE = re.compile(r'[\x00-\x1f\x7f<>:"|?*]')
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_DOCUMENT_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")


def safe_document_filename(name, title=""):
    """The file name a document card shows and a save dialog proposes, from the model's suggestion. Only the last
    path segment counts (no directory can be chosen), the characters Windows refuses and control characters are
    dropped, trailing dots and spaces (which Windows strips silently) go, the stem is capped, reserved device names
    (CON, NUL, COM1, ...) are prefixed, and only .md / .txt survive - any other extension becomes .md. An empty
    result falls back to the title, then to "document"."""
    text = name if isinstance(name, str) else ""
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = _DOCUMENT_BAD_CHARS_RE.sub("", text).strip(" .")
    stem, ext = text, ""
    m = _DOCUMENT_EXT_RE.search(text)
    if m:
        stem, ext = text[:m.start()], m.group(0).lower()
    if ext not in DOCUMENT_EXTENSIONS:
        ext = ".md"
    stem = stem.strip(" .")
    if not stem:
        fallback = title if isinstance(title, str) else ""
        fallback = _DOCUMENT_BAD_CHARS_RE.sub("", fallback.replace("\\", " ").replace("/", " ")).strip(" .")
        stem = re.sub(r"\s+", "-", fallback).strip("-. ") or "document"
    stem = stem[:DOCUMENT_FILENAME_STEM_MAX].rstrip(" .") or "document"
    if stem.split(".", 1)[0].upper() in _DOCUMENT_RESERVED_STEMS:
        stem = "doc-" + stem
    return stem + ext


def tool_create_document(context, title=None, filename=None, content=None, **_ignored):
    """create_document tool implementation. Stores the document attached to the conversation - the reply's row id
    is filled in by _stream_and_save once the reply is saved - and tells the model what was made. `context` is
    {"conv_id", "offset" (how much reply text came before the call), "created" (filled in here)}; without one
    (no conversation) nothing is stored. Always returns a string, "error: ..." included, like tool_calculate."""
    if not isinstance(context, dict) or not context.get("conv_id"):
        return "error: there is no conversation to attach the document to"
    if not isinstance(content, str) or not content.strip():
        return "error: 'content' must be a non-empty string holding the whole document"
    note = None
    if len(content) > DOCUMENT_MAX_CHARS:
        content = content[:DOCUMENT_MAX_CHARS]
        note = f"the content was cut at {DOCUMENT_MAX_CHARS:,} characters"
    safe_name = safe_document_filename(filename, title if isinstance(title, str) else "")
    clean_title = re.sub(r"\s+", " ", _CONTROL_CHARS_RE.sub("", title)).strip() if isinstance(title, str) else ""
    clean_title = clean_title[:DOCUMENT_TITLE_MAX].strip() or os.path.splitext(safe_name)[0]
    size = len(content.encode("utf-8"))
    with db() as conn:
        if conn.execute("SELECT 1 FROM conversations WHERE id=?", (context["conv_id"],)).fetchone() is None:
            return "error: the conversation no longer exists"
        row = conn.execute(
            "SELECT MAX(version) AS v FROM documents WHERE conversation_id=? AND filename=?",
            (context["conv_id"], safe_name),
        ).fetchone()
        version = int(row["v"] or 0) + 1
        cur = conn.execute(
            "INSERT INTO documents (conversation_id, title, filename, content, size, version, char_offset) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (context["conv_id"], clean_title, safe_name, content, size, version, int(context.get("offset") or 0)),
        )
        doc_id = cur.lastrowid
    info = {"id": doc_id, "title": clean_title, "filename": safe_name, "bytes": size, "version": version}
    context.setdefault("created", []).append(info)
    result = {"ok": True, "document_id": doc_id, "filename": safe_name, "title": clean_title, "bytes": size}
    if note:
        result["note"] = note
    return json.dumps(result)


AVAILABLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Evaluates a basic arithmetic expression and returns the numeric result. "
                            "Supports + - * / // % ** and parentheses. No variables or functions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "A basic arithmetic expression, e.g. \"2 + 2\" or \"847 * 293\".",
                    },
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_document",
            "description": "Creates a Markdown (.md) or plain-text (.txt) document that appears in the chat as a card "
                           "the user can open, save and download. Use it when the user asks you to make, write, draft "
                           "or produce a document or file - a system prompt, a guide, a README, notes, a plan, a letter, "
                           "a specification. Put the WHOLE document in `content` (none of it in the chat reply) and keep "
                           "the chat reply itself to a short sentence. Do not use it for ordinary answers or short snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "A short human title for the document, e.g. \"System prompt for a travel agent\".",
                    },
                    "filename": {
                        "type": "string",
                        "description": "A suggested file name ending in .md or .txt, e.g. \"travel-agent-system-prompt.md\".",
                    },
                    "content": {
                        "type": "string",
                        "description": "The complete document text: Markdown for a .md file, plain text for .txt.",
                    },
                },
                "required": ["title", "filename", "content"],
            },
        },
    },
]
AVAILABLE_TOOLS_BY_NAME = {"calculate": tool_calculate, "create_document": tool_create_document}
# Tools that take the turn's context (conversation id, text offset - see execute_tool_calls) as their first argument.
CONTEXT_TOOLS = {"create_document"}
# Which setting switches each tool on; a tool without a row here follows the calculator's switch.
TOOL_SETTING_KEYS = {"calculate": "tools_enabled", "create_document": "document_tool_enabled"}


def model_supports_tools(model_path):
    """Whether the model's chat template handles the OpenAI-style `tools` (True / False), or None when that cannot be
    told (no model, unreadable metadata, no template). A template without tool support makes llama-server fall back
    to its generic tool handling, which turns the whole reply into constrained JSON - not something to switch on for
    every chat by default."""
    if not model_path:
        return None
    try:
        template = gguf_chat_template(model_path)
    except Exception:
        return None
    if not template:
        return None
    low = template.lower()
    return "tools" in low or "tool_call" in low


def tool_capable_model_names():
    """The installed models (file stems) whose chat template handles tools - for the Settings caption that tells the
    user which model to switch to when the active one cannot call tools."""
    names = []
    try:
        files = sorted(f for f in os.listdir(MODELS_DIR) if f.lower().endswith(".gguf"))
    except OSError:
        return names
    for f in files:
        if model_supports_tools(os.path.join(MODELS_DIR, f)):
            names.append(os.path.splitext(f)[0])
    return names


def enabled_tools(settings, model_path=None):
    """The tool definitions a chat request offers under these settings - the calculator and the document tool have
    their own switches - or [], in which case the request carries no `tools` at all, exactly as before tools existed.
    The document tool (on by default) is left out for a model whose template is known not to handle tools - see
    model_supports_tools; the calculator (off by default, the user's explicit choice) is offered as it always was."""
    offered = []
    for t in AVAILABLE_TOOLS:
        name = t["function"]["name"]
        if not settings.get(TOOL_SETTING_KEYS.get(name, "tools_enabled")):
            continue
        if name == "create_document" and model_supports_tools(model_path) is False:
            continue
        offered.append(t)
    return offered


def execute_tool_calls(tool_calls, context=None):
    """tool_calls: dict of index -> {"id", "name", "arguments" (a raw JSON
    string accumulated from streamed fragments - see Handler._stream_one_
    completion)}. Returns a list of {"role": "tool", "tool_call_id",
    "content"} messages, one per call, in the OpenAI tool-result convention
    - always returns one message per call, even for an unknown tool name or
    unparseable arguments (as an "error: ..." content string), so the model
    gets told what went wrong and can react, rather than the round-trip
    silently vanishing.

    `context` (a dict, from _stream_and_save) is handed to the CONTEXT_TOOLS
    as their first argument; for the document tool it also collects
    context["events"] - one {"state": "done", id, title, filename, bytes,
    version, index} or {"state": "failed", reason, index} per create_document
    call - which the caller streams to the client as `document` frames."""
    messages = []
    events = context.setdefault("events", []) if isinstance(context, dict) else []
    for idx, tc in tool_calls.items():
        tool_call_id = tc.get("id") or f"call_{len(messages)}"
        name = tc.get("name")
        tool_fn = AVAILABLE_TOOLS_BY_NAME.get(name)
        if tool_fn is None:
            messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": f"error: unknown tool '{name}'"})
            continue
        raw_args = tc.get("arguments") or "{}"
        try:
            args = json.loads(raw_args)
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (ValueError, TypeError):
            messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": "error: arguments were not valid JSON"})
            if name == "create_document":
                events.append({"state": "failed", "reason": "the model's document call was not valid JSON", "index": idx})
            continue
        try:
            result = tool_fn(context, **args) if name in CONTEXT_TOOLS else tool_fn(**args)
        except Exception as e:
            result = f"error: {e}"
        result = str(result)
        if name == "create_document":
            if result.startswith("error:"):
                events.append({"state": "failed", "reason": result[len("error:"):].strip(), "index": idx})
            elif isinstance(context, dict) and context.get("created"):
                events.append({"state": "done", **context["created"][-1], "index": idx})
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": result})
    return messages


# ---------------------------------------------------------------- HTTP handler

# This API has no login: what keeps other programs' web pages out is that it is
# bound to loopback and that every request must demonstrably come from the app
# itself - see Handler._is_request_from_this_app.
LOOPBACK_HOST_NAMES = ("127.0.0.1", "localhost", "[::1]")

# The most unread request body that is read and thrown away to keep a connection
# usable after a handler answered without reading it (Handler._discard_unread_body),
# and how long a client may stall while that is going on before it is dropped.
MAX_DISCARDED_BODY_BYTES = 256 * 1024 * 1024
DISCARD_BODY_TIMEOUT_S = 10


def split_host_port(netloc):
    """'127.0.0.1:8790' -> ('127.0.0.1', 8790); '[::1]:8790' -> ('[::1]', 8790);
    'LocalHost.' -> ('localhost', None); (None, None) for anything that is not a
    plain host[:port]."""
    netloc = (netloc or "").strip().lower()
    if netloc.startswith("["):
        end = netloc.find("]")
        if end < 0:
            return None, None
        host, rest = netloc[: end + 1], netloc[end + 1:]
    else:
        host, sep, tail = netloc.partition(":")
        host, rest = host.rstrip("."), sep + tail
    if not rest:
        return host, None
    if rest[0] != ":" or not rest[1:].isdigit():
        return None, None
    return host, int(rest[1:])


def is_this_app_netloc(netloc, port, port_required):
    """True if netloc names this machine's loopback interface on `port`. With
    port_required=False a missing port also passes (a hand-written HTTP/1.1
    request may leave it off a Host header; a browser never does)."""
    host, found = split_host_port(netloc)
    if host not in LOOPBACK_HOST_NAMES:
        return False
    if found is None:
        return not port_required
    return found == port


def read_backup_json_object(zf, name, max_bytes=1_000_000):
    """(parsed object, raw bytes, None) for a small JSON-object file inside a
    backup zip, or (None, None, why-it-is-unusable). The size cap keeps a
    crafted entry from being read whole into memory."""
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None, None, "is missing"
    if info.file_size > max_bytes:
        return None, None, f"is larger than {max_bytes // 1000} KB"
    raw = zf.read(name)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None, None, "is not valid JSON"
    if not isinstance(obj, dict):
        return None, None, "is not a JSON object"
    return obj, raw, None


def backup_output_destination(root, relative):
    """Where an `outputs/<relative>` entry of a backup zip may be written under
    `root` (the absolute outputs folder), or None if it must not be. Every
    component has to be a plain file name, and the result has to stay inside
    root. The old check only refused empty / "." / ".." components, which missed
    drive letters: os.path.join(root, "C:", "x.txt") jumps to another drive, so
    an entry named `outputs/C:/x.txt` was written to C:\\x.txt - anywhere on any
    drive (verified), e.g. into a Startup folder. (A `..\\` entry was already
    safe: zipfile turns backslashes into "/" on Windows, so it arrives as a
    ".." component.)"""
    parts = relative.split("/")
    if any(safe_filename(part) != part for part in parts):
        return None
    dest = os.path.abspath(os.path.join(root, *parts))
    try:
        if os.path.commonpath([root, dest]) != root:
            return None
    except ValueError:  # a different drive
        return None
    return dest


class BadRequest(Exception):
    """A request the client got wrong. Raised anywhere under a route handler and
    answered with a 400 and the message (Handler._dispatch_safely) - rather than
    surfacing as a 500 with a traceback in the log for what is not a server bug."""


SQLITE_MAX_INT = 2 ** 63 - 1


def optional_id(value, name):
    """A database id from a request body. Absent / null / 0 / "" mean "none" (the
    long-standing leniency: clients clear a folder or project that way); anything
    else must be a positive integer that SQLite can hold. A list, a string or a
    30-digit number used to reach the query and come back as a 500."""
    if not value:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > SQLITE_MAX_INT:
        raise BadRequest(f"{name} must be a positive integer")
    return value


def text_field(body, key, default=""):
    """body[key] as a string ("" / default when absent or null). A number or list
    where text belongs is a 400, not an AttributeError on .strip()."""
    value = body.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise BadRequest(f"{key} must be text")
    return value


def bool_field(body, key, default=False):
    """body[key] as a real boolean (null/absent -> default). For switches that
    decide whether something is destroyed or kept: bool("false") is True, so a
    stray string used to count as a yes."""
    value = body.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise BadRequest(f"{key} must be true or false")
    return value


def number_field(body, key, default):
    """body[key] as a finite number. When the key is absent - or null, which has
    always meant "no value" - the result is `default`, returned as it is: it is
    the value already stored, and that is NULL for the sort order of every
    ordinary project. (Validating the default too made saving a project's
    instructions fail with "sort_order must be a number" for all of them - found
    by test_ui_journey.py, the one test that sends what the real screen sends.)
    Only a value the client actually supplied can be wrong."""
    if body.get(key) is None:
        return default
    value = body[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > SQLITE_MAX_INT:
        raise BadRequest(f"{key} must be a number")
    return value


def settings_type_problem(key, value):
    """None if `value` is an acceptable kind of thing for the setting `key`, else
    a message. Judged by the type of the setting's default, which is what every
    reader of that setting already assumes: before this, POST /api/settings
    stored whatever it was given for the keys that have no range check, and a
    list saved as active_model made every later request that joins it onto a
    path fail - persistently, since it is written to settings.json. Settings
    that default to None (the model/vision/image-model names, folders, links) are
    a string or unset."""
    default = DEFAULT_SETTINGS[key]
    if default is None:
        ok = value is None or isinstance(value, str)
    elif isinstance(default, bool):
        ok = isinstance(value, bool)
    elif isinstance(default, (int, float)):
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    elif isinstance(default, str):
        ok = isinstance(value, str)
    else:
        ok = isinstance(value, type(default))
    return None if ok else f"{key} has the wrong kind of value"


def gpu_settings_problem(body):
    """None, or a message, for the GPU-related keys of a settings-shaped dict (the lists need their items checked too, which
    settings_type_problem - judging only by the kind of the default - does not do)."""
    if "gpu_placement" in body and body["gpu_placement"] not in gpu_plan.PLACEMENT_MODES:
        return f"gpu_placement must be one of {list(gpu_plan.PLACEMENT_MODES)}"
    for key in ("gpu_order", "gpu_disabled"):
        if key in body and not (
            isinstance(body[key], list) and len(body[key]) <= 64 and all(isinstance(k, str) and 0 < len(k) <= 200 for k in body[key])
        ):
            return f"{key} must be a list of GPU keys (text)"
    for key in ("engine", "image_gpu"):
        if key in body and not (isinstance(body[key], str) and 0 < len(body[key]) <= 200):
            return f"{key} must be text"
    return None


def scrub_surrogates(obj):
    """JSON text may carry a lone UTF-16 surrogate escape (\\ud800). Python
    accepts it as a str, but it cannot be encoded to UTF-8, so it blew up later -
    in SQLite, or on the way to llama-server. A valid pair has already been
    joined by the JSON parser; a lone one becomes U+FFFD, as a lenient decoder
    would have made it."""
    if isinstance(obj, str):
        try:
            obj.encode("utf-8")
            return obj
        except UnicodeEncodeError:
            return obj.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    if isinstance(obj, dict):
        return {scrub_surrogates(k): scrub_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_surrogates(v) for v in obj]
    return obj


class _CountedStream:
    """The request body as the handlers read it, counting what they took so that
    whatever they left unread can be discarded afterwards
    (Handler._discard_unread_body). Every body read in this file is a plain
    .read(n); the other file methods are here so a future one can't skew the
    count."""

    __slots__ = ("_raw", "consumed")

    def __init__(self, raw):
        self._raw = raw
        self.consumed = 0

    def read(self, size=-1):
        data = self._raw.read(size)
        self.consumed += len(data)
        return data

    def read1(self, size=-1):
        data = self._raw.read1(size)
        self.consumed += len(data)
        return data

    def readline(self, size=-1):
        data = self._raw.readline(size)
        self.consumed += len(data)
        return data

    def readinto(self, buf):
        n = self._raw.readinto(buf)
        self.consumed += n or 0
        return n


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    # ---- helpers

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _content_length(self):
        """The declared request body size (0 when there is none). A header that
        isn't a number is the client's mistake - a 400, not the ValueError and
        500 that int() gave every upload route."""
        try:
            return max(int(self.headers.get("Content-Length", 0)), 0)
        except ValueError:
            raise BadRequest("invalid Content-Length")

    def read_json(self):
        """The request body as a dict. No body is {} (plenty of calls have none);
        a body that is not a JSON object is the client's mistake and a 400 -
        every handler below does body.get(...), so a bare list/string/number used
        to be an AttributeError and a 500 in each of them."""
        length = self._content_length()
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise BadRequest("request body must be a JSON object")
            return scrub_surrogates(body) if b"\\ud" in raw.lower() else body
        except (ValueError, UnicodeDecodeError):
            raise BadRequest("request body is not valid JSON")
        except RecursionError:
            raise BadRequest("request body is nested too deeply")

    # ---- routing

    def _dispatch_safely(self, fn):
        """Runs a do_*() route handler and, if it raises, turns that into a JSON
        500 instead of letting the exception escape unhandled - which otherwise
        closes the connection with no response at all (BaseHTTPRequestHandler
        has no built-in try/except around route dispatch). Confirmed real: a
        raise here used to surface client-side as a raw http.client exception
        that ApiClient._request()'s except clauses don't match (only
        HTTPError/URLError are caught), so it fell through uncaught inside a
        background worker thread - the queue never got a result, and anything
        gating on that result (e.g. chat_view.py's export_btn) stayed disabled
        forever, since ChatView is a singleton screen reused across every
        conversation rather than recreated per chat.

        Two more things happen around every route, once, for all of them:
        requests that don't come from the app itself are refused before they
        reach a handler, and any request body a handler left unread (because it
        answered early - a 400, a 409 "busy") is discarded afterwards."""
        raw_rfile = self.rfile
        self.rfile = counted = _CountedStream(raw_rfile)
        try:
            if not self._is_request_from_this_app():
                self._refuse_foreign_request()
                return
            if re.search(r"/\d{19,}(?:/|$)", self.path.split("?")[0]):
                # An id too big for SQLite can't exist; without this the query
                # raised OverflowError and the client got a 500 instead of a 404.
                self.send_json({"error": "not found"}, 404)
                return
            fn()
        except BadRequest as e:
            self.send_json({"error": str(e)}, 400)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as e:
            # The client hung up while its answer was being written (the desktop closing a stream, a poll it no longer wants):
            # not an error of ours, and nobody is left to answer. One line, instead of the full traceback that used to fill
            # data\desktop.log ("[WinError 10053] An established connection was aborted by the software in your host machine").
            print(f"note: the client went away while {self.command} {self.path.split('?')[0]} was being answered ({e.__class__.__name__})")
        except Exception:
            traceback.print_exc()
            try:
                self.send_json({"error": "internal server error"}, 500)
            except Exception:
                pass
        finally:
            self.rfile = raw_rfile
            self._discard_unread_body(counted.consumed)

    def _is_request_from_this_app(self):
        """The API has no login, and a web page open in the user's browser can
        send requests to 127.0.0.1 as easily as the app can. Two checks keep it
        from using this one:

        * Host must be this machine's loopback address on this port. A page can
          only read the answers if it first points its own domain name at
          127.0.0.1 (DNS rebinding), and then the browser still sends that
          domain as Host.
        * Origin - which a browser attaches to every cross-site POST, and which
          the page cannot forge or drop - must be this app's own origin when it
          is present. This is what stops a plain cross-site form/fetch POST
          (no preflight needed for those) from, say, resetting all data. Other
          programs on this machine (the app's own client, scripts, curl) send
          no Origin and are not affected.

        * Sec-Fetch-Site, which every current browser attaches (and a page
          cannot alter), must not say "cross-site". That covers what Origin
          cannot: a cross-site <img>/<script>/<iframe> GET carries no Origin.

        Nothing else in the API is cross-origin readable either: no CORS
        headers are ever sent, and OPTIONS preflights get a 501."""
        port = self.server.server_address[1]
        if (self.headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
            return False
        try:
            for host in self.headers.get_all("Host") or []:
                if not is_this_app_netloc(host, port, port_required=False):
                    return False
            for origin in self.headers.get_all("Origin") or []:
                parts = urllib.parse.urlsplit(origin.strip())
                if parts.scheme != "http" or not is_this_app_netloc(parts.netloc, port, port_required=True):
                    return False
        except ValueError:  # e.g. an unterminated "[" in the Origin
            return False
        return True

    def _refuse_foreign_request(self):
        body = json.dumps({"error": "this request did not come from the app itself"}).encode()
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _discard_unread_body(self, consumed):
        """A handler that answers before reading its request body (an early 400,
        or 409 "a reply is generating, try again") leaves those bytes in the
        socket. On a keep-alive connection they would then be parsed as the next
        request, and when the connection closed with unread data Windows
        answered with a reset that could throw the response away - the client
        saw ConnectionAbortedError instead of the 409 (measured: 24 of 25
        rejected 3 MB uploads). So read and drop what arrives - until the body is
        complete, the client goes quiet or hangs up (the desktop client stops
        sending as soon as it sees the early answer and closes), or a generous
        cap is reached - and only then close.

        read1(), not read(): read(n) waits for n whole bytes, which a client that
        has stopped sending never delivers."""
        try:
            declared = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            return
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True  # chunked bodies are not supported here, and can't be skipped
            return
        remaining = declared - consumed
        if remaining <= 0:
            return
        discarded = 0
        try:
            self.connection.settimeout(DISCARD_BODY_TIMEOUT_S)
            while remaining > 0 and discarded < MAX_DISCARDED_BODY_BYTES:
                chunk = self.rfile.read1(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                discarded += len(chunk)
            if remaining > 0:
                self.close_connection = True
        except OSError:
            self.close_connection = True
        finally:
            try:
                self.connection.settimeout(None)
            except OSError:
                pass

    def do_GET(self):
        self._dispatch_safely(self._route_get)

    def do_POST(self):
        self._dispatch_safely(self._route_post)

    def do_PATCH(self):
        self._dispatch_safely(self._route_patch)

    def do_DELETE(self):
        self._dispatch_safely(self._route_delete)

    def _route_get(self):
        path = self.path.split("?")[0]
        if path == "/api/health":
            self.send_json(llama_health())
        elif path == "/api/model":
            model = find_model()
            settings = load_settings()
            self.send_json({
                "model": os.path.basename(model) if model else None,
                "n_ctx_train": gguf_context_length(model) if model else None,
                "thinking": model_thinking_capability(model),
                # The budget actually in force for this model right now, so
                # Settings can state a real number instead of describing the
                # rule and leaving the user to do the arithmetic.
                "thinking_budget_effective": thinking_budget_tokens(settings, model),
                # A3: whether the file fits in free RAM + free VRAM as of the last launch (kept after a crash)
                "fit": llama_launch_fit,
                # Document tool: whether this model's chat template handles tools (None = unknown), and which
                # installed models do - the Settings caption says so when the active one cannot.
                "tool_support": model_supports_tools(model),
                "tool_capable_models": tool_capable_model_names(),
            })
        elif path == "/api/settings":
            self.send_json(load_settings())
        elif path == "/api/vram-status":
            vram = query_vram_mb()
            proc = llama_proc
            running = proc is not None and proc.poll() is None
            process_vram_mb = query_process_vram_mb(proc.pid) if running else None
            process_ram_mb = query_process_ram_mb(proc.pid) if running else None
            settings = load_settings()
            self.send_json({
                "available": vram is not None,
                **(vram or {}),
                "process_running": running,
                "process_vram_mb": process_vram_mb,
                "process_ram_mb": process_ram_mb,
                # Under Auto the layer count lives inside llama-server (--fit
                # picked it and doesn't report it back), so there's no honest
                # number to send - the client shows the flag instead.
                "gpu_layers_auto": settings.get("gpu_layers_auto", True) if running else None,
                "gpu_layers": (
                    settings.get("gpu_layers")
                    if running and not settings.get("gpu_layers_auto", True) else None
                ),
                "kv_offload": settings.get("kv_offload") if running else None,
                "context_size": settings.get("context_size") if running else None,
                # How much of the card other programs held at the moment this
                # model was launched (see llama_launch_vram) - the only figure
                # that can distinguish "your GPU was busy" from "this model is
                # too big for your GPU", since the live card-wide reading above
                # can't be attributed per-process on consumer GPUs.
                "launch_foreign_used_mb": (
                    (llama_launch_vram or {}).get("foreign_used_mb") if running else None
                ),
                "launch_free_mb": (llama_launch_vram or {}).get("free_mb") if running else None,
                # What "Automatic" model loading actually decided for the
                # running model: True = read fully into RAM (--no-mmap).
                "model_loaded_into_ram": llama_launch_load["into_ram"] if running else None,
                "foreign_vram_warn_mb": FOREIGN_VRAM_WARN_MB,
                # Weights on disk vs. the whole card: when this exceeds total_mb
                # no setting can fully offload the model, which the UI needs to
                # say outright rather than let the user keep tuning toward it.
                "model_mb": (
                    os.path.getsize(find_model()) / (1024 * 1024)
                    if running and find_model() else None
                ),
            })
        elif path == "/api/gpus":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            self.send_json(gpus_payload(load_settings(), refresh=(qs.get("refresh") or ["0"])[0] == "1"))
        elif path == "/api/backup":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            include_outputs = (qs.get("include_outputs") or ["0"])[0] == "1"
            self.handle_backup(include_outputs)
        elif path == "/api/conversations":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            search = (qs.get("search") or [""])[0].strip()
            with db() as conn:
                if search:
                    like = f"%{search}%"
                    rows = conn.execute(
                        "SELECT DISTINCT c.id, c.title, c.project_id, c.folder_id, c.pinned, c.sort_order, "
                        "c.created_at, c.updated_at FROM conversations c "
                        "LEFT JOIN messages m ON m.conversation_id = c.id "
                        "WHERE c.title LIKE ? OR m.content LIKE ? ORDER BY c.updated_at DESC",
                        (like, like),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id, title, project_id, folder_id, pinned, sort_order, created_at, updated_at "
                        "FROM conversations ORDER BY updated_at DESC"
                    ).fetchall()
            self.send_json([dict(r) for r in rows])
        elif re.fullmatch(r"/api/conversations/\d+", path):
            conv_id = int(path.rsplit("/", 1)[1])
            with db() as conn:
                conv = conn.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
                if conv is None:
                    self.send_json({"error": "not found"}, 404)
                    return
                # has_reasoning, not the reasoning text itself: a reasoning
                # model's thinking is routinely longer than its answer, and
                # this payload is re-fetched on every conversation switch,
                # while the Thoughts panel is opened rarely. The flag is all
                # the client needs to label its button; the text comes from
                # /thoughts on demand. Same shape as conversation_files'
                # SUBSTR snippet below.
                #
                # version_pos/version_count drive the "[<] 2/3 [>]" control on
                # a regenerated reply. Counted in the same statement (a
                # correlated subquery over an indexed column) rather than a
                # second round trip per message.
                msgs = conn.execute(
                    "SELECT id, role, content, created_at, version_pos, "
                    "(reasoning IS NOT NULL) AS has_reasoning, "
                    "1 + (SELECT COUNT(*) FROM message_versions v WHERE v.message_id=messages.id) "
                    "AS version_count "
                    "FROM messages WHERE conversation_id=? ORDER BY id",
                    (conv_id,),
                ).fetchall()
                msg_ids = [m["id"] for m in msgs]
                images_by_message = {}
                documents_by_message = {}
                if msg_ids:
                    placeholders = ",".join("?" * len(msg_ids))
                    img_rows = conn.execute(
                        f"SELECT id, message_id, filename, mime_type FROM message_images "
                        f"WHERE message_id IN ({placeholders}) AND version_id IS NULL ORDER BY id",
                        msg_ids,
                    ).fetchall()
                    for r in img_rows:
                        images_by_message.setdefault(r["message_id"], []).append(
                            {"id": r["id"], "filename": r["filename"], "mime_type": r["mime_type"]}
                        )
                    # The document cards (create_document), without their content - that comes from
                    # GET /api/documents/<id> when a card is opened, the same split as has_reasoning.
                    doc_rows = conn.execute(
                        f"SELECT id, message_id, title, filename, size, version, char_offset FROM documents "
                        f"WHERE message_id IN ({placeholders}) AND version_id IS NULL ORDER BY id",
                        msg_ids,
                    ).fetchall()
                    for r in doc_rows:
                        documents_by_message.setdefault(r["message_id"], []).append({
                            "id": r["id"], "title": r["title"], "filename": r["filename"], "bytes": r["size"],
                            "version": r["version"], "offset": r["char_offset"],
                        })
                conv_files = conn.execute(
                    "SELECT id, filename, size_bytes, added_at, content_tokens, "
                    "SUBSTR(content, 1, 300) AS content_snippet FROM conversation_files "
                    "WHERE conversation_id=? ORDER BY added_at",
                    (conv_id,),
                ).fetchall()
                files_out = [{**dict(f), "scope": "conversation"} for f in conv_files]
                if conv["project_id"]:
                    proj_files = conn.execute(
                        "SELECT id, filename, size_bytes, added_at FROM project_files "
                        "WHERE project_id=? ORDER BY added_at",
                        (conv["project_id"],),
                    ).fetchall()
                    files_out += [{**dict(f), "scope": "project"} for f in proj_files]
            msgs_out = [
                {**dict(m), "images": images_by_message.get(m["id"], []), "documents": documents_by_message.get(m["id"], [])}
                for m in msgs
            ]
            self.send_json({**dict(conv), "messages": msgs_out, "files": files_out})
        elif path == "/api/models":
            self.handle_list_models()
        elif path == "/api/models/download-progress":
            self.handle_download_progress()
        elif path == "/api/mmproj":
            self.handle_list_mmproj()
        elif path == "/api/mmproj/download-progress":
            self.handle_mmproj_download_progress()
        elif path == "/api/image-models":
            self.handle_list_image_models()
        elif path == "/api/image-models/download-progress":
            self.handle_image_model_download_progress()
        elif path == "/api/generate-image/status":
            self.handle_generate_image_status()
        elif path == "/api/images":
            self.handle_gallery_list()
        elif path.startswith("/api/images/"):
            self.handle_gallery_get(urllib.parse.unquote(path[len("/api/images/"):]))
        elif path == "/api/generation-progress":
            self.handle_generation_progress()
        elif re.fullmatch(r"/api/messages/\d+/images/\d+", path):
            parts = path.split("/")
            self.handle_get_message_image(int(parts[3]), int(parts[5]))
        elif re.fullmatch(r"/api/messages/\d+/images/\d+/export", path):
            parts = path.split("/")
            self.handle_export_image(int(parts[3]), int(parts[5]), save_to_disk=False)
        elif path == "/api/projects":
            self.handle_list_projects()
        elif re.fullmatch(r"/api/projects/\d+", path):
            project_id = int(path.rsplit("/", 1)[1])
            self.handle_get_project(project_id)
        elif re.fullmatch(r"/api/projects/\d+/images/\d+", path):
            parts = path.split("/")
            self.handle_get_project_image(int(parts[3]), int(parts[5]))
        elif path == "/api/folders":
            self.handle_list_folders()
        elif path == "/api/project-groups":
            self.handle_list_project_groups()
        elif re.fullmatch(r"/api/conversations/\d+/messages/\d+/export", path):
            parts = path.split("/")
            self.handle_export_message(int(parts[3]), int(parts[5]), save_to_disk=False)
        elif re.fullmatch(r"/api/conversations/\d+/export", path):
            conv_id = int(path.split("/")[3])
            self.handle_export_conversation(conv_id, save_to_disk=False)
        elif re.fullmatch(r"/api/conversations/\d+/context-usage", path):
            conv_id = int(path.split("/")[3])
            self.handle_context_usage(conv_id)
        elif re.fullmatch(r"/api/conversations/\d+/title-status", path):
            conv_id = int(path.split("/")[3])
            self.handle_title_status(conv_id)
        elif re.fullmatch(r"/api/conversations/\d+/thoughts", path):
            conv_id = int(path.split("/")[3])
            self.handle_thoughts(conv_id)
        elif re.fullmatch(r"/api/documents/\d+", path):
            self.handle_get_document(int(path.rsplit("/", 1)[1]))
        else:
            self.send_json({"error": "not found"}, 404)

    def _route_post(self):
        path = self.path.split("?")[0]
        if path == "/api/conversations":
            body = self.read_json()
            project_id = optional_id(body.get("project_id"), "project_id")
            folder_id = optional_id(body.get("folder_id"), "folder_id") if not project_id else None
            with db() as conn:
                if project_id:
                    cur = conn.execute(
                        "INSERT INTO conversations (project_id) VALUES (?)", (project_id,)
                    )
                elif folder_id:
                    cur = conn.execute(
                        "INSERT INTO conversations (folder_id) VALUES (?)", (folder_id,)
                    )
                else:
                    cur = conn.execute("INSERT INTO conversations DEFAULT VALUES")
                conv_id = cur.lastrowid
                conv = conn.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
            self.send_json(dict(conv), 201)
        elif path == "/api/chat":
            self.handle_chat()
        elif path == "/api/settings":
            body = self.read_json()
            for key, value in body.items():
                problem = settings_type_problem(key, value) if key in DEFAULT_SETTINGS else None
                if problem:
                    self.send_json({"error": problem}, 400)
                    return
            if "temperature" in body and not isinstance(body["temperature"], (int, float)):
                self.send_json({"error": "temperature must be a number"}, 400)
                return
            if "repeat_penalty" in body and not isinstance(body["repeat_penalty"], (int, float)):
                self.send_json({"error": "repeat_penalty must be a number"}, 400)
                return
            if "min_p" in body and not (
                isinstance(body["min_p"], (int, float)) and 0 <= body["min_p"] <= 1
            ):
                self.send_json({"error": "min_p must be a number between 0 and 1"}, 400)
                return
            if "top_p" in body and not (
                isinstance(body["top_p"], (int, float)) and 0 <= body["top_p"] <= 1
            ):
                self.send_json({"error": "top_p must be a number between 0 and 1"}, 400)
                return
            if "top_k" in body and not (
                isinstance(body["top_k"], (int, float)) and 0 <= body["top_k"] <= 1000
            ):
                self.send_json({"error": "top_k must be a number between 0 and 1,000"}, 400)
                return
            if "max_upload_mb" in body and not (
                isinstance(body["max_upload_mb"], (int, float)) and 1 <= body["max_upload_mb"] <= 500
            ):
                self.send_json({"error": "max_upload_mb must be a number between 1 and 500"}, 400)
                return
            if "max_file_tokens" in body and not (
                isinstance(body["max_file_tokens"], (int, float)) and 250 <= body["max_file_tokens"] <= 500_000
            ):
                self.send_json({"error": "max_file_tokens must be a number between 250 and 500,000"}, 400)
                return
            if "project_content_budget_pct" in body and not (
                isinstance(body["project_content_budget_pct"], (int, float))
                and 0.05 <= body["project_content_budget_pct"] <= 0.95
            ):
                self.send_json(
                    {"error": "project_content_budget_pct must be a number between 0.05 and 0.95"}, 400
                )
                return
            if "gpu_layers" in body and not (
                isinstance(body["gpu_layers"], (int, float)) and 0 <= body["gpu_layers"] <= 999
            ):
                self.send_json({"error": "gpu_layers must be a number between 0 and 999"}, 400)
                return
            if "max_parallel_chats" in body and not (
                isinstance(body["max_parallel_chats"], (int, float)) and 1 <= body["max_parallel_chats"] <= 8
            ):
                self.send_json({"error": "max_parallel_chats must be a number between 1 and 8"}, 400)
                return
            if "thinking_mode" in body and body["thinking_mode"] not in ("auto", "on", "off"):
                self.send_json({"error": "thinking_mode must be auto, on, or off"}, 400)
                return
            if "thinking_effort" in body and body["thinking_effort"] not in THINKING_EFFORT_LEVELS:
                self.send_json(
                    {"error": f"thinking_effort must be one of {list(THINKING_EFFORT_LEVELS)}"}, 400
                )
                return
            if "thinking_budget_auto" in body and not isinstance(body["thinking_budget_auto"], bool):
                self.send_json({"error": "thinking_budget_auto must be true or false"}, 400)
                return
            if "thinking_budget" in body and not (
                isinstance(body["thinking_budget"], (int, float)) and 0 <= body["thinking_budget"] <= 32768
            ):
                self.send_json({"error": "thinking_budget must be a number between 0 and 32,768"}, 400)
                return
            if "max_reply_tokens" in body:
                limit = body["max_reply_tokens"]
                if isinstance(limit, bool) or not (isinstance(limit, (int, float)) and 0 <= limit <= 1_000_000):
                    self.send_json({"error": "max_reply_tokens must be a number between 0 (no limit) and 1,000,000"}, 400)
                    return
                body["max_reply_tokens"] = int(limit)
            if "gpu_layers_auto" in body and not isinstance(body["gpu_layers_auto"], bool):
                self.send_json({"error": "gpu_layers_auto must be true or false"}, 400)
                return
            gpu_problem = gpu_settings_problem(body)
            if gpu_problem:
                self.send_json({"error": gpu_problem}, 400)
                return
            if "verbosity" in body and body["verbosity"] not in RESPONSE_LENGTH_LEVELS:
                self.send_json(
                    {"error": f"verbosity must be one of {RESPONSE_LENGTH_LEVELS}"}, 400
                )
                return
            if "dry_multiplier" in body and not (
                isinstance(body["dry_multiplier"], (int, float)) and 0 <= body["dry_multiplier"] <= 5
            ):
                self.send_json({"error": "dry_multiplier must be a number between 0 and 5"}, 400)
                return
            if "presence_penalty" in body and not (
                isinstance(body["presence_penalty"], (int, float)) and -2 <= body["presence_penalty"] <= 2
            ):
                self.send_json({"error": "presence_penalty must be a number between -2 and 2"}, 400)
                return
            if "frequency_penalty" in body and not (
                isinstance(body["frequency_penalty"], (int, float)) and -2 <= body["frequency_penalty"] <= 2
            ):
                self.send_json({"error": "frequency_penalty must be a number between -2 and 2"}, 400)
                return
            if "kv_offload" in body and not isinstance(body["kv_offload"], bool):
                self.send_json({"error": "kv_offload must be true or false"}, 400)
                return
            if "kv_cache_type" in body and body["kv_cache_type"] not in KV_CACHE_TYPES:
                self.send_json({"error": f"kv_cache_type must be one of {sorted(KV_CACHE_TYPES)}"}, 400)
                return
            if "batch_size" in body and not (
                isinstance(body["batch_size"], (int, float)) and 32 <= body["batch_size"] <= 8192
            ):
                self.send_json({"error": "batch_size must be a number between 32 and 8,192"}, 400)
                return
            if "model_load_mode" in body and body["model_load_mode"] not in MODEL_LOAD_MODES:
                self.send_json({"error": f"model_load_mode must be one of {list(MODEL_LOAD_MODES)}"}, 400)
                return
            if "mlock_model" in body and not isinstance(body["mlock_model"], bool):
                self.send_json({"error": "mlock_model must be true or false"}, 400)
                return
            with llama_restart_lock:
                settings = load_settings()
                if "context_size" in body:
                    if not isinstance(body["context_size"], (int, float)):
                        self.send_json({"error": "context_size must be a number"}, 400)
                        return
                    target_model = body.get("active_model", settings.get("active_model"))
                    ceiling = None
                    if target_model:
                        model_path = os.path.join(MODELS_DIR, safe_filename(target_model) or "")
                        if os.path.isfile(model_path):
                            ceiling = gguf_context_length(model_path)
                    ceiling = ceiling or 131072
                    body["context_size"] = max(512, min(ceiling, int(body["context_size"])))
                # --reasoning-budget is launch-only, and with
                # thinking_budget_auto on it's derived from the response
                # length - so rather than enumerate which keys can move it
                # (thinking_budget_auto, thinking_budget, verbosity, and any
                # future input), just compute the number both ways and see if
                # it actually changed. A no-op edit stays a no-op.
                prospective = dict(settings)
                for key in DEFAULT_SETTINGS:
                    if key in body:
                        prospective[key] = body[key]
                current_model = find_model()
                thinking_budget_changed = (
                    thinking_budget_tokens(prospective, current_model)
                    != thinking_budget_tokens(settings, current_model)
                )
                needs_restart = (
                    thinking_budget_changed
                    or ("context_size" in body and int(body["context_size"]) != int(settings.get("context_size", 16384)))
                    or (
                        "max_parallel_chats" in body
                        and int(body["max_parallel_chats"]) != int(settings.get("max_parallel_chats", 1))
                    )
                    or (
                        "gpu_layers_auto" in body
                        and bool(body["gpu_layers_auto"]) != bool(settings.get("gpu_layers_auto", True))
                    )
                    # Only a real restart trigger while the manual override is
                    # in force - under Auto the number isn't passed to
                    # llama-server at all, so changing it changes nothing.
                    or (
                        "gpu_layers" in body
                        and not body.get("gpu_layers_auto", settings.get("gpu_layers_auto", True))
                        and int(body["gpu_layers"]) != int(settings.get("gpu_layers", 99))
                    )
                    or ("kv_offload" in body and bool(body["kv_offload"]) != bool(settings.get("kv_offload", True)))
                    or ("kv_cache_type" in body and body["kv_cache_type"] != settings.get("kv_cache_type", "q8_0"))
                    or ("active_model" in body and body["active_model"] != settings.get("active_model"))
                    or ("batch_size" in body and int(body["batch_size"]) != int(settings.get("batch_size", 2048)))
                    or (
                        "model_load_mode" in body
                        and body["model_load_mode"] != settings.get("model_load_mode", "auto")
                    )
                    or (
                        "mlock_model" in body
                        and bool(body["mlock_model"]) != bool(settings.get("mlock_model", False))
                    )
                    # Which GPUs and which engine build: only a change that alters what a launch would do counts (see
                    # gpu_config_signature) - not, say, reordering GPUs that are not installed.
                    or (
                        any(k in body for k in ("gpu_placement", "gpu_order", "gpu_disabled", "engine"))
                        and gpu_config_signature(prospective) != gpu_config_signature(settings)
                    )
                )
                if needs_restart and (any_chat_active() or image_gen_status["active"] or compact_active):
                    self.send_json(
                        {"error": "cannot change these settings while a response is generating"}, 409
                    )
                    return
                for key in DEFAULT_SETTINGS:
                    if key in body:
                        settings[key] = body[key]
                save_settings(settings)
                if needs_restart:
                    stop_llama()
                    actual_gpu_layers = start_llama()
                    # "auto" (llama.cpp's --fit sized the offload) carries no
                    # number to report, and gpu_layers stays an int over the
                    # wire - leave the manual-override value as-is in that
                    # case rather than shipping a string the client's numeric
                    # widget can't take.
                    if isinstance(actual_gpu_layers, int):
                        # start_llama() may have clamped gpu_layers to fit
                        # free VRAM (safe_gpu_layers) - it already persisted
                        # that via its own save_settings() call, but this
                        # handler's own `settings`/response snapshot was
                        # taken before that happened, so without this it
                        # would report back whatever the client originally
                        # asked for instead of what's actually running. A
                        # client that trusted the stale response (as this
                        # app's desktop client used to) would keep re-
                        # sending the wrong value on every later save,
                        # forcing a real, pointless model reload each time.
                        settings["gpu_layers"] = actual_gpu_layers
            # _restart_triggered is transient response metadata, not a real
            # setting - built into a copy so it's never written to disk by
            # a later save_settings(settings) call elsewhere. The desktop
            # client used to predict needs_restart itself from a snapshot
            # taken whenever Settings was last opened, which could go stale
            # (e.g. safe_gpu_layers's own auto-clamp changes gpu_layers
            # server-side during an unrelated model switch, with no signal
            # back to an already-open Settings screen) - it now defers to
            # this field as the sole authority instead of guessing.
            response = dict(settings)
            response["_restart_triggered"] = needs_restart
            self.send_json(response)
        elif path == "/api/settings/estimate-vram":
            self.handle_estimate_vram()
        elif path == "/api/gpus/plan":
            self.handle_gpu_plan()
        elif path == "/api/shutdown":
            self.send_json({"ok": True})
            threading.Thread(target=shutdown, daemon=True).start()
        elif path == "/api/show-window":
            gui_events.put("show")
            # "root": which folder this copy was started from, so a second launch from ANOTHER folder (a copy on another drive)
            # can say so instead of silently doing nothing (desktop_main._second_launch).
            self.send_json({"ok": True, "root": ROOT})
        elif path == "/api/reset":
            self.handle_reset_data()
        elif path == "/api/restore":
            self.handle_restore()
        elif path == "/api/model-server/restart":
            self.handle_restart_model_server()
        elif path == "/api/models/active":
            self.handle_set_active_model()
        elif path == "/api/models/upload":
            self.handle_upload_model()
        elif path == "/api/models/download":
            self.handle_download_model()
        elif path == "/api/mmproj/active":
            self.handle_set_active_mmproj()
        elif path == "/api/mmproj/upload":
            self.handle_upload_mmproj()
        elif path == "/api/mmproj/download":
            self.handle_download_mmproj()
        elif path == "/api/image-models/active":
            self.handle_set_active_image_model()
        elif path == "/api/image-models/upload":
            self.handle_upload_image_model()
        elif path == "/api/image-models/download":
            self.handle_download_image_model()
        elif path == "/api/generate-image":
            self.handle_generate_image()
        elif path == "/api/images/generate":
            self.handle_gallery_generate()
        elif path == "/api/edit-image":
            self.handle_edit_image()
        elif path == "/api/generate-image/cancel":
            self.handle_cancel_image_gen()
        elif path == "/api/projects":
            self.handle_create_project()
        elif re.fullmatch(r"/api/projects/\d+/files", path):
            project_id = int(path.split("/")[3])
            self.handle_upload_project_file(project_id)
        elif re.fullmatch(r"/api/projects/\d+/images", path):
            project_id = int(path.split("/")[3])
            self.handle_upload_project_image(project_id)
        elif re.fullmatch(r"/api/projects/\d+/memory", path):
            project_id = int(path.split("/")[3])
            self.handle_add_project_memory(project_id)
        elif path == "/api/folders":
            self.handle_create_folder()
        elif path == "/api/project-groups":
            self.handle_create_project_group()
        elif re.fullmatch(r"/api/conversations/\d+/compact", path):
            conv_id = int(path.split("/")[3])
            self.handle_compact(conv_id)
        elif re.fullmatch(r"/api/conversations/\d+/files", path):
            conv_id = int(path.split("/")[3])
            self.handle_upload_conversation_file(conv_id)
        elif re.fullmatch(r"/api/conversations/\d+/messages/\d+/version", path):
            parts = path.split("/")
            self.handle_switch_message_version(int(parts[3]), int(parts[5]))
        elif re.fullmatch(r"/api/conversations/\d+/messages/\d+/export", path):
            parts = path.split("/")
            self.handle_export_message(int(parts[3]), int(parts[5]), save_to_disk=True)
        elif re.fullmatch(r"/api/conversations/\d+/export", path):
            conv_id = int(path.split("/")[3])
            self.handle_export_conversation(conv_id, save_to_disk=True)
        elif re.fullmatch(r"/api/messages/\d+/images/\d+/export", path):
            parts = path.split("/")
            self.handle_export_image(int(parts[3]), int(parts[5]), save_to_disk=True)
        else:
            self.send_json({"error": "not found"}, 404)

    def _route_patch(self):
        path = self.path.split("?")[0]
        if re.fullmatch(r"/api/conversations/\d+", path):
            conv_id = int(path.rsplit("/", 1)[1])
            body = self.read_json()
            updates, params = [], []
            if "title" in body:
                title = str(body.get("title", "")).strip()[:120]
                if not title:
                    self.send_json({"error": "title required"}, 400)
                    return
                updates.append("title=?")
                params.append(title)
            if "pinned" in body:
                updates.append("pinned=?")
                params.append(1 if body["pinned"] else 0)
            if "folder_id" in body:
                updates.append("folder_id=?")
                params.append(optional_id(body["folder_id"], "folder_id"))
            if "sort_order" in body:
                updates.append("sort_order=?")
                params.append(number_field(body, "sort_order", 0))
            if "draft" in body:
                draft = body["draft"]
                if draft is not None and not isinstance(draft, str):
                    self.send_json({"error": "draft must be a string or null"}, 400)
                    return
                if draft is not None and len(draft) > MAX_DRAFT_CHARS:
                    self.send_json(
                        {"error": f"draft must be at most {MAX_DRAFT_CHARS:,} characters"}, 400
                    )
                    return
                # Normalized to NULL when empty so "has an unsent draft" is a
                # plain NULL check and cleared composers don't leave rows
                # holding empty strings.
                updates.append("draft=?")
                params.append(draft or None)
            if not updates:
                self.send_json({"error": "nothing to update"}, 400)
                return
            params.append(conv_id)
            with db() as conn:
                cur = conn.execute(f"UPDATE conversations SET {', '.join(updates)} WHERE id=?", params)
                if cur.rowcount == 0:
                    self.send_json({"error": "not found"}, 404)
                    return
            self.send_json({"ok": True})
        elif re.fullmatch(r"/api/projects/\d+", path):
            project_id = int(path.rsplit("/", 1)[1])
            self.handle_update_project(project_id)
        elif re.fullmatch(r"/api/folders/\d+", path):
            folder_id = int(path.rsplit("/", 1)[1])
            self.handle_update_folder(folder_id)
        elif re.fullmatch(r"/api/project-groups/\d+", path):
            group_id = int(path.rsplit("/", 1)[1])
            self.handle_update_project_group(group_id)
        elif re.fullmatch(r"/api/conversations/\d+/messages/\d+", path):
            parts = path.split("/")
            self.handle_edit_message(int(parts[3]), int(parts[5]))
        else:
            self.send_json({"error": "not found"}, 404)

    def _route_delete(self):
        path = self.path.split("?")[0]
        if re.fullmatch(r"/api/documents/\d+", path):
            self.handle_delete_document(int(path.rsplit("/", 1)[1]))
        elif re.fullmatch(r"/api/conversations/\d+", path):
            conv_id = int(path.rsplit("/", 1)[1])
            with db() as conn:
                # PRAGMA foreign_keys is never enabled anywhere in this codebase,
                # so the schema's ON DELETE CASCADE for message_images/
                # conversation_files is inert - both must be deleted explicitly
                # or they're orphaned forever (confirmed real: this was silently
                # leaking every attached image/file on every conversation delete
                # before this fix, same root cause as the reset-feature bug).
                conn.execute(
                    "DELETE FROM message_images WHERE message_id IN "
                    "(SELECT id FROM messages WHERE conversation_id=?)",
                    (conv_id,),
                )
                conn.execute(
                    "DELETE FROM message_versions WHERE message_id IN "
                    "(SELECT id FROM messages WHERE conversation_id=?)",
                    (conv_id,),
                )
                conn.execute("DELETE FROM documents WHERE conversation_id=?", (conv_id,))
                conn.execute("DELETE FROM messages WHERE conversation_id=?", (conv_id,))
                conn.execute("DELETE FROM conversation_files WHERE conversation_id=?", (conv_id,))
                conn.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
            self.send_json({"ok": True})
        elif re.fullmatch(r"/api/projects/\d+/files/\d+", path):
            parts = path.split("/")
            self.handle_delete_project_file(int(parts[3]), int(parts[5]))
        elif re.fullmatch(r"/api/projects/\d+/images/\d+", path):
            parts = path.split("/")
            self.handle_delete_project_image(int(parts[3]), int(parts[5]))
        elif re.fullmatch(r"/api/projects/\d+/memory/\d+", path):
            parts = path.split("/")
            self.handle_delete_project_memory(int(parts[3]), int(parts[5]))
        elif re.fullmatch(r"/api/projects/\d+", path):
            project_id = int(path.rsplit("/", 1)[1])
            self.handle_delete_project(project_id)
        elif re.fullmatch(r"/api/folders/\d+", path):
            folder_id = int(path.rsplit("/", 1)[1])
            self.handle_delete_folder(folder_id)
        elif re.fullmatch(r"/api/project-groups/\d+", path):
            group_id = int(path.rsplit("/", 1)[1])
            self.handle_delete_project_group(group_id)
        elif re.fullmatch(r"/api/conversations/\d+/files/\d+", path):
            parts = path.split("/")
            self.handle_delete_conversation_file(int(parts[3]), int(parts[5]))
        elif path.startswith("/api/mmproj/"):
            filename = urllib.parse.unquote(path[len("/api/mmproj/"):])
            self.handle_delete_mmproj(filename)
        elif path.startswith("/api/images/"):
            self.handle_gallery_delete(urllib.parse.unquote(path[len("/api/images/"):]))
        elif path.startswith("/api/image-models/"):
            filename = urllib.parse.unquote(path[len("/api/image-models/"):])
            self.handle_delete_image_model(filename)
        elif path.startswith("/api/models/"):
            filename = urllib.parse.unquote(path[len("/api/models/"):])
            self.handle_delete_model(filename)
        else:
            self.send_json({"error": "not found"}, 404)

    # ---- model management

    def handle_list_models(self):
        active_path = find_model()
        active_name = os.path.basename(active_path) if active_path else None
        models = []
        if os.path.isdir(MODELS_DIR):
            try:
                names = os.listdir(MODELS_DIR)
            except OSError as e:
                # a folder that is there but cannot be read (permissions) is not an empty one: say so
                self.send_json({"error": f"cannot read the models folder {MODELS_DIR} ({e.strerror or e}) - check its permissions"}, 500)
                return
            for f in names:
                if f.lower().endswith(".gguf"):
                    full = os.path.join(MODELS_DIR, f)
                    cap = model_thinking_capability(full)
                    models.append({
                        "filename": f,
                        "size_bytes": os.path.getsize(full),
                        "active": f == active_name,
                        # Enough for the picker to badge each row without a
                        # second round trip per model. Reading the template is
                        # a small seek into the header, and it's cached by
                        # (path, mtime) - see gguf_chat_template.
                        "thinking": cap["supports"],
                        "thinking_label": cap["label"],
                    })
        models.sort(key=lambda m: m["filename"].lower())
        self.send_json({"models": models, "active": active_name, "dir": MODELS_DIR})    # "dir": where a file goes

    def handle_estimate_vram(self):
        """Rough VRAM projection for draft (unsaved) Advanced Settings values, so the
        UI can warn before the user saves settings that would overflow their card —
        the global gpu_layers setting has no per-model safety net otherwise."""
        body = self.read_json()
        settings = load_settings()
        filename = safe_filename(body.get("model")) or settings.get("active_model")
        model_path = os.path.join(MODELS_DIR, filename) if filename else None
        if not model_path or not os.path.isfile(model_path):
            model_path = find_model()
        if model_path is None:
            self.send_json({"error": "no model available to estimate against"}, 404)
            return
        gpu_layers = body.get("gpu_layers", settings.get("gpu_layers", 99))
        # Under Auto there is no layer count to project - llama.cpp decides at
        # load time against its own allocator. Full offload is the right thing
        # to show: it's the ceiling --fit aims for, so the meter answers "will
        # this model fit entirely on the card?", which is the question that
        # actually matters now that partial offload is no longer this app's
        # doing. The flag goes back in the response so the client can label it
        # rather than present it as a prediction of the real figure.
        auto_layers = bool(body.get("gpu_layers_auto", settings.get("gpu_layers_auto", True)))
        if auto_layers:
            gpu_layers = (gguf_block_count(model_path) or 0) + 1
        context_size = body.get("context_size", settings.get("context_size", 16384))
        kv_cache_type = body.get("kv_cache_type", settings.get("kv_cache_type", "q8_0"))
        kv_offload = body.get("kv_offload", settings.get("kv_offload", True))
        parallel = max(1, int(body.get("max_parallel_chats", max_parallel_chats(settings))))
        # Same total-vs-per-chat distinction as start_llama() - the real KV
        # cache cost scales with what actually gets passed as -c.
        total_context_size = context_size * parallel
        estimate = estimate_vram_mb(model_path, gpu_layers, total_context_size, kv_cache_type, kv_offload)
        vram = query_vram_mb()
        self.send_json({
            "estimated_mb_low": estimate["low"] if estimate else None,
            "estimated_mb_high": estimate["high"] if estimate else None,
            "uses_sliding_window": bool(estimate and estimate["low"] != estimate["high"]),
            "block_count": gguf_block_count(model_path),
            "kv_cache_layers": gguf_kv_cache_layers(model_path),
            "gpu_layers_auto": auto_layers,
            "vram_available": vram is not None,
            **(vram or {}),
        })

    def handle_gpu_plan(self):
        """Where the model would go under the given (possibly unsaved) GPU settings, for the Settings screen: the placement plan, and
        the GPUs in the order they would be used. Asked for a model that does not fit on the first GPU alone, the first answer takes a
        few seconds (a handful of fit-tool probes); the same question again is answered from a cache."""
        body = self.read_json()
        settings = load_settings()
        prospective = dict(settings)
        keys = ("gpu_placement", "gpu_order", "gpu_disabled", "engine", "context_size", "kv_cache_type", "kv_offload", "batch_size", "max_parallel_chats")
        for key in keys:
            if key in body:
                prospective[key] = body[key]
        problem = gpu_settings_problem(prospective)
        for key in keys:
            if problem is None and key in body:
                problem = settings_type_problem(key, body[key])
        if problem:
            self.send_json({"error": problem}, 400)
            return
        filename = safe_filename(body.get("model")) or settings.get("active_model")
        model_path = os.path.join(MODELS_DIR, filename) if filename else None
        if not model_path or not os.path.isfile(model_path):
            model_path = find_model()
        if model_path is None:
            self.send_json({"error": "no model available to plan for"}, 404)
            return
        plan, inv = compute_gpu_plan(prospective, model_path, find_mmproj(), force_inventory=body.get("refresh") is True, preview=True)
        self.send_json({
            "available": inv is not None,
            "plan": plan,
            "devices": [public_gpu(d) for d in inv["devices"]] if inv else [],
            "engine": inv["engine"]["name"] if inv and inv["engine"] else None,
        })

    def handle_set_active_model(self):
        body = self.read_json()
        filename = safe_filename(body.get("filename"))
        if not filename:
            self.send_json({"error": "filename required"}, 400)
            return
        model_path = os.path.join(MODELS_DIR, filename)
        if not os.path.isfile(model_path):
            self.send_json({"error": "model not found"}, 404)
            return
        if any_chat_active() or compact_active:
            self.send_json({"error": "cannot switch models while a response is generating"}, 409)
            return
        with llama_restart_lock:
            settings = load_settings()
            mmproj_cleared = False
            active_mmproj = settings.get("active_mmproj")
            if active_mmproj:
                mmproj_path = os.path.join(MMPROJ_DIR, active_mmproj)
                model_dim = gguf_embedding_length(model_path)
                proj_dim = gguf_mmproj_projection_dim(mmproj_path) if os.path.isfile(mmproj_path) else None
                if model_dim is not None and proj_dim is not None and model_dim != proj_dim:
                    settings["active_mmproj"] = None
                    mmproj_cleared = True
            stop_llama()
            settings["active_model"] = filename
            save_settings(settings)
            start_llama()
        self.send_json({"ok": True, "mmproj_cleared": mmproj_cleared})

    def handle_restart_model_server(self):
        """A2: the user's RESTART (Home's status row, Settings > Model). Refused with a sentence the desktop shows as a
        denied notice while an image job holds the GPU or a reply is generating; else the chat model server is stopped
        and started again with the current settings - the same path as Set active, so the desktop follows it with the
        same load overlay and health poll. Before, the only restart from the window was Set active on the same model,
        and nothing restarted a server that had died."""
        if image_gen_status["active"]:
            self.send_json({"error": "Can't restart the model server while an image is generating - try again when it finishes."}, 409)
            return
        if any_chat_active() or compact_active:
            self.send_json({"error": "Can't restart the model server while a reply is generating - try again when it finishes."}, 409)
            return
        with llama_restart_lock:
            stop_llama()
            start_llama()
        self.send_json({"ok": True})

    def handle_upload_model(self):
        filename = safe_filename(urllib.parse.unquote(self.headers.get("X-Filename", "")))
        if not filename or not filename.lower().endswith(".gguf"):
            self.send_json({"error": "X-Filename header with a .gguf filename is required"}, 400)
            return
        length = self._content_length()
        if length <= 0:
            self.send_json({"error": "empty body"}, 400)
            return
        os.makedirs(MODELS_DIR, exist_ok=True)
        tmp_path = os.path.join(MODELS_DIR, filename + ".part")
        final_path = os.path.join(MODELS_DIR, filename)
        remaining = length
        try:
            with open(tmp_path, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(DOWNLOAD_CHUNK, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            os.replace(tmp_path, final_path)
        except OSError as e:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            self.send_json({"error": str(e)}, 500)
            return
        self.send_json({"ok": True, "filename": filename})

    def handle_download_model(self):
        body = self.read_json()
        url = str(body.get("url", "")).strip()
        filename = safe_filename(body.get("filename"))
        if not url or not filename or not filename.lower().endswith(".gguf"):
            self.send_json({"error": "url and a .gguf filename are required"}, 400)
            return
        if not (url.startswith("http://") or url.startswith("https://")):
            self.send_json({"error": "url must be http(s)"}, 400)
            return
        with download_progress_lock:
            download_progress[filename] = {"downloaded": 0, "total": 0, "done": False, "error": None}
        threading.Thread(target=download_model_worker, args=(url, filename), daemon=True).start()
        self.send_json({"ok": True, "filename": filename})

    def handle_download_progress(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        filename = (qs.get("filename") or [""])[0]
        with download_progress_lock:
            progress = download_progress.get(filename)
        if progress is None:
            self.send_json({"error": "unknown download"}, 404)
            return
        self.send_json(progress)

    def handle_delete_model(self, filename):
        filename = safe_filename(filename)
        if not filename:
            self.send_json({"error": "bad filename"}, 400)
            return
        active_path = find_model()
        active_name = os.path.basename(active_path) if active_path else None
        if filename == active_name:
            self.send_json({"error": "cannot delete the active model; switch to another model first"}, 400)
            return
        if any_chat_active():
            self.send_json({"error": "cannot delete a model while a response is generating"}, 409)
            return
        full = os.path.join(MODELS_DIR, filename)
        if not os.path.isfile(full):
            self.send_json({"error": "not found"}, 404)
            return
        os.remove(full)
        self.send_json({"ok": True})

    # ---- vision (mmproj)

    def handle_list_mmproj(self):
        settings = load_settings()
        active_name = settings.get("active_mmproj")
        files = []
        if os.path.isdir(MMPROJ_DIR):
            try:
                names = os.listdir(MMPROJ_DIR)
            except OSError as e:
                self.send_json({"error": f"cannot read the vision folder {MMPROJ_DIR} ({e.strerror or e}) - check its permissions"}, 500)
                return
            for f in names:
                if f.lower().endswith(".gguf"):
                    full = os.path.join(MMPROJ_DIR, f)
                    files.append(
                        {"filename": f, "size_bytes": os.path.getsize(full), "active": f == active_name}
                    )
        files.sort(key=lambda m: m["filename"].lower())
        self.send_json({"models": files, "active": active_name, "dir": MMPROJ_DIR})

    def handle_set_active_mmproj(self):
        body = self.read_json()
        filename = safe_filename(body.get("filename"))  # may be None/"" to disable vision
        if filename and not os.path.isfile(os.path.join(MMPROJ_DIR, filename)):
            self.send_json({"error": "mmproj file not found"}, 404)
            return
        if filename:
            model = find_model()
            if model:
                model_dim = gguf_embedding_length(model)
                proj_dim = gguf_mmproj_projection_dim(os.path.join(MMPROJ_DIR, filename))
                if model_dim is not None and proj_dim is not None and model_dim != proj_dim:
                    self.send_json({
                        "error": (
                            "this vision file doesn't match the active model's family "
                            f"(model embedding size {model_dim} vs vision file {proj_dim}) — "
                            "pick a vision file made for this model"
                        )
                    }, 400)
                    return
        if any_chat_active() or compact_active:
            self.send_json({"error": "cannot switch models while a response is generating"}, 409)
            return
        with llama_restart_lock:
            stop_llama()
            settings = load_settings()
            settings["active_mmproj"] = filename or None
            save_settings(settings)
            start_llama()
        self.send_json({"ok": True})

    def handle_upload_mmproj(self):
        filename = safe_filename(urllib.parse.unquote(self.headers.get("X-Filename", "")))
        if not filename or not filename.lower().endswith(".gguf"):
            self.send_json({"error": "X-Filename header with a .gguf filename is required"}, 400)
            return
        length = self._content_length()
        if length <= 0:
            self.send_json({"error": "empty body"}, 400)
            return
        os.makedirs(MMPROJ_DIR, exist_ok=True)
        tmp_path = os.path.join(MMPROJ_DIR, filename + ".part")
        final_path = os.path.join(MMPROJ_DIR, filename)
        remaining = length
        try:
            with open(tmp_path, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(DOWNLOAD_CHUNK, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            os.replace(tmp_path, final_path)
        except OSError as e:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            self.send_json({"error": str(e)}, 500)
            return
        self.send_json({"ok": True, "filename": filename})

    def handle_download_mmproj(self):
        body = self.read_json()
        url = str(body.get("url", "")).strip()
        filename = safe_filename(body.get("filename"))
        if not url or not filename or not filename.lower().endswith(".gguf"):
            self.send_json({"error": "url and a .gguf filename are required"}, 400)
            return
        if not (url.startswith("http://") or url.startswith("https://")):
            self.send_json({"error": "url must be http(s)"}, 400)
            return
        progress_key = f"mmproj:{filename}"
        with download_progress_lock:
            download_progress[progress_key] = {"downloaded": 0, "total": 0, "done": False, "error": None}
        threading.Thread(
            target=download_model_worker,
            args=(url, filename),
            kwargs={"dest_dir": MMPROJ_DIR, "progress_key": progress_key},
            daemon=True,
        ).start()
        self.send_json({"ok": True, "filename": filename})

    def handle_mmproj_download_progress(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        filename = (qs.get("filename") or [""])[0]
        with download_progress_lock:
            progress = download_progress.get(f"mmproj:{filename}")
        if progress is None:
            self.send_json({"error": "unknown download"}, 404)
            return
        self.send_json(progress)

    def handle_delete_mmproj(self, filename):
        filename = safe_filename(filename)
        if not filename:
            self.send_json({"error": "bad filename"}, 400)
            return
        settings = load_settings()
        if filename == settings.get("active_mmproj"):
            self.send_json({"error": "cannot delete the active vision file; switch away from it first"}, 400)
            return
        if any_chat_active():
            self.send_json({"error": "cannot delete a file while a response is generating"}, 409)
            return
        full = os.path.join(MMPROJ_DIR, filename)
        if not os.path.isfile(full):
            self.send_json({"error": "not found"}, 404)
            return
        os.remove(full)
        self.send_json({"ok": True})

    def handle_get_message_image(self, message_id, image_id):
        with db() as conn:
            row = conn.execute(
                "SELECT filename, mime_type, data FROM message_images WHERE id=? AND message_id=?",
                (image_id, message_id),
            ).fetchone()
        if row is None:
            self.send_json({"error": "not found"}, 404)
            return
        data = row["data"]
        self.send_response(200)
        self.send_header("Content-Type", row["mime_type"])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(data)

    def handle_export_image(self, message_id, image_id, save_to_disk):
        with db() as conn:
            row = conn.execute(
                "SELECT filename, mime_type, data FROM message_images WHERE id=? AND message_id=?",
                (image_id, message_id),
            ).fetchone()
        if row is None:
            self.send_json({"error": "image not found"}, 404)
            return
        ext = mimetypes.guess_extension(row["mime_type"]) or os.path.splitext(row["filename"] or "")[1] or ".png"
        stem = os.path.splitext(row["filename"] or "image")[0]
        safe_stem = re.sub(r"[^\w\-]+", "_", stem).strip("_")[:60] or "image"
        filename = f"{safe_stem}-{time.strftime('%Y%m%d-%H%M%S')}{ext}"
        data = row["data"]
        if save_to_disk:
            path = os.path.join(outputs_subdir("images"), filename)
            with open(path, "wb") as f:
                f.write(data)
            self.send_json({"ok": True, "path": os.path.relpath(path, ROOT).replace(os.sep, "/")})
        else:
            self.send_response(200)
            self.send_header("Content-Type", row["mime_type"])
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    # ---- image generation (stable-diffusion.cpp)

    def handle_list_image_models(self):
        settings = load_settings()
        active_name = settings.get("active_image_model")
        files = []
        if os.path.isdir(SD_MODELS_DIR):
            try:
                names = os.listdir(SD_MODELS_DIR)
            except OSError as e:
                self.send_json({"error": f"cannot read the image models folder {SD_MODELS_DIR} ({e.strerror or e}) - check its permissions"}, 500)
                return
            for f in names:
                if f.lower().endswith((".gguf", ".safetensors", ".ckpt")):
                    full = os.path.join(SD_MODELS_DIR, f)
                    kind, why = image_model_kind(full)
                    files.append(
                        {"filename": f, "size_bytes": os.path.getsize(full), "active": f == active_name, "kind": kind, "note": why}
                    )
        files.sort(key=lambda m: m["filename"].lower())
        self.send_json({"models": files, "active": active_name, "dir": SD_MODELS_DIR})

    def handle_set_active_image_model(self):
        body = self.read_json()
        filename = safe_filename(body.get("filename"))
        if filename and not os.path.isfile(os.path.join(SD_MODELS_DIR, filename)):
            self.send_json({"error": "image model not found"}, 404)
            return
        if filename:
            problem = image_model_problem(os.path.join(SD_MODELS_DIR, filename))
            if problem:
                self.send_json({"error": problem}, 400)
                return
        if any_chat_active() or image_gen_status["active"]:
            self.send_json({"error": "cannot switch models while busy"}, 409)
            return
        if image_model_resident:
            self.send_json(
                {
                    "error": "an image model is currently loaded for fast follow-ups - "
                    "send a chat message first to release it, or wait a few minutes"
                },
                409,
            )
            return
        settings = load_settings()
        settings["active_image_model"] = filename or None
        save_settings(settings)
        self.send_json({"ok": True})

    def handle_upload_image_model(self):
        filename = safe_filename(urllib.parse.unquote(self.headers.get("X-Filename", "")))
        if not filename or not filename.lower().endswith((".gguf", ".safetensors", ".ckpt")):
            self.send_json({"error": "X-Filename header with a .gguf/.safetensors/.ckpt filename is required"}, 400)
            return
        length = self._content_length()
        if length <= 0:
            self.send_json({"error": "empty body"}, 400)
            return
        os.makedirs(SD_MODELS_DIR, exist_ok=True)
        tmp_path = os.path.join(SD_MODELS_DIR, filename + ".part")
        final_path = os.path.join(SD_MODELS_DIR, filename)
        remaining = length
        try:
            with open(tmp_path, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(DOWNLOAD_CHUNK, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            os.replace(tmp_path, final_path)
        except OSError as e:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            self.send_json({"error": str(e)}, 500)
            return
        self.send_json({"ok": True, "filename": filename})

    def handle_download_image_model(self):
        body = self.read_json()
        url = str(body.get("url", "")).strip()
        filename = safe_filename(body.get("filename"))
        if not url or not filename or not filename.lower().endswith((".gguf", ".safetensors", ".ckpt")):
            self.send_json({"error": "url and a .gguf/.safetensors/.ckpt filename are required"}, 400)
            return
        if not (url.startswith("http://") or url.startswith("https://")):
            self.send_json({"error": "url must be http(s)"}, 400)
            return
        progress_key = f"image:{filename}"
        with download_progress_lock:
            download_progress[progress_key] = {"downloaded": 0, "total": 0, "done": False, "error": None}
        threading.Thread(
            target=download_model_worker,
            args=(url, filename),
            kwargs={"dest_dir": SD_MODELS_DIR, "progress_key": progress_key},
            daemon=True,
        ).start()
        self.send_json({"ok": True, "filename": filename})

    def handle_image_model_download_progress(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        filename = (qs.get("filename") or [""])[0]
        with download_progress_lock:
            progress = download_progress.get(f"image:{filename}")
        if progress is None:
            self.send_json({"error": "unknown download"}, 404)
            return
        self.send_json(progress)

    def handle_delete_image_model(self, filename):
        filename = safe_filename(filename)
        if not filename:
            self.send_json({"error": "bad filename"}, 400)
            return
        settings = load_settings()
        if filename == settings.get("active_image_model"):
            self.send_json({"error": "cannot delete the active image model; switch away from it first"}, 400)
            return
        if any_chat_active() or image_gen_status["active"]:
            self.send_json({"error": "cannot delete a model while busy"}, 409)
            return
        full = os.path.join(SD_MODELS_DIR, filename)
        if image_model_resident and sd_resident_model_path == full:
            self.send_json(
                {"error": "this model is currently loaded - send a chat message first to release it"}, 409
            )
            return
        if not os.path.isfile(full):
            self.send_json({"error": "not found"}, 404)
            return
        os.remove(full)
        self.send_json({"ok": True})

    def handle_generate_image(self):
        body = self.read_json()
        conv_id = optional_id(body.get("conversation_id"), "conversation_id")
        prompt = text_field(body, "prompt").strip()
        if not conv_id or not prompt:
            self.send_json({"error": "conversation_id and prompt are required"}, 400)
            return
        settings = load_settings()
        use_comfyui = settings.get("image_backend") == "comfyui"
        if use_comfyui:
            if not settings.get("comfyui_url"):
                self.send_json({"error": "no ComfyUI URL is configured — add one in Settings first"}, 400)
                return
            workflow_path = settings.get("comfyui_workflow_path") or ""
            if not workflow_path or not os.path.isfile(workflow_path):
                self.send_json({"error": "no ComfyUI workflow file is configured — add one in Settings first"}, 400)
                return
        elif not find_image_model():
            self.send_json({"error": "no image model is configured — add one in Settings first"}, 400)
            return
        with image_gen_lock:
            if any_chat_active() or image_gen_status["active"] or compact_active:
                self.send_json({"error": "busy — try again in a moment"}, 409)
                return
            image_gen_status["active"] = True
            # ComfyUI is an externally-run process this app never launches -
            # there's no local model to unload/load first, so this job
            # starts straight in the "generating" stage.
            image_gen_status["stage"] = "generating" if use_comfyui else "unloading_chat_model"
            image_gen_status["error"] = None
            image_gen_status["cancelled"] = False
        cancel_pending_titles()  # the chat model is about to be unloaded under any title request
        with db() as conn:
            conv = conn.execute("SELECT id FROM conversations WHERE id=?", (conv_id,)).fetchone()
            if conv is None:
                with image_gen_lock:
                    image_gen_status["active"] = False
                self.send_json({"error": "conversation not found"}, 404)
                return
        worker = comfyui_generate_worker if use_comfyui else generate_image_worker
        threading.Thread(target=worker, args=(conv_id, prompt), daemon=True).start()
        self.send_json({"ok": True})

    def handle_generate_image_status(self):
        self.send_json(image_gen_status)

    def handle_cancel_image_gen(self):
        """sd-server's own /sdcpp/v1/capabilities reports
        "cancel_generating": false - it has no in-place way to abort a
        request it's actively computing, only queued ones - so the only real
        way to interrupt the worker's blocking urllib call is to kill the
        process out from under it. That raises inside the worker (a broken
        connection), landing in its existing except-block recovery (restores
        the chat model) same as any other failure - cancelled=True here just
        lets it report "cancelled" instead of a raw connection-error string."""
        with image_gen_lock:
            if not image_gen_status["active"]:
                self.send_json({"error": "no image generation is in progress"}, 409)
                return
            image_gen_status["cancelled"] = True
        stop_sd_server()
        self.send_json({"ok": True})

    def handle_gallery_generate(self):
        body = self.read_json()
        settings = load_settings()
        p, problem = gallery_clean_params(body, settings)
        if problem:
            self.send_json({"error": problem}, 400)
            return
        if p["backend"] == "comfyui":
            if not settings.get("comfyui_url"):
                self.send_json({"error": "no ComfyUI URL is configured - set one in the Image uplink screen first"}, 400)
                return
            workflow_path = settings.get("comfyui_workflow_path") or ""
            if not workflow_path or not os.path.isfile(workflow_path):
                self.send_json({"error": "no ComfyUI workflow file is configured - set one in the Image uplink screen first"}, 400)
                return
        elif not find_image_model():
            self.send_json({"error": "no usable image model - choose one in Settings > Image model"}, 400)
            return
        with image_gen_lock:
            if any_chat_active() or image_gen_status["active"] or compact_active:
                self.send_json({"error": "busy - try again in a moment"}, 409)
                return
            image_gen_status["active"] = True
            image_gen_status["stage"] = "generating" if p["backend"] == "comfyui" else "unloading_chat_model"
            image_gen_status["error"] = None
            image_gen_status["cancelled"] = False
        cancel_pending_titles()
        threading.Thread(target=gallery_generate_worker, args=(p,), daemon=True).start()
        self.send_json({"ok": True})

    def handle_gallery_list(self):
        folder = outputs_subdir(GALLERY_CATEGORY)
        items = []
        try:
            names = sorted(n for n in os.listdir(folder) if GALLERY_NAME_RE.fullmatch(n))
        except OSError as e:
            self.send_json({"error": f"cannot read the gallery folder {folder} ({e.strerror or e})"}, 500)
            return
        for name in names:
            meta = {}
            try:
                with open(os.path.join(folder, name[:-4] + ".json"), "r", encoding="utf-8") as f:
                    meta = json.load(f)
                if not isinstance(meta, dict):
                    meta = {}
            except (OSError, ValueError):
                meta = {}
            try:
                size = os.path.getsize(os.path.join(folder, name))
            except OSError:
                continue
            items.append({**meta, "name": name, "bytes": size})
        items.sort(key=lambda m: m["name"], reverse=True)
        self.send_json({"images": items[:500], "folder": folder})

    def handle_gallery_get(self, name):
        full = os.path.join(outputs_subdir(GALLERY_CATEGORY), name)
        if not GALLERY_NAME_RE.fullmatch(name) or not os.path.isfile(full):
            self.send_json({"error": "not found"}, 404)
            return
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(data)

    def handle_gallery_delete(self, name):
        folder = outputs_subdir(GALLERY_CATEGORY)
        full = os.path.join(folder, name)
        if not GALLERY_NAME_RE.fullmatch(name) or not os.path.isfile(full):
            self.send_json({"error": "not found"}, 404)
            return
        os.remove(full)
        meta = os.path.join(folder, name[:-4] + ".json")
        if os.path.isfile(meta):
            os.remove(meta)
        self.send_json({"ok": True})

    def handle_edit_image(self):
        body = self.read_json()
        conv_id = optional_id(body.get("conversation_id"), "conversation_id")
        source_message_id = optional_id(body.get("source_message_id"), "source_message_id")
        source_image_id = optional_id(body.get("source_image_id"), "source_image_id")
        prompt = text_field(body, "prompt").strip()
        ref_images_in = body.get("ref_images") or []
        if not conv_id or not source_message_id or not source_image_id or not prompt:
            self.send_json(
                {"error": "conversation_id, source_message_id, source_image_id, and prompt are required"}, 400
            )
            return
        if not isinstance(ref_images_in, list) or len(ref_images_in) > 4:
            self.send_json({"error": "ref_images must be a list of at most 4 images"}, 400)
            return
        if not find_image_model():
            self.send_json({"error": "no image model is configured — add one in Settings first"}, 400)
            return
        with db() as conn:
            conv = conn.execute("SELECT id FROM conversations WHERE id=?", (conv_id,)).fetchone()
            if conv is None:
                self.send_json({"error": "conversation not found"}, 404)
                return
            source = conn.execute(
                "SELECT mime_type, data FROM message_images WHERE id=? AND message_id=?",
                (source_image_id, source_message_id),
            ).fetchone()
            if source is None:
                self.send_json({"error": "source image not found"}, 404)
                return
            ref_images = []
            for ref in ref_images_in:
                if not isinstance(ref, dict):
                    self.send_json({"error": "each reference image must be an object"}, 400)
                    return
                if "message_id" in ref and "image_id" in ref:
                    row = conn.execute(
                        "SELECT mime_type, data FROM message_images WHERE id=? AND message_id=?",
                        (optional_id(ref["image_id"], "image_id"), optional_id(ref["message_id"], "message_id")),
                    ).fetchone()
                    if row is None:
                        self.send_json({"error": "a reference image was not found"}, 404)
                        return
                    ref_images.append((row["mime_type"], row["data"]))
                elif "data" in ref:
                    try:
                        data = base64.b64decode(ref["data"], validate=True)
                    except (ValueError, TypeError):
                        self.send_json({"error": "a reference image's data was not valid base64"}, 400)
                        return
                    if not data or len(data) > MAX_IMAGE_BYTES:
                        self.send_json({"error": "a reference image is too large"}, 400)
                        return
                    ref_images.append((str(ref.get("mime_type") or "image/png"), data))
                else:
                    self.send_json(
                        {"error": "each reference image needs either message_id+image_id or data"}, 400
                    )
                    return
        with image_gen_lock:
            if any_chat_active() or image_gen_status["active"] or compact_active:
                self.send_json({"error": "busy — try again in a moment"}, 409)
                return
            image_gen_status["active"] = True
            image_gen_status["stage"] = "unloading_chat_model"
            image_gen_status["error"] = None
            image_gen_status["cancelled"] = False
        cancel_pending_titles()
        threading.Thread(
            target=edit_image_worker,
            args=(conv_id, source["data"], source["mime_type"], prompt, ref_images),
            daemon=True,
        ).start()
        self.send_json({"ok": True})

    # ---- backup / restore

    def handle_backup(self, include_outputs):
        """Zips a consistent snapshot of chats.db (via sqlite3's own backup()
        API, safe on a live/WAL DB - unlike a raw file copy, which risks
        grabbing it mid-write) plus settings.json/desktop_theme.json, and
        optionally the outputs/ dir (off by default - it's only ever
        user-exported copies already derivable from the DB, not primary
        content)."""
        tmp_db_path = os.path.join(DATA_DIR, "backup_chats.db.tmp")
        try:
            src = sqlite3.connect(DB_PATH)
            try:
                dst = sqlite3.connect(tmp_db_path)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(tmp_db_path, arcname="chats.db")
                if os.path.exists(SETTINGS_PATH):
                    zf.write(SETTINGS_PATH, arcname="settings.json")
                if os.path.exists(THEME_PATH):
                    zf.write(THEME_PATH, arcname="desktop_theme.json")
                if include_outputs:
                    root = outputs_root()
                    if os.path.isdir(root):
                        for dirpath, _dirnames, filenames in os.walk(root):
                            for fname in filenames:
                                full = os.path.join(dirpath, fname)
                                arcname = "outputs/" + os.path.relpath(full, root).replace(os.sep, "/")
                                zf.write(full, arcname=arcname)
            data = buf.getvalue()
        finally:
            try:
                os.remove(tmp_db_path)
            except OSError:
                pass

        # Backups made before the product was renamed are called local-ai-chatbot-backup-*.zip and restore exactly the same: _do_restore
        # looks at what is INSIDE the zip (chats.db / settings.json / desktop_theme.json), never at its file name.
        filename = f"personal-terminal-backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        self._send_export_file_bytes(data, "application/zip", filename)

    def handle_restore(self):
        """Restores chats.db/settings.json/desktop_theme.json (and
        optionally outputs/) from a zip produced by handle_backup. chats.db
        is sanity-checked *before* the live DB is ever touched, then
        hot-swapped via swap_db_file - no app restart needed, since that
        retires the pooled connections (see db()) before replacing the file
        and the pool simply reopens on the new one.
        settings.json likewise applies live (restarting llama-server if one's
        running, same as handle_reset_data's reset_settings branch).
        desktop_theme.json is the one exception - Theme is loaded once at
        window construction with no live re-theming anywhere in this app, so
        that one genuinely needs a restart, signaled back to the client via
        restart_needed_for_theme."""
        if any_chat_active() or image_gen_status["active"] or compact_active:
            self.send_json({"error": "a generation is in progress — try again shortly"}, 409)
            return
        length = self._content_length()
        if length <= 0:
            self.send_json({"error": "empty body"}, 400)
            return

        tmp_path = os.path.join(DATA_DIR, "restore_upload.zip.part")
        remaining = length
        try:
            with open(tmp_path, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(DOWNLOAD_CHUNK, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
        except OSError as e:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            self.send_json({"error": f"could not receive upload: {e}"}, 500)
            return

        try:
            self._do_restore(tmp_path)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def _do_restore(self, tmp_path):
        try:
            zf = zipfile.ZipFile(tmp_path)
        except zipfile.BadZipFile:
            self.send_json({"error": "not a valid backup file"}, 400)
            return
        with zf:
            names = set(zf.namelist())
            has_db = "chats.db" in names
            has_settings = "settings.json" in names
            has_theme = "desktop_theme.json" in names
            if not (has_db or has_settings or has_theme):
                self.send_json({"error": "this zip doesn't look like a Personal Terminal backup"}, 400)
                return

            # Every small part is read and checked BEFORE anything is changed:
            # the settings used to be parsed only after the database had already
            # been swapped (a bad settings.json then left a half-applied
            # restore behind a 400), and the theme file was copied in unread -
            # a theme that was not a JSON object crashed the next start-up.
            new_settings, dropped_settings = None, []
            if has_settings:
                parsed, _raw, problem = read_backup_json_object(zf, "settings.json")
                if problem:
                    self.send_json({"error": f"backup's settings.json {problem}"}, 400)
                    return
                new_settings, dropped_settings = sanitize_stored_settings(parsed)
            theme_raw, theme_problem = None, None
            if has_theme:
                _parsed, theme_raw, theme_problem = read_backup_json_object(zf, "desktop_theme.json")

            restored = {"chats_db": False, "settings": False, "theme": False}

            if has_db:
                staged_db_path = os.path.join(DATA_DIR, "restore_chats.db.staged")
                try:
                    with zf.open("chats.db") as src, open(staged_db_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    # Sanity-check the extracted file is an actually-usable
                    # SQLite DB *before* ever touching the live one - a
                    # corrupt/truncated upload must fail loud with the live
                    # DB completely untouched.
                    check_conn = sqlite3.connect(staged_db_path)
                    try:
                        check_conn.execute("PRAGMA integrity_check")
                        check_conn.execute("SELECT id FROM conversations LIMIT 1")
                    finally:
                        check_conn.close()
                except (sqlite3.Error, KeyError, OSError) as e:
                    try:
                        os.remove(staged_db_path)
                    except OSError:
                        pass
                    self.send_json({"error": f"backup's database file looks corrupt: {e}"}, 400)
                    return
                with db_reset_lock:
                    error = swap_db_file(staged_db_path)
                if error:
                    # The live database is untouched (see swap_db_file); don't
                    # leave a database-sized copy of the upload lying around.
                    try:
                        os.remove(staged_db_path)
                    except OSError:
                        pass
                    self.send_json({"error": error}, 500)
                    return
                restored["chats_db"] = True

            if has_settings:
                save_settings(new_settings)
                restored["settings"] = True
                with llama_restart_lock:
                    if llama_proc is not None and llama_proc.poll() is None:
                        stop_llama()
                        start_llama()

            if has_theme and theme_problem is None:
                # Written exactly as it came (unknown keys and all): the server
                # treats the theme as an opaque file the desktop owns. Atomically
                # (save_theme_bytes) - and a file that cannot be written is
                # reported as left out, not a 500 after the database and
                # settings have already been restored.
                try:
                    save_theme_bytes(theme_raw)
                    restored["theme"] = True
                except OSError as e:
                    theme_problem = f"could not be written: {e}"

            outputs_restored = 0
            outputs_skipped = 0
            root = os.path.abspath(outputs_root())
            for name in names:
                if not name.startswith("outputs/") or name.endswith("/"):
                    continue
                dest = backup_output_destination(root, name[len("outputs/"):])
                if dest is None:
                    outputs_skipped += 1  # a malformed / path-traversal entry is skipped, never trusted
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(name) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                outputs_restored += 1

        response = {
            "ok": True,
            "restored": restored,
            "outputs_restored": outputs_restored,
            "restart_needed_for_theme": restored["theme"],
        }
        # What was left out, so the client can say so instead of implying a full restore.
        skipped = {}
        if dropped_settings:
            skipped["settings_keys"] = dropped_settings
        if theme_problem:
            skipped["theme"] = f"the backup's desktop_theme.json {theme_problem}"
        if outputs_skipped:
            skipped["outputs_entries"] = outputs_skipped
        if skipped:
            response["skipped"] = skipped
        self.send_json(response)

    # ---- reset

    def handle_reset_data(self):
        if any_chat_active() or image_gen_status["active"] or compact_active:
            self.send_json({"error": "a generation is in progress — try again shortly"}, 409)
            return
        body = self.read_json()
        wipe_conversations = bool_field(body, "wipe_conversations")
        wipe_projects = bool_field(body, "wipe_projects")
        reset_settings = bool_field(body, "reset_settings")
        wipe_outputs = bool_field(body, "wipe_outputs")

        error = None
        if wipe_conversations and wipe_projects:
            # Wiping both categories empties chats.db entirely, so delete and
            # recreate the file itself rather than DELETE-ing every table -
            # guarantees nothing is left behind even if a table gets added
            # later and someone forgets to list it here. See swap_db_file's
            # own docstring for why the file swap needs db_reset_lock/event.
            with db_reset_lock:
                error = swap_db_file()
        elif wipe_conversations:
            with db() as conn:
                conn.execute("DELETE FROM message_images")
                conn.execute("DELETE FROM message_versions")
                conn.execute("DELETE FROM documents")
                conn.execute("DELETE FROM messages")
                conn.execute("DELETE FROM conversation_files")
                conn.execute("DELETE FROM conversations")
                conn.execute("DELETE FROM folders")
        elif wipe_projects:
            with db() as conn:
                conn.execute("DELETE FROM project_file_chunks")
                conn.execute("DELETE FROM project_files")
                conn.execute("DELETE FROM project_memory")
                conn.execute("DELETE FROM project_images")
                conn.execute("DELETE FROM projects")
                conn.execute("DELETE FROM project_groups")

        if error:
            self.send_json({"error": error}, 500)
            return

        if wipe_conversations or wipe_projects or reset_settings:
            try:
                if os.path.exists(LLAMA_LOG_PATH):
                    os.remove(LLAMA_LOG_PATH)
            except OSError:
                pass

        if reset_settings:
            # Model/mmproj/image-model choice survives a settings reset -
            # DEFAULT_SETTINGS has all three as None, which means "auto-pick
            # the largest .gguf in models/" (see start_llama()). Blindly
            # resetting to that silently swapped a user's deliberately
            # chosen model out from under them the instant they reset
            # anything else (temperature, context size, ...), which isn't
            # what "reset settings & preferences" implies. The checkbox's
            # own label promises defaults for *preferences*, not a change of
            # which model file is loaded.
            prior_settings = load_settings()
            new_settings = dict(DEFAULT_SETTINGS)
            for key in ("active_model", "active_mmproj", "active_image_model"):
                new_settings[key] = prior_settings.get(key)
            save_settings(new_settings)
            with llama_restart_lock:
                if llama_proc is not None and llama_proc.poll() is None:
                    stop_llama()
                    start_llama()

        outputs_skipped = False
        if wipe_outputs:
            # outputs_dir is user-configurable and can be redirected anywhere
            # on disk (e.g. Desktop, Documents) - only delete if it's still
            # at the app's own default location, never an arbitrary path the
            # user pointed it at, even though this checkbox is itself opt-in.
            current = os.path.normcase(os.path.normpath(os.path.abspath(outputs_root())))
            default = os.path.normcase(os.path.normpath(os.path.abspath(DEFAULT_OUTPUTS_DIR)))
            if current == default:
                shutil.rmtree(DEFAULT_OUTPUTS_DIR, ignore_errors=True)
            else:
                outputs_skipped = True

        response = {"ok": True}
        if wipe_outputs and outputs_skipped:
            response["outputs_skipped"] = True
        self.send_json(response)

    # ---- folders

    def handle_list_folders(self):
        with db() as conn:
            rows = conn.execute("SELECT id, name, created_at FROM folders ORDER BY name").fetchall()
        self.send_json([dict(r) for r in rows])

    def handle_create_folder(self):
        body = self.read_json()
        name = text_field(body, "name").strip()[:120]
        if not name:
            self.send_json({"error": "name required"}, 400)
            return
        with db() as conn:
            cur = conn.execute("INSERT INTO folders (name) VALUES (?)", (name,))
            folder = conn.execute("SELECT * FROM folders WHERE id=?", (cur.lastrowid,)).fetchone()
        self.send_json(dict(folder), 201)

    def handle_update_folder(self, folder_id):
        body = self.read_json()
        name = text_field(body, "name").strip()[:120]
        if not name:
            self.send_json({"error": "name required"}, 400)
            return
        with db() as conn:
            folder = conn.execute("SELECT id FROM folders WHERE id=?", (folder_id,)).fetchone()
            if folder is None:
                self.send_json({"error": "not found"}, 404)
                return
            conn.execute("UPDATE folders SET name=? WHERE id=?", (name, folder_id))
        self.send_json({"ok": True})

    def handle_delete_folder(self, folder_id):
        with db() as conn:
            conn.execute("UPDATE conversations SET folder_id=NULL WHERE folder_id=?", (folder_id,))
            conn.execute("DELETE FROM folders WHERE id=?", (folder_id,))
        self.send_json({"ok": True})

    # ---- project groups

    def handle_list_project_groups(self):
        with db() as conn:
            rows = conn.execute(
                "SELECT id, name, sort_order, created_at FROM project_groups ORDER BY name"
            ).fetchall()
        self.send_json([dict(r) for r in rows])

    def handle_create_project_group(self):
        body = self.read_json()
        name = text_field(body, "name").strip()[:120]
        if not name:
            self.send_json({"error": "name required"}, 400)
            return
        with db() as conn:
            cur = conn.execute("INSERT INTO project_groups (name) VALUES (?)", (name,))
            group = conn.execute("SELECT * FROM project_groups WHERE id=?", (cur.lastrowid,)).fetchone()
        self.send_json(dict(group), 201)

    def handle_update_project_group(self, group_id):
        body = self.read_json()
        with db() as conn:
            group = conn.execute("SELECT * FROM project_groups WHERE id=?", (group_id,)).fetchone()
            if group is None:
                self.send_json({"error": "not found"}, 404)
                return
            if "name" not in body and "sort_order" in body:
                conn.execute(
                    "UPDATE project_groups SET sort_order=? WHERE id=?",
                    (number_field(body, "sort_order", 0), group_id),
                )
            else:
                # Falling through to the single send_json() after the `with`
                # block exits (instead of responding here) matters: the
                # sqlite3 context manager commits in __exit__, which runs
                # *after* whatever runs inside the block - responding early
                # let a client's very next request race ahead of the commit
                # and read stale data (reproduced: ~96% of the time under a
                # tight request loop, since the write is delayed relative to
                # when the response reaches the client).
                name = text_field(body, "name", group["name"]).strip()[:120] or group["name"]
                sort_order = number_field(body, "sort_order", group["sort_order"])
                conn.execute(
                    "UPDATE project_groups SET name=?, sort_order=? WHERE id=?", (name, sort_order, group_id)
                )
        self.send_json({"ok": True})

    def handle_delete_project_group(self, group_id):
        with db() as conn:
            conn.execute("UPDATE projects SET group_id=NULL WHERE group_id=?", (group_id,))
            conn.execute("DELETE FROM project_groups WHERE id=?", (group_id,))
        self.send_json({"ok": True})

    # ---- projects

    def handle_list_projects(self):
        with db() as conn:
            rows = conn.execute(
                "SELECT id, name, instructions, sort_order, group_id, created_at, updated_at "
                "FROM projects ORDER BY name"
            ).fetchall()
        self.send_json([dict(r) for r in rows])

    def handle_create_project(self):
        body = self.read_json()
        name = text_field(body, "name").strip()[:120]
        instructions = text_field(body, "instructions").strip()
        if not name:
            self.send_json({"error": "name required"}, 400)
            return
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO projects (name, instructions) VALUES (?, ?)", (name, instructions)
            )
            proj = conn.execute("SELECT * FROM projects WHERE id=?", (cur.lastrowid,)).fetchone()
        self.send_json(dict(proj), 201)

    def handle_get_project(self, project_id):
        with db() as conn:
            proj = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if proj is None:
                self.send_json({"error": "not found"}, 404)
                return
            files = conn.execute(
                "SELECT id, filename, size_bytes, added_at, content_tokens "
                "FROM project_files WHERE project_id=? ORDER BY added_at",
                (project_id,),
            ).fetchall()
            memory = conn.execute(
                "SELECT id, content, content_tokens, created_at FROM project_memory "
                "WHERE project_id=? ORDER BY created_at",
                (project_id,),
            ).fetchall()
            images = conn.execute(
                "SELECT id, filename, mime_type, size_bytes, caption, caption_tokens, added_at "
                "FROM project_images WHERE project_id=? ORDER BY added_at",
                (project_id,),
            ).fetchall()
            used = project_content_tokens_used(conn, project_id)
        settings = load_settings()
        self.send_json({
            **dict(proj),
            "files": [dict(f) for f in files],
            "memory": [dict(m) for m in memory],
            "images": [dict(i) for i in images],
            "project_content_tokens_used": used,
            "project_content_token_budget": project_content_token_budget(settings),
            "effective_slot_context_tokens": effective_slot_context_tokens(settings),
            "context_size": settings.get("context_size", 16384),
            "max_parallel_chats": max_parallel_chats(settings),
            "project_content_budget_pct": settings.get("project_content_budget_pct", 0.5),
        })

    def handle_update_project(self, project_id):
        body = self.read_json()
        with db() as conn:
            proj = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if proj is None:
                self.send_json({"error": "not found"}, 404)
                return
            if "name" not in body and "instructions" not in body and ("sort_order" in body or "group_id" in body):
                # Pure reorder/regroup — don't touch name/instructions/updated_at.
                sort_order = number_field(body, "sort_order", proj["sort_order"])
                group_id = optional_id(body["group_id"], "group_id") if "group_id" in body else proj["group_id"]
                conn.execute(
                    "UPDATE projects SET sort_order=?, group_id=? WHERE id=?",
                    (sort_order, group_id, project_id),
                )
            else:
                # Falling through to the single send_json() after the `with`
                # block exits (instead of responding from inside either
                # branch) matters: the sqlite3 context manager commits in
                # __exit__, which runs *after* this block finishes -
                # responding early let a client's very next request race
                # ahead of the commit and read stale data (reproduced: ~96%
                # of the time under a tight request loop, since the write
                # was consistently delayed relative to when the response
                # reached the client).
                name = text_field(body, "name", proj["name"]).strip()[:120] or proj["name"]
                instructions = text_field(body, "instructions", proj["instructions"])
                sort_order = number_field(body, "sort_order", proj["sort_order"])
                group_id = optional_id(body["group_id"], "group_id") if "group_id" in body else proj["group_id"]
                conn.execute(
                    "UPDATE projects SET name=?, instructions=?, sort_order=?, group_id=?, "
                    "updated_at=datetime('now','localtime') WHERE id=?",
                    (name, instructions, sort_order, group_id, project_id),
                )
        self.send_json({"ok": True})

    def handle_delete_project(self, project_id):
        body = self.read_json()
        keep_conversations = bool_field(body, "keep_conversations")
        keep_instructions = bool_field(body, "keep_instructions")
        keep_files = bool_field(body, "keep_files")
        keep_images = bool_field(body, "keep_images")
        keep_memory = bool_field(body, "keep_memory")

        with db() as conn:
            if keep_conversations:
                conn.execute(
                    "UPDATE conversations SET project_id=NULL WHERE project_id=?", (project_id,)
                )
            else:
                conv_ids = [
                    r["id"] for r in conn.execute(
                        "SELECT id FROM conversations WHERE project_id=?", (project_id,)
                    ).fetchall()
                ]
                # Same orphan-data bug as the single-conversation DELETE route
                # (PRAGMA foreign_keys is never enabled, so ON DELETE CASCADE
                # doesn't run) - message_images/conversation_files must be
                # cleaned up explicitly here too.
                for cid in conv_ids:
                    conn.execute(
                        "DELETE FROM message_images WHERE message_id IN "
                        "(SELECT id FROM messages WHERE conversation_id=?)",
                        (cid,),
                    )
                    conn.execute(
                        "DELETE FROM message_versions WHERE message_id IN "
                        "(SELECT id FROM messages WHERE conversation_id=?)",
                        (cid,),
                    )
                    conn.execute("DELETE FROM documents WHERE conversation_id=?", (cid,))
                    conn.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
                    conn.execute("DELETE FROM conversation_files WHERE conversation_id=?", (cid,))
                conn.execute("DELETE FROM conversations WHERE project_id=?", (project_id,))

            if keep_instructions:
                pass
            else:
                conn.execute("UPDATE projects SET instructions='' WHERE id=?", (project_id,))

            if keep_files:
                pass
            else:
                conn.execute("DELETE FROM project_file_chunks WHERE project_id=?", (project_id,))
                conn.execute("DELETE FROM project_files WHERE project_id=?", (project_id,))

            if keep_images:
                pass
            else:
                conn.execute("DELETE FROM project_images WHERE project_id=?", (project_id,))

            if keep_memory:
                pass
            else:
                conn.execute("DELETE FROM project_memory WHERE project_id=?", (project_id,))

            # Only the project's own data (instructions/files/images/memory) needs
            # the project row to survive — keep_conversations just rehomes chats to
            # General and doesn't require the project shell to stick around.
            if not (keep_instructions or keep_files or keep_images or keep_memory):
                conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
        self.send_json({"ok": True})

    def _validate_and_extract_upload(self, settings):
        """Validate an uploaded file's headers/size, extract its text, and enforce the
        configured per-file token limit (a real count via count_tokens(), not a char
        estimate). Returns (filename, content, raw, content_tokens) on success, or None
        after already sending an error response."""
        max_upload_bytes = int(settings.get("max_upload_mb", 20)) * 1024 * 1024
        max_file_tokens = int(settings.get("max_file_tokens", 25_000))
        filename = safe_filename(urllib.parse.unquote(self.headers.get("X-Filename", "")))
        if not filename:
            self.send_json({"error": "X-Filename header required"}, 400)
            return None
        length = self._content_length()
        if length <= 0:
            self.send_json({"error": "empty file"}, 400)
            return None
        if length > max_upload_bytes:
            self.send_json(
                {"error": f"file too large (max {max_upload_bytes // (1024 * 1024)} MB)"}, 400
            )
            return None
        raw = self.rfile.read(length)
        try:
            content = normalize_extracted_text(extract_upload_text(filename, raw))
        except ExtractionError as e:
            self.send_json({"error": str(e)}, 400)
            return None
        if not content.strip():
            self.send_json(
                {"error": "no extractable text was found in this file (a scanned/image-only PDF has no text layer)"},
                400,
            )
            return None
        content_tokens = count_tokens(content)
        if content_tokens is None:
            self.send_json(
                {"error": "model server not reachable — the chat model must be running to add files"}, 503
            )
            return None
        if content_tokens > max_file_tokens:
            self.send_json(
                {"error": f"extracted text too large ({content_tokens:,} tokens); max is {max_file_tokens:,}"}, 400
            )
            return None
        return filename, content, raw, content_tokens

    def handle_upload_project_file(self, project_id):
        settings = load_settings()
        result = self._validate_and_extract_upload(settings)
        if result is None:
            return
        filename, content, raw, content_tokens = result
        with db() as conn:
            proj = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
            if proj is None:
                self.send_json({"error": "project not found"}, 404)
                return
            existing = conn.execute(
                "SELECT filename, content FROM project_files WHERE project_id=?", (project_id,)
            ).fetchall()
            for f in existing:
                if f["content"] == content:
                    self.send_json(
                        {"error": f'identical content is already in this project as "{f["filename"]}"'}, 409
                    )
                    return
            used = project_content_tokens_used(conn, project_id)
            budget = project_content_token_budget(settings)
            if used + content_tokens > budget:
                self.send_json({
                    "error": (
                        f"this project's content would use {used + content_tokens:,} of its {budget:,}-token "
                        f"budget ({used:,} already used by files/images/notes). Remove a file/image/note, or "
                        "raise the project content budget in Settings → Advanced."
                    )
                }, 400)
                return
            conn.execute(
                "INSERT INTO project_files (project_id, filename, content, size_bytes, content_tokens) "
                "VALUES (?, ?, ?, ?, ?)",
                (project_id, filename, content, len(raw), content_tokens),
            )
        self.send_json({"ok": True})

    def handle_delete_project_file(self, project_id, file_id):
        with db() as conn:
            conn.execute("DELETE FROM project_file_chunks WHERE project_file_id=?", (file_id,))
            conn.execute(
                "DELETE FROM project_files WHERE id=? AND project_id=?", (file_id, project_id)
            )
        self.send_json({"ok": True})

    def _validate_and_read_image_upload(self):
        """Validate an uploaded image's headers/extension/size and read its raw bytes.
        Returns (filename, mime_type, raw) on success, or None after already sending an
        error response. Unlike _validate_and_extract_upload, no text extraction - this is
        for the raw-bytes project-image path (see handle_upload_project_image)."""
        filename = safe_filename(urllib.parse.unquote(self.headers.get("X-Filename", "")))
        if not filename:
            self.send_json({"error": "X-Filename header required"}, 400)
            return None
        ext = os.path.splitext(filename)[1].lower()
        if ext in UNSUPPORTED_IMAGE_EXTENSIONS:
            self.send_json(
                {"error": f"{ext} images aren't supported by the vision model (stb_image can't decode them)"}, 400
            )
            return None
        if ext not in IMAGE_EXTENSIONS:
            self.send_json({"error": f"unsupported file type: {ext or filename}"}, 400)
            return None
        length = self._content_length()
        if length <= 0:
            self.send_json({"error": "empty file"}, 400)
            return None
        if length > MAX_IMAGE_BYTES:
            self.send_json(
                {"error": f"image too large (max {MAX_IMAGE_BYTES // (1024 * 1024)} MB)"}, 400
            )
            return None
        raw = self.rfile.read(length)
        return filename, _IMAGE_MIME[ext], raw

    def handle_upload_project_image(self, project_id):
        settings = load_settings()
        with db() as conn:
            proj = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
        if proj is None:
            self.send_json({"error": "project not found"}, 404)
            return
        if not find_mmproj():
            self.send_json(
                {"error": "configure a vision model in Settings → Vision first, then add images to this project"},
                400,
            )
            return
        result = self._validate_and_read_image_upload()
        if result is None:
            return
        filename, mime_type, raw = result
        try:
            caption = caption_image(raw, mime_type)
        except Exception as e:
            self.send_json({"error": model_server_down_message() if is_connection_failure(e) else f"could not describe this image: {e}"}, 502)
            return
        caption_tokens = count_tokens(caption)
        if caption_tokens is None:
            self.send_json({"error": "model server not reachable"}, 503)
            return
        with db() as conn:
            used = project_content_tokens_used(conn, project_id)
            budget = project_content_token_budget(settings)
            if used + caption_tokens > budget:
                self.send_json({
                    "error": (
                        f"this project's content would use {used + caption_tokens:,} of its {budget:,}-token "
                        f"budget ({used:,} already used by files/images/notes). Remove a file/image/note, or "
                        "raise the project content budget in Settings → Advanced."
                    )
                }, 400)
                return
            cur = conn.execute(
                "INSERT INTO project_images (project_id, filename, mime_type, data, caption, "
                "caption_tokens, size_bytes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (project_id, filename, mime_type, raw, caption, caption_tokens, len(raw)),
            )
        self.send_json({"ok": True, "id": cur.lastrowid, "caption": caption, "caption_tokens": caption_tokens})

    def handle_delete_project_image(self, project_id, image_id):
        with db() as conn:
            conn.execute(
                "DELETE FROM project_images WHERE id=? AND project_id=?", (image_id, project_id)
            )
        self.send_json({"ok": True})

    def handle_get_project_image(self, project_id, image_id):
        with db() as conn:
            row = conn.execute(
                "SELECT filename, mime_type, data FROM project_images WHERE id=? AND project_id=?",
                (image_id, project_id),
            ).fetchone()
        if row is None:
            self.send_json({"error": "not found"}, 404)
            return
        data = row["data"]
        self.send_response(200)
        self.send_header("Content-Type", row["mime_type"])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(data)

    def handle_add_project_memory(self, project_id):
        body = self.read_json()
        content = text_field(body, "content").strip()
        if not content:
            self.send_json({"error": "content required"}, 400)
            return
        settings = load_settings()
        content_tokens = count_tokens(content)
        if content_tokens is None:
            self.send_json({"error": "model server not reachable"}, 503)
            return
        with db() as conn:
            proj = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
            if proj is None:
                self.send_json({"error": "project not found"}, 404)
                return
            used = project_content_tokens_used(conn, project_id)
            budget = project_content_token_budget(settings)
            if used + content_tokens > budget:
                self.send_json({
                    "error": (
                        f"this project's content would use {used + content_tokens:,} of its {budget:,}-token "
                        f"budget ({used:,} already used by files/images/notes). Remove a file/image/note, or "
                        "raise the project content budget in Settings → Advanced."
                    )
                }, 400)
                return
            conn.execute(
                "INSERT INTO project_memory (project_id, content, content_tokens) VALUES (?, ?, ?)",
                (project_id, content, content_tokens),
            )
        self.send_json({"ok": True})

    def handle_delete_project_memory(self, project_id, mem_id):
        with db() as conn:
            conn.execute(
                "DELETE FROM project_memory WHERE id=? AND project_id=?", (mem_id, project_id)
            )
        self.send_json({"ok": True})

    # ---- export

    def _export_format(self, save_to_disk):
        if save_to_disk:
            return self.read_json().get("format", "md")
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        return (qs.get("format") or ["md"])[0]

    def _send_export_file(self, content, mime, filename):
        body_bytes = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _send_export_file_bytes(self, data, mime, filename):
        """Binary counterpart of _send_export_file - no text/charset framing,
        `data` is already raw bytes (e.g. a zip)."""
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _save_export_file(self, content, filename):
        path = os.path.join(outputs_subdir("exports"), filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        self.send_json({"ok": True, "path": os.path.relpath(path, ROOT).replace(os.sep, "/")})

    def handle_export_conversation(self, conv_id, save_to_disk):
        fmt = self._export_format(save_to_disk)
        if fmt not in EXPORT_MIME:
            self.send_json({"error": "invalid format"}, 400)
            return
        with db() as conn:
            conv = conn.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
            if conv is None:
                self.send_json({"error": "not found"}, 404)
                return
            messages = conn.execute(
                "SELECT role, content, created_at FROM messages WHERE conversation_id=? ORDER BY id",
                (conv_id,),
            ).fetchall()
        content, mime = build_conversation_export(conv, messages, fmt)
        filename = export_filename(conv["title"], fmt)
        if save_to_disk:
            self._save_export_file(content, filename)
        else:
            self._send_export_file(content, mime, filename)

    def handle_context_usage(self, conv_id):
        settings = load_settings()
        with db() as conn:
            conv = conn.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
            if conv is None:
                self.send_json({"error": "conversation not found"}, 404)
                return
            system_content = build_system_prompt(conn, conv, settings)
            messages_payload = build_chat_messages_payload(conn, conv)
            compactable = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE conversation_id=? AND id > ?",
                (conv_id, conv["compacted_through_id"] or 0),
            ).fetchone()["n"]
        history_parts = []
        for m in messages_payload:
            content = m.get("content")
            if isinstance(content, str):
                history_parts.append(content)
            elif isinstance(content, list):
                history_parts.extend(part.get("text", "") for part in content if part.get("type") == "text")
        # Two separate tokenize calls (system-only, then the full combined text) rather
        # than one, so the meter can show a system/history breakdown — the combined
        # figure stays the single source of truth for "used", the breakdown is derived
        # from it rather than independently tokenized (avoids two counts disagreeing).
        system_tokens = count_tokens(system_content)
        used = count_tokens("\n".join([system_content] + history_parts))
        if system_tokens is None or used is None:
            self.send_json({"error": "model server not reachable"}, 503)
            return
        history_tokens = max(0, used - system_tokens)
        self.send_json({
            "used": used,
            "max": effective_slot_context_tokens(settings),
            "system_tokens": system_tokens,
            "history_tokens": history_tokens,
            "compactable_messages": compactable,
            "compacted": bool(conv["compacted_summary"]),
        })

    def handle_title_status(self, conv_id):
        """Cheap in-memory lookup (no DB hit) so the client can poll whether
        generate_conversation_title's background thread is still running for
        this conversation, instead of guessing with a fixed delay - see
        title_pending's own comment."""
        with title_pending_lock:
            pending = conv_id in title_pending
        self.send_json({"pending": pending})

    def handle_get_document(self, doc_id):
        """GET /api/documents/<id>: a document the create_document tool made, content included. `?raw=1` sends the
        bare text instead (text/markdown or text/plain, as a download named after the document) so a save needs no
        JSON round trip for a large document."""
        raw = "raw=1" in (self.path.split("?", 1) + [""])[1]
        with db() as conn:
            row = conn.execute(
                "SELECT id, conversation_id, message_id, title, filename, content, size, version, char_offset, created_at "
                "FROM documents WHERE id=?",
                (doc_id,),
            ).fetchone()
        if row is None:
            self.send_json({"error": "not found"}, 404)
            return
        if raw:
            data = row["content"].encode("utf-8")
            mime = "text/markdown; charset=utf-8" if row["filename"].lower().endswith(".md") else "text/plain; charset=utf-8"
            # The header is latin-1: an ASCII stand-in plus the RFC 5987 form carries any name (Unicode titles included).
            ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", row["filename"])
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{urllib.parse.quote(row["filename"])}',
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_json({
            "id": row["id"], "conversation_id": row["conversation_id"], "message_id": row["message_id"],
            "title": row["title"], "filename": row["filename"], "bytes": row["size"], "version": row["version"],
            "offset": row["char_offset"], "created_at": row["created_at"], "content": row["content"],
        })

    def handle_delete_document(self, doc_id):
        """DELETE /api/documents/<id>: removes one document card (the reply it sat in stays)."""
        with db() as conn:
            cur = conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        if cur.rowcount == 0:
            self.send_json({"error": "not found"}, 404)
            return
        self.send_json({"ok": True})

    def handle_thoughts(self, conv_id):
        """Every saved reasoning transcript for this conversation, oldest
        first, paired with the user prompt each one was thinking about.

        Split out of the main conversation GET on purpose - see the
        has_reasoning comment there. The prompt lookup is a correlated
        subquery rather than a second round trip plus a join in Python: the
        result set is at most one row per assistant message, and this keeps
        the ordering authoritative in SQL."""
        with db() as conn:
            if conn.execute("SELECT 1 FROM conversations WHERE id=?", (conv_id,)).fetchone() is None:
                self.send_json({"error": "not found"}, 404)
                return
            rows = conn.execute(
                "SELECT m.id, m.reasoning, m.created_at, ("
                "  SELECT u.content FROM messages u"
                "  WHERE u.conversation_id = m.conversation_id AND u.role = 'user' AND u.id < m.id"
                "  ORDER BY u.id DESC LIMIT 1"
                ") AS prompt "
                "FROM messages m WHERE m.conversation_id=? AND m.reasoning IS NOT NULL "
                "ORDER BY m.id",
                (conv_id,),
            ).fetchall()
        self.send_json({"thoughts": [dict(r) for r in rows]})

    def handle_export_message(self, conv_id, message_id, save_to_disk):
        fmt = self._export_format(save_to_disk)
        if fmt not in EXPORT_MIME:
            self.send_json({"error": "invalid format"}, 400)
            return
        with db() as conn:
            conv = conn.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
            if conv is None:
                self.send_json({"error": "conversation not found"}, 404)
                return
            message = conn.execute(
                "SELECT * FROM messages WHERE id=? AND conversation_id=?", (message_id, conv_id)
            ).fetchone()
            if message is None:
                self.send_json({"error": "message not found"}, 404)
                return
        content, mime = build_message_export(conv, message, fmt)
        filename = export_filename(conv["title"], fmt, suffix="message")
        if save_to_disk:
            self._save_export_file(content, filename)
        else:
            self._send_export_file(content, mime, filename)

    # ---- conversation files

    def handle_upload_conversation_file(self, conv_id):
        result = self._validate_and_extract_upload(load_settings())
        if result is None:
            return
        # content_tokens already computed once inside _validate_and_extract_upload
        # (needed there for the max_file_tokens check) - reused here rather than
        # calling count_tokens() a second time. See that call for why it's cached
        # at all: count_tokens() is a blocking HTTP round-trip to llama-server (up
        # to ~15s); the desktop client loads a conversation synchronously on its
        # main thread with no worker thread, so recomputing per file on every load
        # would freeze the GUI.
        filename, content, raw, content_tokens = result
        with db() as conn:
            conv = conn.execute("SELECT id FROM conversations WHERE id=?", (conv_id,)).fetchone()
            if conv is None:
                self.send_json({"error": "conversation not found"}, 404)
                return
            cur = conn.execute(
                "INSERT INTO conversation_files (conversation_id, filename, content, size_bytes, content_tokens) "
                "VALUES (?, ?, ?, ?, ?)",
                (conv_id, filename, content, len(raw), content_tokens),
            )
        self.send_json({"ok": True, "id": cur.lastrowid, "content_tokens": content_tokens})

    def handle_delete_conversation_file(self, conv_id, file_id):
        with db() as conn:
            conn.execute(
                "DELETE FROM conversation_files WHERE id=? AND conversation_id=?", (file_id, conv_id)
            )
        self.send_json({"ok": True})

    # ---- chat streaming

    def handle_generation_progress(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        raw_conv_id = (qs.get("conversation_id") or [None])[0]
        slot_id = None
        if raw_conv_id is not None:
            try:
                with active_chats_lock:
                    slot_id = active_chats.get(int(raw_conv_id))
            except ValueError:
                pass
        if slot_id is None:
            self.send_json({"prompt_tokens_processed": None})
            return
        self.send_json({"prompt_tokens_processed": get_active_slot_progress(slot_id)})

    def _check_project_content_budget(self, conn, conv, settings):
        """Catches a project's already-stored content exceeding the CURRENT
        effective budget - e.g. context_size was lowered, or the model
        switched to a smaller-context one, after the project was populated
        near the old (larger) ceiling. Upload-time checks alone can't catch
        this, since they only run when new content is added. Used to be an
        unconditional hard block here ("no safe partial-content fallback to
        offer without relevance-based selection") - that relevance-based
        selection now exists (see retrieve_relevant_chunks), so an
        over-budget project is only actually blocked if the embedding server
        isn't reachable to serve it; build_system_prompt's own over-budget
        branch is what actually engages retrieval instead of the raw dump.
        Returns True if OK to proceed; on failure, already sends the error
        and returns False."""
        if not conv["project_id"]:
            return True
        used = project_content_tokens_used(conn, conv["project_id"])
        budget = project_content_token_budget(settings)
        if used <= budget:
            return True
        if embed_health():
            return True
        self.send_json({"error": (
            f"this project's stored content now uses {used:,} tokens, over the {budget:,}-token "
            f"budget for the current context size. Remove some files/images/memory notes, raise "
            f"context size in Settings → Advanced, or start the embedding server (models/embedding/) "
            f"to enable relevance-based retrieval instead, to continue chatting here."
        )}, 400)
        return False

    def _reserve_chat_slot(self, conv_id):
        """Atomically checks and reserves one of the currently-configured
        max_parallel_chats concurrent-generation slots for conv_id. Returns
        the assigned slot id on success. On failure, already sends the
        appropriate error response and returns None - callers must just
        `return` when they get None back, not send anything themselves."""
        parallel = max_parallel_chats()
        with active_chats_lock:
            if conv_id in active_chats:
                self.send_json({"error": "this conversation is already generating a reply"}, 409)
                return None
            if len(active_chats) >= parallel:
                self.send_json(
                    {
                        "error": f"already generating the maximum of {parallel} replies "
                        "at once — wait for one to finish"
                    },
                    409,
                )
                return None
            used = set(active_chats.values())
            slot_id = next(s for s in range(parallel) if s not in used)
            active_chats[conv_id] = slot_id
            return slot_id

    def handle_chat(self):
        if image_gen_status["active"] or compact_active:
            self.send_json({"error": "busy generating an image or compacting — chat will resume shortly"}, 409)
            return
        restore_error = ensure_chat_model_available()
        if restore_error:
            self.send_json({"error": restore_error}, 502)
            return
        body = self.read_json()
        conv_id = optional_id(body.get("conversation_id"), "conversation_id")
        user_text = text_field(body, "message").strip()
        images = body.get("images") or []
        if not isinstance(images, list) or not all(isinstance(img, dict) for img in images):
            raise BadRequest("images must be a list of objects")
        if not conv_id or (not user_text and not images):
            self.send_json({"error": "conversation_id and message (or an image) required"}, 400)
            return
        if images and not find_mmproj():
            self.send_json({"error": "no vision model is configured — add one in Settings first"}, 400)
            return
        slot_id = self._reserve_chat_slot(conv_id)
        if slot_id is None:
            return
        cancel_pending_titles()  # a title still being generated must not make this reply wait for the slot

        settings = load_settings()
        try:
            prepared = self._begin_chat_turn(conv_id, user_text, images, settings)
        except BaseException:
            # Whatever went wrong, the slot must not stay reserved for a reply
            # that is never going to start: it used to be, and every later
            # message in the conversation was then answered with a permanent
            # "already generating a reply" error until the app restarted.
            active_chats.pop(conv_id, None)
            raise
        if prepared is None:
            return  # an error response has already been sent (and the slot released)
        payload, new_message_id, is_first_message = prepared
        self._stream_and_save(
            conv_id, payload, slot_id, auto_title=is_first_message, user_text=user_text,
            user_message_id=new_message_id,
        )

    def _begin_chat_turn(self, conv_id, user_text, images, settings):
        """Everything handle_chat does between reserving a slot and starting
        the stream: validates, stores the user's message, and builds the
        request. Returns (payload, user_message_id, is_first_message), or None
        if it has already answered the client with an error."""
        with db() as conn:
            conv = conn.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
            if conv is None:
                active_chats.pop(conv_id, None)
                self.send_json({"error": "conversation not found"}, 404)
                return None
            if not self._check_project_content_budget(conn, conv, settings):
                active_chats.pop(conv_id, None)
                return None
            cur = conn.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'user', ?)",
                (conv_id, user_text),
            )
            new_message_id = cur.lastrowid
            for img in images:
                filename = safe_filename(img.get("filename")) or "image"
                mime_type = str(img.get("mime_type") or "image/png")
                try:
                    data = base64.b64decode(img.get("data") or "", validate=True)
                except (ValueError, TypeError):
                    continue
                if not data or len(data) > MAX_IMAGE_BYTES:
                    continue
                conn.execute(
                    "INSERT INTO message_images (message_id, filename, mime_type, data) VALUES (?, ?, ?, ?)",
                    (new_message_id, filename, mime_type, data),
                )
            # Title new conversations after their first message — a quick truncated
            # placeholder now, upgraded to an AI-generated title once the reply lands.
            is_first_message = conv["title"] == "New chat"
            if is_first_message:
                title = re.sub(r"\s+", " ", user_text)[:60] or "Image"
                conn.execute("UPDATE conversations SET title=? WHERE id=?", (title, conv_id))
            conn.execute(
                "UPDATE conversations SET updated_at=datetime('now','localtime') WHERE id=?",
                (conv_id,),
            )
        # The write transaction above is committed by now. Building the prompt
        # can talk to the embedding server and the model's tokenizer (retrieval
        # for an over-budget project) - it must not do that with a write
        # transaction open, or every other write in the app waits behind it.
        try:
            with db() as conn:
                messages_payload = build_chat_messages_payload(conn, conv, current_message_id=new_message_id)
                system_content = build_system_prompt(conn, conv, settings, query_text=user_text)
        except BaseException:
            # The user's message is already saved; with no reply coming it would
            # be left as a dangling unanswered turn (which breaks strict
            # user/assistant alternation on the next attempt) - same rollback
            # _stream_and_save does when the model can't be reached.
            self._discard_user_turn(conv_id, new_message_id, is_first_message)
            raise

        payload = {
            "messages": [{"role": "system", "content": system_content}] + messages_payload,
            "stream": True,
            **sampling_params(settings),
        }
        tools = enabled_tools(settings, find_model())
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload, new_message_id, is_first_message

    def _discard_user_turn(self, conv_id, user_message_id, revert_title):
        """Removes a just-saved user message (and its images) whose reply is
        never going to be generated, and puts back the 'New chat' title if it
        was the one that set it."""
        try:
            with db() as conn:
                conn.execute("DELETE FROM message_images WHERE message_id=?", (user_message_id,))
                conn.execute("DELETE FROM messages WHERE id=?", (user_message_id,))
                if revert_title:
                    conn.execute("UPDATE conversations SET title='New chat' WHERE id=?", (conv_id,))
        except Exception:
            traceback.print_exc()

    def handle_edit_message(self, conv_id, message_id):
        """PATCH /api/conversations/{conv_id}/messages/{message_id} - two
        modes selected by whether the body has a "content" key:

        - {"content": "..."}: edit-and-truncate. message_id must be a user
          message; its content is overwritten in place and every message
          after it is deleted (mirrors the orphan-image cleanup pattern used
          at every other DELETE FROM messages call site in this file), then
          a fresh reply is generated as if this had just been sent - no
          branch/version history is kept, this app deliberately doesn't have
          conversation branching.
        - no "content" key (optionally {"instruction": "..."}): regenerate.
          The superseded reply is NOT thrown away: it is archived into
          message_versions and the fresh one takes its place in the same
          `messages` row, so the client can flip between them with the
          "[<] 2/3 [>]" control (see handle_switch_message_version). Still
          only allowed when message_id is literally the most recent message -
          regenerating mid-conversation would invalidate every turn after it,
          which is the branching this app deliberately doesn't do.
        """
        if image_gen_status["active"] or compact_active:
            self.send_json({"error": "busy generating an image or compacting — chat will resume shortly"}, 409)
            return
        restore_error = ensure_chat_model_available()
        if restore_error:
            self.send_json({"error": restore_error}, 502)
            return
        body = self.read_json()
        has_content = "content" in body
        new_content = text_field(body, "content").strip() if has_content else None
        instruction = text_field(body, "instruction").strip()
        if has_content and not new_content:
            self.send_json({"error": "content cannot be empty"}, 400)
            return
        slot_id = self._reserve_chat_slot(conv_id)
        if slot_id is None:
            return
        cancel_pending_titles()  # same as handle_chat

        settings = load_settings()
        try:
            prepared = self._begin_edit_turn(conv_id, message_id, has_content, new_content, instruction, settings)
        except BaseException:
            # Same reasoning as handle_chat: never leave the slot reserved for a
            # reply that is not going to start.
            active_chats.pop(conv_id, None)
            raise
        if prepared is None:
            return  # an error response has already been sent (and the slot released)
        payload, current_message_id, regenerate_message_id = prepared
        # current_user_message_id (not user_message_id) - reports this turn's
        # user message id in the message_ids SSE frame below without also
        # enabling _stream_and_save's rollback-the-user-message-on-failure
        # path, which only makes sense for handle_chat's fresh INSERT. Here
        # the user message already existed (either unchanged - regenerate -
        # or a deliberate edit the user made) and should never be silently
        # deleted just because the follow-up generation failed.
        self._stream_and_save(
            conv_id, payload, slot_id, current_user_message_id=current_message_id,
            regenerate_message_id=regenerate_message_id,
        )

    def _begin_edit_turn(self, conv_id, message_id, has_content, new_content, instruction, settings):
        """Everything handle_edit_message does between reserving a slot and
        starting the stream: validates, applies the edit (or picks the reply
        to regenerate), and builds the request. Returns (payload,
        current_user_message_id, regenerate_message_id), or None if it has
        already answered the client with an error."""
        regenerate_message_id = None
        with db() as conn:
            conv = conn.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
            if conv is None:
                active_chats.pop(conv_id, None)
                self.send_json({"error": "conversation not found"}, 404)
                return None
            if not self._check_project_content_budget(conn, conv, settings):
                active_chats.pop(conv_id, None)
                return None
            target = conn.execute(
                "SELECT * FROM messages WHERE id=? AND conversation_id=?", (message_id, conv_id)
            ).fetchone()
            if target is None:
                active_chats.pop(conv_id, None)
                self.send_json({"error": "message not found"}, 404)
                return

            if has_content:
                if target["role"] != "user":
                    active_chats.pop(conv_id, None)
                    self.send_json({"error": "only a user message can be edited"}, 400)
                    return
                conn.execute("UPDATE messages SET content=? WHERE id=?", (new_content, message_id))
                conn.execute(
                    "DELETE FROM message_images WHERE message_id IN "
                    "(SELECT id FROM messages WHERE conversation_id=? AND id > ?)",
                    (conv_id, message_id),
                )
                conn.execute(
                    "DELETE FROM message_versions WHERE message_id IN "
                    "(SELECT id FROM messages WHERE conversation_id=? AND id > ?)",
                    (conv_id, message_id),
                )
                conn.execute(
                    "DELETE FROM documents WHERE message_id IN "
                    "(SELECT id FROM messages WHERE conversation_id=? AND id > ?)",
                    (conv_id, message_id),
                )
                conn.execute("DELETE FROM messages WHERE conversation_id=? AND id > ?", (conv_id, message_id))
                current_message_id = message_id
                query_text = new_content
            else:
                last = conn.execute(
                    "SELECT * FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1",
                    (conv_id,),
                ).fetchone()
                if last is None or last["id"] != message_id or last["role"] != "assistant":
                    active_chats.pop(conv_id, None)
                    self.send_json({"error": "only the most recent response can be regenerated"}, 400)
                    return
                # Nothing is deleted or written here any more. The old reply
                # stays exactly as-is until the replacement has actually been
                # generated, at which point _stream_and_save archives it into
                # message_versions and overwrites this same row in one
                # transaction (see its regenerate_message_id parameter). Two
                # things fall out of that:
                #   - a failed/empty regeneration now leaves the original reply
                #     untouched instead of destroying it, which the old
                #     delete-first ordering really did (the row was gone before
                #     llama-server was even contacted);
                #   - the DB row id is stable across a regenerate, so the
                #     transcript's version control keeps addressing one message.
                # The stale reply must still be kept out of the prompt, though -
                # exclude_message_id below does that without touching the row.
                regenerate_message_id = message_id
                last_user = conn.execute(
                    "SELECT id, content FROM messages WHERE conversation_id=? AND id<? "
                    "ORDER BY id DESC LIMIT 1",
                    (conv_id, message_id),
                ).fetchone()
                current_message_id = last_user["id"] if last_user else None
                query_text = last_user["content"] if last_user else None

            conn.execute(
                "UPDATE conversations SET updated_at=datetime('now','localtime') WHERE id=?",
                (conv_id,),
            )

        # The write transaction above is committed by now - building the prompt
        # can reach the embedding server and the tokenizer (retrieval for an
        # over-budget project) and must not do that with the write lock held.
        # (If it fails, an edit has already been applied: the user sees the
        # error and can regenerate; nothing is left half-done.)
        with db() as conn:
            messages_payload = build_chat_messages_payload(
                conn, conv, current_message_id=current_message_id,
                exclude_message_id=regenerate_message_id,
            )
            system_content = build_system_prompt(conn, conv, settings, query_text=query_text)

        # Merge the rewrite instruction into the existing last user turn rather
        # than appending a new one — many chat templates (Gemma included)
        # require strict user/assistant alternation and reject back-to-back
        # user messages. The last turn's content may be a plain string or a
        # multimodal content-part list if it included an image. Only applies
        # to the regenerate path - an edit's new_content already *is* the
        # revised user turn, nothing to merge in.
        if instruction and not has_content and messages_payload and messages_payload[-1]["role"] == "user":
            last_turn = dict(messages_payload[-1])
            note = f"(Please revise your previous response: {instruction})"
            if isinstance(last_turn["content"], list):
                last_turn["content"] = last_turn["content"] + [{"type": "text", "text": note}]
            else:
                last_turn["content"] = (last_turn["content"] or "") + f"\n\n{note}"
            messages_payload[-1] = last_turn

        payload = {
            "messages": [{"role": "system", "content": system_content}] + messages_payload,
            "stream": True,
            **sampling_params(settings),
        }
        tools = enabled_tools(settings, find_model())
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload, current_message_id, regenerate_message_id

    def handle_switch_message_version(self, conv_id, message_id):
        """POST /api/conversations/{conv_id}/messages/{message_id}/version
        {"position": N} - shows archived version N of a regenerated reply,
        parking the one currently on screen in the slot it vacates.

        Purely a swap between rows that already exist: nothing is generated,
        nothing is deleted, and the model is never contacted, so unlike
        regenerate this is allowed on any assistant message, not just the last
        one. Returns the newly-active message so the client can re-render that
        one bubble without reloading the whole conversation.
        """
        with active_chats_lock:
            if conv_id in active_chats:
                self.send_json({"error": "this conversation is generating a reply"}, 409)
                return
        body = self.read_json()
        try:
            position = int(body.get("position"))
        except (TypeError, ValueError):
            self.send_json({"error": "position must be a number"}, 400)
            return
        with db() as conn:
            msg = conn.execute(
                "SELECT id, role FROM messages WHERE id=? AND conversation_id=?", (message_id, conv_id)
            ).fetchone()
            if msg is None:
                self.send_json({"error": "message not found"}, 404)
                return
            if msg["role"] != "assistant":
                self.send_json({"error": "only an assistant reply has versions"}, 400)
                return
            if switch_message_version(conn, message_id, position) is None:
                self.send_json({"error": "no such version"}, 404)
                return
            # Bumped for the same reason an edit does: which version is live
            # decides what the model sees on every later turn, so this really
            # is a change to the conversation, not just to what's displayed.
            conn.execute(
                "UPDATE conversations SET updated_at=datetime('now','localtime') WHERE id=?",
                (conv_id,),
            )
            row = conn.execute(
                "SELECT content, (reasoning IS NOT NULL) AS has_reasoning FROM messages WHERE id=?",
                (message_id,),
            ).fetchone()
            images = conn.execute(
                "SELECT id, filename, mime_type FROM message_images "
                "WHERE message_id=? AND version_id IS NULL ORDER BY id",
                (message_id,),
            ).fetchall()
            documents = conn.execute(
                "SELECT id, title, filename, size, version, char_offset FROM documents "
                "WHERE message_id=? AND version_id IS NULL ORDER BY id",
                (message_id,),
            ).fetchall()
            version_pos, version_count = message_version_state(conn, message_id)
        self.send_json({
            "ok": True,
            "message_id": message_id,
            "content": row["content"],
            "has_reasoning": bool(row["has_reasoning"]),
            "images": [dict(r) for r in images],
            # the version's document cards (the client redraws the reply with them in place)
            "documents": [{"id": r["id"], "title": r["title"], "filename": r["filename"], "bytes": r["size"],
                           "version": r["version"], "offset": r["char_offset"]} for r in documents],
            "version_pos": version_pos,
            "version_count": version_count,
        })

    def handle_compact(self, conv_id):
        """Folds all but the most recent `keep_last` messages into a single AI-generated
        summary, freeing most of their context-window cost while keeping the gist available
        to the model on every future turn (see build_system_prompt/build_chat_messages_payload).
        The original messages are left untouched in the DB — only the sent-to-model payload
        changes — so nothing is lost from the visible transcript or exports."""
        global compact_active
        if any_chat_active():
            self.send_json({"error": "cannot compact while a response is generating"}, 409)
            return
        if image_gen_status["active"]:
            self.send_json({"error": "generating an image — try again shortly"}, 409)
            return
        # Must happen before compact_active is claimed below - ensure_chat_model_available()
        # itself treats a True compact_active as "someone else is busy," so calling it after
        # claiming the flag here would make it reject its own caller.
        restore_error = ensure_chat_model_available()
        if restore_error:
            self.send_json({"error": restore_error}, 502)
            return
        with compact_lock:
            if compact_active:
                self.send_json({"error": "a compaction is already in progress — try again shortly"}, 409)
                return
            compact_active = True
        cancel_pending_titles()
        try:
            body = self.read_json()
            try:
                keep_last = max(2, int(body.get("keep_last", 6)))
            except (TypeError, ValueError):
                keep_last = 6

            with db() as conn:
                conv = conn.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
                if conv is None:
                    self.send_json({"error": "conversation not found"}, 404)
                    return
                rows = conn.execute(
                    "SELECT id, role, content FROM messages WHERE conversation_id=? AND id > ? ORDER BY id",
                    (conv_id, conv["compacted_through_id"] or 0),
                ).fetchall()
                if len(rows) <= keep_last:
                    self.send_json({"error": "not enough conversation history to compact yet"}, 400)
                    return
                to_summarize = rows[:-keep_last]
                cutoff_id = to_summarize[-1]["id"]
                transcript_parts = []
                if conv["compacted_summary"]:
                    transcript_parts.append("Summary of even earlier turns:\n" + conv["compacted_summary"])
                transcript_parts += [f"{m['role']}: {m['content']}" for m in to_summarize if m["content"]]
                transcript = "\n\n".join(transcript_parts)

            summarize_payload = {
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Summarize the conversation below into a compact note for your own future "
                            "reference — you will only see this note, not the original messages. Preserve "
                            "names, facts, decisions, preferences, and any unresolved questions or tasks. "
                            "Omit small talk and filler. Write plain prose or short bullet points, not a "
                            "transcript."
                        ),
                    },
                    {"role": "user", "content": transcript},
                ],
                "stream": False,
                "temperature": 0.2,
                "max_tokens": 800,
            }
            try:
                req = urllib.request.Request(
                    LLAMA_URL + "/v1/chat/completions",
                    data=json.dumps(summarize_payload).encode(),
                    headers=llama_headers({"Content-Type": "application/json"}),
                )
                with urllib.request.urlopen(req, timeout=180) as resp:
                    data = json.loads(resp.read().decode())
                summary = data["choices"][0]["message"]["content"].strip()
            except Exception as e:
                self.send_json({"error": model_server_down_message() if is_connection_failure(e) else f"could not summarize conversation: {e}"}, 502)
                return
            if not summary:
                self.send_json({"error": "model returned an empty summary"}, 502)
                return

            with db() as conn:
                conn.execute(
                    "UPDATE conversations SET compacted_summary=?, compacted_through_id=? WHERE id=?",
                    (summary, cutoff_id, conv_id),
                )
            self.send_json({"ok": True, "compacted_through_id": cutoff_id, "summary": summary})
        finally:
            compact_active = False

    def _write_frame(self, obj):
        """Writes one `data: {...}` frame to the client; False once the client is gone (Stop / window closed)."""
        try:
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
            self.wfile.flush()
            return True
        except OSError:
            return False

    def _stream_one_completion(self, upstream, client_gone):
        """Consumes one llama-server SSE response, forwarding content
        through to self.wfile exactly as this whole method's body did before
        tool-calling existed (it's the same code, just extracted so
        _stream_and_save can call it more than once for a tool-call
        round-trip - see there), while also accumulating any tool_calls
        delta fragments. This accumulation is harmless/inert when none ever
        appear: a delta with neither content nor tool_calls was already
        safely ignored by the client's SSE parser before tool-calls existed
        (yields nothing), so nothing here changes behavior for a request
        that never included "tools" in the first place.

        Returns (assistant_text_list, reasoning_text_list, tool_calls_dict,
        client_gone, hallucination_detected, finish). tool_calls_dict (index ->
        {id, name, arguments}) is only ever non-empty if the stream's own
        finish_reason was literally "tool_calls" - matching the OpenAI
        streaming contract where finish_reason, not the mere presence of a
        tool_calls delta, is the authoritative "the model is done requesting
        calls" signal. `finish` is {"reason": the stream's final finish_reason
        or None, "tokens": how many tokens it generated} - "length" is how
        llama-server says a reply was cut short (the reply limit, or a full
        context window), which _stream_and_save must not mistake for a
        finished reply.

        reasoning_text is accumulated purely so _stream_and_save can persist
        it; the reasoning_content lines themselves still stream straight
        through to the client untouched, exactly as before."""
        assistant_text = []
        reasoning_text = []
        tool_calls = {}
        saw_tool_calls_finish = False
        announced = {}                   # create_document calls told to the client, index -> the title announced (or None)
        finish = {"reason": None, "tokens": 0, "ended": False}
        streamed_pieces = 0
        # See FAKE_IMAGE_CLAIM_RE's comment - the start of the real answer
        # content is held back (never the reasoning_content stream, which
        # keeps flowing live and unaffected) just long enough to rule a
        # hallucinated claim in or out before committing to show it.
        content_buffer_raw = []
        content_buffer_text = ""
        content_decided = False
        hallucination_detected = False
        try:
            with upstream:
                for raw in upstream:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    piece = None
                    if data == "[DONE]":
                        finish["ended"] = True
                    if data != "[DONE]":
                        try:
                            parsed = json.loads(data)
                            # The final chunk says why the reply ended and how much it generated; read before the
                            # delta, which the final chunk may not carry.
                            timings = parsed.get("timings")
                            if isinstance(timings, dict) and isinstance(timings.get("predicted_n"), int):
                                finish["tokens"] = timings["predicted_n"]
                            stop_reason = parsed["choices"][0].get("finish_reason")
                            if stop_reason:
                                finish["reason"] = stop_reason
                                finish["ended"] = True
                            delta = parsed["choices"][0]["delta"]
                            piece = delta.get("content")
                            if piece:
                                assistant_text.append(piece)
                                streamed_pieces += 1
                            reasoning_piece = delta.get("reasoning_content")
                            if reasoning_piece:
                                reasoning_text.append(reasoning_piece)
                                streamed_pieces += 1
                            for tc_delta in delta.get("tool_calls") or []:
                                idx = tc_delta.get("index", 0)
                                entry = tool_calls.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                                if tc_delta.get("id"):
                                    entry["id"] = tc_delta["id"]
                                fn = tc_delta.get("function") or {}
                                if fn.get("name"):
                                    entry["name"] = fn["name"]
                                if fn.get("arguments"):
                                    entry["arguments"] += fn["arguments"]
                                if entry["name"] == "create_document" and not client_gone:
                                    # The document tool at work: the client shows "creating document" (with the title
                                    # once it is complete in the arguments) instead of typing the arguments out.
                                    title = partial_json_string(entry["arguments"], "title")
                                    if idx not in announced or (title and announced[idx] != title):
                                        announced[idx] = title
                                        if content_buffer_raw and not content_decided:
                                            # The reply's first words are held back for the image-claim lookahead
                                            # (above); a tool call is no such claim, and the client must get them
                                            # BEFORE the creating frame, or the row lands ahead of the text it follows.
                                            content_decided = True
                                            if not client_gone:
                                                try:
                                                    for buffered in content_buffer_raw:
                                                        self.wfile.write((buffered if buffered.endswith(b"\n") else buffered + b"\n") + b"\n")
                                                    self.wfile.flush()
                                                except OSError:
                                                    client_gone = True
                                            content_buffer_raw = []
                                        if not self._write_frame({"document": {"state": "creating", "title": title, "index": idx}}):
                                            client_gone = True
                            if parsed["choices"][0].get("finish_reason") == "tool_calls":
                                saw_tool_calls_finish = True
                        except (ValueError, KeyError, IndexError, AttributeError, TypeError):
                            pass

                    if content_decided or not piece:
                        # Reasoning-only/tool-call-only lines (piece is None)
                        # always pass straight through, same as before this
                        # check existed - only the leading edge of real answer
                        # content is ever held back, and only until decided.
                        if not client_gone:
                            try:
                                self.wfile.write((raw if raw.endswith(b"\n") else raw + b"\n") + b"\n")
                                self.wfile.flush()
                            except OSError:
                                # Client aborted (Stop button / closed window).
                                # Breaking out of `with upstream:` here closes our
                                # connection to llama-server immediately instead
                                # of continuing to drain it - llama-server detects
                                # the closed downstream connection and cancels
                                # that slot's generation, so Stop actually stops
                                # the model instead of just stopping the display
                                # (confirmed live via /slots: a stopped generation
                                # showed is_processing:false ~5s later, while an
                                # identical un-stopped control prompt was still
                                # is_processing:true 20+s in - the break is what
                                # ends it, not coincidental natural completion).
                                # assistant_text already has whatever streamed
                                # before the disconnect, so nothing generated so
                                # far is lost.
                                client_gone = True
                                break
                        continue

                    content_buffer_raw.append(raw)
                    content_buffer_text += piece
                    if len(content_buffer_text) < FAKE_IMAGE_CLAIM_LOOKAHEAD_CHARS:
                        continue
                    if FAKE_IMAGE_CLAIM_RE.match(content_buffer_text):
                        hallucination_detected = True
                        break
                    content_decided = True
                    if not client_gone:
                        try:
                            for buffered in content_buffer_raw:
                                self.wfile.write((buffered if buffered.endswith(b"\n") else buffered + b"\n") + b"\n")
                            self.wfile.flush()
                        except OSError:
                            client_gone = True
                    content_buffer_raw = []
        except (OSError, http.client.HTTPException):
            # The model server died mid-reply: its chunked stream ends in an http.client.IncompleteRead - an
            # HTTPException, not an OSError - which used to escape here, past the "stopped" note, the rollback and
            # active_chats.pop below, leaving the chat "already generating" until the app restarted.
            pass
        # A short reply can end (or the connection drop) before ever
        # reaching the lookahead threshold above - decide now instead of
        # leaving it stuck in the buffer and silently never sent.
        if not hallucination_detected and content_buffer_raw and not content_decided:
            if FAKE_IMAGE_CLAIM_RE.match(content_buffer_text):
                hallucination_detected = True
            elif not client_gone:
                try:
                    for buffered in content_buffer_raw:
                        self.wfile.write((buffered if buffered.endswith(b"\n") else buffered + b"\n") + b"\n")
                    self.wfile.flush()
                except OSError:
                    client_gone = True
        if not finish["tokens"]:
            finish["tokens"] = streamed_pieces       # no timings in the stream: one delta is about one token
        return (
            assistant_text, reasoning_text, (tool_calls if saw_tool_calls_finish else {}),
            client_gone, hallucination_detected, finish,
        )

    def _stream_and_save(
        self, conv_id, payload, slot_id, auto_title=False, user_text=None, user_message_id=None,
        current_user_message_id=None, regenerate_message_id=None,
    ):
        """regenerate_message_id: when set, the finished reply overwrites that
        existing assistant row instead of being INSERTed as a new one, and the
        content it displaces is archived into message_versions - see
        handle_edit_message's regenerate branch. Nothing is written until the
        reply is known to be non-empty, so a failed regeneration leaves the
        original answer in place."""
        # id_slot pins this request to the slot _reserve_chat_slot() already
        # reserved for conv_id, so get_active_slot_progress() can report this
        # specific conversation's progress instead of guessing which of up to
        # max_parallel_chats() simultaneously-processing slots it is.
        payload = dict(payload, id_slot=slot_id)
        try:
            # Nothing listening on the model server's port: say why straight away. Asking a closed port costs ~2 s on Windows before it is
            # refused, and the refusal itself - "<urlopen error [WinError 10061] No connection could be made because the target machine
            # actively refused it>" - says nothing of why (still loading? crashed? never started?).
            if not port_accepts_connections(LLAMA_PORT):
                raise urllib.error.URLError(ConnectionRefusedError(10061, "nothing is listening on the model server's port"))
            req = urllib.request.Request(
                LLAMA_URL + "/v1/chat/completions",
                data=json.dumps(payload).encode(),
                headers=llama_headers({"Content-Type": "application/json"}),
            )
            upstream = urllib.request.urlopen(req, timeout=600)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            # urllib wraps only CONNECT-phase errors in URLError: a model server that accepts the connection and then
            # dies before its status line raises a bare RemoteDisconnected / BadStatusLine (getresponse in do_open is
            # outside that wrapping). It escaped here, so the user's turn was never rolled back and the slot stayed
            # reserved ("already generating a reply" until a restart). is_connection_failure reads the bare error too.
            active_chats.pop(conv_id, None)
            detail = str(e)
            server_down = not isinstance(e, urllib.error.HTTPError) and is_connection_failure(e)
            if server_down:
                detail = model_server_down_message()
            if isinstance(e, urllib.error.HTTPError):
                try:
                    detail = json.loads(e.read().decode())["error"]["message"]
                except Exception:
                    pass
            if user_message_id is not None:
                # The user's message was already saved before this request was
                # attempted. Roll it back on total failure so the conversation
                # isn't left with a dangling, unanswered turn that would break
                # strict user/assistant alternation on the next attempt.
                with db() as conn:
                    conn.execute("DELETE FROM message_images WHERE message_id=?", (user_message_id,))
                    conn.execute("DELETE FROM messages WHERE id=?", (user_message_id,))
                    if auto_title:
                        # The temp truncated-message title was set optimistically
                        # before this failure — revert it too, or the sidebar
                        # shows a title for a conversation with zero messages.
                        conn.execute(
                            "UPDATE conversations SET title='New chat' WHERE id=?", (conv_id,)
                        )
            self.send_json({"error": detail if server_down else f"model server error: {detail}", "rolled_back": user_message_id is not None}, 502)
            return

        # Stream Server-Sent Events through to the browser. "Connection: close"
        # lets us stream without chunked encoding on HTTP/1.1.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        assistant_text = []
        # Accumulated across every round trip, not per-round: a tool-call turn
        # can think, call a tool, then think again before answering, and the
        # Thoughts panel should show that as one continuous transcript for the
        # prompt rather than silently keeping only the last leg.
        reasoning_text = []
        client_gone = False
        hallucination_detected = False
        current_payload = payload
        # A tool-call round-trip re-requests llama-server with the tool's
        # real result appended - each iteration streams through this SAME
        # already-open client connection (headers are only ever sent once,
        # above), so the user still sees live token-by-token output for
        # whichever turn ends up being the real answer, not a synthetic
        # replay of already-known text. Capped at MAX_TOOL_ROUND_TRIPS (a
        # single tool-call-then-answer cycle, not a full agentic loop) - if
        # the follow-up turn also wants to call a tool, that request is
        # simply not honored, same graceful-degradation posture as
        # client_gone below.
        finish = {"reason": None, "tokens": 0}
        # Documents the create_document tool stored this turn: attached to the reply's row once it is saved below, or
        # removed again if the reply never lands (nothing is stored at all for a call the client cancelled mid-way:
        # the tools only run once the model's call is complete and the client is still there).
        created_documents = []
        for round_trip in range(MAX_TOOL_ROUND_TRIPS + 1):
            (
                round_text, round_reasoning, tool_calls, client_gone, hallucination_detected, finish,
            ) = self._stream_one_completion(upstream, client_gone)
            # Accumulated across the rounds like the reasoning: what the model said before calling a tool ("ok, one
            # moment") was streamed to the user but dropped from the saved reply - only the last round survived.
            assistant_text += round_text
            reasoning_text += round_reasoning
            if not tool_calls or client_gone or round_trip == MAX_TOOL_ROUND_TRIPS:
                break
            tool_context = {"conv_id": conv_id, "offset": len("".join(assistant_text)), "created": [], "events": []}
            tool_result_messages = execute_tool_calls(tool_calls, tool_context)
            created_documents += tool_context["created"]
            for event in tool_context["events"]:
                if not client_gone and not self._write_frame({"document": event}):
                    client_gone = True
            current_payload = dict(current_payload, messages=current_payload["messages"] + [
                {
                    "role": "assistant",
                    "content": "".join(round_text) or None,
                    "tool_calls": [
                        {
                            "id": tc.get("id") or f"call_{i}",
                            "type": "function",
                            "function": {"name": tc.get("name"), "arguments": tc.get("arguments") or "{}"},
                        }
                        for i, tc in enumerate(tool_calls.values())
                    ],
                },
                *tool_result_messages,
            ])
            try:
                req = urllib.request.Request(
                    LLAMA_URL + "/v1/chat/completions",
                    data=json.dumps(dict(current_payload, id_slot=slot_id)).encode(),
                    headers=llama_headers({"Content-Type": "application/json"}),
                )
                upstream = urllib.request.urlopen(req, timeout=600)
            except (urllib.error.URLError, OSError, http.client.HTTPException):
                # (a bare RemoteDisconnected escapes urllib's URLError wrapping - see the first request above)
                # Headers are already committed to 200 by this point, so
                # there's no clean way to surface this as an error response -
                # stop the round-trip loop and fall through with whatever
                # assistant_text exists (likely none, since a tool-call-only
                # turn has no real content of its own), same degrade-
                # gracefully posture as client_gone. The first round ended cleanly (finish_reason
                # tool_calls), so the fall-through used to read "no response was generated - try
                # again or adjust sampling settings": it is the model server that went away between
                # the rounds - the server-cut note says so instead.
                finish = dict(finish, ended=False, reason=None)
                break

        active_chats.pop(conv_id, None)
        if hallucination_detected:
            # Discard whatever fake claim was accumulating (never sent to
            # the client - it was still held in content_buffer_raw) and
            # replace it with an honest explanation, both streamed live
            # and saved to the DB in its place, so this turn's own
            # history doesn't perpetuate the same pattern on the next one.
            replacement = (
                "I can't actually generate or edit an image as part of a regular reply — "
                'try "generate an image of ...", "edit the image to ...", or (right after a '
                'real image) a quick follow-up like "make it ..."/"what about ...".'
            )
            assistant_text = [replacement]
            if not client_gone:
                try:
                    self.wfile.write(
                        b"data: " + json.dumps(
                            {"choices": [{"delta": {"content": replacement}, "index": 0, "finish_reason": None}]}
                        ).encode() + b"\n\n"
                    )
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except OSError:
                    pass
        full = "".join(assistant_text)
        # Stored alongside the reply it produced, so it inherits every
        # existing cleanup path for free (message delete, conversation delete,
        # edit-and-truncate, regenerate, "Reset app data", backup/restore) -
        # a separate table would have needed each of those taught about it.
        # NULL rather than "" when the model didn't think, so has_reasoning
        # below is a straight NULL check.
        full_reasoning = "".join(reasoning_text).strip() or None
        # A reply llama-server ended with finish_reason "length" was cut short - by the reply limit if the user set one and it was
        # reached, otherwise because the context window filled up. It used to be saved as if it were complete (or, if the model was
        # still thinking, thrown away along with the user's message), with no word of why. Now the reply says so, in the saved
        # text as well, so a reload shows it too.
        cut = None
        if finish["reason"] == "length" and not client_gone and not hallucination_detected:
            cut = describe_cut_off(current_payload.get("max_tokens"), finish["tokens"], load_settings(), find_model())
        elif not finish["ended"] and not client_gone and not hallucination_detected:
            # The stream from llama-server stopped without its final chunk or [DONE]: the model server died mid-reply (a CUDA error on a GPU
            # the engine cannot really run on does exactly this), the connection dropped, or it stopped answering. The reply just ended,
            # silently, as if it were complete.
            cut = {"reason": "server", "generated": finish["tokens"], "limit": None, "context": None, "max_context": None,
                   "message": server_stopped_reason()}
        note_only = False
        if cut is not None:
            note = cut_off_note(cut, has_answer=bool(full.strip()), has_thoughts=bool(full_reasoning))
            if not full.strip() and regenerate_message_id is None and (
                user_message_id is not None or current_user_message_id is not None
            ) and (cut["reason"] != "server" or full_reasoning):
                # Nothing but thinking came out. The turn stays (the user's message is kept - the reply that explains is stored in
                # its place, so the conversation still alternates) and so do the thoughts. (A server that died before it produced
                # anything at all is a failed request: that one is rolled back below, like a refused connection.)
                note_only = True
            elif not full.strip():
                note = ""            # a regenerate that came to nothing keeps the reply it was meant to replace
            if note:
                assistant_text = assistant_text + [note]
                full = "".join(assistant_text)
            if not client_gone:
                try:
                    if note:
                        self.wfile.write(
                            b"data: " + json.dumps({"choices": [{"delta": {"content": note}, "index": 0, "finish_reason": None}]}).encode() + b"\n\n"
                        )
                    # The client shows a notice for it (and a regenerate that came to nothing says why it kept the old reply).
                    self.wfile.write(b"data: " + json.dumps({"truncated": cut}).encode() + b"\n\n")
                    self.wfile.flush()
                except OSError:
                    client_gone = True
        version_pos = version_count = 1
        if full or created_documents:
            with db() as conn:
                if regenerate_message_id is not None:
                    new_assistant_id, version_pos, version_count = archive_and_replace_version(
                        conn, regenerate_message_id, full, full_reasoning
                    )
                    if new_assistant_id is None:
                        # The row vanished between the request starting and the
                        # reply landing (conversation deleted, "Reset app data",
                        # a restore swapping the DB out from under this). There
                        # is nothing left to overwrite and re-INSERTing would
                        # resurrect a turn the user removed, so drop the reply -
                        # the client's own [DONE] handling still ends the stream
                        # cleanly, and a reload shows the real current state.
                        for doc in created_documents:
                            conn.execute("DELETE FROM documents WHERE id=?", (doc["id"],))
                        return
                else:
                    cur = conn.execute(
                        "INSERT INTO messages (conversation_id, role, content, reasoning) "
                        "VALUES (?, 'assistant', ?, ?)",
                        (conv_id, full, full_reasoning),
                    )
                    new_assistant_id = cur.lastrowid
                for doc in created_documents:
                    conn.execute("UPDATE documents SET message_id=? WHERE id=?", (new_assistant_id, doc["id"]))
                conn.execute(
                    "UPDATE conversations SET updated_at=datetime('now','localtime') WHERE id=?",
                    (conv_id,),
                )
            # Reported *after* [DONE] (already forwarded live, mid-loop,
            # above) rather than replacing it - confirmed the client's SSE
            # reader (api_client.py's stream_chat) only stops once the
            # connection actually closes, never on seeing "data: [DONE]"
            # itself (that line is explicitly skipped, not treated as a
            # terminator) - so anything written here still arrives. Lets
            # the transcript widget tag which rendered bubbles correspond
            # to which DB row ids, for the edit/regenerate affordances,
            # without a full reload. Reports whichever user message this
            # reply answers - the fresh INSERT's id for an ordinary send
            # (user_message_id), or the pre-existing one an edit/
            # regenerate turn was for (current_user_message_id).
            report_user_id = user_message_id if user_message_id is not None else current_user_message_id
            if not client_gone and report_user_id is not None:
                try:
                    self.wfile.write(
                        b"data: " + json.dumps(
                            {"message_ids": {
                                "user": report_user_id, "assistant": new_assistant_id,
                                # Lets the transcript draw the "[<] 2/3 [>]"
                                # control on a just-regenerated reply straight
                                # away instead of only after the next reload.
                                # Always 1/1 for an ordinary send.
                                "version_pos": version_pos, "version_count": version_count,
                                # The document cards this reply made (their frames came earlier, mid-stream).
                                "documents": [doc["id"] for doc in created_documents],
                            }}
                        ).encode() + b"\n\n"
                    )
                    self.wfile.flush()
                except OSError:
                    pass
            if auto_title and not note_only:
                start_title_generation(conv_id, user_text, full)
            elif not note_only:
                # A title abandoned earlier (the user sent this message before it was done) is asked for again now that the
                # model is free - it used to be lost for good. generate_conversation_title still only replaces the placeholder.
                retry = take_title_retry(conv_id)
                if retry:
                    start_title_generation(conv_id, *retry)
        elif not client_gone and user_message_id is not None:
            # The stream ran to completion (not user-stopped) but never
            # produced any final `content` - only `reasoning_content`, which
            # is deliberately never persisted (see api_client.py). Confirmed
            # live: sampling settings open enough (e.g. top_k/top_p both
            # disabled with high temperature) let a reasoning model ramble
            # in reasoning_content and stop without ever answering. Left
            # alone, that used to silently vanish - no error, no assistant
            # message - and the dangling unanswered user turn would break
            # strict user/assistant alternation the next time this
            # conversation was used (same hazard the URLError branch above
            # already guards against for a harder failure). Roll it back
            # the same way, and tell the client - the SSE response is
            # already committed to 200 by this point so a JSON error isn't
            # possible; a synthetic content delta is the only way left to
            # surface this instead of silence.
            note = "_(no response was generated - try again or adjust sampling settings.)_"
            if cut is not None and cut["reason"] == "server":
                note = cut_off_note(cut, has_answer=False, has_thoughts=False)
            try:
                self.wfile.write(
                    b"data: " + json.dumps(
                        {"choices": [{"delta": {"content": note}, "index": 0, "finish_reason": None}]}
                    ).encode() + b"\n\n"
                )
                self.wfile.flush()
            except OSError:
                pass
            with db() as conn:
                conn.execute("DELETE FROM message_images WHERE message_id=?", (user_message_id,))
                conn.execute("DELETE FROM messages WHERE id=?", (user_message_id,))
                if auto_title:
                    conn.execute(
                        "UPDATE conversations SET title='New chat' WHERE id=?", (conv_id,)
                    )


# ---------------------------------------------------------------- lifecycle

httpd = None


class AppHTTPServer(ThreadingHTTPServer):
    """The app's API server. HTTPServer turns on SO_REUSEADDR, which on Windows
    (unlike Linux) lets a second process bind a port that something else is
    already listening on - verified: the second bind succeeds and the two
    servers then share the port, with connections going to either. That would
    let a stray second copy (or a headless server.py next to the desktop app)
    quietly take over half the requests. Turning it off makes the second bind
    fail with WinError 10048, which is what every caller wants; rebinding
    right after a normal exit is unaffected (checked with connections in
    TIME_WAIT)."""

    allow_reuse_address = False

    def handle_error(self, request, client_address):
        """socketserver's hook for an exception that escaped a request - here one raised after the route itself finished, while the
        answer was being flushed or the connection closed. A client that hung up gets one line; everything else the usual report."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            print(f"note: a client went away before its answer was fully sent ({exc.__class__.__name__})")
            return
        super().handle_error(request, client_address)


def shutdown():
    time.sleep(0.3)  # let the /api/shutdown response flush
    stop_llama()
    # A resident image model (see image_model_resident) is only ever torn
    # down by a real chat message or the idle watcher - neither necessarily
    # happens before the app closes, so without this an image-editing
    # session left "warm" at shutdown orphaned sd-server.exe (confirmed
    # live: it kept holding VRAM after a clean /api/shutdown).
    stop_sd_server()
    stop_embed_server()
    if httpd:
        httpd.shutdown()


def already_running():
    # Detects a prior instance still holding the app port (e.g. the desktop
    # window was closed without using the power button) so we never spawn a
    # second llama-server on top of it. desktop_main.py additionally holds an
    # OS mutex from its very first lines, which covers the window before this
    # port is even bound; this is the check that still matters for the
    # headless server.py entry point and for a window-less leftover instance.
    if not port_accepts_connections(APP_PORT):
        return False  # nothing listening (and a closed port is slow to ask - see port_accepts_connections)
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{APP_PORT}/api/health", timeout=5)
        return True
    except urllib.error.HTTPError:
        return False  # something else is answering on this port, and it isn't us
    except Exception:
        # Listening but slow or silent - most likely us, mid-startup or busy.
        # Wrongly guessing "not running" would spawn a second llama-server.
        return True


def main():
    # Headless dev entry point: runs the API server in the foreground with
    # no GUI. The normal way to run the app is app/desktop_main.py.
    global httpd
    if already_running():
        print("Personal Terminal is already running on port", APP_PORT)
        return
    # Bind the port FIRST, as desktop_main does: if something holds it we can say
    # so before a model load has been started (it used to bind last, after
    # start_llama(), and then had to tear that down again). Nothing is served
    # until serve_forever() below.
    try:
        httpd = AppHTTPServer(("127.0.0.1", APP_PORT), Handler)
    except OSError as e:
        print(f"ERROR: could not start the web server ({e})")
        return
    try:
        init_db()
        if not os.path.exists(SETTINGS_PATH):
            save_settings(DEFAULT_SETTINGS)
        # Before start_llama(), which reads the settings this may rewrite.
        migrate_settings()
        start_llama()
        start_embed_server()
        start_idle_restore_watcher()
        start_db_maintenance()
        print(f"Personal Terminal running at http://127.0.0.1:{APP_PORT}")
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Also reached when a start-up step raises: whatever was already
        # launched (llama-server, say, when the embedding server then failed)
        # must not be left running with no app.
        stop_llama()
        stop_sd_server()
        stop_embed_server()
        httpd.server_close()
        close_db()


if __name__ == "__main__":
    main()
