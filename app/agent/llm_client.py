"""An OpenAI-compatible LLM client using only the standard library.

Why this exists before the orchestrator: the whole agent design rests on one
unverified assumption -- that a model returns JSON matching
:class:`~app.contracts.agent.IntentResult`. This client is the smallest thing
that can test it against a real provider.

Design constraints taken from the repo, not invented here:

* **No new dependency.** ``requirements.lock.txt`` is owned by A and pins five
  packages; none of them is an HTTP client. ``urllib.request`` is enough and
  keeps the lock file honest.
* **Implements the existing Protocol.** ``app.contracts.agent.LLMClient``
  already declares ``complete(request) -> LLMResponse``. This class adds no new
  boundary; a provider is a configuration detail.
* **Provider-neutral.** Any OpenAI-compatible ``/chat/completions`` endpoint
  works: DeepSeek, OpenAI, Qwen, Moonshot, OpenRouter, a local server.

The client never retries and never repairs. A malformed reply is returned as
text and fails validation in the caller, which is where a repair attempt would
be recorded as ``ParseSource.REPAIRED`` rather than hidden.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.contracts.agent import LLMRequest, LLMResponse

#: Used when neither the environment nor the config file names one.
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"

#: The repository root, so a relative ``.env`` resolves the same from a script,
#: a test and a route handler.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Provider error codes the caller may surface as ``ErrorCode.LLM_UNAVAILABLE``.
TRANSPORT_ERROR = "LLM_TRANSPORT_ERROR"
TIMEOUT_ERROR = "LLM_TIMEOUT"
EMPTY_REPLY = "LLM_EMPTY_REPLY"
BAD_JSON = "LLM_BAD_JSON"


class LLMCallError(RuntimeError):
    """A call that never produced usable text.

    Carries a ``code`` from the module constants so a caller can map it to a
    structured error without parsing the message.
    """

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class ProviderConfig:
    """Where to send the request and what to call the model.

    ``base_url`` is the API root, not the full endpoint: ``/chat/completions``
    is appended. Both spellings are accepted so a pasted URL with the path
    already on it does not silently produce ``/chat/completions/chat/completions``.
    """

    base_url: str
    model: str
    api_key: str
    timeout_seconds: float = 60.0

    #: DeepSeek and OpenAI both accept ``{"type": "json_object"}``. Turning it
    #: off is how you measure whether the prompt alone is sufficient.
    json_mode: bool = True

    #: Where each value came from. The values themselves are never logged.
    sources: dict[str, str] = field(default_factory=dict)
    thinking_mode: str | None = None

    def endpoint(self) -> str:
        root = self.base_url.rstrip("/")
        if root.endswith("/chat/completions"):
            return root
        return root + "/chat/completions"

    def redacted(self) -> dict[str, Any]:
        """A description safe to log or print."""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "endpoint": self.endpoint(),
            "api_key": f"<{len(self.api_key)} chars>" if self.api_key else "<empty>",
            "json_mode": self.json_mode,
            "timeout_seconds": self.timeout_seconds,
        }

    # -- construction -------------------------------------------------------

    @classmethod
    def from_env(
        cls, env_file: Path | str | None = None, *, json_mode: bool = True
    ) -> "ProviderConfig":
        """Resolve configuration: environment first, then the config file.

        Both layers, not one. A ``.env`` that supplied only the key would
        otherwise leave ``LLM_MODEL`` and ``LLM_BASE_URL`` silently ignored,
        which makes "write .env and run" untrue.

        Raises :class:`LLMCallError` when no key is present. A missing key is a
        configuration failure rather than an empty reply, and the caller decides
        whether that means falling back to the deterministic parser.
        """
        path = Path(env_file) if env_file is not None else PROJECT_ROOT / ".env"
        from_file = read_env_file(path)
        label = path.name if path.parent == PROJECT_ROOT else str(path)

        def pick(name: str) -> tuple[str | None, str]:
            value = os.environ.get(name)
            if value:
                return value, "environment"
            value = from_file.get(name)
            if value:
                return value, label
            return None, "unset"

        chosen: dict[str, str | None] = {}
        sources: dict[str, str] = {}
        for name, key in (
            ("LLM_API_KEY", "api_key"),
            ("LLM_BASE_URL", "base_url"),
            ("LLM_MODEL", "model"),
            ("LLM_TIMEOUT_SECONDS", "timeout_seconds"),
        ):
            chosen[key], sources[key] = pick(name)

        if not chosen["api_key"]:
            raise LLMCallError(
                TRANSPORT_ERROR,
                f"no LLM_API_KEY in the environment or in {path}",
            )

        try:
            timeout = float(chosen["timeout_seconds"] or 60.0)
        except (TypeError, ValueError):
            timeout = 60.0
            sources["timeout_seconds"] = "invalid, using 60"

        return cls(
            base_url=chosen["base_url"] or DEFAULT_BASE_URL,
            model=chosen["model"] or DEFAULT_MODEL,
            api_key=str(chosen["api_key"]),
            timeout_seconds=timeout,
            json_mode=json_mode,
            sources=sources,
            thinking_mode=pick("LLM_THINKING")[0],
        )


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal ``KEY=value`` reader: no interpolation, no multi-line, no YAML.

    Deliberately not a dotenv dependency. The file is ours and the format is
    fixed; a parser that understands more than the format will eventually be
    handed something it understands wrongly.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip("'\"")
    return values


class OpenAICompatibleClient:
    """``LLMClient`` over ``POST {base_url}/chat/completions``."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._successful_calls = 0
        self._last_model = None

    def public_status(self) -> dict[str, Any]:
        """Successful provider responses, without credentials or provider errors."""
        with self._lock:
            return {"configured": True, "configured_model": self.config.model,
                    "last_model": self._last_model,
                    "successful_calls": self._successful_calls}

    # -- LLMClient Protocol -------------------------------------------------

    def complete(self, request: LLMRequest) -> LLMResponse:
        payload = self._payload(request)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        http_request = urllib.request.Request(
            self.config.endpoint(),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
                "Accept": "application/json",
            },
        )

        timeout = min(request.timeout_seconds, self.config.timeout_seconds)
        try:
            with urllib.request.urlopen(http_request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").replace(self.config.api_key, "<redacted>")[:800]
            raise LLMCallError(
                TRANSPORT_ERROR,
                f"HTTP {exc.code} from {self.config.endpoint()}: {detail}",
                status=exc.code,
            ) from exc
        except TimeoutError as exc:
            raise LLMCallError(TIMEOUT_ERROR, f"timed out after {timeout}s") from exc
        except urllib.error.URLError as exc:
            raise LLMCallError(
                TRANSPORT_ERROR, f"cannot reach {self.config.endpoint()}: {exc.reason}"
            ) from exc
        except OSError as exc:
            raise LLMCallError(TRANSPORT_ERROR, "provider connection was interrupted") from exc

        result = self._parse_envelope(raw)
        with self._lock:
            self._successful_calls += 1
            self._last_model = result.model
        return result

    # -- internals ----------------------------------------------------------

    def _payload(self, request: LLMRequest) -> dict[str, Any]:
        system = request.system_prompt
        if self.config.json_mode and "json" not in system.lower():
            # OpenAI and DeepSeek both reject json_object mode unless the word
            # appears in the prompt. Appending beats failing at the provider.
            system = system + "\n\nReply with a single JSON object."

        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": request.user_text},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }
        if self.config.json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.config.thinking_mode in {"enabled", "disabled"}:
            payload["thinking"] = {"type": self.config.thinking_mode}
        return payload

    def _parse_envelope(self, raw: str) -> LLMResponse:
        try:
            envelope = json.loads(raw)
        except ValueError as exc:
            raise LLMCallError(BAD_JSON, f"provider envelope is not JSON: {exc}") from exc

        if isinstance(envelope.get("error"), dict):
            message = str(envelope["error"].get("message", "unspecified provider error")).replace(self.config.api_key, "<redacted>")
            raise LLMCallError(TRANSPORT_ERROR, f"provider error: {message}")

        choices = envelope.get("choices") or []
        if not choices:
            raise LLMCallError(EMPTY_REPLY, "no choices in provider reply")

        message = choices[0].get("message") or {}
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            # A reasoning model puts its scratchpad in reasoning_content and can
            # still return empty content; that is a failure, not a blank answer.
            raise LLMCallError(
                EMPTY_REPLY,
                f"empty content (finish_reason={choices[0].get('finish_reason')!r})",
            )

        usage = envelope.get("usage") or {}
        return LLMResponse(
            text=text,
            model=envelope.get("model") or self.config.model,
            usage={k: v for k, v in usage.items() if isinstance(v, int)},
        )


def strip_code_fence(text: str) -> str:
    """Remove a ```json ... ``` wrapper if the model added one anyway.

    Recorded rather than silently forgiven: the caller notes that a fence was
    present, because a provider that needs this is a provider whose output
    cannot be fed straight into ``json.loads``.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) >= 2 and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


__all__ = [
    "BAD_JSON",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "EMPTY_REPLY",
    "LLMCallError",
    "OpenAICompatibleClient",
    "PROJECT_ROOT",
    "ProviderConfig",
    "TIMEOUT_ERROR",
    "TRANSPORT_ERROR",
    "read_env_file",
    "strip_code_fence",
]
