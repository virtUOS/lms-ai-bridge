"""A deliberately simple retrieval provider.

This exists so the grounded path demonstrates end to end. It is **not** an
attempt to build a better RAG engine than ByCS's `local_ai_content` or
OSKI.nrw's stack — both of those are further along and better resourced.

What matters here is the *interface* it implements, not the algorithm. See
`external_stub.py` for how a real engine would take its place.

Retrieval is lexical (BM25-ish scoring over token overlap), not vector-based.
That is a deliberate prototype choice: no embedding model, no vector database,
no extra infrastructure to stand up before a colleague can run the demo. A real
deployment would use embeddings — which is exactly what the other two projects
provide.

**What lexical retrieval cannot do, measured on a real course** (370 documents,
685 chunks, 2026-08-25):

- *Works.* Questions containing a content word that appears in the material:
  "Was wurde zu KI-Kompetenzen gesagt?" retrieves the right slides. German
  compounds are handled by splitting them, so "Lehrendenbefragung" also reaches
  a page saying "Befragung der Lehrenden".
- *Cannot work.* Questions with **no content word at all**. "Worum geht es in
  dieser Veranstaltung?" reduces to `geht`/`dieser`/`veranstaltung` and shares
  **zero** tokens with a course description — which will say "Weiterbildung",
  "digitale Lehre", "KI", and never "Veranstaltung". No amount of tuning fixes
  this: the words simply are not there.

  Before the relevance floor existed, that question appeared to work — it
  matched noise and the model wrote a plausible summary over whatever came
  back. That is worse than an honest refusal, and the fix is embeddings, where
  "Worum geht es" is semantically near a description. `bge-m3` is already
  available on the Osnabrück gateway.

The floor and the compound splitting below are the fallback earning its keep on
German text. They are not a substitute for a real retrieval engine.
"""

from __future__ import annotations

import json
import math
import re
import threading
from collections import Counter
from pathlib import Path

from ..contract import IndexRequest, RetrievalProvider, Source

_WORD = re.compile(r"\w+", re.UNICODE)

# Keep a hit only if it scores at least this fraction of the best hit for the
# same query. Tuned on the virtUOS-Weiterbildung course: 0.25 keeps genuinely
# related passages while dropping the single-shared-word matches that made
# refusals look like failures.
_RELATIVE_FLOOR = 0.25

# ...and a match on a single query term only counts when that term is rare in
# this course. The relative floor cannot help when just one document matches at
# all: it is then its own best hit and always passes. The case this catches is
# "Welche Erfahrungen gibt es mit Segelbooten?" matching a service handout on
# the word "Erfahrungen" alone.
#
# Rarity is expressed as a fraction of the maximum idf this corpus can produce,
# because raw idf scales with corpus size — a unique term scores 0.69 across 2
# chunks and 5.84 across 685, so no fixed number works for both.
_MIN_DISTINCT_TERMS = 2
_RARE_TERM_SHARE = 0.6

# German + English stopwords, enough to stop scoring being dominated by glue.
_STOP = {
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem",
    "eines", "und", "oder", "aber", "ist", "sind", "war", "waren", "wird",
    "werden", "hat", "haben", "kann", "können", "für", "mit", "von", "zu",
    "auf", "in", "im", "an", "am", "als", "auch", "sich", "nicht", "man",
    "wie", "was", "wenn", "dass", "es", "sie", "er", "ich", "wir", "bei",
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be",
    "of", "to", "in", "on", "at", "for", "with", "that", "this", "it", "as",
    # Question words and filler verbs. Without these a question matches pages
    # that share nothing but its grammar: "Welche Erfahrungen gibt es …?" hit
    # three pages of an unrelated handout on `welche` + `gibt` + `e` alone,
    # three "distinct" terms of pure filler.
    "welche", "welcher", "welches", "welchen", "wer", "wann", "wo", "warum",
    "wieso", "weshalb", "wodurch", "womit", "wofür", "worum", "wozu",
    "gibt", "geben", "gab", "gibts", "sagt", "sagte", "steht", "macht",
    "soll", "sollen", "muss", "müssen", "darf", "dürfen", "will", "wollen",
    "hier", "dort", "dabei", "damit", "dazu", "daran", "davon", "denn",
    "noch", "schon", "nur", "auch", "sehr", "mehr", "etwa", "also",
    "which", "what", "when", "where", "why", "how", "who", "does", "do",
    "did", "can", "could", "should", "would", "there", "about", "some",
}


