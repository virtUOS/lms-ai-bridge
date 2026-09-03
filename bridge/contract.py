"""The LMS AI Bridge contract.

This module is the deliverable. Everything else in this prototype is one
possible implementation of what is defined here.

The contract is deliberately small: four endpoints, opaque references, and a
capability handshake so that one interface can serve institutions with very
different infrastructure. See DESIGN.md for why each piece is shaped this way.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal

CONTRACT_VERSION = "0.1.0"

Capability = Literal["chat", "retrieval", "index"]


# --------------------------------------------------------------------------
# References
# --------------------------------------------------------------------------
# The bridge never models courses, users or activities. It takes opaque,
# namespaced strings and scopes work by them. This is the single most important
# design decision here: inventing a cross-LMS course ontology is where projects
# like this usually die.
#
#   moodle:1234                     a course
#   moodle:1234:activity:56         an activity within it
#   studip:a1b2c3                   a Stud.IP Veranstaltung
#   ilias:crs_9988                  an ILIAS course object
#
# The bridge parses only the leading namespace, and only for reporting.


def namespace_of(ref: str) -> str:
    """Return the LMS namespace of a reference, or 'unknown'."""
    if not ref or ":" not in ref:
        return "unknown"
    return ref.split(":", 1)[0]


# --------------------------------------------------------------------------
# Wire types
# --------------------------------------------------------------------------


@dataclass
class Message:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass
class Source:
    """A citation. Present because a grounded answer without a source is not
    meaningfully better than an ungrounded one — the user cannot check it."""

    title: str
    locator: str = ""          # "S. 12", "Abschnitt 3.1", "" if not applicable
    activity_ref: str = ""     # opaque ref back into the LMS, if known
    # Where the material sits, so an answer can say *this document, in this
    # folder, in this course* rather than only naming a file. Requested by the
    # HAWKI team, 2026-09-03. Both are display names for a human reading a
    # citation — `activity_ref` remains the machine-readable handle.
    course_name: str = ""      # "Funktionale Programmierung"
    folder: str = ""           # "Skripte/Kapitel 3", "" at the course root
    score: float | None = None  # retrieval score, when the provider exposes one


@dataclass
class ChatRequest:
    messages: list[Message]
    course_ref: str = ""       # omit for ungrounded chat
    user_ref: str = ""         # opaque; for quota attribution upstream
    locale: str = "de"

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ChatRequest":
        raw = d.get("messages") or []
        if not isinstance(raw, list) or not raw:
            raise ValueError("messages must be a non-empty list")
        msgs = []
        for m in raw:
            if not isinstance(m, dict) or "role" not in m or "content" not in m:
                raise ValueError("each message needs 'role' and 'content'")
            msgs.append(Message(role=m["role"], content=str(m["content"])))
        return ChatRequest(
            messages=msgs,
            course_ref=str(d.get("course_ref") or ""),
            user_ref=str(d.get("user_ref") or ""),
            locale=str(d.get("locale") or "de"),
        )


@dataclass
class ChatResponse:
    answer: str
    sources: list[Source] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    provider: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        # `sources` is always present, even when empty, so LMS adapters need no
        # special-casing for deployments without retrieval.
        return {
            "answer": self.answer,
            "sources": [asdict(s) for s in self.sources],
            "usage": self.usage,
            "provider": self.provider,
        }


@dataclass
class IndexDocument:
    """One unit of course content offered for indexing.

    The LMS pushes content; the bridge never reaches back into the LMS. That
    keeps the trust boundary one-directional and makes per-activity opt-in the
    natural model rather than something bolted on afterwards.
    """

    activity_ref: str
    title: str
    text: str
    locator: str = ""          # "S. 12" for one page of a PDF; "" for whole units
    # Optional, because an adapter that cannot determine them must still work.
    # On Stud.IP the folder carries a second meaning from 2026-09: a folder type
    # marks material a lecturer released for AI, so the same value is both the
    # consent marker and part of the citation.
    course_name: str = ""
    folder: str = ""

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "IndexDocument":
        if not d.get("text"):
            raise ValueError("document needs 'text'")
        return IndexDocument(
            activity_ref=str(d.get("activity_ref") or ""),
            title=str(d.get("title") or "untitled"),
            text=str(d["text"]),
            locator=str(d.get("locator") or ""),
            course_name=str(d.get("course_name") or ""),
            folder=str(d.get("folder") or ""),
        )


@dataclass
class IndexRequest:
    course_ref: str
    documents: list[IndexDocument]
    replace: bool = True   # re-index semantics: replace this course's entries

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "IndexRequest":
        if not d.get("course_ref"):
            raise ValueError("course_ref is required")
        docs = [IndexDocument.from_dict(x) for x in (d.get("documents") or [])]
        if not docs:
            raise ValueError("documents must be a non-empty list")
        return IndexRequest(
            course_ref=str(d["course_ref"]),
            documents=docs,
            replace=bool(d.get("replace", True)),
        )


# --------------------------------------------------------------------------
# Provider interfaces
# --------------------------------------------------------------------------
# These two protocols are the extension seam. An institution running ByCS's
# `local_ai_content` or OSKI.nrw's RAG stack should be able to implement
# RetrievalProvider against it and change nothing else.


class ChatProvider:
    """Turns messages (plus any retrieved context) into an answer."""

    name = "abstract"

    def complete(
        self,
        messages: list[Message],
        context: list[Source] | None = None,
        grounded: bool = False,
    ) -> tuple[str, dict[str, int]]:
        """`grounded` marks that a course was named, even if `context` is empty.

        An implementation must not answer from general knowledge when a course
        was named and nothing was retrieved — see SYSTEM_NO_CONTEXT.
        """
        raise NotImplementedError


class RetrievalProvider:
    """Stores course content and retrieves passages relevant to a query.

    Implementations must be safe to call when a course has never been indexed:
    return an empty list rather than raising.
    """

    name = "abstract"

    def index(self, req: IndexRequest) -> int:
        """Index documents. Returns the number of chunks stored."""
        raise NotImplementedError

    def retrieve(self, course_ref: str, query: str, k: int = 4) -> list[Source]:
        raise NotImplementedError

    def passages(self, course_ref: str, query: str, k: int = 4) -> list[str]:
        """The text behind the sources, for prompt construction."""
        raise NotImplementedError

    def count(self, course_ref: str) -> int:
        """How many chunks are stored for a course. 0 means not indexed.

        Part of the contract because an LMS has to be able to ask "is this
        course prepared?" directly. Inferring it from an empty answer is
        unreliable: a course can be fully indexed and still have nothing
        relevant to a given question.
        """
        return 0

    def forget(self, course_ref: str) -> int:
        """Delete everything indexed for a course. Returns chunks removed.

        Required by the contract, not optional: indexing creates a copy of
        course material, so deleting it must be as easy as creating it.
        """
        raise NotImplementedError
