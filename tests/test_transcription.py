"""Tests for the transcription provider and the background job runner.

No network and no ASR server: the WhisperX response shape is stubbed from what
`transcription-whisper` documents, and the runner is exercised with plain
callables. The point is the *behaviour under waiting*, which is the part that
would otherwise only show up in a live demo with a five-minute pause in it.

Run:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.jobs import JobRunner  # noqa: E402
from bridge.providers.transcription import (  # noqa: E402
    WhisperXTranscription, is_transcribable, make_transcription,
)


class TestTranscribableDetection(unittest.TestCase):
    def test_audio_and_video_are_recognised(self):
        self.assertTrue(is_transcribable("Vorlesung.mp3"))
        self.assertTrue(is_transcribable("aufnahme.m4a", "audio/mp4"))
        self.assertTrue(is_transcribable("clip.mp4", "video/mp4"))
        self.assertTrue(is_transcribable("x", "audio/ogg"))

    def test_documents_are_not(self):
        # These have real extractors; sending them to a GPU would be waste.
        self.assertFalse(is_transcribable("Folien.pptx"))
        self.assertFalse(is_transcribable("Skript.pdf", "application/pdf"))


class TestSegmentation(unittest.TestCase):
    """Timestamps are why segmenting matters: "12:30" is the audio equivalent
    of "S. 12", and a citation nobody can check is barely a citation.

    Shapes here match the live MurmurAI schema: `utterances`, with `start` and
    `end` in **milliseconds**, and an optional `speaker`.
    """

    def test_utterances_become_timestamped_chunks(self):
        result = {"utterances": [
            {"start": 0, "end": 30000, "text": "A" * 500},
            {"start": 30000, "end": 60000, "text": "B" * 500},
            {"start": 750000, "end": 780000, "text": "C" * 100},
        ]}
        units = WhisperXTranscription._segment(result)
        self.assertGreaterEqual(len(units), 2)
        self.assertEqual(units[0][0], "0:00")
        self.assertTrue(any(":" in loc for loc, _ in units))

    def test_short_utterances_are_grouped_not_indexed_alone(self):
        # WhisperX emits one utterance per breath. A lone "Ja, genau." retrieves
        # nothing useful and clutters the index.
        result = {"utterances": [
            {"start": i * 1000, "end": (i + 1) * 1000, "text": "Ja, genau."}
            for i in range(20)
        ]}
        units = WhisperXTranscription._segment(result)
        self.assertEqual(len(units), 1)

    def test_speaker_labels_are_kept_inline(self):
        # A seminar with speakers attributed retrieves better than a wall of
        # text, and lets an answer say who said something.
        result = {"utterances": [
            {"start": 0, "end": 5000, "speaker": "SPEAKER_00",
             "text": "Willkommen zur Vorlesung."},
            {"start": 5000, "end": 9000, "speaker": "SPEAKER_01",
             "text": "Eine kurze Frage dazu."},
        ]}
        text = WhisperXTranscription._segment(result)[0][1]
        self.assertIn("SPEAKER_00: Willkommen", text)
        self.assertIn("SPEAKER_01: Eine kurze Frage", text)

    def test_the_same_speaker_is_not_relabelled_every_utterance(self):
        result = {"utterances": [
            {"start": 0, "end": 1000, "speaker": "A", "text": "Erstens."},
            {"start": 1000, "end": 2000, "speaker": "A", "text": "Zweitens."},
        ]}
        text = WhisperXTranscription._segment(result)[0][1]
        self.assertEqual(text.count("A:"), 1)

    def test_a_response_without_utterances_still_yields_text(self):
        units = WhisperXTranscription._segment({"text": "Ein ganzer Vortrag."})
        self.assertEqual(units, [("", "Ein ganzer Vortrag.")])

    def test_hours_are_formatted(self):
        result = {"utterances": [
            {"start": 3725000, "end": 3730000, "text": "X" * 950}]}
        self.assertEqual(WhisperXTranscription._segment(result)[0][0], "1:02:05")


class TestJobBasedApi(unittest.TestCase):
    """The API is submit-then-poll, which is why nobody has to hold a
    connection open for half an hour."""

    def test_submit_returns_the_job_id(self):
        provider = WhisperXTranscription(base_url="http://asr.example.org")
        captured = {}

        def fake(req, timeout):
            captured["url"] = req.full_url
            captured["method"] = req.method
            return {"id": "job-123", "status": "queued"}

        import bridge.providers.transcription as T      # noqa: PLC0415
        original, T._json_call = T._json_call, fake
        try:
            self.assertEqual(provider.submit(b"RIFF....", "vorlesung.mp3"), "job-123")
        finally:
            T._json_call = original
        self.assertTrue(captured["url"].endswith("/v1/transcript"))
        self.assertEqual(captured["method"], "POST")

    def test_wait_for_polls_until_completed(self):
        provider = WhisperXTranscription(base_url="http://asr.example.org")
        provider._POLL_SECONDS = 0
        states = [
            {"status": "processing", "progress": 0.1},
            {"status": "processing", "progress": 0.8},
            {"status": "completed",
             "utterances": [{"start": 0, "end": 1000, "text": "Fertig."}]},
        ]
        provider.poll = lambda job_id: states.pop(0)
        units = provider.wait_for("job-123", timeout=5)
        self.assertEqual(units, [("0:00", "Fertig.")])

    def test_a_failed_job_raises_with_the_server_reason(self):
        provider = WhisperXTranscription(base_url="http://asr.example.org")
        provider._POLL_SECONDS = 0
        provider.poll = lambda job_id: {"status": "error", "error": "bad audio"}
        with self.assertRaises(RuntimeError) as ctx:
            provider.wait_for("j", timeout=5)
        self.assertIn("bad audio", str(ctx.exception))

    def test_timeout_scales_with_file_size(self):
        provider = WhisperXTranscription(base_url="http://x")
        small = provider._timeout_for(500_000)
        large = provider._timeout_for(300_000_000)
        self.assertEqual(small, 1800)          # floor: half an hour
        self.assertGreater(large, small)


class TestTranscriptionAppPlaceholder(unittest.TestCase):
    """The institution's transcription *app* is a richer target than the raw
    ASR server, but its API was not probed — and guessing at endpoints is how
    the Stud.IP download cost three wrong conclusions earlier in this work."""

    def test_it_reports_itself_unconfigured(self):
        from bridge.providers.transcription import (    # noqa: PLC0415
            TranscriptionAppProvider,
        )
        self.assertFalse(TranscriptionAppProvider(base_url="http://app").configured)

    def test_it_refuses_rather_than_guessing(self):
        from bridge.providers.transcription import (    # noqa: PLC0415
            TranscriptionAppProvider,
        )
        with self.assertRaises(NotImplementedError):
            TranscriptionAppProvider().transcribe(b"x", "a.mp3")


class TestNoProviderIsNormal(unittest.TestCase):
    def test_unconfigured_returns_none_rather_than_failing(self):
        # An institution with no ASR server simply does not index audio; the
        # capability is not advertised and nothing errors.
        import os
        saved = os.environ.pop("ASR_URL", None)
        try:
            self.assertIsNone(make_transcription())
        finally:
            if saved:
                os.environ["ASR_URL"] = saved

    def test_explicitly_disabled(self):
        import os
        os.environ["TRANSCRIPTION_PROVIDER"] = "none"
        os.environ["ASR_URL"] = "http://asr.example.org"
        try:
            self.assertIsNone(make_transcription())
        finally:
            os.environ.pop("TRANSCRIPTION_PROVIDER", None)
            os.environ.pop("ASR_URL", None)


class TestJobRunner(unittest.TestCase):
    """The whole point: indexing returns immediately, answers stay possible."""

    def test_work_runs_in_the_background_and_completes(self):
        runner = JobRunner(max_workers=2)
        done = threading.Event()
        runner.submit("studip:x", "Vorlesung.mp3", done.set)
        self.assertTrue(done.wait(2))
        self.assertTrue(runner.wait("studip:x", timeout=2))
        self.assertEqual(runner.status("studip:x").done, 1)

    def test_a_pending_job_produces_a_note_for_the_user(self):
        runner = JobRunner(max_workers=1)
        release = threading.Event()
        runner.submit("studip:y", "Lang.mp3", lambda: release.wait(2))
        time.sleep(0.05)
        note = runner.note("studip:y")
        self.assertIn("Aufnahme", note)
        release.set()
        runner.wait("studip:y", timeout=3)
        self.assertEqual(runner.note("studip:y"), "")

    def test_a_failing_job_is_recorded_not_raised(self):
        # One bad recording must not lose a course its other material, and must
        # not take down the server from a background thread.
        runner = JobRunner(max_workers=1)

        def boom():
            raise RuntimeError("ASR server said no")

        runner.submit("studip:z", "Kaputt.mp3", boom)
        self.assertTrue(runner.wait("studip:z", timeout=2))
        state = runner.status("studip:z")
        self.assertEqual(state.failed, 1)
        self.assertIn("ASR server said no", state.errors[0])

    def test_concurrency_is_capped(self):
        # An ASR server is a shared institutional resource, not this
        # prototype's to saturate.
        runner = JobRunner(max_workers=2)
        active, peak = [0], [0]
        lock = threading.Lock()
        release = threading.Event()

        def work():
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            release.wait(2)
            with lock:
                active[0] -= 1

        for i in range(6):
            runner.submit("studip:c", f"f{i}.mp3", work)
        time.sleep(0.2)
        self.assertLessEqual(peak[0], 2)
        release.set()
        runner.wait("studip:c", timeout=5)

    def test_an_unknown_course_has_no_note(self):
        self.assertEqual(JobRunner().note("studip:never"), "")


class TestOpenAICompatibleWhisper(unittest.TestCase):
    """The common denominator, and the default.

    Most institutions run plain Whisper — faster-whisper, whisper.cpp, vLLM,
    LocalAI, OpenAI itself — not the job-based API Osnabrück happens to have.
    Its `verbose_json` segments are in SECONDS, where MurmurAI uses
    milliseconds; getting that wrong would put every citation at the wrong time.
    """

    def test_seconds_not_milliseconds(self):
        from bridge.providers.transcription import (        # noqa: PLC0415
            OpenAICompatibleTranscription,
        )
        result = {"segments": [
            {"start": 750.0, "end": 780.0, "text": "X" * 950},
        ]}
        units = OpenAICompatibleTranscription._segment(result)
        self.assertEqual(units[0][0], "12:30")

    def test_falls_back_to_the_chat_endpoint_configuration(self):
        # An institution serving Whisper from the same gateway as its chat
        # models needs no extra configuration at all.
        import os
        from bridge.providers.transcription import (        # noqa: PLC0415
            OpenAICompatibleTranscription,
        )
        saved = {k: os.environ.get(k) for k in
                 ("ASR_URL", "OPENAI_BASE_URL", "ASR_MODEL")}
        os.environ.pop("ASR_URL", None)
        os.environ["OPENAI_BASE_URL"] = "https://gateway.example.org/v1"
        try:
            p = OpenAICompatibleTranscription()
            self.assertTrue(p.configured)
            self.assertTrue(p.base_url.startswith("https://gateway"))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_text_only_response_still_works(self):
        from bridge.providers.transcription import (        # noqa: PLC0415
            OpenAICompatibleTranscription,
        )
        units = OpenAICompatibleTranscription._segment({"text": "Nur Text."})
        self.assertEqual(units, [("", "Nur Text.")])


class TestBackendSelection(unittest.TestCase):
    """Detect, do not assume. A hostname is not evidence of an API shape."""

    def setUp(self):
        import os
        self.saved = {k: os.environ.get(k) for k in
                      ("ASR_BACKEND", "ASR_URL", "TRANSCRIPTION_PROVIDER",
                       "OPENAI_BASE_URL", "ASR_MODEL")}
        for k in self.saved:
            os.environ.pop(k, None)

    def tearDown(self):
        import os
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_explicit_openai_backend_is_honoured(self):
        import os
        os.environ["ASR_BACKEND"] = "openai"
        os.environ["ASR_URL"] = "https://whisper.example.org"
        from bridge.providers.transcription import (        # noqa: PLC0415
            OpenAICompatibleTranscription, make_transcription,
        )
        self.assertIsInstance(make_transcription(), OpenAICompatibleTranscription)

    def test_explicit_whisperx_backend_is_honoured(self):
        import os
        os.environ["ASR_BACKEND"] = "whisperx"
        os.environ["ASR_URL"] = "https://asr.example.org"
        from bridge.providers.transcription import (        # noqa: PLC0415
            WhisperXTranscription, make_transcription,
        )
        self.assertIsInstance(make_transcription(), WhisperXTranscription)

    def test_no_configuration_means_no_audio_not_an_error(self):
        from bridge.providers.transcription import make_transcription  # noqa: PLC0415
        self.assertIsNone(make_transcription())

    def test_an_unreachable_server_degrades_instead_of_breaking_startup(self):
        from bridge.providers.transcription import _offers_job_api  # noqa: PLC0415
        self.assertFalse(_offers_job_api("http://127.0.0.1:9", timeout=1))


class TestModelAndDiarizationConfig(unittest.TestCase):
    """Two settings that differ in an important way.

    The **model** is a per-request field, so it belongs in the client's
    configuration. The **HuggingFace token** for diarization is a server-side
    credential (MURMURAI_HF_TOKEN, set at deploy time), so the bridge must never
    ask for one — an API client should not carry a credential for a third-party
    service its server already holds.
    """

    def setUp(self):
        import os
        self.saved = {k: os.environ.get(k) for k in
                      ("ASR_MODEL", "ASR_DIARIZE", "ASR_LANGUAGE")}
        for k in self.saved:
            import os as _os
            _os.environ.pop(k, None)

    def tearDown(self):
        import os
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_model_is_configurable_and_sent_per_request(self):
        provider = WhisperXTranscription(
            base_url="http://asr.example.org", model="large-v3-turbo")
        captured = {}

        def fake(req, timeout):
            captured["body"] = req.data
            return {"id": "j1"}

        import bridge.providers.transcription as T          # noqa: PLC0415
        original, T._json_call = T._json_call, fake
        try:
            provider.submit(b"audio", "a.mp3")
        finally:
            T._json_call = original
        self.assertIn(b"large-v3-turbo", captured["body"])

    def test_no_model_means_the_server_decides(self):
        # Osnabrück's deployment defaults to large-v3-turbo; not naming one lets
        # that stand rather than overriding it with a guess.
        provider = WhisperXTranscription(base_url="http://asr.example.org")
        self.assertEqual(provider.model, "")
        captured = {}

        def fake(req, timeout):
            captured["body"] = req.data
            return {"id": "j1"}

        import bridge.providers.transcription as T          # noqa: PLC0415
        original, T._json_call = T._json_call, fake
        try:
            provider.submit(b"audio", "a.mp3")
        finally:
            T._json_call = original
        self.assertNotIn(b'name="model"', captured["body"])

    def test_no_huggingface_token_is_ever_sent(self):
        # The bridge has no business holding one.
        import os
        os.environ["HF_TOKEN"] = "hf_should_never_be_used"
        try:
            provider = WhisperXTranscription(base_url="http://asr.example.org")
            captured = {}

            def fake(req, timeout):
                captured["body"] = req.data
                return {"id": "j1"}

            import bridge.providers.transcription as T      # noqa: PLC0415
            original, T._json_call = T._json_call, fake
            try:
                provider.submit(b"audio", "a.mp3")
            finally:
                T._json_call = original
            self.assertNotIn(b"hf_should_never_be_used", captured["body"])
        finally:
            os.environ.pop("HF_TOKEN", None)

    def test_diarization_is_a_request_not_a_guarantee(self):
        # A server without the HF token returns utterances with no speakers.
        # That must still produce a usable transcript.
        result = {"utterances": [
            {"start": 0, "end": 4000, "text": "Erster Satz."},
            {"start": 4000, "end": 8000, "text": "Zweiter Satz."},
        ]}
        units = WhisperXTranscription._segment(result)
        self.assertIn("Erster Satz.", units[0][1])
        self.assertNotIn(":", units[0][1].split()[0])

    def test_auto_language_sends_no_language_field(self):
        # The failure this guards is silent: sending the literal string "auto"
        # as a language code would make the server try to transcribe German as
        # a language called "auto", and nothing would look obviously wrong.
        for lang in ("auto", ""):
            provider = WhisperXTranscription(
                base_url="http://asr.example.org", language=lang)
            captured = {}

            def fake(req, timeout):
                captured["body"] = req.data
                return {"id": "j1"}

            import bridge.providers.transcription as T      # noqa: PLC0415
            original, T._json_call = T._json_call, fake
            try:
                provider.submit(b"audio", "a.mp3")
            finally:
                T._json_call = original
            self.assertNotIn(b"language_code", captured["body"],
                             f"language field sent for {lang!r}")

    def test_an_explicit_language_is_sent(self):
        provider = WhisperXTranscription(
            base_url="http://asr.example.org", language="de")
        captured = {}

        def fake(req, timeout):
            captured["body"] = req.data
            return {"id": "j1"}

        import bridge.providers.transcription as T          # noqa: PLC0415
        original, T._json_call = T._json_call, fake
        try:
            provider.submit(b"audio", "a.mp3")
        finally:
            T._json_call = original
        self.assertIn(b"language_code", captured["body"])
        self.assertIn(b"de", captured["body"])

    def test_the_default_is_detection_not_german(self):
        # A German default is an assumption about one institution's courses.
        import os
        os.environ.pop("ASR_LANGUAGE", None)
        self.assertEqual(
            WhisperXTranscription(base_url="http://x").language, "auto")

    def test_diarize_flag_is_sent(self):
        provider = WhisperXTranscription(
            base_url="http://asr.example.org", diarize=True)
        captured = {}

        def fake(req, timeout):
            captured["body"] = req.data
            return {"id": "j1"}

        import bridge.providers.transcription as T          # noqa: PLC0415
        original, T._json_call = T._json_call, fake
        try:
            provider.submit(b"audio", "a.mp3")
        finally:
            T._json_call = original
        self.assertIn(b"speaker_labels", captured["body"])
        self.assertIn(b"true", captured["body"])


if __name__ == "__main__":
    unittest.main()