# German glues words together, and lexical matching does not. "Lehrendenbefragung"
# and "Befragung der Lehrenden" share no token at all, so a question phrased the
# second way misses a document titled the first way — observed live: "Was kam bei
# der Lehrendenbefragung heraus?" returned nothing from a course containing
# `SOUVER@N Lehrendenbefragung.pdf` with 38 indexed pages.
#
# The fix is crude on purpose: alongside each token, index the pieces of any long
# compound that are themselves substantial words. No dictionary, no stemmer, no
# dependency — it splits on the linking forms German actually uses and keeps
# fragments of four characters or more.
#
# A real deployment should use embeddings instead (`bge-m3` is already on the
# Osnabrück gateway). This is the fallback earning its keep on German text, not
# an attempt at proper morphology.
_COMPOUND_PARTS = re.compile(
    r"(befragung|erfahrung|kompetenz|veranstaltung|bewertung|prüfung|klausur"
    r"|ergebnis|unterricht|lehrende[nr]?|studier|schulung|beratung|verwaltung"
    r"|entwicklung|forschung|bildung|nutzung|anwendung|sitzung|modul)",
    re.IGNORECASE,
)

# Common German suffixes, stripped so singular and plural collide: "Klausuren"
# and "Klausur" must match, as must "Kompetenzen" and "Kompetenz".
_SUFFIXES = ("ungen", "enen", "erin", "innen", "chen", "lein", "isch",
             "en", "er", "es", "em", "et", "e", "n", "s")


def _stem(token: str) -> str:
    """Strip one common suffix, if what remains is still a real-sized word."""
    for suf in _SUFFIXES:
        if len(token) - len(suf) >= 5 and token.endswith(suf):
            return token[: -len(suf)]
    return token


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for raw in (w.lower() for w in _WORD.findall(text)):
        # A single character carries no meaning on its own: splitting
        # "E-Klausuren" leaves a stray "e" that matches any page with an "e)"
        # list marker. Bare numbers are noise for the same reason.
        if len(raw) < 2 or raw.isdigit() or raw in _STOP:
            continue
        out.append(raw)
        stem = _stem(raw)
        if stem != raw:
            out.append(stem)
        # Split long compounds into their meaningful parts, so a query using
        # one half still reaches a document that spells out the whole.
        if len(raw) > 11:
            for part in _COMPOUND_PARTS.split(raw):
                part = part.strip()
                if len(part) >= 4 and part != raw and part not in _STOP:
                    out.append(part)
                    if _stem(part) != part:
                        out.append(_stem(part))
    return out


