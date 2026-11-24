"""
HTTP client for the local Local AI ChatBot API (127.0.0.1 only).

Mirrors the fetch() calls in app/ui/app.js exactly - same endpoints, same
JSON shapes, same SSE framing for chat streaming - just called from Python
over urllib instead of from the browser over fetch().
"""

import http.client
import json
import os
import re
import select
import urllib.error
import urllib.parse
import urllib.request


class ApiError(Exception):
    def __init__(self, message, status=None, rolled_back=False):
        super().__init__(message)
        self.status = status
        self.rolled_back = rolled_back


class ApiClient:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def _request(self, method, path, body=None, timeout=10):
        url = self.base_url + path
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise ApiError(*_parse_error(e)) from None
        except urllib.error.URLError as e:
            raise ApiError(str(e.reason)) from None
        except TimeoutError:
            # NOT redundant with URLError above, and not a theoretical case -
            # this is a startup crash that hit twice for real (see the two
            # tracebacks in data/desktop.log). urllib only wraps socket errors
            # raised while *connecting* into a URLError; once the connection is
            # accepted, the response read happens outside that wrapper, so a
            # backend that accepts but is slow to answer (llama-server still
            # coming up) surfaces a bare builtins.TimeoutError straight from
            # socket.readinto. TimeoutError is an OSError, not a URLError, so
            # it sailed past both clauses above and killed whichever caller was
            # unlucky enough to be first. socket.timeout needs no separate
            # clause - it has been an alias of TimeoutError since Python 3.10,
            # and this app ships on 3.12 (runtime/python).
            raise ApiError(_timeout_message(timeout)) from None

    def get_raw(self, path, timeout=30):
        """Like _request(), but for endpoints that respond with a raw file body
        (e.g. the save_to_disk=False export endpoints) instead of a JSON
        envelope - _request() would fail trying to json.loads() those. Returns
        (content_bytes, suggested_filename_or_None)."""
        req = urllib.request.Request(self.base_url + path, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content = resp.read()
                cd = resp.headers.get("Content-Disposition", "")
                match = re.search(r'filename="([^"]+)"', cd)
                return content, (match.group(1) if match else None)
        except urllib.error.HTTPError as e:
            raise ApiError(*_parse_error(e)) from None
        except urllib.error.URLError as e:
            raise ApiError(str(e.reason)) from None
        except TimeoutError:
            raise ApiError(_timeout_message(timeout)) from None

    # ---- lifecycle ----

    def health(self):
        return self._request("GET", "/api/health", timeout=2)

    def show_window(self):
        return self._request("POST", "/api/show-window", timeout=2)

    def shutdown(self):
        return self._request("POST", "/api/shutdown", timeout=5)

    # ---- conversations ----

    def list_conversations(self, search=None):
        path = "/api/conversations"
        if search:
            path += "?search=" + urllib.parse.quote(search)
        return self._request("GET", path)

    def get_conversation(self, conv_id):
        return self._request("GET", f"/api/conversations/{conv_id}")

    def get_document(self, doc_id):
        """A document the create_document tool made, content included (the conversation payload carries only the
        card data: id, title, filename, bytes, version, offset)."""
        return self._request("GET", f"/api/documents/{doc_id}", timeout=30)

    def get_document_bytes(self, doc_id):
        """The document's text as UTF-8 bytes, for saving it to disk: (bytes, filename)."""
        return self.get_raw(f"/api/documents/{doc_id}?raw=1")

    def delete_document(self, doc_id):
        return self._request("DELETE", f"/api/documents/{doc_id}")

    def get_thoughts(self, conv_id):
        """Saved reasoning transcripts for this conversation. Fetched only
        when the Thoughts panel is opened - get_conversation deliberately
        carries just a has_reasoning flag per message, not the text."""
        return self._request("GET", f"/api/conversations/{conv_id}/thoughts")

    def create_conversation(self, project_id=None, folder_id=None):
        body = {}
        if project_id is not None:
            body["project_id"] = project_id
        if folder_id is not None:
            body["folder_id"] = folder_id
        return self._request("POST", "/api/conversations", body=body)

    def rename_conversation(self, conv_id, title):
        return self._request("PATCH", f"/api/conversations/{conv_id}", body={"title": title})

    def update_conversation(self, conv_id, **fields):
        return self._request("PATCH", f"/api/conversations/{conv_id}", body=fields)

    def delete_conversation(self, conv_id):
        return self._request("DELETE", f"/api/conversations/{conv_id}")

    def upload_conversation_file(self, conv_id, filepath):
        # Same raw-stream contract as upload_project_file - the server
        # extracts text (pdf/docx/plain) and stores it against the
        # conversation, not the file bytes themselves.
        return self._stream_upload(f"/api/conversations/{conv_id}/files", filepath, timeout=60)

    def delete_conversation_file(self, conv_id, file_id):
        return self._request("DELETE", f"/api/conversations/{conv_id}/files/{file_id}")

    # ---- settings ----

    def get_settings(self):
        return self._request("GET", "/api/settings")

    def get_vram_status(self):
        return self._request("GET", "/api/vram-status")

    def save_settings(self, body):
        # Can trigger a llama-server restart (stop_llama waits up to 10s for
        # a clean exit before killing it) - give it real headroom.
        return self._request("POST", "/api/settings", body=body, timeout=20)

    def get_model_info(self):
        return self._request("GET", "/api/model")

    def estimate_vram(self, gpu_layers, context_size, kv_cache_type, kv_offload=True, model=None,
                       max_parallel_chats=None, gpu_layers_auto=None):
        body = {
            "gpu_layers": gpu_layers, "context_size": context_size,
            "kv_cache_type": kv_cache_type, "kv_offload": kv_offload,
        }
        if model:
            body["model"] = model
        if max_parallel_chats is not None:
            body["max_parallel_chats"] = max_parallel_chats
        if gpu_layers_auto is not None:
            # The server projects full offload for Auto (gpu_layers above is
            # ignored in that case) - see handle_estimate_vram.
            body["gpu_layers_auto"] = gpu_layers_auto
        return self._request("POST", "/api/settings/estimate-vram", body=body, timeout=10)

    def get_gpus(self, refresh=False):
        """The GPUs the model engine sees, in the order they are used (largest memory first unless the user ordered them), which the
        installed engine build can run, and the placement the running model was given. Asks the engine's own tools (~0.5 s), so
        call it off the Tk thread; `refresh` skips the 15-second cache."""
        return self._request("GET", "/api/gpus" + ("?refresh=1" if refresh else ""), timeout=30)

    def plan_gpus(self, overrides=None, model=None, refresh=False):
        """Where the model would go under GPU or size settings that are not saved yet - any of gpu_placement, gpu_order, gpu_disabled,
        engine, context_size, kv_cache_type, kv_offload, batch_size, max_parallel_chats - as the placement plan. The first answer for
        a model that does not fit on one GPU alone takes a few seconds (the engine's own memory estimates), the same question again
        is cached: call it off the Tk thread."""
        body = dict(overrides or {})
        if model:
            body["model"] = model
        if refresh:
            body["refresh"] = True
        return self._request("POST", "/api/gpus/plan", body=body, timeout=90)

    # ---- model / mmproj asset management ----
    # `kind` is "models" or "mmproj" - the two asset types in scope for the
    # desktop rewrite (image-models are being redesigned separately, not
    # ported - see the project memory on the image-gen decision).

    def list_assets(self, kind):
        return self._request("GET", f"/api/{kind}")

    def set_active_asset(self, kind, filename):
        # filename="" is meaningful for mmproj (turns vision off)
        return self._request("POST", f"/api/{kind}/active", body={"filename": filename}, timeout=20)

    def restart_model_server(self):
        """A2: stop and start the chat model server with the current settings (409 with a sentence while an image job
        or a reply is running); the caller follows it with the same health poll as set_active_asset."""
        return self._request("POST", "/api/model-server/restart", body={}, timeout=30)

    def delete_asset(self, kind, filename):
        return self._request("DELETE", f"/api/{kind}/{urllib.parse.quote(filename)}")

    def download_asset(self, kind, url, filename):
        return self._request("POST", f"/api/{kind}/download", body={"url": url, "filename": filename})

    def download_progress(self, kind, filename):
        return self._request("GET", f"/api/{kind}/download-progress?filename=" + urllib.parse.quote(filename), timeout=5)

    def upload_asset(self, kind, filepath, on_progress=None):
        """Stream a local file to /api/{kind}/upload without loading the
        whole thing into memory - model files can be many GB."""
        return self._stream_upload(f"/api/{kind}/upload", filepath, on_progress=on_progress)

    def _stream_upload(self, path, filepath, on_progress=None, timeout=3600):
        filename = os.path.basename(filepath)
        size = os.path.getsize(filepath)
        parsed = urllib.parse.urlsplit(self.base_url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
        try:
            conn.putrequest("POST", path)
            conn.putheader("X-Filename", urllib.parse.quote(filename))
            conn.putheader("Content-Length", str(size))
            conn.endheaders()
            sent = 0
            with open(filepath, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    conn.send(chunk)
                    sent += len(chunk)
                    if on_progress:
                        on_progress(sent, size)
                    if select.select([conn.sock], [], [], 0)[0]:
                        # The server has already answered - an early 4xx such as
                        # "file too large" or "busy", which it sends before reading
                        # the body. Stop pushing the rest of a possibly multi-GB file
                        # at it and read why. (Without this the server read and threw
                        # away up to 256 MB and then closed the connection, so a 300 MB
                        # file ended in "[WinError 10053] ..." instead of the reason.)
                        break
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status >= 400:
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = {"error": raw.decode(errors="replace") or f"HTTP {resp.status}"}
                raise ApiError(payload.get("error", f"HTTP {resp.status}"), status=resp.status)
            return json.loads(raw) if raw else {}
        except (OSError, http.client.HTTPException) as e:
            raise ApiError(str(e)) from None
        finally:
            conn.close()

    # ---- projects ----

    def list_projects(self):
        return self._request("GET", "/api/projects")

    def get_project(self, project_id):
        return self._request("GET", f"/api/projects/{project_id}")

    def create_project(self, name, instructions=""):
        return self._request("POST", "/api/projects", body={"name": name, "instructions": instructions})

    def update_project(self, project_id, **fields):
        return self._request("PATCH", f"/api/projects/{project_id}", body=fields)

    def delete_project(self, project_id, keep_conversations=False, keep_instructions=False,
                        keep_files=False, keep_images=False, keep_memory=False):
        body = {
            "keep_conversations": keep_conversations, "keep_instructions": keep_instructions,
            "keep_files": keep_files, "keep_images": keep_images, "keep_memory": keep_memory,
        }
        return self._request("DELETE", f"/api/projects/{project_id}", body=body)

    def upload_project_file(self, project_id, filepath):
        # Project documents (PDF/DOCX/txt), not GB-scale models, but streaming
        # costs nothing and reuses the same tested path as upload_asset.
        return self._stream_upload(f"/api/projects/{project_id}/files", filepath, timeout=60)

    def delete_project_file(self, project_id, file_id):
        return self._request("DELETE", f"/api/projects/{project_id}/files/{file_id}")

    def upload_project_image(self, project_id, filepath):
        # Captioning is real vision-model inference, not instant - give it
        # much more headroom than the plain-text file upload's 60s.
        return self._stream_upload(f"/api/projects/{project_id}/images", filepath, timeout=180)

    def delete_project_image(self, project_id, image_id):
        return self._request("DELETE", f"/api/projects/{project_id}/images/{image_id}")

    def get_project_image_bytes(self, project_id, image_id):
        url = self.base_url + f"/api/projects/{project_id}/images/{image_id}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return resp.read(), resp.headers.get("Content-Type", "image/png")
        except urllib.error.HTTPError as e:
            raise ApiError(*_parse_error(e)) from None
        except urllib.error.URLError as e:
            raise ApiError(str(e.reason)) from None
        except TimeoutError:
            raise ApiError(_timeout_message(10)) from None

    def get_message_image_bytes(self, message_id, image_id):
        url = self.base_url + f"/api/messages/{message_id}/images/{image_id}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return resp.read(), resp.headers.get("Content-Type", "image/png")
        except urllib.error.HTTPError as e:
            raise ApiError(*_parse_error(e)) from None
        except urllib.error.URLError as e:
            raise ApiError(str(e.reason)) from None
        except TimeoutError:
            raise ApiError(_timeout_message(10)) from None

    def add_project_memory(self, project_id, content):
        return self._request("POST", f"/api/projects/{project_id}/memory", body={"content": content})

    def delete_project_memory(self, project_id, mem_id):
        return self._request("DELETE", f"/api/projects/{project_id}/memory/{mem_id}")

    # ---- project groups ----

    def list_project_groups(self):
        return self._request("GET", "/api/project-groups")

    def create_project_group(self, name):
        return self._request("POST", "/api/project-groups", body={"name": name})

    def update_project_group(self, group_id, **fields):
        return self._request("PATCH", f"/api/project-groups/{group_id}", body=fields)

    def delete_project_group(self, group_id):
        return self._request("DELETE", f"/api/project-groups/{group_id}")

    # ---- folders ----

    def list_folders(self):
        return self._request("GET", "/api/folders")

    def create_folder(self, name):
        return self._request("POST", "/api/folders", body={"name": name})

    def rename_folder(self, folder_id, name):
        return self._request("PATCH", f"/api/folders/{folder_id}", body={"name": name})

    def delete_folder(self, folder_id):
        return self._request("DELETE", f"/api/folders/{folder_id}")

    # ---- backup / restore ----

    def backup(self, include_outputs=False, timeout=120):
        qs = "?include_outputs=1" if include_outputs else ""
        return self.get_raw(f"/api/backup{qs}", timeout=timeout)

    def restore(self, filepath, timeout=120):
        return self._stream_upload("/api/restore", filepath, timeout=timeout)

    # ---- context meter / compaction ----

    def get_context_usage(self, conv_id):
        return self._request("GET", f"/api/conversations/{conv_id}/context-usage", timeout=15)

    def get_title_status(self, conv_id):
        return self._request("GET", f"/api/conversations/{conv_id}/title-status", timeout=5)

    def compact_conversation(self, conv_id, keep_last=6):
        return self._request(
            "POST", f"/api/conversations/{conv_id}/compact", body={"keep_last": keep_last}, timeout=200
        )

    # ---- chat streaming ----

    def get_generation_progress(self, conversation_id):
        """Real-time prompt-eval progress for conversation_id's own slot
        (proxies llama-server's /slots endpoint, filtered server-side to the
        specific slot this conversation's generation was pinned to - see
        server.py's _reserve_chat_slot/id_slot) - used to show real prefill
        progress instead of a generic spinner while the model is still
        digesting the prompt, before it starts producing any output. Now
        that more than one conversation can be generating at once, conv_id
        disambiguates whose progress this is."""
        path = f"/api/generation-progress?conversation_id={conversation_id}"
        return self._request("GET", path, timeout=3)

    # ---- image generation ----

    def generate_image(self, conversation_id, prompt):
        """Starts an async generate-image job server-side (unloads the chat
        model, runs sd-server, reloads the chat model) - the resulting
        user/assistant messages land directly in the conversation once
        image_gen_status()'s active flag goes back to False, same contract
        the old web UI (app.js) polls."""
        return self._request(
            "POST", "/api/generate-image", body={"conversation_id": conversation_id, "prompt": prompt}, timeout=10
        )

    def image_gen_status(self):
        return self._request("GET", "/api/generate-image/status", timeout=5)

    def cancel_image_gen(self):
        """Kills sd-server out from under the in-flight generate/edit job -
        it has no in-place cancel of its own for a request already being
        computed (only queued ones) - which the worker's existing failure-
        recovery path turns into a clean "cancelled" status instead of a
        raw connection-error string."""
        return self._request("POST", "/api/generate-image/cancel", timeout=10)

    # ---- the Images tab (pictures made outside any chat; files in outputs\generated) ----

    def gallery_generate(self, prompt, **options):
        """Starts a job that saves a picture to the gallery (poll image_gen_status() until active is False, then gallery_list()).
        options: negative_prompt, width, height, steps, cfg_scale, seed, backend ("local" | "comfyui"); the server ignores keys it does not know."""
        return self._request("POST", "/api/images/generate", body={"prompt": prompt, **options}, timeout=10)

    def gallery_list(self):
        """-> {"images": [{name, prompt, width, height, steps, cfg_scale, seed, model, backend, created, bytes}, ...newest first], "folder": path}"""
        return self._request("GET", "/api/images", timeout=10)

    def get_gallery_image_bytes(self, name):
        url = self.base_url + "/api/images/" + urllib.parse.quote(name)
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                return resp.read(), resp.headers.get("Content-Type", "image/png")
        except urllib.error.HTTPError as e:
            raise ApiError(*_parse_error(e)) from None
        except urllib.error.URLError as e:
            raise ApiError(str(e.reason)) from None
        except TimeoutError:
            raise ApiError(_timeout_message(15)) from None

    def delete_gallery_image(self, name):
        return self._request("DELETE", "/api/images/" + urllib.parse.quote(name), timeout=10)

    def edit_image(self, conversation_id, source_message_id, source_image_id, prompt, ref_images=None):
        """Same async-job contract as generate_image() (poll image_gen_status()
        for completion) but conditions on an existing image instead of
        generating from scratch - source_message_id/source_image_id identify
        which prior message_images row to edit."""
        body = {
            "conversation_id": conversation_id,
            "source_message_id": source_message_id,
            "source_image_id": source_image_id,
            "prompt": prompt,
            "ref_images": ref_images or [],
        }
        return self._request("POST", "/api/edit-image", body=body, timeout=10)

    def _stream_sse(self, req, stop_event=None):
        """Shared SSE-consuming loop for stream_chat/stream_edit_message -
        both hit server.py endpoints with identical `data: {...}` framing.
        Yields (kind, value) pairs: "status" ("connected" - the moment the
        HTTP response headers arrive with zero body bytes read yet, meaning
        everything before this point, our request being sent plus server.py's
        own DB/upstream-connect work, is done), "thinking" (a reasoning
        model's chain-of-thought delta, from the OpenAI-compat stream's
        separate `reasoning_content` field - distinct from the final answer
        and never persisted, see server.py's _stream_and_save), "chunk" (a
        real answer text delta), or "message_ids" ({"user":..., "assistant":...}
        - sent once, after the reply is fully saved server-side, so the
        transcript can tag which rendered messages correspond to which DB
        rows without a full reload), or "truncated" ({"reason": "context" |
        "limit", "generated", "limit", "context"} - the reply was cut short,
        either by the reply limit the user set or because the context window
        filled up; sent just before message_ids, and the saved reply already
        says so in its own text).

        Reproduces app.js's exact framing: the server writes `data: {...}`
        lines (one JSON chunk per line), with `data: [DONE]` explicitly
        skipped rather than treated as a terminator - server.py can and does
        write more (the message_ids frame) after it, before actually closing
        the connection, so this walks the same buffer/split/parse loop as the
        JS fetch reader all the way to actual EOF, not just to [DONE].
        """
        try:
            resp = urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as e:
            raise ApiError(*_parse_error(e)) from None
        except urllib.error.URLError as e:
            raise ApiError(str(e.reason)) from None
        except TimeoutError:
            raise ApiError(_timeout_message(600)) from None

        yield ("status", "connected")

        buffer = ""
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                chunk = resp.read(1024)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                *lines, buffer = buffer.split("\n")
                for line in lines:
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    try:
                        parsed = json.loads(line[6:])
                    except ValueError:
                        continue
                    if "message_ids" in parsed:
                        yield ("message_ids", parsed["message_ids"])
                        continue
                    if "truncated" in parsed:
                        yield ("truncated", parsed["truncated"])
                        continue
                    if "document" in parsed:
                        # The document tool at work: {"state": "creating", "title", "index"} while the model's call
                        # streams in, then {"state": "done", "id", "title", "filename", "bytes", "version", "index"}
                        # or {"state": "failed", "reason", "index"} - see server.py's _stream_and_save.
                        yield ("document", parsed["document"])
                        continue
                    try:
                        delta = parsed["choices"][0]["delta"]
                    except (KeyError, IndexError):
                        continue
                    reasoning_piece = delta.get("reasoning_content")
                    if reasoning_piece:
                        yield ("thinking", reasoning_piece)
                    piece = delta.get("content")
                    if piece:
                        yield ("chunk", piece)
        finally:
            resp.close()

    def stream_chat(self, conversation_id, message, images=None, stop_event=None):
        """Yield (kind, value) pairs for a streaming assistant reply - see
        _stream_sse for the possible kinds."""
        url = self.base_url + "/api/chat"
        body = {"conversation_id": conversation_id, "message": message, "images": images or []}
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        yield from self._stream_sse(req, stop_event=stop_event)

    def stream_edit_message(self, conversation_id, message_id, content=None, instruction=None, stop_event=None):
        """Like stream_chat, but PATCHes an existing message instead of
        posting a new one - see server.py's handle_edit_message for the two
        modes this selects between: content given -> edit-and-truncate
        (message_id must be a user message; everything after it is deleted,
        then a fresh reply is generated), content omitted -> regenerate the
        most recent assistant reply (message_id must be that exact message),
        optionally with a revision instruction.

        Regenerating keeps the reply it replaces: the old text becomes an
        archived version of the same message row, and the final message_ids
        frame carries version_pos/version_count so the transcript can show the
        "[<] 2/3 [>]" control right away. See switch_message_version."""
        url = f"{self.base_url}/api/conversations/{conversation_id}/messages/{message_id}"
        body = {}
        if content is not None:
            body["content"] = content
        if instruction:
            body["instruction"] = instruction
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="PATCH",
            headers={"Content-Type": "application/json"},
        )
        yield from self._stream_sse(req, stop_event=stop_event)

    def switch_message_version(self, conversation_id, message_id, position):
        """Show archived version `position` of a regenerated reply. Plain
        request/response, not a stream - nothing is generated, two stored rows
        just trade places. Returns the newly-active message (content, images,
        has_reasoning, version_pos, version_count) so one bubble can be
        re-rendered without reloading the conversation."""
        return self._request(
            "POST",
            f"/api/conversations/{conversation_id}/messages/{message_id}/version",
            {"position": position},
        )


def _timeout_message(timeout):
    return f"the backend did not respond within {timeout}s"


def _parse_error(http_error):
    """(message, HTTP status, rolled_back) for an HTTPError from the backend. The backend answers errors as {"error": "..."} JSON; anything
    else - a foreign program answering on the port with an HTML page, a body cut short, one that cannot be read, JSON that is not an
    object - becomes one short plain line instead of being shown as it is (an HTML page used to land, uncapped, in Home's Model row
    and in notices, and a non-object JSON body raised AttributeError here)."""
    code = getattr(http_error, "code", None)
    try:
        raw = http_error.read()
    except Exception:
        raw = b""
    if not isinstance(raw, (bytes, bytearray)):
        raw = str(raw or "").encode("utf-8", "replace")
    try:
        payload = json.loads(raw)
    except Exception:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("error"), str) and payload["error"].strip():
        return payload["error"][:500], code, bool(payload.get("rolled_back"))
    text = re.sub(r"<[^>]*>", " ", raw.decode("utf-8", "replace"))      # tags out of an HTML page
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return str(http_error), code, False
    return f"HTTP {code}: unexpected non-JSON response from the backend: {text[:200]}", code, False
