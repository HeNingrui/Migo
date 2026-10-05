"""A's half of the mandate: reading it out of a conversation, and asking about it.

The boundary this module respects, in one line: **a draft is not a mandate.**
Everything here produces or edits a
:class:`~app.contracts.mandate.MandateDraft` -- what the user has said so far.
The activated :class:`~app.contracts.mandate.Mandate` is compiled by C, from the
draft's own fields, and A never sees a policy hash until C returns one.

Three decisions shape the code.

**One parse, then deterministic merging.** The parser reads each turn into a
draft carrying only the fields that turn mentioned; this module merges it onto
the draft already on file with ordinary code. The plan is explicit about why the
model must not be asked to reconcile the whole draft a second time: the user's
consent is given to *the summary they were shown*, and if a model re-integrates
the fields afterwards, the thing they agreed to and the thing that gets hashed
can differ. A deterministic merge is the only version of this that can be
argued about.

**A may propose a clause it was not told; it may not propose an amount.** The
line is the one the contract already draws: ``IntentResult.TRACEABLE_FIELDS``
lists the values that decide how much money may move, and a parse carrying one
of those without a quote from the user is refused outright. Everything else --
which merchant, which rail, which address -- A proposes from facts it can point
at: C's merchant of record, C's rails, the configured address. All of it appears
on the summary before anything is activated, which is what turns a proposal into
an agreement.

**Missing and contradictory are different conversations.** ``missing`` is a
question; ``contradictory`` is a statement that two things the user said cannot
both hold. Asking "what is your budget?" while two budgets are on the table
produces a third, so the contradiction is resolved first.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agent import response_renderer as render
from app.contracts.agent import Ambiguity, AmbiguityKind, Clarification
from app.contracts.mandate import MandateDraft
from app.contracts.product import Category

#: The only category the catalog contains, and the only value the contract's
#: ``Category`` literal permits. Not a guess: there is nothing else it could be.
CATALOG_CATEGORIES: tuple[Category, ...] = ("headphones",)

#: The clauses A fills in rather than asking for, because the user cannot be
#: expected to know an identifier and A is not allowed to invent a number.
#: Every one of them is displayed on the summary before activation, marked as a
#: proposal so the signature covers them too.
PROPOSED_FIELDS: tuple[str, ...] = (
    "allowed_merchants",
    "allowed_categories",
    "allowed_payment_routes",
    "shipping_address_id",
    "address_change_allowed",
)

#: One question per unresolved clause, in the user's language. A value the user
#: never states is never defaulted here: these are the fields that move money.
QUESTION_BY_FIELD: dict[str, str] = {
    "cap_per_transaction_cents": "单笔最多能花多少？",
    "rolling_cap_cents": "一段时间内总共最多花多少？",
    "rolling_window_seconds": "这个总额是按多长时间算的？比如 24 小时。",
    "velocity_max_count": "多长时间内最多买几次？",
    "velocity_window_seconds": "这个次数限制是按多长时间算的？",
    "max_quantity_total": "总共最多买几件？",
    "valid_for_seconds": "这份授权有效多久？比如 7 天。",
    "escalate_above_cents": "超过多少钱要先问你？",
    "allowed_merchants": "允许从哪些商家买？",
    "allowed_categories": "允许买哪一类商品？",
    "allowed_payment_routes": "允许用哪种付款方式？",
    "shipping_address_id": "收货地址用哪个？",
    "address_change_allowed": "允许改收货地址吗？",
}


@dataclass(frozen=True)
class MandateGaps:
    """What still stands between a draft and an activated mandate."""

    missing: tuple[str, ...]
    contradictions: tuple[str, ...]

    @property
    def is_complete(self) -> bool:
        return not self.missing and not self.contradictions


def merge_draft(base: MandateDraft | None, patch: MandateDraft) -> MandateDraft:
    """Apply one turn's reading onto the draft already on file. Pure.

    Only fields the patch actually mentions move. ``None`` means "this turn did
    not speak to it", which is why a null is never allowed to clear an earlier
    answer -- "I did not mention the budget" and "I no longer want a budget" are
    different sentences, and only one of them was said.

    ``ambiguities`` and ``unsupported_conditions`` accumulate rather than replace,
    because they are the record of what the user has still to resolve, and
    ``source_spans`` accumulates so that every clause on the summary can be
    traced to the words that produced it.
    """
    if base is None:
        return patch

    merged = base.model_dump(mode="python")
    for name, value in patch.model_dump(mode="python").items():
        if name in ("ambiguities", "unsupported_conditions", "source_spans"):
            continue
        if value is not None:
            merged[name] = value

    merged["ambiguities"] = _unique([*base.ambiguities, *patch.ambiguities])
    merged["unsupported_conditions"] = _unique(
        [*base.unsupported_conditions, *patch.unsupported_conditions])
    merged["source_spans"] = {**base.source_spans, **patch.source_spans}
    return MandateDraft.model_validate(merged)


def canonical_payment_routes(routes: list[str], available: list[str]) -> list[str]:
    """Resolve display names only to configured routes, before user confirmation.

    Unknown identifiers stay unknown; they must never select a default rail.
    Active mandates are immutable and are not passed through this function.
    """
    aliases = {
        "mastercard": "mastercard_demo", "visa": "visa_demo",
        "fps": "fps_demo", "wallet": "wallet_demo",
        "virtual_wallet": "wallet_demo",
    }
    known = {route.casefold(): route for route in available}
    result = []
    for route in routes:
        token = route.strip().casefold()
        resolved = known.get(token) or known.get(aliases.get(token, "")) or route
        if resolved not in result:
            result.append(resolved)
    return result


def propose_clauses(
    draft: MandateDraft | None,
    *,
    merchant_of_record: str,
    payment_routes: list[str],
    shipping_address_id: str,
    address_change_allowed: bool = False,
) -> MandateDraft:
    """Fill the clauses A is allowed to propose, leaving the rest alone.

    Called with facts, not guesses: the merchant and the rails come from C, and
    the address comes from the session's configuration. Nothing here is a number
    the user has to agree to, and nothing here is money.
    """
    base = draft or MandateDraft()
    proposed = base.model_dump(mode="python")
    proposals = {
        "allowed_merchants": list(base.allowed_merchants or [merchant_of_record]),
        "allowed_categories": list(base.allowed_categories or CATALOG_CATEGORIES),
        "allowed_payment_routes": canonical_payment_routes(
            base.allowed_payment_routes if base.allowed_payment_routes is not None
            else payment_routes, payment_routes),
        "shipping_address_id": base.shipping_address_id or shipping_address_id,
        "address_change_allowed": (
            base.address_change_allowed
            if base.address_change_allowed is not None
            else address_change_allowed
        ),
    }
    proposed.update(proposals)
    return MandateDraft.model_validate(proposed)


def gaps(draft: MandateDraft) -> MandateGaps:
    """What is still unanswered, and what cannot both be true."""
    missing = tuple(draft.missing_required_fields())
    contradictions = tuple(draft.structural_problems())
    return MandateGaps(missing=missing, contradictions=contradictions)


def clarification_for(draft: MandateDraft) -> Clarification | None:
    """The single next question, or ``None`` when the draft can be signed.

    A contradiction is asked about before a missing value: the contradiction
    means two of the user's own statements disagree, and adding a third answer
    to a set that already conflicts is how a mandate ends up authorising
    something nobody chose.
    """
    found = gaps(draft)
    if found.contradictions:
        first = found.contradictions[0]
        return Clarification(
            question=_contradiction_question(first),
            about=[first], blocking=True,
        )
    if found.missing:
        field = found.missing[0]
        return Clarification(
            question=QUESTION_BY_FIELD.get(field, f"请补充 {field}"),
            about=[field], blocking=True,
        )
    return None


def ambiguities_for(draft: MandateDraft) -> list[Ambiguity]:
    """The unresolved clauses, as contract ambiguities rather than prose."""
    found = gaps(draft)
    items = [
        Ambiguity(kind=AmbiguityKind.CONTRADICTORY, field=field,
                  detail=field, question=_contradiction_question(field))
        for field in found.contradictions
    ]
    items += [
        Ambiguity(kind=AmbiguityKind.MISSING, field=field, detail=field,
                  question=QUESTION_BY_FIELD.get(field, f"请补充 {field}"))
        for field in found.missing
    ]
    return items


def describe_summary(draft: MandateDraft) -> str:
    """The clauses as the user will sign them.

    Read straight off the draft, in the order the mandate will be compiled from
    it, with every value the user did not state marked as A's proposal. That
    marker is the difference between a summary and a fait accompli: the user is
    being asked to agree to the whole set, including the parts they did not
    dictate.
    """
    lines = ["这份授权的内容是："]
    lines += [f"  · {label}：{value}" for label, value in _clauses(draft)]
    quoted = sorted(draft.source_spans)
    if quoted:
        lines.append(
            "（带「你说过」标记的来自你的原话："
            + "、".join(render.field_label(name) for name in quoted) + "）"
        )
    if draft.unsupported_conditions:
        lines.append("（这些要求目前无法执行，已记录但不会生效："
                     + "、".join(draft.unsupported_conditions) + "）")
    return "\n".join(lines)


def describe_gaps(draft: MandateDraft) -> str:
    """What is still open, phrased as the next thing to settle."""
    found = gaps(draft)
    if found.is_complete:
        return ""
    if found.contradictions:
        return "这几条互相冲突，得先改掉一条：" + "；".join(found.contradictions)
    return f"还差 {len(found.missing)} 项没有决定。"


def _contradiction_question(problem: str) -> str:
    return {
        "transaction cap exceeds rolling cap": "单笔上限比累计上限还高，要把哪个改一下？",
        "escalation threshold exceeds transaction cap": (
            "你设的「超过多少先问」比单笔上限还高，那样永远不会触发，要改哪个？"
        ),
        "rolling cap given without a rolling window": "累计上限是按多长时间算的？",
        "velocity limit needs both a count and a window": "次数限制需要同时给次数和时间范围。",
        "no expiry": "这份授权有效多久？",
        "no shipping address": "收货地址用哪个？",
        "merchant allowlist is present but empty": "商家白名单是空的，那样一笔都买不了。",
        "category allowlist is present but empty": "品类白名单是空的，那样一笔都买不了。",
        "payment route allowlist is present but empty": "付款方式白名单是空的，那样一笔都买不了。",
    }.get(problem, problem)


def _clauses(draft: MandateDraft) -> list[tuple[str, str]]:
    """Every clause on the summary, in the order the user reads them."""
    stated = set(draft.source_spans)
    rows: list[tuple[str, str]] = []

    def add(field: str, text: str) -> None:
        rows.append((render.field_label(field), f"{text}{_origin(field, stated)}"))

    if draft.allowed_merchants:
        add("allowed_merchants", "、".join(draft.allowed_merchants))
    if draft.allowed_categories:
        add("allowed_categories", "、".join(draft.allowed_categories))
    if draft.required_connection is not None:
        add("required_connection", render.connection_label(draft.required_connection))
    if draft.required_device is not None:
        add("required_device", draft.required_device)
    if draft.anc_required is not None:
        add("anc_required", "必须支持" if draft.anc_required else "不要求")
    if draft.cap_per_transaction_cents is not None:
        add("cap_per_transaction_cents", render.money(draft.cap_per_transaction_cents))
    if draft.rolling_cap_cents is not None:
        window = render.duration(draft.rolling_window_seconds)
        add("rolling_cap_cents", f"{window}内累计不超过 "
                                f"{render.money(draft.rolling_cap_cents)}")
    if draft.velocity_max_count is not None:
        window = render.duration(draft.velocity_window_seconds)
        add("velocity_max_count", f"{window}内最多 {draft.velocity_max_count} 笔")
    if draft.max_quantity_total is not None:
        add("max_quantity_total", f"总共 {draft.max_quantity_total} 件")
    if draft.valid_for_seconds is not None:
        add("valid_for_seconds", f"有效 {render.duration(draft.valid_for_seconds)}")
    if draft.escalate_above_cents is not None:
        add("escalate_above_cents",
            f"超过 {render.money(draft.escalate_above_cents)} 先问你")
    if draft.allowed_payment_routes:
        add("allowed_payment_routes", "、".join(draft.allowed_payment_routes))
    if draft.shipping_address_id:
        add("shipping_address_id", draft.shipping_address_id)
    if draft.address_change_allowed is not None:
        add("address_change_allowed",
            "允许改地址" if draft.address_change_allowed else "不允许改地址")
    return rows


def _origin(field: str, stated: set[str]) -> str:
    if field in stated:
        return ""
    if field in PROPOSED_FIELDS:
        return "（我建议的）"
    return ""


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


__all__ = [
    "CATALOG_CATEGORIES",
    "PROPOSED_FIELDS",
    "QUESTION_BY_FIELD",
    "MandateGaps",
    "ambiguities_for",
    "clarification_for",
    "describe_gaps",
    "describe_summary",
    "gaps",
    "merge_draft",
    "propose_clauses",
]