def chunk(text: str, size: int = 900, overlap: int = 150) -> list[str]:
    """Split on paragraph boundaries where possible, else on size."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 2 <= size:
            buf = f"{buf}\n\n{p}" if buf else p
        else:
            if buf:
                chunks.append(buf)
            if len(p) <= size:
                buf = p
            else:
                for i in range(0, len(p), size - overlap):
                    chunks.append(p[i : i + size])
                buf = ""
    if buf:
        chunks.append(buf)
    return chunks or [text[:size]]


class BuiltinRetrieval(RetrievalProvider):
    name = "builtin (lexical)"

    def __init__(self, store_path: str | Path | None = None) -> None:
        self._lock = threading.Lock()
        self._path = Path(store_path) if store_path else None
        # course_ref -> list of {activity_ref, title, text, tokens}
        self._store: dict[str, list[dict]] = {}
        if self._path and self._path.exists():
            self._load()

    # -- persistence (a JSON file; a real provider would use a vector store) --

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._store = {k: v for k, v in raw.items()}
        except (OSError, json.JSONDecodeError):
            self._store = {}

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._store, ensure_ascii=False), encoding="utf-8"
        )

    # -- interface --

    def index(self, req: IndexRequest) -> int:
        entries: list[dict] = []
        for doc in req.documents:
            for piece in chunk(doc.text):
                entries.append(
                    {
                        "activity_ref": doc.activity_ref,
                        "title": doc.title,
                        "locator": doc.locator,
                        "text": piece,
                        "tokens": tokenize(piece),
                    }
                )
        with self._lock:
            if req.replace:
                self._store[req.course_ref] = entries
            else:
                self._store.setdefault(req.course_ref, []).extend(entries)
            self._save()
        return len(entries)

    def _scored(self, course_ref: str, query: str, k: int) -> list[tuple[float, dict]]:
        with self._lock:
            entries = list(self._store.get(course_ref, []))
        if not entries:
            return []

        q = Counter(tokenize(query))
        if not q:
            return []

        # idf over the course's own chunks
        n = len(entries)
        df = Counter()
        for e in entries:
            for t in set(e["tokens"]):
                df[t] += 1

        # The highest idf achievable here: a term appearing in exactly one chunk.
        max_idf = math.log(1 + (n / 2))

        scored: list[tuple[float, dict]] = []
        for e in entries:
            tf = Counter(e["tokens"])
            length = len(e["tokens"]) or 1
            s = 0.0
            # Keyed by stem, so a surface form and its own stem ("erfahrungen"
            # and "erfahr") count as ONE matched term rather than two. Without
            # this the tokenizer's own expansion would defeat the
            # distinct-terms guard below — a single word would look like two.
            matched: dict[str, float] = {}
            for term, qn in q.items():
                if term not in tf:
                    continue
                idf = math.log(1 + (n / (1 + df[term])))
                s += qn * (tf[term] / length) * idf
                key = _stem(term)
                matched[key] = max(matched.get(key, 0.0), idf)
            # One common word in common is not relevance. Accept a single-term
            # match only when the term is rare across this course's own chunks.
            if not matched:
                continue
            if (len(matched) < _MIN_DISTINCT_TERMS
                    and max(matched.values()) < max_idf * _RARE_TERM_SHARE):
                continue
            if s > 0:
                scored.append((s, e))

        scored.sort(key=lambda x: x[0], reverse=True)
        if not scored:
            return []

        # A relevance floor, relative to the best hit for this query.
        #
        # Any shared token at all produces a positive score, so without this a
        # question the course cannot answer still returns four passages. The
        # model then correctly refuses — and the UI prints four Quellen under
        # the refusal, which reads as a broken system. Observed live: "Welche
        # Erfahrungen gibt es mit E-Klausuren?" returned three pages of an
        # unrelated 168-page handout because they shared the word "Erfahrungen".
        #
        # Relative rather than absolute, because scores are not comparable
        # across queries: a rare term scores far higher than a common one, so
        # any fixed threshold would be wrong for one of them.
        best = scored[0][0]
        return [(sc, e) for sc, e in scored[:k] if sc >= best * _RELATIVE_FLOOR]

    def retrieve(self, course_ref: str, query: str, k: int = 4) -> list[Source]:
        return [
            Source(
                title=e["title"],
                # Populated when a document was extracted per page, so a reader
                # can check the claim at "Vorlesung 3.pdf, S. 12" rather than
                # being told only which file it came from.
                locator=e.get("locator", ""),
                activity_ref=e["activity_ref"],
                score=round(s, 4),
            )
            for s, e in self._scored(course_ref, query, k)
        ]

    def passages(self, course_ref: str, query: str, k: int = 4) -> list[str]:
        return [e["text"] for _, e in self._scored(course_ref, query, k)]

    def count(self, course_ref: str) -> int:
        """How many chunks are stored for a course. 0 means not indexed."""
        with self._lock:
            return len(self._store.get(course_ref, []))

    def forget(self, course_ref: str) -> int:
        with self._lock:
            removed = len(self._store.pop(course_ref, []))
            self._save()
        return removed
