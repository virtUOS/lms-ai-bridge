"""The LMS AI Bridge — HTTP service.

Standard library only, so `python3 -m bridge.server` is the whole install.

Endpoints (see DESIGN.md for why these four):
    GET  /v1/health         liveness + which providers are configured
    GET  /v1/capabilities   what this deployment can do
    POST /v1/chat           ask, optionally scoped to a course
    POST /v1/index          submit course content for indexing
    POST /v1/forget         delete a course's index
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .contract import (
    CONTRACT_VERSION,
    ChatRequest,
    ChatResponse,
    IndexRequest,
    namespace_of,
)
from .providers.builtin_retrieval import BuiltinRetrieval
from .demo_page import PAGE
from .jobs import JobRunner
from .providers.embedding_retrieval import EmbeddingRetrieval
from .providers.transcription import make_transcription
from .providers.external_stub import ExternalHTTPRetrieval, NullRetrieval
from .providers.openai_chat import EchoChat, OpenAICompatibleChat

# Text-only requests are small; a request carrying media is not. An 18 MB audio
# file becomes ~25 MB of base64, so a limit sized for documents rejects it — and
# because the check happens before the body is read, the client sees a broken
# pipe mid-upload rather than a clean error. Both halves matter: the ceiling,
# and draining the body so the rejection is readable.
#
# This inline-upload shape is the prototype's weakest point. A production
# contract should take a URL the bridge fetches, or a multipart upload, rather
# than base64 inside JSON — see fetch_course_media in the Stud.IP adapter.
MAX_BODY = int(os.environ.get("BRIDGE_MAX_BODY_MB", "512")) * 1024 * 1024


def build_providers():
    """Wire providers from the environment.

    The bridge degrades rather than failing: with no gateway configured it
    serves offline echo chat, so an adapter can still be demonstrated.
    """
    chat = OpenAICompatibleChat()
    if not chat.configured:
        chat = EchoChat()

    # Default is "auto": use what the institution already has, fall back to what
    # always works. An embedding model gives markedly better retrieval — it is
    # the only thing that answers a question sharing no words with the material
    # — but requiring one would exclude institutions that have none. So the
    # better provider is used when configured, and the fallback keeps the
    # capability available everywhere. `/v1/capabilities` reports which is live.
    mode = os.environ.get("RETRIEVAL_PROVIDER", "auto").strip().lower()
    store = os.environ.get("RETRIEVAL_STORE", "") or (
        Path(__file__).parent.parent / ".index.json"
    )

    if mode == "none":
        retrieval = NullRetrieval()
    elif mode == "external":
        retrieval = ExternalHTTPRetrieval()
    elif mode == "builtin":
        retrieval = BuiltinRetrieval(store)
    else:
        embedding = EmbeddingRetrieval(store)
        if embedding.configured:
            retrieval = embedding
        else:
            if mode == "embeddings":
                # Asked for explicitly but unusable: say so rather than silently
                # serving worse retrieval than the operator thinks they have.
                print(
                    "  RETRIEVAL_PROVIDER=embeddings but EMBEDDING_MODEL is not "
                    "set — falling back to lexical retrieval",
                    file=sys.stderr,
                )
            retrieval = BuiltinRetrieval(store)
    return chat, retrieval



# Models do not reliably emit ASCII brackets: gpt-oss-120b answered one question
# citing 【1】 and 【2】 in fullwidth CJK brackets. Matching only "[1]" silently
# discarded every source of a well-grounded answer, which looked far worse than
# the problem it was meant to solve.
_CITATION = re.compile(r"[\[\uFF3B\u3010]\s*(\d+)\s*[\]\uFF3D\u3011]")


def _keep_cited_sources(answer: str, sources: list) -> tuple[str, list]:
    """Return the answer and only the sources it cited, renumbered from 1.

    Two things happen here, both because the alternative confuses a reader:

    - **Uncited sources are dropped.** Retrieval legitimately returns pages that
      mention the topic without answering the question; listing them under a
      refusal makes a sound judgement look like a bug.
    - **The survivors are renumbered.** A model that cites only its fourth hit
      would otherwise produce an answer referring to [4] above a list whose only
      entry is [4], with no [1] to [3] anywhere. Renumbering keeps the answer
      and the source list consistent.
    """
    cited = [int(m) for m in _CITATION.findall(answer)]
    if not cited:
        return answer, []

    order = sorted({c for c in cited if 1 <= c <= len(sources)})
    if not order:
        return answer, []

    renumber = {old: new for new, old in enumerate(order, 1)}
    # Replace longest-first so [10] is not rewritten as [1]0.
    def _sub(m: "re.Match") -> str:
        n = int(m.group(1))
        return f"[{renumber[n]}]" if n in renumber else m.group(0)

    return _CITATION.sub(_sub, answer), [sources[i - 1] for i in order]


class Handler(BaseHTTPRequestHandler):
    server_version = f"lms-ai-bridge/{CONTRACT_VERSION}"
    chat_provider = None
    retrieval_provider = None
    transcription_provider = None
    jobs = None
    auth_token = ""

    # -- plumbing --

    def log_message(self, fmt, *args):  # quieter, and to stderr
        sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code: int, message: str) -> None:
        self._send(code, {"error": {"code": code, "message": message}})

    def _authorised(self) -> bool:
        if not self.auth_token:
            return True
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {self.auth_token}"

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            # Drain what the client is sending before refusing, so it gets the
            # error rather than a broken pipe halfway through a 25 MB upload.
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            raise ValueError(
                f"body is {length / 1_048_576:.0f} MB, over the "
                f"{MAX_BODY / 1_048_576:.0f} MB limit — raise "
                f"BRIDGE_MAX_BODY_MB, or send fewer files per request"
            )
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_OPTIONS(self):  # noqa: N802
        self._send(204, {})

    # -- routes --

    def do_GET(self):  # noqa: N802
        route = self.path.split("?", 1)[0].rstrip("/")

        # A demo surface, not a product — see bridge/demo_page.py. Served from
        # the bridge so there is no build step and nothing extra to run.
        if route in ("/demo", "/demo/index.html"):
            return self._send_html(PAGE)

        # What is still being transcribed for a course. The demo polls this to
        # show that questions are answerable while recordings are processing;
        # an LMS adapter could use it to show the same thing to a teacher.
        # What is indexed for a course. An LMS needs this to answer "has this
        # course been prepared yet?" without asking a question and inferring
        # from the silence — and inferring is what the demo did wrong: it
        # probed with a nonsense question, retrieved nothing (correctly), and
        # reported an indexed course as empty.
        if route == "/v1/index/status":
            from urllib.parse import parse_qs, urlparse       # noqa: PLC0415
            params = parse_qs(urlparse(self.path).query)
            course_ref = (params.get("course_ref") or [""])[0]
            provider = self.retrieval_provider
            counts = getattr(provider, "count", None)
            chunks = counts(course_ref) if callable(counts) else 0
            return self._send(200, {
                "course_ref": course_ref,
                "indexed": chunks > 0,
                "chunks": chunks,
                "provider": provider.name,
            })

        if route == "/v1/jobs":
            from urllib.parse import parse_qs, urlparse       # noqa: PLC0415
            params = parse_qs(urlparse(self.path).query)
            course_ref = (params.get("course_ref") or [""])[0]
            if self.jobs is None:
                return self._send(200, {"pending": 0, "done": 0, "failed": 0,
                                        "titles": [], "errors": []})
            return self._send(200, self.jobs.status(course_ref).to_dict())

        if self.path.rstrip("/") == "/v1/health":
            return self._send(
                200,
                {
                    "status": "ok",
                    "contract": CONTRACT_VERSION,
                    "providers": {
                        "chat": self.chat_provider.name,
                        "retrieval": self.retrieval_provider.name,
                    },
                },
            )
        if self.path.rstrip("/") == "/v1/capabilities":
            caps = ["chat"]
            if not isinstance(self.retrieval_provider, NullRetrieval):
                caps += ["retrieval", "index"]
            providers = {
                "chat": self.chat_provider.name,
                "retrieval": self.retrieval_provider.name,
            }
            # Advertised only where a provider is configured, so an adapter can
            # hide the feature rather than offer something that will not work.
            if self.transcription_provider is not None:
                caps.append("transcription")
                providers["transcription"] = self.transcription_provider.name
            return self._send(
                200,
                {
                    "contract": CONTRACT_VERSION,
                    "capabilities": caps,
                    "providers": providers,
                },
            )
        return self._error(404, f"no such endpoint: {self.path}")

    def do_POST(self):  # noqa: N802
        if not self._authorised():
            return self._error(401, "missing or invalid bearer token")

        route = self.path.rstrip("/")
        try:
            payload = self._read_json()
        except ValueError as e:
            return self._error(400, f"bad request body: {e}")
        except json.JSONDecodeError as e:
            return self._error(400, f"invalid JSON: {e}")

        try:
            if route == "/v1/chat":
                return self._chat(payload)
            if route == "/v1/index":
                return self._index(payload)
            if route == "/v1/forget":
                return self._forget(payload)
        except ValueError as e:
            return self._error(400, str(e))
        except NotImplementedError as e:
            return self._error(501, str(e))
        except RuntimeError as e:
            return self._error(502, str(e))
        except Exception:  # noqa: BLE001 — prototype: surface the trace
            traceback.print_exc()
            return self._error(500, "internal error; see server log")

        return self._error(404, f"no such endpoint: {self.path}")

    # -- handlers --

    def _chat(self, payload: dict):
        req = ChatRequest.from_dict(payload)
        question = req.messages[-1].content if req.messages else ""

        sources, passages = [], []
        if req.course_ref:
            sources = self.retrieval_provider.retrieve(req.course_ref, question)
            passages = self.retrieval_provider.passages(req.course_ref, question)

        context = list(zip(sources, passages)) if sources else None
        # `grounded` is true whenever a course was named — even if retrieval
        # found nothing, so the provider refuses instead of answering from
        # general knowledge. See SYSTEM_NO_CONTEXT in openai_chat.py.
        answer, usage = self.chat_provider.complete(
            req.messages, context, grounded=bool(req.course_ref)
        )

        # Return only the sources the answer actually cited.
        #
        # Retrieval legitimately returns pages that mention the topic without
        # answering the question — asked about experiences with E-Klausuren, a
        # course handout mentioning "OpenBooks-Klausuren" is a correct lexical
        # hit and a useless citation. The model says so and declines; listing
        # the four hits underneath makes a sound judgement look like a bug.
        #
        # Done here rather than in each adapter so every LMS gets it, and after
        # the model call because only the answer knows what it used.
        answer, sources = _keep_cited_sources(answer, sources)

        # An answer given while recordings are still being transcribed is
        # grounded in incomplete material. Say so in the answer itself — the
        # person asking is the one who needs to know.
        note = self.jobs.note(req.course_ref) if (self.jobs and req.course_ref) else ""
        if note:
            answer = f"{answer}\n\n{note}"

        resp = ChatResponse(
            answer=answer,
            sources=sources,
            usage=usage,
            provider={
                "chat": self.chat_provider.name,
                "retrieval": self.retrieval_provider.name,
                "lms": namespace_of(req.course_ref) if req.course_ref else "none",
            },
        )
        return self._send(200, resp.to_dict())

    def _index(self, payload: dict):
        req = IndexRequest.from_dict(payload)
        chunks = self.retrieval_provider.index(req)

        # Audio arrives as a separate list so the text path never waits on it:
        # `media` entries are queued, transcribed in the background, and indexed
        # when ready. See bridge/jobs.py for why this is not synchronous.
        queued = self._queue_media(req.course_ref, payload.get("media") or [])

        return self._send(
            200,
            {
                "course_ref": req.course_ref,
                "documents": len(req.documents),
                "chunks": chunks,
                "replaced": req.replace,
                "provider": self.retrieval_provider.name,
                "transcription_queued": queued,
            },
        )

    def _queue_media(self, course_ref: str, media: list) -> int:
        """Queue audio/video for background transcription. Returns how many."""
        if not media or self.transcription_provider is None or self.jobs is None:
            return 0

        queued = 0
        for item in media:
            if not isinstance(item, dict) or not item.get("content_base64"):
                continue
            title = str(item.get("title") or "Aufnahme")
            activity_ref = str(item.get("activity_ref") or "")
            try:
                blob = base64.b64decode(item["content_base64"])
            except (ValueError, TypeError):
                continue

            def work(blob=blob, title=title, activity_ref=activity_ref):
                units = self.transcription_provider.transcribe(blob, title)
                docs = [
                    {"activity_ref": activity_ref, "title": title,
                     "locator": locator, "text": text}
                    for locator, text in units if text.strip()
                ]
                if docs:
                    # `replace: False` — the text material is already indexed
                    # and must not be dropped when a transcript lands.
                    self.retrieval_provider.index(IndexRequest.from_dict(
                        {"course_ref": course_ref, "documents": docs,
                         "replace": False}))

            self.jobs.submit(course_ref, title, work)
            queued += 1
        return queued

    def _forget(self, payload: dict):
        course_ref = str(payload.get("course_ref") or "")
        if not course_ref:
            raise ValueError("course_ref is required")
        removed = self.retrieval_provider.forget(course_ref)
        return self._send(200, {"course_ref": course_ref, "removed_chunks": removed})


def main() -> int:
    host = os.environ.get("BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("BRIDGE_PORT", "8080"))

    chat, retrieval = build_providers()
    Handler.chat_provider = chat
    Handler.retrieval_provider = retrieval
    Handler.transcription_provider = make_transcription()
    Handler.jobs = JobRunner(
        max_workers=int(os.environ.get("ASR_MAX_CONCURRENT", "2")))
    Handler.auth_token = os.environ.get("BRIDGE_TOKEN", "")

    print(f"LMS AI Bridge {CONTRACT_VERSION}")
    print(f"  chat provider      : {chat.name}")
    tp = Handler.transcription_provider
    print(f"  transcription      : {tp.name if tp else 'none (audio skipped)'}")
    print(f"  retrieval provider : {retrieval.name}")
    if isinstance(chat, EchoChat):
        print("  NOTE: no OPENAI_BASE_URL configured — running in offline echo mode.")
    if not Handler.auth_token:
        print("  NOTE: BRIDGE_TOKEN unset — no authentication (prototype default).")
    print(f"  listening on http://{host}:{port}")
    print()

    try:
        ThreadingHTTPServer((host, port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
