"""Turn the contracts into a response schema and a prompt, then check the reply.

This is the piece that answers one question: *given a user's sentence, does a
model return the exact shape the contracts demand?*

Three decisions are load-bearing.

**1. The schema is generated from the contract, not hand-written.** A copied
schema drifts the first time a field is renamed. ``IntentResult.model_json_schema()``
cannot drift, so the model is asked for exactly what Pydantic will accept.

**2. Fields the client owns are removed from ``required``.** The model is not
asked to echo ``raw_text`` or to declare ``parse_source`` -- the caller knows
both for certain, and a model's echo of its own input is not evidence. They are
overwritten after the reply arrives.

**3. Anything the contract does not declare is rejected, so nothing is stripped
defensively here.** ``IntentResult`` sets ``extra="forbid"``, which means a reply
carrying a field the contract never defined fails validation. Earlier this module
also stripped ``TRACEABLE_FIELDS`` by hand, because it was a Pydantic field
rather than a ``ClassVar`` and a reply could set it to ``[]`` to switch off the
invented-budget check. That is fixed in ``app/contracts/agent.py``, so the
workaround is gone; a reply offering it now fails as an unknown field.
``tests/agent/test_intent_schema.py::TestTraceabilityCannotBeDisabled`` pins both
halves. Measurements: ``docs/LLM_FORMAT_PROBE.md``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.contracts.agent import IntentResult, LLMRequest, SessionContext

#: Supplied by the caller, never taken from the model.
CLIENT_OWNED_FIELDS: tuple[str, ...] = ("raw_text", "parse_source")


def traceable_fields() -> tuple[str, ...]:
    """The fields whose value must be quotable from the user's own words.

    Read from the contract so a change there cannot desynchronise the prompt.
    """
    return tuple(IntentResult.TRACEABLE_FIELDS)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def build_response_schema() -> dict[str, Any]:
    """The JSON schema the reply must satisfy, as sent to the model."""
    schema = IntentResult.model_json_schema()

    properties = schema.get("properties", {})
    for name in CLIENT_OWNED_FIELDS:
        properties.pop(name, None)

    if "required" in schema:
        schema["required"] = [
            name for name in schema["required"] if name not in CLIENT_OWNED_FIELDS
        ]
    return schema


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """\
You convert one message from a user into a single JSON object.

You extract. You never decide. You never approve, price, reserve or pay.

Reply with one JSON object and nothing else: no prose, no explanation, no
markdown fence, no trailing commentary.

## Exactly one of two shapes

The intent selects the shape, and the wrong pairing is rejected outright:

* The user wants products found, compared or filtered
  -> intent "SEARCH" or "UPDATE_SEARCH", and "search" MUST be present.
* The user asks a **question about the products already shown** -- "are there
  other colours?", "is there anything lighter?" -- -> intent
  "ASK_ALTERNATIVES" with an **empty** ``search`` object. This is a question to
  answer, not an instruction to act on, and the caller answers it from the
  products it already has. Do not use UNKNOWN for these, and do not treat them
  as a new search: the criteria have not changed.
* The user grants, edits or activates spending authority
  -> intent "CREATE_MANDATE", "UPDATE_MANDATE_DRAFT" or "ACTIVATE_MANDATE",
     and "mandate" MUST be present.
* Anything else -> intent "UNKNOWN" with a non-empty "ambiguities" array.

"search" on a non-search intent is rejected. "mandate" on a non-mandate intent
is rejected. A SEARCH intent with no "search" is rejected.

**Before choosing UNKNOWN, check the conversation below.** A turn that adds no
new requirement is not necessarily unrecognised:

* filters are already on file and the user says something vague -- "随便看看",
  "show me", "anything" -- that is ``UPDATE_SEARCH`` with an **empty** ``search``
  object. It means "carry on with what I already told you", and the caller will
  re-run the existing filters. Answering UNKNOWN here makes the agent ask for a
  budget it was given two turns ago, which reads as not listening.
* a message only makes sense against what was shown -- "the second one",
  "cheaper" -- resolve it against the products listed below.
* only use UNKNOWN when there is genuinely nothing to act on: no filters on file
  and no requirement in the message.

**A shopping request with nothing specified is SEARCH, not UNKNOWN.** "我想买个
耳机" / "I need some headphones" states a want and no requirement. Return
``SEARCH`` with an **empty** ``search`` object: the caller shows a starting point
and then asks. UNKNOWN here makes the agent ask "what are you looking for?" of
someone who just said it, which is worse than showing them anything.

**A non-committal turn is UNKNOWN.** "随便看看", "嗯", "hello" add nothing and ask
nothing. Those get a question back, not a product list.

## Extended catalog filters

