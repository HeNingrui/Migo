"""Agent contracts: how a user's words become a structured, checkable proposal.

This module defines the whole natural-language structuring boundary. The design
rests on four decisions.

**1. One vocabulary.** ``Intent`` is the single source of truth for what a user
can ask for. ``UserActionType`` is an alias of it, not a parallel enum, because
a second copy of the same list drifts: the parser learns a value the action
dispatcher has never heard of, and nothing catches it until a demo.

**2. The parser proposes; it never authorises.** ``IntentResult`` can carry a
:class:`~app.contracts.mandate.MandateDraft` or search criteria. It can never
carry an approval, an amount, a policy hash or a payment state. Those are C's.

**3. Everything is traceable or it is not trusted.** ``source_spans`` maps each
populated field to the slice of the user's own words that produced it.
:meth:`IntentResult.untraceable_fields` reports the fields that have none, and a
caller must refuse to act on a draft that fails that check. This is what stops a
model from quietly inventing a budget.

**4. Parsers are interchangeable.** ``FallbackIntentParser`` is deterministic and
needs no network, so the full flow works offline. ``LLMIntentParser`` adds
tolerance for unexpected phrasing. Both return the same type and pass the same
validation, so which one ran never changes what the system will accept.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .common import AgentPhase, NextAction
from .commerce import Quote, Reservation, SpendState
from .mandate import Mandate, MandateDraft
from .policy import (
    DenialReceipt,
    EscalationRequest,
    PaymentFailure,
    PaymentReceipt,
    PolicyDecision,
)
from .product import Connection, FormFactor, HardConstraints
from .profile import UserProfile
from .search import SearchResponse

# ---------------------------------------------------------------------------
# Intent vocabulary -- the single source of truth
# ---------------------------------------------------------------------------

class Intent(str, Enum):
    """What the user is asking for.

    Grouped by the subsystem a value reaches. Anything not recognised is
    ``UNKNOWN``, which the orchestrator answers with a clarification rather than
    a guess.
    """

    # -- browsing: reaches B ------------------------------------------------
    SEARCH = "SEARCH"
    UPDATE_SEARCH = "UPDATE_SEARCH"

    #: A question *about the products already shown* -- "are there other
    #: colours?", "is there anything lighter?". Distinct from UPDATE_SEARCH
    #: because it is a question rather than an instruction: the user is not
    #: changing the criteria, and answering it must not look like being asked
    #: what they want. It reaches B like a search does.
    ASK_ALTERNATIVES = "ASK_ALTERNATIVES"

    # -- authorisation: reaches C's mandate registry ------------------------
    CREATE_MANDATE = "CREATE_MANDATE"
    UPDATE_MANDATE_DRAFT = "UPDATE_MANDATE_DRAFT"
    ACTIVATE_MANDATE = "ACTIVATE_MANDATE"
    REVOKE_MANDATE = "REVOKE_MANDATE"

    # -- delegated execution: reaches C's authority service -----------------
    RUN_DELEGATED_PURCHASE = "RUN_DELEGATED_PURCHASE"
    APPROVE_ESCALATION = "APPROVE_ESCALATION"
    REJECT_ESCALATION = "REJECT_ESCALATION"

    # -- compatibility: the manual, confirm-every-purchase flow -------------
    SELECT_PRODUCT = "SELECT_PRODUCT"
    CONFIRM_MANUAL_PAYMENT = "CONFIRM_MANUAL_PAYMENT"
    CANCEL_ORDER = "CANCEL_ORDER"
    CANCEL_SELECTION = "CANCEL_SELECTION"

    #: The user is not happy with what was recommended. Kept apart from
    #: ``UPDATE_SEARCH`` on purpose: a rejection is not a new criterion. It says
    #: something about the *user* ("too heavy for me", "I care more about
    #: battery"), and the answer is to re-read the requirement and update the
    #: profile before asking B again -- not to hand B a re-rank request it has no
    #: basis for. See ``app/agent/profile_flow.py``.
    REJECT_RECOMMENDATION = "REJECT_RECOMMENDATION"

    CHECK_STATUS = "CHECK_STATUS"
    UNKNOWN = "UNKNOWN"


#: ``UserActionType`` is the same vocabulary. Declared as an alias here so there
#: is exactly one definition; a second enum would drift.
UserActionType = Intent


#: Intents that may carry search criteria.
SEARCH_INTENTS: frozenset[Intent] = frozenset({
    Intent.SEARCH, Intent.UPDATE_SEARCH, Intent.ASK_ALTERNATIVES,
})

#: Intents that may carry a mandate draft.
MANDATE_INTENTS: frozenset[Intent] = frozenset({
    Intent.CREATE_MANDATE,
    Intent.UPDATE_MANDATE_DRAFT,
    Intent.ACTIVATE_MANDATE,
})


class ParseSource(str, Enum):
    """Which parser produced a result.

    Recorded so that a response can be explained, and so that a fallback that
    silently took over is visible rather than invisible.
    """

    LLM = "LLM"
    FALLBACK = "FALLBACK"
    REPAIRED = "REPAIRED"      # an LLM result corrected once against the schema
    UNRESOLVED = "UNRESOLVED"  # nothing could parse it


# ---------------------------------------------------------------------------
# Ambiguity and clarification
# ---------------------------------------------------------------------------

class AmbiguityKind(str, Enum):
    """Why a parse cannot be acted on yet.

    The kinds are separate because they need different questions. Asking "what
    is your budget?" when the user gave two budgets is the wrong question.
    """

    MISSING = "MISSING"                  # a required value was never given
    CONTRADICTORY = "CONTRADICTORY"      # two given values cannot both hold
    UNDERSPECIFIED = "UNDERSPECIFIED"    # a soft threshold with no hard bound
    UNSUPPORTED = "UNSUPPORTED"          # asked for something we cannot evaluate


class Ambiguity(BaseModel):
    """One unresolved point, with the question that would resolve it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: AmbiguityKind
    field: str
    detail: str = Field(min_length=1)
    question: str = Field(min_length=1)

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"[{self.kind.value}] {self.field}: {self.detail}"


