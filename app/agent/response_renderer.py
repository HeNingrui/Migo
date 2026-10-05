"""Turning structured results into something a person reads.

The rule that shapes this module: **no number is ever generated**. Every amount,
duration and weight in the output is interpolated from a contract field. A model
that says "about three hundred" when the quote says 29900 has invented a price,
and the problem statement treats a fabricated rate as fabrication -- so the
prose is a template with slots, not a paraphrase.

Two consequences worth stating, because both were tempting to do differently:

* **Why is a separate module needed if a model can write prose?** Because the
  model writes the *connective* text, and this module writes the *facts*. When
  the LLM parser is available the two are combined; when it is not -- offline,
  rate-limited, rejected -- this module alone still produces a correct answer.
* **Why Chinese?** The users in the demo type Chinese, the plan's examples are
  Chinese, and the judges are in Hong Kong. The product data is English; that is
  data, not interface copy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.contracts.agent import Ambiguity, Clarification, IntentResult, ParseSource
from app.contracts.commerce import Quote, Reservation, SpendState
from app.contracts.mandate import Mandate
from app.contracts.policy import (
    DenialReceipt,
    EscalationRequest,
    PaymentFailure,
    PaymentReceipt,
    PolicyDecision,
)
from app.contracts.product import HardConstraints, Product
from app.contracts.search import SearchResponse

#: How many candidates to describe before saying "and N more".
MAX_LISTED = 3


def display_time(value: datetime) -> str:
    """Render a stored instant in the fixed Hong Kong/China display timezone."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(timezone(timedelta(hours=8)))
    return local.strftime('%Y-%m-%d %H:%M:%S') + ' (UTC+8)'


def money(cents: int | None) -> str:
    """``29900`` -> ``HK$299.00``.

    Integer arithmetic on both sides of the point, because ``cents / 100``
    produces a float and a float that reaches a comparison is a bug waiting for
    the wrong rounding mode.
    """
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    whole, part = divmod(abs(int(cents)), 100)
    return f"{sign}HK${whole:,}.{part:02d}"


def hours(value: float | None) -> str:
    return "—" if value is None else f"{value:g} 小时"


def grams(value: float | None) -> str:
    return "—" if value is None else f"{value:g} 克"


def duration(seconds: int | None) -> str:
    """``86400`` -> ``24 小时``, ``604800`` -> ``7 天``.

    Largest whole unit first, because a mandate clause reads as "7 days", not as
    "604800 秒". Exact only: a remainder falls through to the next unit down, so
    nothing is rounded away and the number on the summary is the number C will
    enforce.
    """
    if seconds is None:
        return "—"
    for unit, size in (("天", 86400), ("小时", 3600), ("分钟", 60)):
        if seconds and seconds % size == 0:
            return f"{seconds // size} {unit}"
    return f"{seconds} 秒"


def connection_label(value: str | None) -> str:
    return {"wireless": "无线", "wired": "有线"}.get(value or "", value or "—")


# ---------------------------------------------------------------------------
# Criteria
# ---------------------------------------------------------------------------

def describe_constraints(constraints: HardConstraints | None) -> str:
    """The requirements as the system understood them, so the user can correct them.

    Every clause names the field it came from. This is the sentence the user
    reads before the agent acts, and the one they argue with.
    """
    if constraints is None:
        return "还没有任何条件"

    parts: list[str] = []
    if constraints.max_price_cents is not None:
        parts.append(f"价格不超过 {money(constraints.max_price_cents)}")
    if constraints.min_price_cents is not None:
        parts.append(f"价格不低于 {money(constraints.min_price_cents)}")
    if constraints.connection is not None:
        parts.append({"wireless": "无线", "wired": "有线"}[constraints.connection])
    if constraints.form_factor is not None:
        parts.append({
            "in_ear": "入耳式", "over_ear": "头戴式", "open_ear": "开放式",
        }[constraints.form_factor])
    if constraints.anc_required:
        parts.append("必须支持主动降噪")
    if constraints.min_battery_hours is not None:
        parts.append(f"续航至少 {hours(constraints.min_battery_hours)}")
    if constraints.max_wearing_weight_g is not None:
        parts.append(f"重量不超过 {grams(constraints.max_wearing_weight_g)}")
    if constraints.brand_allowlist:
        parts.append("品牌限 " + "、".join(constraints.brand_allowlist))
    if constraints.in_stock_only:
        parts.append("只买有货的")

    return "、".join(parts) if parts else "还没有任何条件"


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

