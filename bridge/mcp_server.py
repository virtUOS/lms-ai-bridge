"""LMS AI Bridge as an MCP server, over stdio.

    python3 -m bridge.mcp_server

Exposes the bridge's contract to any MCP host — Claude Code, Claude Desktop,
an IDE, a lecturer's own assistant — as four tools:

  list_indexed_courses            what is in the local index
  index_course(platform, id)      pull a course through its adapter and index it
  search_course(course_ref, q)    passages with citations — **no generation**
  forget_course(course_ref)       remove a course from the index

## What this is for, and what it is not

**This is the per-user route.** The server runs where the user runs it, with
the LMS credentials in that user's environment — their Stud.IP login, their
ILIAS account, their own Moodle token. Authorisation is therefore inherited
from the LMS: the adapters can only extract what that account may see, so
there is no service account and nothing to re-derive. The host's model writes
the answer from the cited passages; `search_course` deliberately returns no
answer text, because generating one here would make this a fourth RAG engine.

**This is not how course data reaches HAWKI.** HAWKI indexes, retrieves,
generates and authorises through its own store and an admin-level LMS
connection (settled 2026-08-26, see HAWKI.md). Pointing HAWKI at this server
with admin credentials would bypass that store and put per-student
authorisation back on us — the one configuration to avoid.

## Implementation notes

Standard library only, like the rest of the prototype. The stdio transport is
JSON-RPC 2.0, one message per line, UTF-8; the methods a host needs are
`initialize`, `notifications/initialized`, `ping`, `tools/list` and
`tools/call`. Everything else answers "method not found", which hosts treat
as "unsupported", not as failure. Logging goes to stderr — stdout is the wire.

The same providers and the same index file as the HTTP server: a course
indexed by `demo-ilias.sh` is immediately searchable here.
"""

from __future__ import annotations

import io
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.contract import IndexRequest  # noqa: E402

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "lms-ai-bridge", "version": "0.1.0"}
PLATFORMS = ("moodle", "studip", "ilias")
# Cosine similarity below which a search result is flagged as loosely related.
LOW_CONFIDENCE = float(os.environ.get("MCP_LOW_CONFIDENCE", "0.5"))