class Clarification(BaseModel):
    """The single question to put to the user, plus what is still open.

    One question at a time. A turn that asks three things gets no useful answer.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str = Field(min_length=1)
    about: list[str] = Field(default_factory=list)
    blocking: bool = True

    @classmethod
    def from_ambiguities(cls, ambiguities: list[Ambiguity]) -> "Clarification | None":
        """Pick the question to ask, highest severity first.

        A contradiction beats a missing value: answering "what is your budget?"
        while two budgets are on the table produces a third.
        """
        if not ambiguities:
            return None
        order = {
            AmbiguityKind.CONTRADICTORY: 0,
            AmbiguityKind.MISSING: 1,
            AmbiguityKind.UNDERSPECIFIED: 2,
            AmbiguityKind.UNSUPPORTED: 3,
        }
        ranked = sorted(ambiguities, key=lambda a: (order[a.kind], a.field))
        chosen = ranked[0]
        return cls(
            question=chosen.question,
            about=[a.field for a in ranked],
            blocking=chosen.kind in (AmbiguityKind.CONTRADICTORY, AmbiguityKind.MISSING),
        )


# ---------------------------------------------------------------------------
# Reference resolution
# ---------------------------------------------------------------------------

class TargetReference(BaseModel):
    """A reference to something already shown, resolved against the session.

    References are resolved against the last product list this session showed,
    never against a fresh search, so "the second one" means what the user saw.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(description="what is being referenced, e.g. 'product'")
    rank: int | None = Field(default=None, ge=1, description="1-based position as shown")
    product_id: str | None = None
    order_id: str | None = None
    mandate_id: str | None = None

    @model_validator(mode="after")
    def _exactly_one_handle(self) -> "TargetReference":
        handles = [self.rank, self.product_id, self.order_id, self.mandate_id]
        if sum(h is not None for h in handles) != 1:
            raise ValueError("a reference must carry exactly one of rank, product_id, order_id, mandate_id")
        return self


