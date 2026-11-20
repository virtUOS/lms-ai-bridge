"""The image-reading provider: configuration, request shape, and answer cleanup.

No network: urlopen is replaced and the request it would have sent is examined.
The configuration tests matter most. This feature sends course material to a
model, so "off unless someone chose a model and said what kind it is" is the
behaviour to pin down.

Run:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import base64
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bridge.providers.image_text as IT  # noqa: E402
from bridge.providers.image_text import (  # noqa: E402
    ImageTextConfigError, ImageTextProvider, make_image_text,
)

BASE = {"OPENAI_BASE_URL": "https://gateway.example/v1", "OPENAI_API_KEY": "sk-chat"}


class TestConfiguration(unittest.TestCase):
    def test_off_without_a_model(self):
        self.assertIsNone(make_image_text(dict(BASE)))

    def test_never_falls_back_to_the_chat_model(self):
        # Chat may run on a commercial provider; images go nowhere unless chosen.
        self.assertIsNone(make_image_text({**BASE, "MODEL": "some-chat-model"}))

    def test_the_kind_of_model_must_be_stated(self):
        with self.assertRaises(ImageTextConfigError):
            make_image_text({**BASE, "IMAGE_TEXT_MODEL": "lightonai/LightOnOCR-3-4B"})

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ImageTextConfigError):
            make_image_text({**BASE, "IMAGE_TEXT_MODEL": "m", "IMAGE_TEXT_MODE": "auto"})

    def test_uses_the_chat_gateway_by_default(self):
        p = make_image_text({**BASE, "IMAGE_TEXT_MODEL": "m", "IMAGE_TEXT_MODE": "vision"})
        self.assertEqual(p.base_url, "https://gateway.example/v1")
        self.assertEqual(p.api_key, "sk-chat")

    def test_an_own_endpoint_overrides_the_gateway(self):
        p = make_image_text({**BASE, "IMAGE_TEXT_MODEL": "m", "IMAGE_TEXT_MODE": "ocr",
                             "IMAGE_TEXT_BASE_URL": "https://ocr.example/v1/",
                             "IMAGE_TEXT_API_KEY": "sk-ocr"})
        self.assertEqual(p.base_url, "https://ocr.example/v1")
        self.assertEqual(p.api_key, "sk-ocr")

    def test_no_endpoint_at_all_is_refused(self):
        with self.assertRaises(ImageTextConfigError):
            make_image_text({"IMAGE_TEXT_MODEL": "m", "IMAGE_TEXT_MODE": "ocr"})

    def test_limits_have_defaults_and_can_be_set(self):
        p = make_image_text({**BASE, "IMAGE_TEXT_MODEL": "m", "IMAGE_TEXT_MODE": "ocr"})
        self.assertEqual((p.max_pages, p.max_concurrent, p.longest_edge), (200, 2, 2048))
        p = make_image_text({**BASE, "IMAGE_TEXT_MODEL": "m", "IMAGE_TEXT_MODE": "ocr",
                             "IMAGE_TEXT_MAX_PAGES": "10", "IMAGE_TEXT_MAX_CONCURRENT": "1",
                             "IMAGE_TEXT_LONGEST_EDGE": "1540"})
        self.assertEqual((p.max_pages, p.max_concurrent, p.longest_edge), (10, 1, 1540))


def _provider(mode: str) -> ImageTextProvider:
    return ImageTextProvider(model="m", mode=mode,
                             base_url="https://gw.example/v1", api_key="sk-x")


class TestRequestShape(unittest.TestCase):
    def test_ocr_mode_sends_the_image_alone(self):
        # What the in-house OCR adapter sends LightOnOCR: one image_url, no text.
        body = _provider("ocr").payload(b"\x89PNG", "image/png")
        content = body["messages"][0]["content"]
        self.assertEqual([c["type"] for c in content], ["image_url"])
        self.assertEqual(content[0]["image_url"]["url"],
                         "data:image/png;base64," + base64.b64encode(b"\x89PNG").decode())
        self.assertEqual(body["temperature"], 0.2)
        self.assertEqual(body["model"], "m")

    def test_vision_mode_asks_for_a_literal_transcription(self):
        body = _provider("vision").payload(b"\xff\xd8", "image/jpeg")
        content = body["messages"][0]["content"]
        self.assertEqual([c["type"] for c in content], ["text", "image_url"])
        instruction = content[0]["text"].lower()
        for word in ("transcribe", "exactly", "do not summarise"):
            self.assertIn(word, instruction)
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(body["temperature"], 0)


def _answer(content: str):
    raw = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
    resp = io.BytesIO(raw)
    resp.__enter__ = lambda s=resp: s
    resp.__exit__ = lambda *a: False
    return resp


class TestReading(unittest.TestCase):
    def _read(self, content: str, mode: str = "vision") -> tuple[str, dict]:
        seen: dict = {}

        def fake_urlopen(req, timeout):
            seen["url"], seen["auth"] = req.full_url, req.get_header("Authorization")
            seen["body"] = json.loads(req.data)
            return _answer(content)

        with mock.patch.object(IT.urllib.request, "urlopen", fake_urlopen):
            return _provider(mode).read(b"\x89PNG"), seen

    def test_posts_to_chat_completions_with_the_key(self):
        _, seen = self._read("Text")
        self.assertEqual(seen["url"], "https://gw.example/v1/chat/completions")
        self.assertEqual(seen["auth"], "Bearer sk-x")

    def test_returns_the_text(self):
        self.assertEqual(self._read("# Vorlesung 7\n\nLernrate = 0,05")[0],
                         "# Vorlesung 7\n\nLernrate = 0,05")

    def test_strips_reasoning_blocks(self):
        self.assertEqual(self._read("<think>Let me look.</think>\nKlausur: 15.02.")[0],
                         "Klausur: 15.02.")

    def test_strips_a_fence_around_the_whole_answer(self):
        self.assertEqual(self._read("```markdown\n| a | b |\n```")[0], "| a | b |")

    def test_a_page_without_text_reads_as_empty(self):
        self.assertEqual(self._read("NO_TEXT")[0], "")

    def test_a_server_error_is_raised_with_its_reason(self):
        def failing(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                         io.BytesIO(b"At most 0 image(s) may be provided"))
        with mock.patch.object(IT.urllib.request, "urlopen", failing):
            with self.assertRaisesRegex(RuntimeError, "400.*At most 0 image"):
                _provider("vision").read(b"\x89PNG")


class TestStartup(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.store = Path(self.dir.name) / "index.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_not_configured_says_scans_are_skipped(self):
        from bridge.server import image_text_from_env
        reader, cache, line = image_text_from_env(dict(BASE), self.store)
        self.assertIsNone(reader)
        self.assertIsNone(cache)
        self.assertIn("scans skipped", line)

    def test_a_misconfiguration_leaves_it_off_and_says_why(self):
        # Chat must keep working; the reason must be in the startup log.
        from bridge.server import image_text_from_env
        reader, _, line = image_text_from_env({**BASE, "IMAGE_TEXT_MODEL": "m"}, self.store)
        self.assertIsNone(reader)
        self.assertIn("IMAGE_TEXT_MODE", line)

    def test_configured_keeps_its_cache_beside_the_index(self):
        from bridge.server import image_text_from_env
        reader, cache, line = image_text_from_env(
            {**BASE, "IMAGE_TEXT_MODEL": "m", "IMAGE_TEXT_MODE": "ocr"}, self.store)
        self.assertEqual(reader.name, "ocr:m")
        self.assertEqual(cache.path, self.store.with_name("image-text-cache.json"))
        self.assertIn("ocr:m", line)

    def test_the_cache_path_can_be_set(self):
        from bridge.server import image_text_from_env
        own = Path(self.dir.name) / "elsewhere.json"
        _, cache, _ = image_text_from_env(
            {**BASE, "IMAGE_TEXT_MODEL": "m", "IMAGE_TEXT_MODE": "ocr",
             "IMAGE_TEXT_CACHE": str(own)}, self.store)
        self.assertEqual(cache.path, own)


if __name__ == "__main__":
    unittest.main()
