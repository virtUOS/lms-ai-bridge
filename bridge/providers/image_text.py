"""Reading text from images, as a provider slot.

**The bridge does not do OCR. It asks a model that can.** The same seam as
transcription: an institution very likely already runs something that reads
images — Osnabrück runs a dedicated OCR model (LightOnOCR) and several chat
models that take images — and which one fits is the deployment's call:

- an **OCR model** is trained to transcribe literally, and is cheap to run;
- a **general vision model** may read hard pages better, but tends to smooth
  text (fixing typos, dropping lines), and on a commercial API every page costs.

Both answer the same OpenAI-compatible call — chat completions with one image —
so one code path serves both. What differs is whether the model needs an
instruction, and the bridge cannot tell from a model name, so `IMAGE_TEXT_MODE`
must say. **There is no fallback to the chat model**: chat may run on a
commercial provider, and course images should go nowhere nobody chose.

Settings (see the design in docs/superpowers/specs/2026-11-20-image-text-design.md):

    IMAGE_TEXT_MODEL           model that reads images; unset = off
    IMAGE_TEXT_MODE            ocr | vision
    IMAGE_TEXT_BASE_URL / _API_KEY   default OPENAI_BASE_URL / OPENAI_API_KEY
    IMAGE_TEXT_MAX_PAGES       pages per /v1/index request (200)
    IMAGE_TEXT_MAX_CONCURRENT  parallel calls (2)
    IMAGE_TEXT_LONGEST_EDGE    rendered page size in pixels (2048)
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from typing import Mapping

__all__ = ["ImageTextConfigError", "ImageTextProvider", "make_image_text", "MODES"]

MODES = ("ocr", "vision")

# Kept literal on purpose: the page text becomes what an answer cites.
VISION_INSTRUCTION = (
    "Transcribe all text in this image exactly as it appears, in reading order, "
    "as Markdown. Use Markdown tables for tables and LaTeX for formulas. "
    "Do not summarise, translate, correct or add anything, and do not describe "
    "pictures. If the image contains no text at all, answer with exactly: NO_TEXT"
)
_EMPTY = "NO_TEXT"

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"\A```[a-zA-Z]*\n(.*)\n```\Z", re.DOTALL)


class ImageTextConfigError(ValueError):
    """The settings name a model but cannot be used as they are."""


class ImageTextProvider:
    def __init__(self, *, model: str, mode: str, base_url: str, api_key: str = "",
                 max_pages: int = 200, max_concurrent: int = 2,
                 longest_edge: int = 2048, timeout: int = 180,
                 max_tokens: int = 4096) -> None:
        if mode not in MODES:
            raise ImageTextConfigError(f"IMAGE_TEXT_MODE must be one of {', '.join(MODES)}")
        self.model, self.mode = model, mode
        self.base_url, self.api_key = base_url.rstrip("/"), api_key
        self.max_pages, self.max_concurrent = max_pages, max_concurrent
        self.longest_edge, self.timeout, self.max_tokens = longest_edge, timeout, max_tokens

    @property
    def name(self) -> str:
        return f"{self.mode}:{self.model}"

    def payload(self, image: bytes, mime: str) -> dict:
        url = f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"
        content: list[dict] = [{"type": "image_url", "image_url": {"url": url}}]
        if self.mode == "vision":
            content.insert(0, {"type": "text", "text": VISION_INSTRUCTION})
        return {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            # 0.2 is what the in-house OCR adapter sends LightOnOCR; a general
            # model transcribing should not be creative at all.
            "temperature": 0.2 if self.mode == "ocr" else 0,
            "max_tokens": self.max_tokens,
        }

    def read(self, image: bytes, mime: str = "image/png") -> str:
        """The image's text, or "" when it has none. Raises on failure."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(self.payload(image, mime)).encode("utf-8"),
            headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            with e:
                detail = e.read()[:300].decode("utf-8", "replace")
            raise RuntimeError(f"{self.model}: HTTP {e.code} {detail}") from None
        content = data["choices"][0]["message"].get("content") or ""
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return _clean(content)


def _clean(text: str) -> str:
    text = _THINK.sub("", text).strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    return "" if text == _EMPTY else text


def make_image_text(env: Mapping[str, str] | None = None) -> ImageTextProvider | None:
    """The configured provider, None when off. Raises on unusable settings."""
    env = os.environ if env is None else env
    model = env.get("IMAGE_TEXT_MODEL", "").strip()
    if not model:
        return None
    mode = env.get("IMAGE_TEXT_MODE", "").strip().lower()
    if mode not in MODES:
        raise ImageTextConfigError(
            f"IMAGE_TEXT_MODEL is set but IMAGE_TEXT_MODE is {mode or 'missing'}: "
            f"say whether {model} is an OCR model (ocr) or a general vision model (vision)")
    base_url = (env.get("IMAGE_TEXT_BASE_URL") or env.get("OPENAI_BASE_URL") or "").strip()
    if not base_url:
        raise ImageTextConfigError(
            "IMAGE_TEXT_MODEL is set but neither IMAGE_TEXT_BASE_URL nor OPENAI_BASE_URL is")
    return ImageTextProvider(
        model=model, mode=mode, base_url=base_url,
        api_key=env.get("IMAGE_TEXT_API_KEY") or env.get("OPENAI_API_KEY") or "",
        max_pages=int(env.get("IMAGE_TEXT_MAX_PAGES", "200")),
        max_concurrent=int(env.get("IMAGE_TEXT_MAX_CONCURRENT", "2")),
        longest_edge=int(env.get("IMAGE_TEXT_LONGEST_EDGE", "2048")),
    )
