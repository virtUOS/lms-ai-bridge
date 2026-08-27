"""Transcription as a provider slot.

**The bridge does not transcribe. It asks something that does.** Audio and video
need inference, not parsing, and an institution running teaching platforms very
likely already has that capability — Osnabrück runs WhisperX; a Stud.IP instance
connected to Opencast may hold transcripts *already made*, in which case the
right move is a fetch, not a GPU. Building transcription into the bridge would
duplicate infrastructure that exists and exclude institutions whose setup differs.

So this mirrors the retrieval seam: an interface, a WhisperX implementation
against the endpoint an institution already runs, and nothing at all where no
provider is configured. `/v1/capabilities` reports which is live.

WhisperX speaks `POST /v1/audio/transcriptions` — the same OpenAI-compatible
shape as chat and embeddings, so it needs no new configuration pattern. The
`transcription-whisper` project at Osnabrück calls it exactly this way, and its
operational limits are worth copying rather than rediscovering: cap concurrency
(it defaults to 3), and scale the timeout to the audio's length rather than
using one fixed number.

**On making people wait.** Transcription takes minutes; indexing a course takes
seconds. Blocking one on the other would mean a course with five recordings is
unusable for an hour. It is not needed: indexing is not interactive — a teacher
opts a course in and comes back — while *asking* must stay fast. So transcripts
are produced in the background and join the index when they are ready, and an
answer given before then is grounded in the text material and says so. See
`bridge/jobs.py`.
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import urllib.error
import time
import urllib.request
import uuid
from pathlib import Path

__all__ = [
    "TranscriptionProvider",
    "OpenAICompatibleTranscription",
    "WhisperXTranscription",
    "TranscriptionAppProvider",
    "make_transcription",
]

# Formats worth sending. Anything else is skipped rather than sent hopefully:
# a failed job costs minutes of somebody's GPU.
AUDIO_SUFFIXES = (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus", ".wma")
VIDEO_SUFFIXES = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v")


def is_transcribable(name: str, mime: str = "") -> bool:
    n, m = name.lower(), (mime or "").lower()
    return (n.endswith(AUDIO_SUFFIXES) or n.endswith(VIDEO_SUFFIXES)
            or m.startswith(("audio/", "video/")))


class TranscriptionProvider:
    """Turns audio or video bytes into text, with timed segments where possible."""

    name = "abstract"

    @property
    def configured(self) -> bool:
        return False

    def transcribe(self, data: bytes, filename: str) -> list[tuple[str, str]]:
        """Return `(locator, text)` pairs, the same shape the extractors use.

        The locator is a timestamp — "12:30" — so a citation points at the
        moment in the recording rather than at the file as a whole. That is the
        audio equivalent of "S. 12", and the reason for segmenting at all.
        """
        raise NotImplementedError


class WhisperXTranscription(TranscriptionProvider):
    """MurmurAI / WhisperX, the transcription API Osnabrück already operates.

    **Job-based, not a long POST.** `POST /v1/transcript` returns an id
    immediately; `GET /v1/transcript/{id}` reports `status` and `progress` until
    it is done. That matters: holding an HTTP connection open for thirty minutes
    is fragile in a way a poll loop is not, and `progress` gives a real number to
    show someone rather than a spinner.

    Two features of this API are worth using and are why the richer
    `utterances` shape is preferred over plain text:

    - **Speaker labels.** A seminar recording with "Sprecher A" and "Sprecher B"
      attributed is far more useful to retrieve over than an undifferentiated
      wall of text, and a citation can name who said it.
    - **Timestamps per utterance**, in milliseconds, which become the locator —
      "12:30" is the audio equivalent of "S. 12".

    **Diarization needs a HuggingFace token, and it is not the bridge's.** The
    speaker models are gated on HuggingFace, so the ASR server holds the token
    (`MURMURAI_HF_TOKEN`, set at deploy time — see `whisperx-api-setup`). A
    client asks for `speaker_labels` and either gets them or does not. That
    division is deliberate: an API client should never carry a credential for a
    third-party service its server already holds, and asking each LMS adapter
    to configure a HuggingFace token would be exactly the sprawl this contract
    exists to prevent.

    So `ASR_DIARIZE=true` is a *request*, not a guarantee. Where the server has
    no token the transcript comes back without speakers, and everything else
    still works — the same fallback principle as retrieval.
    """

    name = "whisperx (murmurai)"

    # WhisperX emits one utterance per breath. A lone "Ja, genau." retrieves
    # nothing useful and clutters the index, so they are grouped.
    _CHUNK_CHARS = 900
    _POLL_SECONDS = 5

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        language: str | None = None,
        diarize: bool | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("ASR_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("ASR_API_KEY", "")
        # Empty means "whatever the server is configured for" — Osnabrück's
        # deployment defaults to large-v3-turbo. Naming a model here overrides
        # that per request, which the deployment explicitly supports (its
        # `feat/per-request-model-selection` patch exists for this).
        self.model = model or os.environ.get("ASR_MODEL", "")
        # "auto" by default: whether a given recording is German is not
        # something the bridge can know. Osnabrück runs courses in English too,
        # and an institution elsewhere may run none in German at all — so
        # Whisper's own detection beats a guess from here. Setting a language
        # explicitly is still worth it where a course really is single-language:
        # detection can go wrong on a noisy opening or a bilingual greeting.
        self.language = language or os.environ.get("ASR_LANGUAGE", "auto")
        self.diarize = (
            os.environ.get("ASR_DIARIZE", "true").lower() in ("1", "true", "yes")
            if diarize is None else diarize
        )

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _timeout_for(self, size_bytes: int) -> int:
        """Overall budget for one file, scaled to its size.

        Copied in spirit from `transcription-whisper`: a fixed timeout is wrong
        in both directions — too short for a lecture, needlessly long for a
        two-minute clip. Roughly 1 MB per minute of speech audio, times a
        generous factor, floored at 30 minutes.
        """
        minutes = max(1.0, size_bytes / 1_000_000)
        return int(max(1800, minutes * 60))

    def transcribe(self, data: bytes, filename: str) -> list[tuple[str, str]]:
        if not self.configured:
            raise RuntimeError("ASR_URL is not set")
        job_id = self.submit(data, filename)
        return self.wait_for(job_id, self._timeout_for(len(data)))

    # -- the two halves, separately usable --

    def submit(self, data: bytes, filename: str) -> str:
        """Hand the file over and return a job id. Returns in seconds."""
        fields = {
            "speaker_labels": "true" if self.diarize else "false",
            "word_timestamps": "true",
        }
        if self.language and self.language != "auto":
            fields["language_code"] = self.language
        if self.model:
            fields["model"] = self.model

        body, content_type = _multipart(fields, filename, data)
        req = urllib.request.Request(
            f"{self.base_url}/v1/transcript",
            data=body,
            headers={"Content-Type": content_type, **self._headers()},
            method="POST",
        )
        result = _json_call(req, timeout=300)
        job_id = result.get("id")
        if not job_id:
            raise RuntimeError(f"no job id in response: {str(result)[:200]}")
        return str(job_id)

    def poll(self, job_id: str) -> dict:
        """One status check: `status`, `progress`, and the result when ready."""
        req = urllib.request.Request(
            f"{self.base_url}/v1/transcript/{job_id}",
            headers=self._headers(),
        )
        return _json_call(req, timeout=60)

    def wait_for(self, job_id: str, timeout: int) -> list[tuple[str, str]]:
        deadline = time.time() + timeout
        while True:
            state = self.poll(job_id)
            status = str(state.get("status") or "").lower()
            if status in ("completed", "done", "finished", "success"):
                if self.diarize and not _has_speakers(state):
                    # Asked for and not delivered: almost always a missing
                    # HuggingFace token on the server. Say so once rather than
                    # letting someone wonder why transcripts have no speakers.
                    print(
                        "  note: speaker labels were requested but none came "
                        "back — the ASR server may have no HuggingFace token "
                        "for the diarization models",
                        file=sys.stderr,
                    )
                return self._segment(state)
            if status in ("error", "failed"):
                raise RuntimeError(
                    f"transcription failed: {state.get('error') or 'unknown'}")
            if time.time() > deadline:
                raise RuntimeError(
                    f"transcription timed out after {timeout}s "
                    f"(status={status}, progress={state.get('progress')})")
            time.sleep(self._POLL_SECONDS)

    @classmethod
    def _segment(cls, result: dict) -> list[tuple[str, str]]:
        """Group utterances into retrievable chunks with timestamps.

        Speaker labels are kept inline. A transcript that reads "Sprecher A: …"
        retrieves better than an undifferentiated wall, and lets an answer say
        who said something.
        """
        utterances = result.get("utterances") or []
        if not utterances:
            text = (result.get("text") or "").strip()
            return [("", text)] if text else []

        out: list[tuple[str, str]] = []
        buf: list[str] = []
        size = 0
        start_ms = int(utterances[0].get("start") or 0)
        last_speaker = None

        for utt in utterances:
            piece = (utt.get("text") or "").strip()
            if not piece:
                continue
            speaker = utt.get("speaker")
            if speaker and speaker != last_speaker:
                piece = f"{speaker}: {piece}"
                last_speaker = speaker
            buf.append(piece)
            size += len(piece)
            if size >= cls._CHUNK_CHARS:
                out.append((_stamp(start_ms / 1000), " ".join(buf)))
                buf, size = [], 0
                start_ms = int(utt.get("end") or start_ms)
                last_speaker = None
        if buf:
            out.append((_stamp(start_ms / 1000), " ".join(buf)))
        return out


class OpenAICompatibleTranscription(TranscriptionProvider):
    """Plain Whisper behind `POST /v1/audio/transcriptions`.

    **This is the one most institutions will actually have.** OpenAI's own API,
    faster-whisper servers, whisper.cpp's server, vLLM, LocalAI and Speaches all
    speak it, so it is the common denominator — the same reasoning that makes
    the chat and embedding providers OpenAI-compatible rather than tied to a
    gateway. `WhisperXTranscription` below is the *specialisation*, for the
    richer job-based API Osnabrück happens to run.

    It blocks for the length of the request, because that is all this API
    offers: there is no job id to poll. That is fine here only because the
    bridge already runs transcription in the background (`bridge/jobs.py`) — the
    blocking happens on a worker thread, not in anyone's request.

    Segment timings come back in **seconds** with `verbose_json`, where the
    MurmurAI API uses milliseconds. Diarization is not part of this API, so
    transcripts carry no speaker labels.
    """

    name = "openai-compatible whisper"

    _CHUNK_CHARS = 900

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        language: str | None = None,
    ) -> None:
        # Falls back to the chat endpoint's configuration: an institution
        # serving Whisper from the same gateway as its chat models — which
        # LiteLLM, vLLM and LocalAI all support — then needs no extra setup.
        self.base_url = (base_url or os.environ.get("ASR_URL")
                         or os.environ.get("OPENAI_BASE_URL", "")).rstrip("/")
        self.api_key = (api_key or os.environ.get("ASR_API_KEY")
                        or os.environ.get("OPENAI_API_KEY", ""))
        # "whisper-1" is OpenAI's own name and what most compatible servers
        # accept; faster-whisper and vLLM deployments often want a real model
        # id such as "large-v3-turbo" or "Systran/faster-whisper-large-v3".
        self.model = model or os.environ.get("ASR_MODEL", "") or "whisper-1"
        # "auto" — see WhisperXTranscription for why detection beats a guess.
        self.language = language or os.environ.get("ASR_LANGUAGE", "auto")

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    def transcribe(self, data: bytes, filename: str) -> list[tuple[str, str]]:
        if not self.configured:
            raise RuntimeError("ASR_URL (or OPENAI_BASE_URL) and ASR_MODEL are needed")

        fields = {"model": self.model, "response_format": "verbose_json"}
        if self.language and self.language != "auto":
            fields["language"] = self.language

        body, content_type = _multipart(fields, filename, data)
        headers = {"Content-Type": content_type}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(
            f"{self.base_url}/v1/audio/transcriptions",
            data=body, headers=headers, method="POST",
        )
        minutes = max(1.0, len(data) / 1_000_000)
        return self._segment(_json_call(req, timeout=int(max(1800, minutes * 60))))

    @classmethod
    def _segment(cls, result: dict) -> list[tuple[str, str]]:
        """Group `verbose_json` segments. Timings are in seconds here."""
        segments = result.get("segments") or []
        if not segments:
            text = (result.get("text") or "").strip()
            return [("", text)] if text else []

        out: list[tuple[str, str]] = []
        buf: list[str] = []
        size = 0
        start = float(segments[0].get("start") or 0)

        for seg in segments:
            piece = (seg.get("text") or "").strip()
            if not piece:
                continue
            buf.append(piece)
            size += len(piece)
            if size >= cls._CHUNK_CHARS:
                out.append((_stamp(start), " ".join(buf)))
                buf, size = [], 0
                start = float(seg.get("end") or start)
        if buf:
            out.append((_stamp(start), " ".join(buf)))
        return out


class TranscriptionAppProvider(TranscriptionProvider):
    """The institution's transcription *application*, not the raw ASR server.

    Osnabrück runs `transcription-whisper` on top of the same engine: it adds an
    archive, presets, LLM-based refinement, translation and analysis. Where an
    institution has that, pointing at it rather than at the bare ASR server
    means reusing work already done — the same argument one level up.

    **Not implemented.** Its API surface was not probed in this session, and
    guessing at endpoints is how the Stud.IP download cost three wrong
    conclusions earlier in this branch. What is needed before writing it: the
    submit endpoint, how a finished transcript is fetched, and whether it too is
    job-based. `TRANSCRIPTION_PROVIDER=app` selects it once it exists.
    """

    name = "transcription-app (not implemented)"

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = (base_url or os.environ.get("TRANSCRIPTION_APP_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("TRANSCRIPTION_APP_KEY", "")

    @property
    def configured(self) -> bool:
        return False        # deliberately: see the docstring

    def transcribe(self, data: bytes, filename: str) -> list[tuple[str, str]]:
        raise NotImplementedError(
            "The transcription-app provider is a placeholder. Probe its API "
            "first — see this class's docstring."
        )


def _has_speakers(state: dict) -> bool:
    return any(u.get("speaker") for u in (state.get("utterances") or []))


def _multipart(fields: dict, filename: str, data: bytes) -> tuple[bytes, str]:
    """Build a multipart/form-data body without a dependency."""
    boundary = f"----bridge{uuid.uuid4().hex}"
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body = bytearray()
    for key, value in fields.items():
        body += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                 f"{value}\r\n").encode("utf-8")
    body += (f"--{boundary}\r\n"
             f'Content-Disposition: form-data; name="file"; '
             f'filename="{Path(filename).name}"\r\n'
             f"Content-Type: {mime}\r\n\r\n").encode("utf-8")
    body += data + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def _json_call(req: urllib.request.Request, timeout: int) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"ASR {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot reach ASR server: {e.reason}") from e


def _stamp(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def make_transcription() -> TranscriptionProvider | None:
    """Build the configured provider, or None when the institution has none.

    None is a normal outcome, not an error: audio is simply not indexed and
    `/v1/capabilities` does not advertise transcription.

    **The default assumes plain Whisper, not WhisperX.** Most institutions run
    something OpenAI-compatible — faster-whisper, whisper.cpp, vLLM, LocalAI, or
    OpenAI itself — so that is what `auto` reaches for. Osnabrück's MurmurAI
    server is a richer, job-based API, and `auto` detects it rather than
    guessing: it exposes `/v1/transcript`, which the OpenAI shape does not.

    Set `ASR_BACKEND` explicitly (`openai`, `whisperx`, `app`) to skip detection.
    """
    mode = os.environ.get("TRANSCRIPTION_PROVIDER", "auto").strip().lower()
    if mode in ("none", "off"):
        return None

    backend = os.environ.get("ASR_BACKEND", "").strip().lower()

    if backend == "app" or mode == "app":
        app = TranscriptionAppProvider()
        if app.configured:
            return app
        # Falls through: the app provider is a placeholder, and silently
        # serving nothing would be worse than using an ASR server directly.

    if backend in ("whisperx", "murmurai"):
        provider = WhisperXTranscription()
        return provider if provider.configured else None

    if backend in ("openai", "whisper", "openai-compatible"):
        provider = OpenAICompatibleTranscription()
        return provider if provider.configured else None

    # Auto: prefer the richer API when the server actually offers it.
    whisperx = WhisperXTranscription()
    if whisperx.configured and _offers_job_api(whisperx.base_url):
        return whisperx

    openai_style = OpenAICompatibleTranscription()
    return openai_style if openai_style.configured else None


def _offers_job_api(base_url: str, timeout: int = 5) -> bool:
    """Does this server expose MurmurAI's job API rather than plain Whisper?

    Checked by asking, not assumed from a hostname. A 401 counts as yes: the
    route exists and merely wants a key. Any failure means "no", so a slow or
    unreachable server degrades to the common denominator instead of breaking
    startup.
    """
    try:
        req = urllib.request.Request(f"{base_url}/v1/transcript", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code in (401, 403, 405, 422)
    except Exception:                                    # noqa: BLE001
        return False
