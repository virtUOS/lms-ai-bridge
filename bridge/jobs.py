"""Background work that must not make anyone wait.

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
    """Runs transcription jobs in the background, capped and observable."""

    def __init__(self, max_workers: int = 2) -> None:
        self.max_workers = max_workers
        self._sem = threading.Semaphore(max_workers)
        self._lock = threading.Lock()
        self._courses: dict[str, CourseJobs] = {}

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
        n = state.pending
        what = "Aufnahme wird" if n == 1 else "Aufnahmen werden"
        return (f"Hinweis: {n} {what} noch verarbeitet und "
                f"stehen für diese Frage noch nicht zur Verfügung.")

    # -- submission --

    def submit(
        self,
        course_ref: str,
        title: str,
        work: Callable[[], None],
    ) -> None:
        """Queue one unit of work for a course.

        `work` must be self-contained: it does its own indexing and raises on
        failure. The runner only tracks counts and errors.
        """
        with self._lock:
            state = self._courses.setdefault(course_ref, CourseJobs())
            state.pending += 1
            if title not in state.titles:
                state.titles.append(title)

        def run() -> None:
            with self._sem:
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
                        self._courses[course_ref].pending -= 1

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