def describe_candidate(index: int, candidate) -> str:
    product: Product = candidate.product
    bits = [f"{index}. {product.name}（{product.brand}）— {money(product.price_cents)}"]
    specs: list[str] = []
    if product.anc is True:
        specs.append("主动降噪")
    elif product.anc is None:
        specs.append("降噪未知")
    if product.battery_hours is not None:
        specs.append(hours(product.battery_hours))
    if product.wearing_weight_g is not None:
        specs.append(grams(product.wearing_weight_g))
    if product.stock <= 0:
        specs.append("缺货")
    if specs:
        bits.append("，".join(specs))
    return " | ".join(bits)


def describe_results(response: SearchResponse, *, limit: int = MAX_LISTED) -> str:
    """The reply after a search. Numbers come from the response, nowhere else."""
    if response.total_matches == 0:
        return "按这个条件没有找到任何商品。"

    head = (
        f"符合条件的共 {response.total_matches} 款"
        + (f"，先看前 {response.returned} 款：" if response.returned < response.total_matches
           else "：")
    )
    lines = [head]
    for index, candidate in enumerate(response.candidates[:limit], start=1):
        lines.append(describe_candidate(index, candidate))

    if response.gaps.unsupported_criteria:
        lines.append(
            "（这些要求商品数据里没有，无法判断："
            + "、".join(response.gaps.unsupported_criteria) + "）"
        )
    if response.gaps.missing_data_attributes:
        lines.append(
            "（这些参数有商品没标："
            + "、".join(response.gaps.missing_data_attributes) + "）"
        )
    return "\n".join(lines)


def describe_comparison(response: SearchResponse) -> str:
    """The side-by-side table, as text. Empty when nothing distinguishes them."""
    rows = [r for r in response.comparison if r.is_distinguishing]
    if not rows:
        return ""
    lines = ["对比："]
    for row in rows:
        label = {
            "price_cents": "价格", "battery_hours": "续航",
            "wearing_weight_g": "重量", "anc": "降噪",
        }.get(row.attribute, row.attribute)
        cells = []
        for cell in row.values:
            if not cell.known:
                cells.append("未知")
            elif row.attribute == "price_cents":
                cells.append(money(int(cell.value)))
            elif row.attribute == "battery_hours":
                cells.append(hours(float(cell.value)))
            elif row.attribute == "wearing_weight_g":
                cells.append(grams(float(cell.value)))
            else:
                cells.append(str(cell.value))
        lines.append(f"  {label}：" + " / ".join(cells))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Questions, refusals and decisions
# ---------------------------------------------------------------------------

#: The question to ask about each traceable field, in the language the users
#: actually type. The parsers carry their own phrasing, which is English because
#: the contracts are; this table is what the user sees. Keeping the two apart
#: means a parser can be added without also being a translator.
QUESTION_BY_FIELD: dict[str, str] = {
    "max_price_cents": "预算大概是多少？说个上限就行。",
    "min_price_cents": "价格有下限吗？",
    "connection": "想要无线的还是有线的？",
    "form_factor": "偏好入耳式、头戴式还是开放式？",
    "anc_required": "需要主动降噪吗？",
    "min_battery_hours": "续航至少要多少小时？",
    "max_wearing_weight_g": "重量最多能接受多少克？",
    "brand_allowlist": "有偏好的品牌吗？",
    "use_cases": "主要拿来做什么用？通勤、学习、运动还是别的？",
    "message": "能说说你想找什么样的耳机吗？大概什么价位？",
    "cap_per_transaction_cents": "单笔最多能花多少？",
    "rolling_cap_cents": "一段时间内总共最多花多少？",
    "rolling_window_seconds": "这个总额是按多长时间算的？",
    "velocity_max_count": "多长时间内最多买几次？",
    "velocity_window_seconds": "这个次数限制是按多长时间算的？",
    "valid_for_seconds": "这份授权有效多久？",
    "escalate_above_cents": "超过多少钱要先问你？",
    "max_quantity_total": "总共最多买几件？",
}


#: Fields the parser can ask about that map onto something already recorded.
#: Used to stop the agent re-asking a question the conversation answered.
_KNOWN_BY_FIELD: dict[str, str] = {
    "max_price_cents": "max_price_cents",
    "min_price_cents": "min_price_cents",
    "connection": "connection",
    "form_factor": "form_factor",
    "anc_required": "anc_required",
    "min_battery_hours": "min_battery_hours",
    "max_wearing_weight_g": "max_wearing_weight_g",
    "brand_allowlist": "brand_allowlist",
}


