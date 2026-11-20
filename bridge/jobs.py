"""Background work that must not make anyone wait.

Two kinds: transcribing recordings and reading scans (bridge/scans.py). Both
take far longer than indexing text — transcription minutes, a scanned page
some seconds.

Transcription takes minutes; indexing a course takes seconds. Blocking one on
the other would leave a course with five recordings unusable for an hour, which
is the wrong trade because **indexing is not interactive**. A teacher opts a
course in and comes back later; what has to stay fast is *asking a question*.

So `/v1/index` returns as soon as the text is indexed, queues any audio, and the
transcripts join the index when they are ready. A question asked in between is
answered from the text material, with a note saying recordings are still being
processed — an honest partial answer beats both a spinner and a silent gap.

Deliberately small: a bounded thread pool and a dict of job states. Concurrency
is capped because an ASR server is a shared institutional resource, not this
prototype's to saturate — `transcription-whisper` caps it at 3 and this follows
that. A production deployment would want a real queue that survives a restart;
that belongs behind this same interface, which is the part worth keeping.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class CourseJobs:
    """What is still being processed for one course."""

    pending: int = 0
    done: int = 0
    failed: int = 0
    titles: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Pending jobs per kind ("transcription", "scan"), for the note: the person
    # asking should know *which* material is still missing.
    pending_by_kind: dict[str, int] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.pending > 0

    def to_dict(self) -> dict:
        return {
            "pending": self.pending,
            "done": self.done,
            "failed": self.failed,
            "titles": list(self.titles),
            "errors": list(self.errors[:5]),
        }


class JobRunner:
    """Runs background jobs, capped per kind and observable.

    Transcription and reading scans go to different servers with different
    limits, so each kind has its own cap: a lecture recording that takes ten
    minutes must not hold up a scanned handout, nor the reverse.
    """

    def __init__(self, max_workers: int = 2,
                 limits: dict[str, int] | None = None) -> None:
        self.max_workers = max_workers
        self._limits = {"transcription": max_workers, **(limits or {})}
        self._sems: dict[str, threading.Semaphore] = {}
        self._lock = threading.Lock()
        self._courses: dict[str, CourseJobs] = {}

    def _sem(self, kind: str) -> threading.Semaphore:
        with self._lock:
            if kind not in self._sems:
                self._sems[kind] = threading.Semaphore(
                    self._limits.get(kind, self.max_workers))
            return self._sems[kind]

    # -- state --

    def status(self, course_ref: str) -> CourseJobs:
        with self._lock:
            return self._courses.get(course_ref, CourseJobs())

    def note(self, course_ref: str) -> str:
        """A sentence for the user, or "" when there is nothing outstanding.

        Included in the answer rather than logged, because the person asking is
        the one who needs to know the material is incomplete.
        """
        state = self.status(course_ref)
        if not state.active:
            return ""
        rec = state.pending_by_kind.get("transcription", 0)
        scans = state.pending_by_kind.get("scan", 0)
        recordings = f"{rec} Aufnahme" + ("" if rec == 1 else "n")
        files = f"{scans} gescannte Datei" + ("" if scans == 1 else "en")
        tail = "für diese Frage noch nicht zur Verfügung."
        if rec and scans:
            return f"Hinweis: {recordings} und {files} werden noch verarbeitet und stehen {tail}"
        if scans:
            verb = ("wird noch gelesen und steht" if scans == 1
                    else "werden noch gelesen und stehen")
            return f"Hinweis: {files} {verb} {tail}"
        n = state.pending
        verb = ("wird noch verarbeitet und steht" if n == 1
                else "werden noch verarbeitet und stehen")
        what = "Aufnahme" if n == 1 else "Aufnahmen"
        return f"Hinweis: {n} {what} {verb} {tail}"

    # -- submission --

    def submit(
        self,
        course_ref: str,
        title: str,
        work: Callable[[], None],
        kind: str = "transcription",
    ) -> None:
        """Queue one unit of work for a course.

        `work` must be self-contained: it does its own indexing and raises on
        failure. The runner only tracks counts and errors.
        """
        with self._lock:
            state = self._courses.setdefault(course_ref, CourseJobs())
            state.pending += 1
            state.pending_by_kind[kind] = state.pending_by_kind.get(kind, 0) + 1
            if title not in state.titles:
                state.titles.append(title)
        sem = self._sem(kind)

        def run() -> None:
            with sem:
                try:
                    work()
                    with self._lock:
                        self._courses[course_ref].done += 1
                except Exception as e:                       # noqa: BLE001
                    # A failed transcription must not take down the server or
                    # lose the course's other material.
                    with self._lock:
                        self._courses[course_ref].failed += 1
                        self._courses[course_ref].errors.append(
                            f"{title}: {str(e)[:200]}")
                finally:
                    with self._lock:
                        state = self._courses[course_ref]
                        state.pending -= 1
                        state.pending_by_kind[kind] -= 1

        threading.Thread(target=run, daemon=True, name=f"job:{title[:20]}").start()

    def wait(self, course_ref: str, timeout: float = 0) -> bool:
        """Block until a course's jobs finish. Returns True if they did.

        Only for tests and the demo script — the server never calls this.
        """
        deadline = time.time() + timeout if timeout else None
        while self.status(course_ref).active:
            if deadline and time.time() > deadline:
                return False
            time.sleep(0.05)
        return True