# ---------------------------------------------------------------------------
# Patches -- what this turn changed, never the whole state
# ---------------------------------------------------------------------------

class ConstraintPatch(BaseModel):
    """An update to the current search criteria.

    Only fields the user actually mentioned are set. A ``clear_*`` flag is
    separate from a ``None`` value on purpose: "do not care about battery"
    and "you have not told me about battery" are different states, and treating
    them as one is how a relaxed constraint silently becomes a required one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: ``StrictInt``, matching every other money field in the repository. In lax
    #: mode Pydantic coerces, so ``300.0`` became 300, ``"30000"`` became 30000
    #: and ``True`` became 1 -- a reply of ``true`` silently meaning HK$0.01.
    #: This is the boundary a language model writes to, which is exactly where a
    #: silent coercion must not happen.
    max_price_cents: StrictInt | None = Field(default=None, ge=0)
    min_price_cents: StrictInt | None = Field(default=None, ge=0)

    #: Narrowed to the real enum rather than ``str``. A model asked for
    #: ``form_factor`` will sometimes answer ``"headphones"`` -- which is the
    #: *category*, not a shape -- and with ``str`` that mistake survived
    #: validation and only failed later, when the patch was merged. Here it is
    #: rejected where it happens, so the repair attempt can name the field.
    connection: Connection | None = None
    form_factor: FormFactor | None = None
    anc_required: bool | None = None
    min_battery_hours: float | None = Field(default=None, ge=0)
    max_wearing_weight_g: float | None = Field(default=None, gt=0)
    brand_allowlist: list[str] | None = None
    in_stock_only: bool | None = None

    color: str | None = None
    required_device: str | None = None
    tags: list[str] | None = None
    max_estimated_delivery_days: StrictInt | None = Field(default=None, ge=0)
    clear_color: bool = False
    clear_required_device: bool = False
    clear_tags: bool = False
    clear_max_estimated_delivery_days: bool = False

    clear_connection: bool = False
    clear_form_factor: bool = False
    clear_anc_required: bool = False
    clear_min_battery_hours: bool = False
    clear_max_wearing_weight_g: bool = False
    clear_brand_allowlist: bool = False

    def apply_to(self, base: HardConstraints) -> HardConstraints:
        """Merge onto existing criteria. Deterministic, no model involved."""
        data = base.model_dump(mode="python")
        for name in ("max_price_cents", "min_price_cents", "connection", "form_factor",
                     "anc_required", "min_battery_hours", "max_wearing_weight_g",
                     "brand_allowlist", "in_stock_only", "color", "required_device", "tags", "max_estimated_delivery_days"):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        for name in ("connection", "form_factor", "anc_required", "min_battery_hours",
                     "max_wearing_weight_g", "brand_allowlist", "color", "required_device", "tags", "max_estimated_delivery_days"):
            if getattr(self, f"clear_{name}"):
                data[name] = False if name == "anc_required" else (
                    [] if name in {"brand_allowlist", "tags"} else None
                )
        return HardConstraints.model_validate(data)

    def is_empty(self) -> bool:
        value_fields = ("max_price_cents", "min_price_cents", "connection", "form_factor",
                        "anc_required", "min_battery_hours", "max_wearing_weight_g",
                        "brand_allowlist", "in_stock_only", "color", "required_device", "tags", "max_estimated_delivery_days")
        clear_fields = ("connection", "form_factor", "anc_required", "min_battery_hours",
                        "max_wearing_weight_g", "brand_allowlist", "color", "required_device", "tags", "max_estimated_delivery_days")
        return (
            not any(getattr(self, name) is not None for name in value_fields)
            and not any(getattr(self, f"clear_{name}") for name in clear_fields)
        )


class NonNegativeIntPatch(BaseModel):
    """An integer-valued update. ``None`` means the user did not mention it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: StrictInt = Field(ge=0)