def has_criteria(constraints: HardConstraints | None) -> bool:
    """Whether the conversation has established anything to search on."""
    if constraints is None:
        return False
    return bool(constraints.explicitly_set_fields()) or constraints.anc_required


def already_known(field: str, constraints: HardConstraints | None) -> bool:
    return (
        constraints is not None
        and field in _KNOWN_BY_FIELD
        and getattr(constraints, _KNOWN_BY_FIELD[field], None) is not None
    )


def describe_clarification(
    clarification: Clarification | None,
    *,
    constraints: HardConstraints | None = None,
) -> str:
    """One question, and never one the conversation has already answered.

    The failure this exists to prevent: with "≤HK$200, wireless, ANC" already on
    file, a vague turn like "随便看看" was answered with "what are you looking
    for, and roughly what price?" -- asking for something the agent had been
    told two turns earlier. The user reads that as the agent not listening.

    Three cases, in order:

    1. **Nothing was understood, and criteria exist.** Offer to carry on with
       them, or to change them. This is the common case for "just browsing".
    2. **A specific value is asked for that is already known.** Confirm the
       existing value instead of re-asking.
    3. **Otherwise.** Ask about the field, in the user's language.
    """
    if clarification is None:
        return "能再说得具体一点吗？"

    known = has_criteria(constraints)
    summary = describe_constraints(constraints)

    asked = [f for f in clarification.about if f != "message"]
    everything_asked_is_known = bool(asked) and all(
        already_known(f, constraints) for f in asked
    )

    if known and (not asked or everything_asked_is_known):
        return (
            f"目前记下的是：{summary}。\n"
            "要按这个条件再看一遍，还是想改点什么？"
        )

    if known and asked and any(already_known(f, constraints) for f in asked):
        # Partly known: name what is settled so the question is only about the rest.
        settled = "、".join(
            f"{_label(f)} {_format_by_field(f, getattr(constraints, f, None))}"
            for f in asked if already_known(f, constraints)
        )
        rest = next(f for f in asked if not already_known(f, constraints))
        return f"（{settled} 先按这个记着。）\n{QUESTION_BY_FIELD.get(rest, clarification.question)}"

    for field in clarification.about:
        if field in QUESTION_BY_FIELD:
            return QUESTION_BY_FIELD[field]
    return clarification.question


def describe_ambiguities(ambiguities: list[Ambiguity]) -> list[str]:
    return [f"[{a.kind.value}] {a.field}: {a.detail}" for a in ambiguities]


def describe_parse_source(source: ParseSource | None) -> str | None:
    """A one-line, user-facing account of how the turn was read.

    Shown rather than logged. A fallback that takes over silently is how a
    system starts being wrong without anyone noticing. ``None`` means the turn
    did not come from a parser at all -- a button -- and needs no explanation.
    """
    if source is None:
        return None
    if source.value == "FALLBACK":
        return "（用本地规则理解，未调用模型）"
    if source.value == "REPAIRED":
        return "（模型第一次输出不合格式，已修正一次）"
    return None


def describe_parse_note(result: IntentResult) -> str | None:
    """Kept for callers holding a parse; see :func:`describe_parse_source`."""
    return describe_parse_source(result.parse_source)


def describe_relax_hints(response: SearchResponse) -> str:
    """Why nothing matched, and what would fix it -- one dimension at a time.

    Rendered verbatim from B's hints. A is not allowed to decide that "301 is
    close enough to 300", so this states the fact and leaves the choice open:
    the hint says how many products a change would admit, and the user decides
    whether that change is acceptable.
    """
    hints = response.relax_hints
    if hints is None or not hints.hints:
        return ""
    lines = ["没有符合全部条件的商品。单独放宽一条的话："]
    for hint in hints.hints:
        frm = _format_by_field(hint.field, hint.from_value)
        to = _format_by_field(hint.field, hint.to_value)
        lines.append(
            f"  · {_label(hint.field)}：{frm} → {to}，可以多出 {hint.gained_count} 款"
            f"（例如 {hint.example_product_id}）"
        )
    return "\n".join(lines)


