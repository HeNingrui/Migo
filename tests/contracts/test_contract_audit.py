"""对抗性契约审计的回归测试。

这些测试来自一次把怀疑写成可执行探针的审计。每一条都对应一个曾经存在的
缺口，或者一个明确排除的怀疑。留在仓库里是为了让它们不会再回来。

审计方法与这里保留的结论：

    探针 1  空 allowlist 能否进入 Mandate            → 曾是缺口，已修，见 test_mandate
    探针 2  policy_hash 是否绑定身份                  → 有意不绑定，见下方说明
    探针 3  Draft 到 Mandate 的字段差                 → 设计如此，见下方说明
    探针 4  PaymentReceipt 金额是否含费歧义            → 曾是缺口，已修
    探针 5  summary_only 与独立端点并存                → 曾是缺口，已修
    探针 6  rolling_window_start 语义未定义            → 曾是缺口，已修
    探针 7  不可用支付路线是否被拦                      → 排除怀疑，本来就拦
    探针 8  重复对比维度                              → 曾是缺口，已修
    探针 9  空 RelaxHints 是否合法                     → 合法，语义由 classify_outcome 区分
    探针 10 错误码 HTTP 映射是否完整                    → 曾是缺口，已修
    探针 11 单据能否由决策唯一构造                      → 排除怀疑
    探针 12 审计事件与决策之间的链接                    → 见下方说明
    探针 13 未知 use_case 是否被拒                     → 排除怀疑
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.contracts import (
    BUSINESS_OUTCOMES,
    DEFAULT_HTTP_STATUS,
    POLICY_DENIAL_CODES,
    AuditEvent,
    DenialReceipt,
    ErrorCode,
    GapReport,
    HardConstraints,
    Mandate,
    PaymentReceipt,
    PolicyDecision,
)

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)

#: The mandate fields that are rules rather than identity or bookkeeping. Only
#: these belong in the hashed policy.
MANDATE_RULE_FIELDS = frozenset({
    "allowed_merchants", "allowed_categories", "required_connection",
    "anc_required", "required_device", "cap_per_transaction_cents",
    "rolling_cap_cents", "rolling_window_seconds", "velocity_max_count",
    "velocity_window_seconds", "max_quantity_total", "escalate_above_cents",
    "allowed_payment_routes",
})


# ---------------------------------------------------------------------------
# 探针 10：每个错误码都必须有一个深思熟虑的 HTTP 状态
# ---------------------------------------------------------------------------

#: Codes deliberately left to the 400 default rather than individually mapped.
#: Empty today: every code resolves to something more specific, which is the
#: state this test guards. Kept as a named set so a future unclassified code has
#: an obvious place to be justified instead of silently joining the default.
UNCLASSIFIED_BY_DESIGN: frozenset[ErrorCode] = frozenset()


def test_every_error_code_resolves_deliberately():
    """No code may reach the 400 default without a reason.

    An earlier state left 25 codes unmapped, including ``OUT_OF_STOCK`` and
    ``INSUFFICIENT_BALANCE`` -- business outcomes a client acts on, answering
    400 to which tells it that it sent something wrong.
    """
    unexplained = [
        code for code in ErrorCode
        if code not in DEFAULT_HTTP_STATUS
        and code not in POLICY_DENIAL_CODES
        and code not in BUSINESS_OUTCOMES
        and code not in UNCLASSIFIED_BY_DESIGN
    ]
    assert not unexplained, f"unmapped error codes: {[c.value for c in unexplained]}"


@pytest.mark.parametrize("code", [
    ErrorCode.INSUFFICIENT_BALANCE,
    ErrorCode.OUT_OF_STOCK,
    ErrorCode.PAYMENT_FAILED,
    ErrorCode.PAYMENT_STATUS_UNKNOWN,
    ErrorCode.MANDATE_REVOKED,
    ErrorCode.MANDATE_EXPIRED,
    ErrorCode.QUOTE_EXPIRED,
    ErrorCode.CAPABILITY_REPLAYED,
    ErrorCode.ESCALATION_TIMEOUT,
])
def test_business_outcomes_are_not_transport_errors(code):
    """A handled outcome is HTTP 200 with ``ok=false``.

    The payment came back short of funds; that is the system working, not the
    client misbehaving.
    """
    from app.contracts.common import http_status_for, is_business_outcome

    assert http_status_for(code) == 200
    assert is_business_outcome(code) is True


def test_the_three_classifications_do_not_overlap():
    assert not (BUSINESS_OUTCOMES & POLICY_DENIAL_CODES)
    assert not (BUSINESS_OUTCOMES & set(DEFAULT_HTTP_STATUS))
    assert not (POLICY_DENIAL_CODES & set(DEFAULT_HTTP_STATUS))


# ---------------------------------------------------------------------------
# 探针 2：policy_hash 有意不绑定身份
# ---------------------------------------------------------------------------

def test_policy_hash_is_a_rule_digest_not_an_identity_binding():
    """Two mandates with the same rules share a hash, whatever their ids.

    This is intentional and worth stating: the hash answers "are the rules I am
    looking at the ones that were executed?", not "whose mandate is this?".
    Identity is checked by the evaluator against the proposal, and authority
    comes from C holding the row.
    """
    from app.contracts import canonical_bytes, policy_hash

    rules = {"cap_per_transaction_cents": 30000, "allowed_merchants": ["m"]}
    reordered = {"allowed_merchants": ["m"], "cap_per_transaction_cents": 30000}
    assert policy_hash(rules) == policy_hash(reordered)
    assert canonical_bytes(rules) == canonical_bytes(reordered)


def test_identity_fields_are_not_in_the_hashed_policy():
    """Adding a principal or a timestamp would break hash stability for no gain."""
    policy_keys = set(Mandate.model_fields) - {
        "mandate_id", "version", "principal_id", "agent_id", "status", "currency",
        "valid_from", "expires_at", "shipping_address_id", "address_change_allowed",
        "canonical_policy", "policy_hash", "consent_event_id", "created_at",
    }
    assert policy_keys, "the mandate must carry enforceable rules"
    assert policy_keys <= set(MANDATE_RULE_FIELDS)


# ---------------------------------------------------------------------------
# 探针 3：Draft 到 Mandate 的字段差是设计，不是遗漏
# ---------------------------------------------------------------------------

def test_draft_and_mandate_differ_by_exactly_cs_obligations():
    """A supplies the rules; C supplies identity, time and the canonical form.

    Anything A could supply that C must own would be a boundary leak.
    """
    from app.contracts import MandateDraft

    draft_only = set(MandateDraft.model_fields) - set(Mandate.model_fields)
    mandate_only = set(Mandate.model_fields) - set(MandateDraft.model_fields)

    assert draft_only == {
        "ambiguities", "unsupported_conditions", "source_spans", "valid_for_seconds",
    }
    # C owns every one of these. A must not be able to set them.
    assert {
        "mandate_id", "version", "principal_id", "agent_id", "status",
        "canonical_policy", "policy_hash", "consent_event_id", "created_at",
        "valid_from", "expires_at", "currency",
    } <= mandate_only


# ---------------------------------------------------------------------------
# 探针 11 与 12：单据、决策与审计之间的链接
# ---------------------------------------------------------------------------

def test_a_decision_carries_everything_a_receipt_needs():
    """So C can build a receipt without re-reading the mandate mid-transaction."""
    needed = {
        "mandate_id", "mandate_version", "policy_hash", "quote_id", "quote_hash",
        "cash_total_cents", "currency", "observed_values", "applicable_limits",
        "payment_route_id",
    }
    assert needed <= set(PolicyDecision.model_fields)


def test_a_decision_does_not_carry_the_rail_so_a_receipt_may_omit_it():
    """``PaymentReceipt.rail`` is optional precisely because of this."""
    assert "rail" not in PolicyDecision.model_fields
    assert PaymentReceipt.model_fields["rail"].default is None


def test_audit_ids_are_attached_after_the_fact_not_inside_the_decision():
    """The decision is pure and cannot know the audit sequence it will be given.

    The link is recorded on the receipt and the event, not on the decision --
    a decision that carried an audit id could not be replayed.
    """
    assert "audit_event_id" not in PolicyDecision.model_fields
    assert DenialReceipt.model_fields["audit_event_id"].default is None
    assert PaymentReceipt.model_fields["audit_event_id"].default is None
    assert "event_id" in AuditEvent.model_fields


# ---------------------------------------------------------------------------
# 探针 9：空 RelaxHints 合法，语义由分类函数区分
# ---------------------------------------------------------------------------

def test_an_empty_relax_hint_set_is_legal_and_means_impossible():
    from app.contracts import RelaxHints, SearchResponse, classify_outcome, SearchOutcome

    empty = RelaxHints(hints=[], closest_candidates=[])
    response = SearchResponse(
        total_matches=0, returned=0, candidates=[], comparison=[],
        applied_constraints=HardConstraints(), applied_criteria=[],
        gaps=GapReport(), relax_hints=empty,
    )
    assert classify_outcome(response) is SearchOutcome.IMPOSSIBLE