# ---------------------------------------------------------------------------
# The parse result
# ---------------------------------------------------------------------------

class IntentResult(BaseModel):
    """A parser's reading of one user turn.

    This is a *proposal*. Nothing here is authoritative: no amount, no approval,
    no policy hash, no payment state. Passing it downstream cannot move money,
    because C re-derives every one of those from its own records.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: Intent
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)

    raw_text: str = Field(min_length=1)
    parse_source: ParseSource = ParseSource.FALLBACK
    parser_note: str | None = None

    # -- what this turn produces, if anything ------------------------------
    search: ConstraintPatch | None = None
    #: True when the user wants a new search rather than an edit of the last one.
    search_is_new: bool = False
    mandate: MandateDraft | None = None

    #: What this turn said about the user, as opposed to about a product.
    #:
    #: Kept apart from ``search`` because the two answer different questions and
    #: have different power: a search patch can eliminate products, a profile
    #: fact cannot. Nothing here becomes a hard constraint -- see
    #: :mod:`app.contracts.profile` for the type-level reason.
    profile: UserProfile | None = None

    # -- what this turn points at ------------------------------------------
    target: TargetReference | None = None

    # -- what is still open -------------------------------------------------
    ambiguities: list[Ambiguity] = Field(default_factory=list)

    #: field name -> the slice of ``raw_text`` that produced it.
    source_spans: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _intent_matches_payload(self) -> "IntentResult":
        if self.search is not None and self.intent not in SEARCH_INTENTS:
            raise ValueError(f"intent {self.intent.value} must not carry search criteria")
        if self.mandate is not None and self.intent not in MANDATE_INTENTS:
            raise ValueError(f"intent {self.intent.value} must not carry a mandate draft")
        if self.intent in SEARCH_INTENTS and self.search is None:
            raise ValueError(f"intent {self.intent.value} must carry search criteria")
        if self.intent == Intent.UNKNOWN and not self.ambiguities:
            raise ValueError("an unrecognised turn must say what is unclear")
        # A rejection carries no criteria: the whole point of the intent is that
        # the user has not said what to look for instead. A parse that carried
        # both would be a refinement wearing a rejection's name, and the
        # re-analysis branch would skip the requirement it was asked to re-read.
        if self.intent == Intent.REJECT_RECOMMENDATION and self.search is not None:
            raise ValueError(
                "REJECT_RECOMMENDATION must not carry search criteria; "
                "state the new requirement instead"
            )
        return self

    # -- traceability -------------------------------------------------------

    #: Fields whose value must be traceable to the user's own words. These are
    #: the values that decide how much money may move, so an untraceable one is
    #: treated as invented.
    #:
    #: ``ClassVar`` is load-bearing, not decoration. As a plain annotated
    #: attribute Pydantic treats this as a *field*: it appears in the generated
    #: JSON schema, a reply may supply it, and :meth:`untraceable_fields` reads
    #: it -- so a model could send ``[]`` and switch off the check that exists
    #: to catch an invented budget. It also makes
    #: ``IntentResult.TRACEABLE_FIELDS`` raise ``AttributeError`` at class level,
    #: because Pydantic removes field defaults from the class namespace.
    TRACEABLE_FIELDS: ClassVar[tuple[str, ...]] = (
        "max_price_cents", "min_price_cents", "anc_required",
        "cap_per_transaction_cents", "rolling_cap_cents", "rolling_window_seconds",
        "velocity_max_count", "velocity_window_seconds", "valid_for_seconds",
        "escalate_above_cents", "max_quantity_total",
    )

    def populated_traceable_fields(self) -> list[str]:
        """Which traceable fields this turn actually set."""
        found: list[str] = []
        if self.search is not None:
            payload = self.search.model_dump(mode="python")
            found += [f for f in self.TRACEABLE_FIELDS
                      if f in payload and payload[f] is not None]
        if self.mandate is not None:
            payload = self.mandate.model_dump(mode="python")
            found += [f for f in self.TRACEABLE_FIELDS
                      if f in payload and payload[f] is not None]
        return sorted(set(found))

    def untraceable_fields(self) -> list[str]:
        """Traceable fields that carry a value but no supporting quote.

        A non-empty result means the caller must not act on this parse. It is
        the check that catches a model inventing a budget.
        """
        return [f for f in self.populated_traceable_fields() if not self.source_spans.get(f)]

    def is_actionable(self) -> bool:
        """Whether this turn can be acted on without asking the user again."""
        if self.intent == Intent.UNKNOWN:
            return False
        if any(a.kind in (AmbiguityKind.CONTRADICTORY, AmbiguityKind.MISSING)
               for a in self.ambiguities):
            return False
        return not self.untraceable_fields()

    def blocking_ambiguities(self) -> list[Ambiguity]:
        return [a for a in self.ambiguities
                if a.kind in (AmbiguityKind.CONTRADICTORY, AmbiguityKind.MISSING)]

    def clarification(self) -> Clarification | None:
        return Clarification.from_ambiguities(self.blocking_ambiguities())

    def requires_reparse_attempt(self) -> bool:
        """An LLM result worth repairing once before giving up on it."""
        return self.parse_source == ParseSource.LLM and bool(self.untraceable_fields())


# ---------------------------------------------------------------------------
# Parser boundary
# ---------------------------------------------------------------------------

class SessionContext(BaseModel):
    """The minimum a parser needs to read a turn in context.

    Deliberately small and free of money: a parser gets enough to resolve "the
    second one" and to patch current criteria, and nothing that would let it
    approve anything.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_id: str = Field(min_length=1)
    phase: AgentPhase = AgentPhase.IDLE

    current_constraints: HardConstraints | None = None
    current_mandate_draft: MandateDraft | None = None

    #: What is already known about the user, so a parser can avoid asking again
    #: and can tell the difference between a new self-description and a repeat of
    #: one. Carries no money and no authority, for the same reason as the rest of
    #: this model: a profile shapes ranking, never a payment.
    current_profile: UserProfile | None = None

    #: Product ids in the order they were last shown, so ranks resolve to what
    #: the user actually saw.
    last_shown_product_ids: list[str] = Field(default_factory=list)

    #: The product the user last chose. Present so that a bare "buy it" can be
    #: read as a confirmation of *that* purchase rather than of the list, which
    #: is the difference between a confirmation and a guess.
    selected_product_id: str | None = None

    has_active_mandate: bool = False
    open_escalation_proposal_id: str | None = None