def field_label(field: str) -> str:
    """The user-facing name of a field, in the language the demo speaks."""
    return {
        "max_price_cents": "价格上限", "min_price_cents": "价格下限",
        "max_wearing_weight_g": "重量上限", "min_battery_hours": "续航下限",
        "brand_allowlist": "品牌限制", "connection": "连接方式",
        "form_factor": "佩戴形式", "anc_required": "必须降噪",
        "in_stock_only": "只买有货的",
        "color": "颜色", "tags": "用途标签", "required_device": "适用设备",
        "max_estimated_delivery_days": "最多配送天数",
        "allowed_merchants": "商家", "allowed_categories": "品类",
        "required_connection": "连接方式", "cap_per_transaction_cents": "单笔上限",
        "rolling_cap_cents": "累计上限", "rolling_window_seconds": "累计时段",
        "velocity_max_count": "购买次数", "velocity_window_seconds": "次数时段",
        "max_quantity_total": "总件数", "valid_for_seconds": "有效期",
        "escalate_above_cents": "先问你的门槛", "allowed_payment_routes": "付款方式",
        "shipping_address_id": "收货地址", "address_change_allowed": "改地址",
    }.get(field, field)


#: Retained so existing call sites keep working; the public name is preferred.
_label = field_label


def _format_by_field(field: str, value: object) -> str:
    if value is None:
        return "不限"
    if field in ("max_price_cents", "min_price_cents"):
        return money(int(value))
    if field == "max_wearing_weight_g":
        return grams(float(value))
    if field == "min_battery_hours":
        return hours(float(value))
    if field == "anc_required":
        return "要求" if value else "不要求"
    if field == "in_stock_only":
        return "只看有货" if value else "含缺货"
    if isinstance(value, list):
        return "、".join(str(v) for v in value) or "不限"
    return str(value)


def describe_decision(decision: PolicyDecision) -> str:
    """Say what happened, and name the rule that decided it."""
    if decision.outcome == "APPROVE":
        return (
            f"已批准：{money(decision.cash_total_cents)}，"
            f"规则版本 {decision.mandate_version}。"
        )
    reasons = "；".join(describe_violation(v) for v in decision.ordered_violations())
    label = {"DENY": "已拒绝", "ESCALATE": "需要你确认"}[decision.outcome]
    return f"{label}（{decision.primary_reason.value if decision.primary_reason else '—'}）：{reasons}"


#: Fields whose recorded value is money, so a denial says "HK$320.00" rather
#: than "32000". ``observed`` and ``limit`` are deliberately untyped in the
#: contract -- a violation may report a scalar, a set of allowed values, or
#: nothing -- so the field name is what says how to read them.
_MONEY_FIELDS: frozenset[str] = frozenset({
    "cash_total_cents", "cap_per_transaction_cents", "rolling_cap_cents",
    "escalate_above_cents", "amount_cents", "projected_exposure_cents",
    "current_exposure_cents", "balance_cents", "required_cents",
})


def describe_violation(violation) -> str:
    """One violated rule, with the measured value and the recorded limit.

    The numbers are formatted rather than printed raw: "observed=50900" is a
    value the reader has to divide by a hundred in their head before they
    recognise it as the HK$509 they were quoted a moment ago.
    """
    text = f"{field_label(violation.field)}：{violation.message}"
    if violation.observed is None and violation.limit is None:
        return text
    return (
        f"{text}（实际 {_format_observed(violation.field, violation.observed)}，"
        f"上限 {_format_observed(violation.field, violation.limit)}）"
    )


def _format_observed(field: str, value: object) -> str:
    if value is None:
        return "—"
    if field in _MONEY_FIELDS and isinstance(value, int) and not isinstance(value, bool):
        return money(value)
    if isinstance(value, list):
        return "、".join(str(item) for item in value) or "—"
    return str(value)


def describe_denial(denial: DenialReceipt) -> str:
    lines = [
        f"这笔没有通过，原因：{denial.primary_reason.value}",
        f"金额 {money(denial.cash_total_cents)}，规则哈希 {denial.policy_hash[:23]}…",
    ]
    for violation in denial.violations:
        lines.append("  · " + describe_violation(violation))
    return "\n".join(lines)


def describe_escalation(escalation: EscalationRequest) -> str:
    return (
        f"{escalation.question}\n"
        f"金额 {money(escalation.cash_total_cents)}，"
        f"超过你设定的 {money(escalation.escalate_above_cents)}，"
        f"请确认是否继续。"
    )


# ---------------------------------------------------------------------------
# C's results, as the user reads them
# ---------------------------------------------------------------------------

