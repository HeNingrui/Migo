"""The model-backed parser.

Same contract as the deterministic one, same return type, same validation. What
differs is tolerance: this one understands phrasing the vocabulary tables do not
list, which is the whole reason to call a model at all.

Three rules, all of them about not letting a model's confidence become the
system's:

**1. The schema is generated from the contract.** ``build_response_schema()``
derives it from ``IntentResult``, so the model is asked for exactly what Pydantic
will accept. A hand-written schema drifts the first time a field is renamed.

**2. One repair attempt, then give up.** A reply that fails validation is
resent once with the specific violations appended -- a model told *which* rule
it broke usually fixes it, and a model told only "invalid" does not. A second
failure raises, and the orchestrator falls back to the deterministic parser.
The attempt is recorded as ``ParseSource.REPAIRED`` rather than hidden.

**3. A transport failure is not a parse failure.** Unreachable, unauthorised and
rate-limited raise ``LLM_UNAVAILABLE``; a reply that arrived but was wrong raises
``LLM_PARSE_FAILED``. Both fall back, but only the second is worth repairing, and
collapsing them would make the repair attempt fire on a network error.
"""

from __future__ import annotations

from app.agent.intent_schema import build_llm_request, parse_reply
from app.agent.llm_client import (
    LLMCallError,
    OpenAICompatibleClient,
    ProviderConfig,
)
from app.contracts.agent import (
    IntentResult,
    LLMClient,
    LLMRequest,
    ParseSource,
    SessionContext,
)
from app.errors import llm_parse_failed, llm_unavailable

#: One repair. More attempts buy tolerance the fallback parser already provides.
MAX_ATTEMPTS = 2


class LLMIntentParser:
    """``IntentParser`` backed by an OpenAI-compatible model."""

    source = ParseSource.LLM

    def __init__(
        self,
        client: LLMClient | None = None,
        *,
        config: ProviderConfig | None = None,
        env_file: str | None = None,
        max_output_tokens: int = 2048,
    ) -> None:
        if client is None:
            client = OpenAICompatibleClient(config or ProviderConfig.from_env(env_file))
        self._client = client
        self._max_output_tokens = max_output_tokens

    def parse(self, text: str, context: SessionContext) -> IntentResult:
        request = build_llm_request(
            text, context=context, max_output_tokens=self._max_output_tokens
        )
        reply = self._complete(request)

        outcome = parse_reply(reply, raw_text=text)
        if outcome.schema_valid and not outcome.invented_values:
            result = outcome.result
            assert result is not None  # schema_valid implies it
            if outcome.problems:
                # Valid but flagged -- currently only an untraceable value, which
                # is decided by the caller. Reported, not silently accepted.
                return result.model_copy(update={"parser_note": "; ".join(outcome.problems)})
            return result

        # -- one repair -----------------------------------------------------
        repair = self._repair_request(request, outcome.problems)
        second = self._complete(repair)
        retried = parse_reply(second, raw_text=text)
        if retried.schema_valid and not retried.invented_values:
            result = retried.result
            assert result is not None
            return result.model_copy(update={
                "parse_source": ParseSource.REPAIRED,
                "parser_note": "the first reply was rejected; a second attempt passed",
            })

        raise llm_parse_failed(
            [*outcome.problems, *retried.problems],
            raw_reply=second or reply,
        )

    # -- internals ----------------------------------------------------------

    def _complete(self, request: LLMRequest) -> str:
        """Call the model, mapping transport failures to their own error."""
        try:
            return self._client.complete(request).text
        except LLMCallError as exc:
            raise llm_unavailable(str(exc), model=getattr(
                getattr(self._client, "config", None), "model", None
            )) from exc

    @staticmethod
    def _repair_request(request: LLMRequest, problems: list[str]) -> LLMRequest:
        """Resend with the violations named.

        The problems are appended verbatim: "intent SEARCH must carry search
        criteria" is actionable, and "your reply was invalid" is not.
        """
        listed = "\n".join(f"- {problem}" for problem in problems[:12])
        return request.model_copy(update={
            "system_prompt": (
                request.system_prompt
                + "\n\n## Your previous reply was rejected\n\n"
                + listed
                + "\n\nReturn a corrected JSON object. Change nothing else."
            ),
            "temperature": 0.0,
        })


def build_parser_from_env(
    *, env_file: str | None = None, json_mode: bool = True
) -> LLMIntentParser | None:
    """Best-effort construction for the app entry point.

    Returns ``None`` rather than raising when no key is configured: an
    unconfigured model is a normal state -- the full flow is specified to work
    offline -- and the caller wires up the deterministic parser instead.
    """
    try:
        config = ProviderConfig.from_env(env_file, json_mode=json_mode)
    except LLMCallError:
        return None
    return LLMIntentParser(OpenAICompatibleClient(config))


__all__ = ["LLMIntentParser", "MAX_ATTEMPTS", "build_parser_from_env"]
