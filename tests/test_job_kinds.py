"""The job runner with two kinds of work: transcription and reading scans.

They run on different servers with different limits, so one must not queue
behind the other, and the note in an answer must say which material is still
missing.
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.jobs import JobRunner  # noqa: E402


class TestKinds(unittest.TestCase):
    def test_a_long_transcription_does_not_hold_up_a_scan(self):
        runner = JobRunner(max_workers=1, limits={"scan": 1})
        release, scanned = threading.Event(), threading.Event()
        runner.submit("c", "Vorlesung.mp3", lambda: release.wait(5))
        runner.submit("c", "Skript.pdf", scanned.set, kind="scan")
        try:
            self.assertTrue(scanned.wait(2), "the scan waited for the transcription")
        finally:
            release.set()
            runner.wait("c", timeout=5)

    def test_each_kind_keeps_its_own_limit(self):
        runner = JobRunner(max_workers=2, limits={"scan": 1})
        release = threading.Event()
        running, peak, lock = [0], [0], threading.Lock()

        def work():
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            release.wait(0.2)
            with lock:
                running[0] -= 1

        for i in range(3):
            runner.submit("c", f"scan{i}.pdf", work, kind="scan")
        release.set()
        runner.wait("c", timeout=5)
        self.assertEqual(peak[0], 1)


class TestNote(unittest.TestCase):
    def _hold(self, runner: JobRunner, *jobs: tuple[str, str]) -> threading.Event:
        release = threading.Event()
        for title, kind in jobs:
            runner.submit("c", title, lambda: release.wait(5), kind=kind)
        return release

    def test_recordings_only_keeps_the_wording_with_singular_agreement(self):
        # Was "1 Aufnahme wird … und stehen"; the verb now agrees.
        runner = JobRunner(max_workers=2, limits={"scan": 2})
        release = self._hold(runner, ("a.mp3", "transcription"))
        try:
            self.assertEqual(runner.note("c"),
                             "Hinweis: 1 Aufnahme wird noch verarbeitet und steht "
                             "für diese Frage noch nicht zur Verfügung.")
        finally:
            release.set()
            runner.wait("c", timeout=5)

    def test_scans_only_names_files_being_read(self):
        runner = JobRunner(max_workers=2, limits={"scan": 2})
        release = self._hold(runner, ("a.pdf", "scan"), ("b.png", "scan"))
        try:
            self.assertEqual(runner.note("c"),
                             "Hinweis: 2 gescannte Dateien werden noch gelesen und "
                             "stehen für diese Frage noch nicht zur Verfügung.")
        finally:
            release.set()
            runner.wait("c", timeout=5)

    def test_both_kinds_are_named(self):
        runner = JobRunner(max_workers=2, limits={"scan": 2})
        release = self._hold(runner, ("a.mp3", "transcription"), ("b.pdf", "scan"))
        try:
            self.assertEqual(runner.note("c"),
                             "Hinweis: 1 Aufnahme und 1 gescannte Datei werden noch "
                             "verarbeitet und stehen für diese Frage noch nicht zur "
                             "Verfügung.")
        finally:
            release.set()
            runner.wait("c", timeout=5)


if __name__ == "__main__":
    unittest.main()