def describe_quote(quote: Quote) -> str:
    """The priced offer, before anything is submitted.

    Every figure is interpolated from C's ``Quote``. A does not add a shipping
    estimate, a fee or a discount of its own -- the totals shown are the totals
    C will enforce, and the hash is printed because it is what the receipt will
    carry and what a reviewer will recompute.
    """
    lines = [
        f"{quote.product_name} × {quote.quantity}：{money(quote.unit_price_cents)} / 件",
        f"运费 {money(quote.shipping_cents)}"
        + (f"，税 {money(quote.tax_cents)}" if quote.tax_cents else "")
        + (f"，折扣 -{money(quote.discount_cents)}" if quote.discount_cents else ""),
        f"到手合计 {money(quote.merchant_total_cents)}（{quote.merchant_id}）",
    ]
    if quote.source_type.value != "OBSERVED_PUBLIC_SOURCE":
        lines.append(f"（运费与手续费来源于 {quote.source_type.value} 参数，不是实测费率）")
    return "\n".join(lines)


def describe_mandate(mandate: Mandate) -> str:
    """The activated authorisation, including the hash that makes it checkable.

    Shown rather than logged: ``policy_hash`` is the answer to "why did it do
    that?", and it is only an answer if the user was told which value to quote.
    """
    lines = [
        f"授权已生效（版本 {mandate.version}）：",
        f"  单笔上限 {money(mandate.cap_per_transaction_cents)}，"
        f"{duration(mandate.rolling_window_seconds)}内累计不超过 "
        f"{money(mandate.rolling_cap_cents)}",
        f"  {duration(mandate.velocity_window_seconds)}内最多 {mandate.velocity_max_count} 笔，"
        f"总共 {mandate.max_quantity_total} 件，"
        f"有效至 {display_time(mandate.expires_at)}",
        f"  商家 {'、'.join(mandate.allowed_merchants)}，"
        f"付款方式 {'、'.join(mandate.allowed_payment_routes)}",
        f"  规则指纹 {mandate.policy_hash}",
    ]
    if mandate.escalate_above_cents is not None:
        lines.insert(
            3, f"  超过 {money(mandate.escalate_above_cents)} 会先问你")
    return "\n".join(lines)


def describe_receipt(receipt: PaymentReceipt) -> str:
    """A settled purchase. The amount is the one that left the account."""
    lines = [
        f"已付款 {money(receipt.cash_total_cents)}，"
        f"订单 {receipt.order_id}。",
        f"  商品 {money(receipt.merchant_total_cents or 0)}"
        + (f" + 手续费 {money(receipt.fee_cents)}" if receipt.fee_cents else "")
        + f"，余额 {money(receipt.balance_after_cents)}",
        f"  回执 {receipt.payment_id}，"
        f"时间 {display_time(receipt.settled_at)}",
    ]
    return "\n".join(lines)


def describe_payment_failure(failure: PaymentFailure) -> str:
    """A refusal that happened after the decision, and is not a policy denial."""
    lines = [
        f"这笔没能完成付款（{failure.code.value}）：{failure.message}",
        f"  预留 {failure.reservation_id}，时间 "
        f"{display_time(failure.failed_at)}",
    ]
    if failure.retryable:
        lines.append("  可以重试。")
    else:
        lines.append("  不要自动重试：这笔的状态需要先和支付方对账。")
    return "\n".join(lines)


def describe_reservation(reservation: Reservation) -> str:
    return (
        f"已预留 {money(reservation.amount_cents)}（{reservation.status}），"
        f"预留号 {reservation.reservation_id}"
    )


def describe_spend_state(state: SpendState) -> str:
    """How much of the mandate is already committed."""
    return (
        f"已用 {money(state.exposure_cents)}，{state.exposure_count} 笔，"
        f"{state.exposure_quantity} 件"
        + (f"（其中在途 {money(state.reserved_cents)}）" if state.reserved_cents else "")
    )


def describe_sandbox_settlement() -> str:
    """The label a sandbox settlement has to carry wherever it is shown.

    The problem statement treats a fabricated rate as fabrication; a sandbox
    payment is only honest while nothing implies it moved real money.
    """
    return "（支付由 SANDBOX 沙箱通道完成，没有真实扣款）"


__all__ = [
    "MAX_LISTED",
    "QUESTION_BY_FIELD",
    "connection_label",
    "describe_ambiguities",
    "describe_candidate",
    "describe_clarification",
    "describe_comparison",
    "describe_constraints",
    "describe_decision",
    "describe_denial",
    "describe_escalation",
    "describe_mandate",
    "describe_parse_note",
    "describe_parse_source",
    "describe_payment_failure",
    "describe_quote",
    "describe_receipt",
    "describe_relax_hints",
    "describe_reservation",
    "describe_results",
    "describe_sandbox_settlement",
    "describe_spend_state",
    "duration",
    "field_label",
    "grams",
    "hours",
    "money",
]