Use the current schema's color, required_device, tags and
max_estimated_delivery_days fields when explicitly requested. Chinese colors map
to the English enums (白色 white, 黑色 black). Use cases commute, study, music,
calls, sports and gaming must be selected from the user's words. Source spans
must quote exact substrings of the current message for traceable values.
Questions about reliability, review hype, comfort or the differences between the
products already shown are ASK_ALTERNATIVES, with an empty search object. They
must not reset the user's budget or filters. The explanation layer will answer
from catalog facts and public review evidence.
For profile facts, wearing only accepts in_ear, over_ear or open_ear; do NOT infer
a preferred wearing style from discomfort. Omit that fact when not explicitly
stated. Profile preferences such as anc/weight accept preferred, avoided, high,
medium or low, not required/comfort. Use cases mentioned as background belong
in profile; tags are hard requirements only when explicitly demanded.

## Never invent a value -- this rule matters most

If the user did not state a value, that field is null. Not a typical value, not
a sensible default, not an inference from context. A missing budget becomes a
question; an invented budget becomes a purchase.

Leave the field null when:

* the user was vague -- "a decent pair", "not too expensive", "good sound"
* the value is implied but never stated -- "the usual amount", "same as last time"
* filling it would require you to assume a currency, a period or a quantity

In particular: a request to buy with no amount stated has NO cap. Do not supply
one. Record an "ambiguities" entry of kind "MISSING" for the field instead.

## Quote your evidence

For every field you set from this list, add an entry to "source_spans": the key
is the field name, the value is the EXACT substring of the user's message that
gave you the value.

{traceable_fields}

A value with no quote is treated as invented and the entire parse is discarded.
Copy the user's characters exactly; do not translate, tidy or paraphrase them.

## Units

Money is an integer count of Hong Kong cents, never a float and never a string.
  "HK$300" -> 30000    "under 300" -> max_price_cents 30000
  "HK$500 in 24 hours" -> rolling_cap_cents 50000

Durations are integer seconds.
  "7 days" -> 604800   "24 hours" -> 86400   "5 minutes" -> 300

A price cap the user states as a budget for one item is "max_price_cents" when
they are searching, and "cap_per_transaction_cents" when they are authorising
spending.

## Field names that are easy to confuse

* ``form_factor`` is the **shape**: ``in_ear`` (入耳/earbud), ``over_ear``
  (头戴/headband), ``open_ear`` (开放). It is **not** ``headphones``.
* ``category`` is always ``headphones`` and is not part of a constraint patch.
* ``connection`` is ``wired`` or ``wireless``.
* Mandate ``allowed_payment_routes`` contains internal route IDs, not card brand
  labels: Mastercard -> ``mastercard_demo``, Visa -> ``visa_demo``,
  FPS -> ``fps_demo``. Quote the user's original
  wording in source_spans. Never substitute another rail for an unknown one.
* ``qty``-style values are ``quantity``; a per-purchase limit is
  ``cap_per_transaction_cents`` in a mandate and ``max_price_cents`` in a search.

A value outside these sets causes the reply to be rejected.

**Set nothing the user did not say.** ``connection`` and ``form_factor`` are not
in the traceable list above, because they do not move money -- but filling one
in "because most headphones are in-ear" silently narrows what the user is shown,
and they never agreed to it. Use the conversation block below for values carried
over from earlier turns, and leave a field null when this turn did not mention
it. A wrong filter is invisible; a missing one costs a question.

## Unknown is not the same as satisfied

Set "anc_required": true only when the user demands noise cancelling. A wish
("ideally", "最好", "preferably") is not a demand. If you cannot tell, leave it
null -- an unknown specification never satisfies a requirement.

## Escalation and revocation

"ask me first above HK$280" -> escalate_above_cents 28000.
"I can revoke any time" is a property of the system, not a field: do not invent
a field for it.

## Two mistakes that get a reply rejected

**A reference carries exactly ONE handle.** "第二款" is ``rank: 2``. Do not also
fill in the ``product_id`` you worked out -- the resolver does that against what
the user actually saw, and a reply carrying both is rejected. The same applies to
``order_id`` and ``mandate_id``.

**A direction with no number is UNDERSPECIFIED, not MISSING.** "cheaper",
"lighter", "longer battery" state a direction the caller can turn into a value
from the results already on screen. Record them as::

    {{"kind": "UNDERSPECIFIED", "field": "max_price_cents",
     "detail": "...", "question": "..."}}

``MISSING`` means a value the caller *cannot* derive and must ask for. Choosing
it for "cheaper" makes the agent ask a question the results could have answered,
and makes the same sentence behave differently depending on which parser ran.

## Fields you must not produce

Do not output "raw_text" or "parse_source"; the caller owns them. Do not invent
fields that are not in the schema below -- an unrecognised field causes the whole
reply to be rejected.

## The conversation so far

A turn like "cheaper" or "the second one" carries no requirements of its own.
It is only meaningful against what was already said and already shown, so both
are given below. Read the new message against them.

{context}

## The schema your reply must satisfy

