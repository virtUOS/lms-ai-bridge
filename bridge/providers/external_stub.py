"""How an existing RAG engine plugs into the bridge.

**This is the most important file in the prototype**, and it is deliberately
not a working integration. It shows the seam.

Two engines are already being built for Moodle in German higher education:

  - `local_ai_content` — ByCS / ISB Bayern, GPLv3, RAG as new subplugin types
    inside `local_ai_manager` (embedding purpose, openaiembedding tool, a
    vectorstores type with Qdrant and Postgres).
    ByCS/ISB, public on GitHub
  - OSKI.nrw LMS-RAG — RUB with Uni Köln, funded by MKW NRW. Answers with
    source citations (filename, page). Backend not published as of 2026-08-20.
    OSKI.nrw, RUB and Uni Köln

If either exposes an HTTP retrieval API, an institution implements
`RetrievalProvider` against it — roughly the class below — sets one environment
variable, and every LMS adapter keeps working unchanged. That is the whole
argument for defining the contract before building an engine.

**What is not known**, and would need to be asked of both teams before this
could be finished — the questions are listed in each fact sheet:

  - Do they expose retrieval over HTTP at all, or only in-process within Moodle?
  - What is the request/response shape?
  - How is a course identified, and does it map onto our opaque `course_ref`?
  - Do they return citations in a structured form, or only rendered text?

Until those are answered this file stays a stub, and saying so is more useful
than a plausible-looking integration that has never run.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from ..contract import IndexRequest, RetrievalProvider, Source


class ExternalHTTPRetrieval(RetrievalProvider):
    """Sketch of an adapter onto an external retrieval service.

    Shape is a guess. It is here to make the extension point concrete and to
    give the ByCS and OSKI teams something specific to react to — "would this
    work against your API?" is a better opening question than "what is your
    API?".
    """

    name = "external-http (stub)"

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = (
            base_url or os.environ.get("RETRIEVAL_BASE_URL", "")
        ).rstrip("/")
        self.api_key = api_key or os.environ.get("RETRIEVAL_API_KEY", "")

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def index(self, req: IndexRequest) -> int:
        raise NotImplementedError(
            "External retrieval indexing is not implemented. The engines this "
            "would target index course content from inside the LMS, so the "
            "bridge may not need to push documents at all — one of the open "
            "questions in DESIGN.md."
        )

    def retrieve(self, course_ref: str, query: str, k: int = 4) -> list[Source]:
        raise NotImplementedError(
            "Awaiting the retrieval API shape from ByCS / OSKI.nrw. "
            "See the module docstring for the specific questions."
        )

    def passages(self, course_ref: str, query: str, k: int = 4) -> list[str]:
        raise NotImplementedError

    def forget(self, course_ref: str) -> int:
        raise NotImplementedError


class NullRetrieval(RetrievalProvider):
    """No retrieval configured.

    Returns empty rather than raising, so a deployment without retrieval still
    serves ungrounded chat and `/v1/capabilities` simply omits `retrieval`.
    This is how one contract serves institutions with different infrastructure.
    """

    name = "none"

    def index(self, req: IndexRequest) -> int:
        raise RuntimeError("no retrieval provider configured")

    def retrieve(self, course_ref: str, query: str, k: int = 4) -> list[Source]:
        return []

    def passages(self, course_ref: str, query: str, k: int = 4) -> list[str]:
        return []

    def forget(self, course_ref: str) -> int:
        return 0