TOOLS = [
    {
        "name": "list_indexed_courses",
        "description": (
            "List the courses currently in the local index, with chunk counts and the "
            "course_ref to use with search_course. Call this first."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "index_course",
        "description": (
            "Extract a course from its LMS with the credentials in this environment and "
            "index it. platform is moodle, studip or ilias; course_id is the platform's own "
            "id (Moodle course id, Stud.IP Veranstaltung id, ILIAS ref_id). Re-indexing "
            "replaces the course's previous entries. Takes seconds to minutes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": list(PLATFORMS)},
                "course_id": {"type": "string", "description": "the LMS's own course id"},
            },
            "required": ["platform", "course_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_course",
        "description": (
            "Retrieve the passages of an indexed course most relevant to a question, each "
            "with its citation (document, page or slide, course, folder). Returns no answer: "
            "write the answer from these passages and cite them. An empty result means the "
            "material does not cover the question — say so rather than guessing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "course_ref": {"type": "string", "description": "e.g. ilias:86, moodle:5, studip:<id>"},
                "query": {"type": "string"},
                "k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 6},
            },
            "required": ["course_ref", "query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "forget_course",
        "description": (
            "Remove a course from the local index. The index is a copy of someone's teaching "
            "material; this is how the copy is deleted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"course_ref": {"type": "string"}},
            "required": ["course_ref"],
            "additionalProperties": False,
        },
    },
]


class ToolError(Exception):
    """A failure the *model* should see and can act on — wrong argument, course
    not indexed. Reported as a tool result with isError, not a protocol error."""


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


def _courses(provider) -> dict[str, int]:
    listing = getattr(provider, "courses", None)
    if callable(listing):
        return dict(listing())
    return {}


def _fetch_documents(platform: str, course_id: str) -> tuple[str, list[dict]]:
    """Run the platform adapter in-process. Separate so tests can patch it."""
    adapters_dir = Path(__file__).resolve().parent.parent / "adapters"
    if str(adapters_dir) not in sys.path:
        sys.path.insert(0, str(adapters_dir))
    if platform == "moodle":
        import moodle_adapter  # noqa: PLC0415
        return moodle_adapter.fetch_course_documents(int(course_id))
    if platform == "studip":
        import studip_adapter  # noqa: PLC0415
        return studip_adapter.fetch_course_documents(course_id)
    if platform == "ilias":
        import ilias_adapter  # noqa: PLC0415
        return ilias_adapter.fetch_course_documents(course_id)
    raise ToolError(f"unknown platform {platform!r}; one of {', '.join(PLATFORMS)}")


def _require(args: dict, *names: str) -> None:
    missing = [n for n in names if not args.get(n)]
    if missing:
        raise ToolError(f"missing argument(s): {', '.join(missing)}")


def _citation(s) -> str:
    where = ", ".join(x for x in (s.title, s.locator) if x)
    scope = " / ".join(x for x in (s.course_name, s.folder) if x)
    return f"{where} ({scope})" if scope else where


def tool_list_indexed_courses(provider, args: dict) -> dict:
    return {
        "provider": provider.name,
        "courses": [
            {"course_ref": ref, "chunks": n}
            for ref, n in sorted(_courses(provider).items())
        ],
    }


def tool_index_course(provider, args: dict) -> dict:
    _require(args, "platform", "course_id")
    platform = str(args["platform"]).lower()
    if platform not in PLATFORMS:
        raise ToolError(f"unknown platform {platform!r}; one of {', '.join(PLATFORMS)}")
    try:
        course_ref, docs = _fetch_documents(platform, str(args["course_id"]))
    except ToolError:
        raise
    except Exception as exc:  # adapter errors are the model's business too
        raise ToolError(f"{platform} adapter failed: {exc}") from exc
    if not docs:
        raise ToolError(f"no indexable text found in {platform} course {args['course_id']}")
    req = IndexRequest.from_dict({"course_ref": course_ref, "documents": docs, "replace": True})
    chunks = provider.index(req)
    return {"course_ref": course_ref, "documents": len(docs), "chunks": chunks,
            "provider": provider.name}


def tool_search_course(provider, args: dict) -> dict:
    _require(args, "course_ref", "query")
    course_ref = str(args["course_ref"])
    if provider.count(course_ref) == 0:
        raise ToolError(
            f"{course_ref} is not indexed. Use list_indexed_courses to see what is, or "
            f"index_course to pull it from the LMS."
        )
    k = int(args.get("k") or 6)
    hits = provider.retrieve(course_ref, str(args["query"]), k=max(1, min(k, 20)))
    passages = []
    for s in hits:
        d = asdict(s)
        d["citation"] = _citation(s)
        passages.append(d)
    out = {"course_ref": course_ref, "passages": passages}
    if not passages:
        out["confidence"] = "none"
        out["note"] = ("Retrieval found nothing relevant in this course. Tell the user the "
                       "material does not cover the question; do not answer from memory.")
    else:
        # Embedding retrieval returns *something* for almost any question — a
        # question about tuition fees came back with six passages around 0.45.
        # The HTTP chat path lets the model notice that; here the host model
        # is the one deciding, so hand it the judgement explicitly.
        scores = [p["score"] for p in passages if p.get("score") is not None]
        best = max(scores) if scores else None
        if best is not None and best < LOW_CONFIDENCE:
            out["confidence"] = "low"
            out["note"] = (f"Best similarity {best:.2f} is below {LOW_CONFIDENCE}: these passages "
                           "are probably only loosely related. Check them before answering, and "
                           "if they do not address the question, say the material does not cover it.")
        else:
            out["confidence"] = "ok"
    return out


def tool_forget_course(provider, args: dict) -> dict:
    _require(args, "course_ref")
    removed = provider.forget(str(args["course_ref"]))
    return {"course_ref": str(args["course_ref"]), "removed_chunks": removed}


TOOL_HANDLERS = {
    "list_indexed_courses": tool_list_indexed_courses,
    "index_course": tool_index_course,
    "search_course": tool_search_course,
    "forget_course": tool_forget_course,
}


# --------------------------------------------------------------------------
# JSON-RPC loop
# --------------------------------------------------------------------------


def _result(rid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _tool_result(payload: dict, is_error: bool = False) -> dict:
    out = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=1)}]}
    if is_error:
        out["isError"] = True
    return out


def handle(msg: dict, provider) -> dict | None:
    """Answer one JSON-RPC message; None for notifications."""
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}
    is_notification = "id" not in msg

    if method == "initialize":
        return _result(rid, {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": (
                "Course material from Moodle, ILIAS and Stud.IP. Start with "
                "list_indexed_courses. Answer only from search_course passages and cite "
                "them by their 'citation' field; if passages are empty, say the material "
                "does not cover it."
            ),
        })
    if method == "ping":
        return _result(rid, {})
    if method == "tools/list":
        return _result(rid, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name", "")
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return _result(rid, _tool_result({"error": f"unknown tool {name!r}"}, is_error=True))
        try:
            return _result(rid, _tool_result(handler(provider, params.get("arguments") or {})))
        except ToolError as exc:
            return _result(rid, _tool_result({"error": str(exc)}, is_error=True))
        except Exception as exc:  # never let one tool call kill the session
            print(f"mcp: {name} crashed: {exc!r}", file=sys.stderr)
            return _result(rid, _tool_result({"error": f"{name} failed: {exc}"}, is_error=True))
    if is_notification:
        return None
    return _error(rid, -32601, f"method not found: {method}")


def serve(stdin: io.TextIOBase, stdout: io.TextIOBase, provider) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply = _error(None, -32700, "parse error")
        else:
            reply = handle(msg, provider) if isinstance(msg, dict) else _error(None, -32600, "invalid request")
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()


def _load_dotenv() -> None:
    """Read the repo's .env if present, without overriding a set environment.
    An MCP host launches the server with a bare environment, so this is how the
    user's LMS credentials and the model gateway reach the adapters."""
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, _, value = raw.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def main() -> int:
    _load_dotenv()
    from bridge.server import build_providers  # noqa: PLC0415  (after .env)
    _, retrieval = build_providers()
    print(f"lms-ai-bridge MCP server on stdio, retrieval={retrieval.name}, "
          f"courses={len(_courses(retrieval))}", file=sys.stderr)
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", write_through=True)
    serve(stdin, stdout, retrieval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