{schema}
"""

#: Shown in the context block when nothing has happened yet.
NO_CONTEXT = "  (this is the first turn; nothing has been established yet)"


def describe_context(context: SessionContext | None) -> str:
    """The session, as the model needs it -- and nothing more.

    Deliberately excludes money: no balance, no cap, no remaining budget. The
    parser resolves references and applies patches; it does not need to know
    what has been spent, and a model that is told a budget will eventually
    reason about it.
    """
    if context is None:
        return NO_CONTEXT

    lines: list[str] = []
    constraints = getattr(context, "current_constraints", None)
    if constraints is None:
        lines.append("  current filters: none")
    else:
        set_fields = [
            f"{name}={getattr(constraints, name)!r}"
            for name in constraints.explicitly_set_fields()
        ]
        if getattr(constraints, "anc_required", False):
            set_fields.append("anc_required=True")
        lines.append("  current filters: " + (", ".join(set_fields) or "none"))
        lines.append(
            "  (a refinement updates these; a restatement of the whole request "
            "replaces them)"
        )

    shown = list(getattr(context, "last_shown_product_ids", []) or [])
    if shown:
        lines.append(
            "  products last shown, in order, so ordinals resolve to them: "
            + ", ".join(f"{i}. {pid}" for i, pid in enumerate(shown, start=1))
        )
    else:
        lines.append("  no products have been shown yet")

    if getattr(context, "open_escalation_proposal_id", None):
        lines.append(
            f"  an escalation is open on proposal "
            f"{context.open_escalation_proposal_id}"
        )
    if getattr(context, "current_mandate_draft", None) is not None:
        lines.append("  a mandate draft is in progress")
    return "\n".join(lines)


def build_system_prompt(context: SessionContext | None = None) -> str:
    return PROMPT_TEMPLATE.format(
        traceable_fields="\n".join(f"  {name}" for name in traceable_fields()),
        context=describe_context(context),
        schema=json.dumps(build_response_schema(), indent=2, ensure_ascii=False),
    )


def build_llm_request(
    text: str,
    *,
    context: SessionContext | None = None,
    max_output_tokens: int = 2048,
    temperature: float = 0.0,
    timeout_seconds: float = 60.0,
) -> LLMRequest:
    """A provider-neutral extraction request for one user turn.

    ``context`` is what makes a second turn parseable. Without it "cheaper" has
    no referent and the model correctly -- but uselessly -- answers UNKNOWN.
    """
    return LLMRequest(
        system_prompt=build_system_prompt(context),
        user_text=text,
        response_schema=build_response_schema(),
        max_output_tokens=max_output_tokens,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Validating the reply
# ---------------------------------------------------------------------------

@dataclass
class ParseOutcome:
    """What came back, and every way it failed to be usable.

    ``problems`` is empty only when the reply validated. ``result`` may still be
    non-None with problems present -- a schema-valid reply that invented a
    number is exactly that case.
    """

    result: IntentResult | None = None
    problems: list[str] = field(default_factory=list)
    raw_reply: str = ""
    had_code_fence: bool = False

    @property
    def schema_valid(self) -> bool:
        return self.result is not None

    @property
    def invented_values(self) -> list[str]:
        """Traceable fields carrying a value but no quote from the user."""
        return self.result.untraceable_fields() if self.result else []

    @property
    def actionable(self) -> bool:
        return bool(self.result and self.result.is_actionable())

    def summary(self) -> str:
        if self.result is None:
            return "REJECTED: " + "; ".join(self.problems)
        tags = []
        if self.invented_values:
            tags.append("INVENTED:" + ",".join(self.invented_values))
        if not self.actionable and not tags:
            tags.append("NOT_ACTIONABLE")
        return f"{self.result.intent.value} " + (" ".join(tags) if tags else "ok")


def parse_reply(reply_text: str, *, raw_text: str) -> ParseOutcome:
    """Validate a model reply against the contract.

    Never repairs and never guesses. A failure is reported as a failure, because
    the point of the exercise is to find out how often that happens.
    """
    from app.agent.llm_client import strip_code_fence

    outcome = ParseOutcome(raw_reply=reply_text)
    stripped = strip_code_fence(reply_text)
    outcome.had_code_fence = stripped != reply_text.strip()

    if not stripped:
        outcome.problems.append("empty reply")
        return outcome

    try:
        data = json.loads(stripped)
    except ValueError as exc:
        outcome.problems.append(f"reply is not JSON: {exc}")
        return outcome

    if not isinstance(data, dict):
        outcome.problems.append(f"reply is {type(data).__name__}, not an object")
        return outcome

    # The caller knows these for certain; a model's echo is not evidence.
    data["raw_text"] = raw_text
    data["parse_source"] = "LLM"

    try:
        outcome.result = IntentResult.model_validate(data)
    except ValidationError as exc:
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "<root>"
            outcome.problems.append(f"{location}: {error['msg']}")
        return outcome

    invented = outcome.invented_values
    if invented:
        outcome.problems.append(
            "invented values with no quote from the user: " + ", ".join(invented)
        )
    return outcome


__all__ = [
    "CLIENT_OWNED_FIELDS",
    "NO_CONTEXT",
    "PROMPT_TEMPLATE",
    "ParseOutcome",
    "build_llm_request",
    "build_response_schema",
    "build_system_prompt",
    "describe_context",
    "parse_reply",
    "traceable_fields",
]