class ParserAttempt(BaseModel):
    """One parser's outcome, kept so a fallback is visible in the transcript."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: ParseSource
    ok: bool
    detail: str | None = None

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.source.value}: {'ok' if self.ok else 'failed'}{' - ' + self.detail if self.detail else ''}"


@runtime_checkable
class IntentParser(Protocol):
    """Anything that can read a turn into an :class:`IntentResult`.

    Implementations must not perform I/O that can move money, must not read the
    clock to stamp authority, and must return a result that satisfies the same
    schema as every other implementation.
    """

    source: ParseSource

    def parse(self, text: str, context: SessionContext) -> IntentResult:
        ...


# ---------------------------------------------------------------------------
# LLM boundary -- provider neutral on purpose
# ---------------------------------------------------------------------------

class LLMRequest(BaseModel):
    """A provider-neutral extraction request.

    Kept free of provider SDK types so the chosen model is a configuration
    detail, not an architectural one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    system_prompt: str
    user_text: str
    #: JSON schema the reply must satisfy. The reply is validated with the same
    #: Pydantic model as any other parser's output.
    response_schema: dict[str, Any]
    max_output_tokens: int = Field(default=1024, gt=0)
    temperature: float = Field(default=0.0, ge=0.0)
    timeout_seconds: float = Field(default=15.0, gt=0)


class LLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    model: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)


@runtime_checkable
class LLMClient(Protocol):
    """A chat/completion provider.

    The only obligation: given a prompt and a schema, return JSON text. The
    caller validates it. A provider that returns prose fails validation and the
    fallback parser takes over.
    """

    def complete(self, request: LLMRequest) -> LLMResponse:
        ...


# ---------------------------------------------------------------------------
# Turn I/O
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None
    message: str = Field(min_length=1)


class AgentActionRequest(BaseModel):
    """An explicit user action, taken from a button rather than free text.

    This path never goes through a parser. It exists so that consent and
    confirmation are unambiguous: a click is a click.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    intent: Intent
    product_id: str | None = None
    quantity: int | None = Field(default=None, ge=1)
    mandate_id: str | None = None
    proposal_id: str | None = None
    order_id: str | None = None
    request_id: str | None = None
    idempotency_key: str | None = None
    #: Echoed back by the client on payment so a stale button cannot pay a
    #: different amount than the one that was displayed. ``StrictInt`` for the
    #: same reason as ``ConstraintPatch``: it is money, and it arrives from
    #: outside the process.
    confirmed_total_cents: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _required_handles_per_intent(self) -> "AgentActionRequest":
        #: Which handles each action cannot be expressed without.
        #:
        #: ``ACTIVATE_MANDATE`` deliberately does **not** require ``mandate_id``.
        #: The control this action backs is the mandate-consent gate, and at the
        #: moment it is rendered there is no mandate yet -- only the draft the
        #: user is being asked to sign. Requiring an identifier that cannot exist
        #: there made the gate unreachable, which is a gap rather than a
        #: validation: ``AgentResponse.requires_user_action()`` promises a
        #: control for ``NextAction.CONFIRM_MANDATE``, and no action could carry
        #: it. The field is still accepted, and when a client does supply one the
        #: orchestrator checks it against the session rather than ignoring it.
        required: dict[Intent, tuple[str, ...]] = {
            Intent.SELECT_PRODUCT: ("product_id",),
            Intent.CONFIRM_MANUAL_PAYMENT: ("order_id", "confirmed_total_cents"),
            Intent.CANCEL_ORDER: ("order_id",),
            Intent.REVOKE_MANDATE: ("mandate_id",),
            Intent.APPROVE_ESCALATION: ("proposal_id",),
            Intent.REJECT_ESCALATION: ("proposal_id",),
        }
        missing = [name for name in required.get(self.intent, ())
                   if getattr(self, name) is None]
        if missing:
            raise ValueError(f"{self.intent.value} requires {missing}")
        return self


class AgentResponse(BaseModel):
    """One turn, as the client receives it.

    A renders; it never decides. Every payload below is C's or B's own type,
    passed through unmodified, so the client can render from structured fields
    rather than by parsing the prose.

    ``message`` is the sentence to show. **Every number in it is interpolated
    from a field on this object, never generated by a model** -- a model that
    says "about three hundred" when the quote says 29900 has invented a price,
    and the problem statement treats a fabricated rate as fabrication.

    ``notes`` carries honest degradation: which parser actually ran, that a
    fallback took over, that a constraint was dropped. It is shown to the user
    rather than logged, because a silent fallback is how a system starts lying
    without anyone noticing.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)

    # -- what to say --------------------------------------------------------
    message: str = Field(min_length=1)
    phase: AgentPhase = AgentPhase.IDLE
    next_action: NextAction = NextAction.NONE

    # -- what was understood ------------------------------------------------
    intent: Intent | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    parse_source: ParseSource | None = None
    #: field name -> the slice of the user's own words that produced it.
    source_spans: dict[str, str] = Field(default_factory=dict)
    #: Traceable fields that carry a value with no quote behind them. A non-empty
    #: value means the turn must not be acted on.
    untraceable_fields: list[str] = Field(default_factory=list)

    # -- what is still open -------------------------------------------------
    clarification: Clarification | None = None
    ambiguities: list[Ambiguity] = Field(default_factory=list)

    # -- structured payloads, unmodified ------------------------------------
    constraints: HardConstraints | None = None
    results: SearchResponse | None = None

    #: What A understands about the user, and whether each part was stated or
    #: inferred. Carried so the client can show *why* an ordering was chosen --
    #: and so a user can correct an inference instead of arguing with a ranking
    #: they cannot see the reason for. It is not an input to any decision C
    #: makes: a payment is authorised by a mandate, never by a profile.
    profile: UserProfile | None = None

    #: The product the user just chose. Carried as an id rather than as a
    #: position, so a confirm control is bound to a product rather than to
    #: wherever it happened to sit in a list that the next search will replace.
    selected_product_id: str | None = None

    #: C's objects, passed through unmodified. Each is here for one reason: the
    #: ``message`` rule above says every number in it is interpolated from a
    #: field on this object, and a rendered price, mandate or receipt whose
    #: source object were absent would break that rule.
    quote: Quote | None = None
    mandate: Mandate | None = None
    spend_state: SpendState | None = None
    reservation: Reservation | None = None

    mandate_draft: MandateDraft | None = None
    decision: PolicyDecision | None = None
    denial: DenialReceipt | None = None
    escalation: EscalationRequest | None = None
    receipt: PaymentReceipt | None = None
    #: A payment that was refused or left unresolved. Not a policy denial: the
    #: purchase was authorised and the money did not move, and both halves of
    #: that have to reach the user.
    payment_failure: PaymentFailure | None = None

    # -- honest degradation -------------------------------------------------
    notes: list[str] = Field(default_factory=list)
    #: Which parsers were tried, in order, so a fallback is visible.
    trace: list[ParserAttempt] = Field(default_factory=list)

    # -- consistency --------------------------------------------------------

    @model_validator(mode="after")
    def _the_turn_says_something_useful(self) -> "AgentResponse":
        if self.next_action == NextAction.ANSWER_QUESTION and self.clarification is None:
            raise ValueError("answer_question requires a clarification to ask")
        if (self.next_action == NextAction.DESCRIBE_PREFERENCES
                and self.clarification is None):
            raise ValueError("describe_preferences requires a clarification to ask")
        if self.clarification is not None and self.untraceable_fields:
            raise ValueError(
                "a turn with untraceable values must not also ask a question; "
                "the invented value has to be resolved first"
            )
        return self

    def has_structured_payload(self) -> bool:
        return any((
            self.results, self.quote, self.mandate, self.spend_state,
            self.reservation, self.mandate_draft, self.decision, self.denial,
            self.escalation, self.receipt, self.payment_failure,
        ))

    def requires_user_action(self) -> bool:
        """Whether the client should render a control rather than just text.

        ``CONFIRM_MANDATE`` and ``CONFIRM_PAYMENT`` are the two consent gates:
        the standing authorisation, and the single purchase.
        """
        return self.next_action in (
            NextAction.SELECT_PRODUCT,
            NextAction.CONFIRM_MANDATE,
            NextAction.CONFIRM_PAYMENT,
            NextAction.APPROVE_ESCALATION,
        )


__all__ = [
    "AgentActionRequest",
    "AgentResponse",
    "Ambiguity",
    "AmbiguityKind",
    "ChatRequest",
    "Clarification",
    "ConstraintPatch",
    "Intent",
    "IntentParser",
    "IntentResult",
    "LLMClient",
    "LLMRequest",
    "LLMResponse",
    "MANDATE_INTENTS",
    "NonNegativeIntPatch",
    "ParseSource",
    "ParserAttempt",
    "SEARCH_INTENTS",
    "SessionContext",
    "TargetReference",
    "UserActionType",
]
