"""Chat provider for any OpenAI-compatible endpoint.

Per `prototypes/README.md`, configuration comes from the environment and no
host, vendor SDK or gateway is hardcoded. A LiteLLM gateway, a bare vLLM
server, GWDG's academic cloud or OpenAI itself all satisfy the same contract.

Standard library only — no dependency to install before a colleague can run it.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from ..contract import ChatProvider, Message, Source

SYSTEM_GROUNDED = (
    "Du bist eine Lernassistenz für eine Hochschulveranstaltung. "
    "Beantworte die Frage AUSSCHLIESSLICH auf Grundlage der bereitgestellten "
    "Auszüge aus den Kursmaterialien. Wenn die Auszüge die Frage nicht "
    "beantworten, sage das ausdrücklich und rate nicht. "
    "Verweise auf die Quellen mit ihrer Nummer, z. B. [1]."
)

# Used when a course was named but retrieval found nothing. Without this the
# model answers from general knowledge and sounds authoritative doing it: asked
# "Wie reiche ich einen Job ein?" against an HPC course whose passages did not
# match, gpt-oss-120b produced a confident guide to applying for jobs on
# LinkedIn. Nothing marked it as ungrounded. An assistant that invents an answer
# about a course is worse than one that says it does not know, so the refusal is
# instructed explicitly rather than left to the model's judgement.
SYSTEM_NO_CONTEXT = (
    "Du bist eine Lernassistenz für eine Hochschulveranstaltung. "
    "Zu dieser Frage wurden KEINE passenden Stellen in den Kursmaterialien "
    "gefunden. Sage das dem Nutzer klar und in einem Satz. "
    "Beantworte die Frage NICHT aus deinem Allgemeinwissen und rate nicht. "
    "Schlage stattdessen vor, die Frage anders zu formulieren oder die "
    "Lehrperson zu fragen."
)


class OpenAICompatibleChat(ChatProvider):
    name = "openai-compatible"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int = 120,
        max_tokens: int | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model or os.environ.get("MODEL", "")
        self.timeout = timeout
        # Generous by default: a reasoning model spends most of its budget
        # thinking, and a cap sized for a plain model looks like a failure.
        self.max_tokens = int(max_tokens or os.environ.get("CHAT_MAX_TOKENS", 1500))

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    def _payload(
        self,
        messages: list[Message],
        context: list[Source] | None,
        grounded: bool = False,
    ) -> list[dict[str, str]]:
        """Build the wire messages, prepending grounding context if present.

        `grounded` says a course was named, regardless of whether retrieval
        found anything. That distinction matters: "no course given" is ordinary
        open chat, but "course given, nothing found" must refuse rather than
        answer from general knowledge.
        """
        out: list[dict[str, str]] = []
        if grounded and not context:
            # A course was named but nothing matched: instruct the refusal.
            out.append({"role": "system", "content": SYSTEM_NO_CONTEXT})
        if context:
            blocks = []
            for i, (src, text) in enumerate(context, start=1):
                loc = f", {src.locator}" if src.locator else ""
                blocks.append(f"[{i}] {src.title}{loc}\n{text}")
            out.append({"role": "system", "content": SYSTEM_GROUNDED})
            out.append(
                {
                    "role": "system",
                    "content": "Auszüge aus den Kursmaterialien:\n\n"
                    + "\n\n".join(blocks),
                }
            )
        out.extend({"role": m.role, "content": m.content} for m in messages)
        return out

    def complete(
        self, messages: list[Message], context=None, grounded: bool = False
    ) -> tuple[str, dict[str, int]]:
        if not self.configured:
            raise RuntimeError(
                "Chat provider not configured: set OPENAI_BASE_URL and MODEL "
                "(see .env.example)"
            )

        body = json.dumps(
            {
                "model": self.model,
                "messages": self._payload(messages, context, grounded),
                "temperature": 0.2,
                "max_tokens": self.max_tokens,
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(f"upstream {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"cannot reach {self.base_url}: {e.reason}") from e

        try:
            choice = data["choices"][0]
            answer = choice["message"].get("content")
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"unexpected response shape: {str(data)[:300]}") from e

        # Reasoning models put their chain of thought in `reasoning_content` and
        # leave `content` null until they have finished thinking. Both models on
        # the Osnabrück gateway do this — Qwen3.5 spent 209 of 218 completion
        # tokens reasoning before answering "Hannover". So a null `content` with
        # finish_reason "length" is not a broken model, it is a token budget too
        # small to reach the answer, and saying so beats returning None.
        if not answer:
            if choice.get("finish_reason") == "length":
                raise RuntimeError(
                    "the model used its whole token budget on internal reasoning "
                    "and never produced an answer — raise CHAT_MAX_TOKENS "
                    f"(currently {self.max_tokens})"
                )
            raise RuntimeError(
                f"the model returned no content: {str(choice)[:200]}"
            )

        u = data.get("usage") or {}
        usage = {
            "prompt_tokens": int(u.get("prompt_tokens", 0)),
            "completion_tokens": int(u.get("completion_tokens", 0)),
        }
        return answer, usage


class EchoChat(ChatProvider):
    """Offline stand-in so the demo and tests run with no gateway reachable.

    It does not call a model. It states plainly what it received, which makes it
    useful for showing an adapter works without needing credentials.
    """

    name = "echo (offline)"

    def complete(
        self, messages: list[Message], context=None, grounded: bool = False
    ) -> tuple[str, dict[str, int]]:
        question = messages[-1].content if messages else ""
        if context:
            cites = " ".join(f"[{i}]" for i in range(1, len(context) + 1))
            answer = (
                f'(Offline-Modus, kein Modell aufgerufen.) Zur Frage \u201e{question}\u201c '
                f"wurden {len(context)} Passagen aus den Kursmaterialien gefunden: "
                f"{cites}. Mit konfiguriertem OPENAI_BASE_URL würde hier die "
                f"Antwort des Modells stehen."
            )
        else:
            answer = (
                f'(Offline-Modus, kein Modell aufgerufen.) Frage empfangen: '
                f'\u201e{question}\u201c. Kein Kursbezug angegeben, daher keine Recherche '
                f'in Kursmaterialien.'
            )
        return answer, {"prompt_tokens": 0, "completion_tokens": 0}
