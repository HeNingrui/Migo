"""The deterministic parser: no network, no model, no surprises.

This is the parser that always works. It is not a competitor to the LLM parser
and it is not expected to understand everything -- it is what runs when the
model is unreachable, rate-limited, or returns something the contract rejects,
and it is what makes the conversation testable, because a deterministic parser
can be tested exhaustively and a model cannot.

Design consequences of being the fallback:

* **It never guesses.** A phrase it does not recognise produces ``UNKNOWN`` with
  an ambiguity, not a plausible value. A wrong budget that looks confident is
  worse than a question.
* **It quotes itself.** Every value it extracts records the substring it came
  from, so ``untraceable_fields()`` is empty by construction rather than by
  luck. The check that catches an invented number in a model's reply is
  satisfied trivially here -- which is the point of having a reference
  implementation to compare the model against.
* **The vocabulary is explicit.** Patterns live in module-level tables so that
  adding "主動降噪" or "有線" is a one-line change with no logic to re-derive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from app.agent import profile_flow
from app.contracts.agent import (
    Ambiguity,
    AmbiguityKind,
    ConstraintPatch,
    Intent,
    IntentResult,
    ParseSource,
    SessionContext,
)
from app.contracts.mandate import MandateDraft
from app.contracts.product import CONNECTIONS, FORM_FACTORS, USE_CASES
from app.contracts.profile import UserProfile

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Use-case words, both languages. Ordered longest-first at match time so
#: "主动降噪" cannot be shadowed by a shorter overlapping token.
USE_CASE_WORDS: dict[str, tuple[str, ...]] = {
    "commute": ("通勤", "上下班", "通学", "通學", "commut", "travel to work"),
    "study": ("学习", "學習", "自习", "自習", "读书", "讀書", "study", "studying", "revision"),
    "gaming": ("游戏", "遊戲", "打游戏", "打遊戲", "gaming", "gamer"),
    "sports": ("运动", "運動", "跑步", "健身", "sport", "running", "gym", "workout"),
    "calls": ("通话", "通話", "打电话", "打電話", "会议", "會議", "开会", "開會",
              "call", "calls", "meeting"),
    "music": ("音乐", "音樂", "听歌", "聽歌", "music", "listening"),
}

#: Both character sets, everywhere. The converter is not reliable enough to run
#: at parse time, and a missing traditional form is a silent misread rather than
#: an error -- "無線" simply becomes "no connection preference".
CONNECTION_WORDS: dict[str, tuple[str, ...]] = {
    "wireless": ("无线", "無線", "藍牙", "蓝牙", "wireless", "bluetooth", "bt"),
    "wired": ("有线", "有線", "wired", "3.5mm", "aux"),
}

FORM_FACTOR_WORDS: dict[str, tuple[str, ...]] = {
    "in_ear": ("入耳", "耳塞", "in-ear", "in ear", "earbud", "iem"),
    "over_ear": ("头戴", "頭戴", "罩耳", "over-ear", "over ear", "headband"),
    "open_ear": ("开放", "開放", "挂耳", "掛耳", "open-ear", "open ear",
                 "bone conduction"),
}

ANC_WORDS: tuple[str, ...] = (
    "主动降噪", "主動降噪", "降噪", "抗噪", "anc",
    "noise cancell", "noise-cancell", "noise cancel", "noise-cancel",
)

#: Words that mark a *wish* rather than a requirement. If one appears nearer to
#: the ANC word than any hard word does, the requirement is not set --
#: "最好有降噪" is not "必须降噪", and "ideally with ANC" is not "with ANC".
SOFT_WORDS: tuple[str, ...] = (
    "最好", "最好有", "希望", "尽量", "盡量", "如果", "可以的话", "可以的話",
    "ideally", "preferably", "if possible", "would be nice", "nice to have",
)

#: Words that mark a demand. They exist so that proximity can be compared
#: rather than absolute position: in "最好便宜点，必须降噪" the wish is about the
#: price and the demand is about ANC, and only the nearer word should count.
HARD_WORDS: tuple[str, ...] = (
    "必须", "必須", "一定要", "务必", "務必", "需要", "要有",
    "must", "need", "require", "have to", "has to",
)

#: Budget markers. The number is taken from the nearest preceding quantity.
MAX_PRICE_CUES: tuple[str, ...] = (
    "以内", "以內", "以下", "不超过", "不超過", "最多", "预算", "預算",
    "块以内", "塊以內", "元以内", "元以內",
    "under", "below", "less than", "no more than", "at most", "budget", "max",
    "up to",
)
MIN_PRICE_CUES: tuple[str, ...] = ("以上", "至少", "at least", "more than", "over")

#: A refinement that lowers the price without naming a number.
CHEAPER_WORDS: tuple[str, ...] = (
    "便宜", "便宜点", "便宜點", "再低", "低一点", "低一點",
    "cheaper", "less expensive", "lower price", "too expensive",
    "贵了", "貴了", "太贵", "太貴",
)
LIGHTER_WORDS: tuple[str, ...] = (
    "轻", "輕", "轻一点", "輕一點", "lighter", "less heavy", "too heavy", "重了",
)
LONGER_BATTERY_WORDS: tuple[str, ...] = (
    "续航", "續航", "电池", "電池", "battery", "longer life", "last longer",
)

#: "the second one", "第一款", "the cheapest"
RANK_WORDS: dict[str, int] = {
    "第一": 1, "第一款": 1, "第一个": 1, "第一个": 1, "first": 1,
    "第二": 2, "第二款": 2, "第二个": 2, "second": 2,
    "第三": 3, "第三款": 3, "第三个": 3, "third": 3,
    "第四": 4, "第四款": 4, "fourth": 4,
    "第五": 5, "第五款": 5, "fifth": 5,
}
CHEAPEST_WORDS: tuple[str, ...] = ("最便宜", "cheapest", "least expensive")
BEST_WORDS: tuple[str, ...] = ("最合适", "最合適", "最好那款", "best", "top pick")

#: Refinements that state a direction without a number. Kept as a named set
#: because both the parser and the ambiguity it emits depend on the membership.
_DIRECTION_KINDS: frozenset[str] = frozenset({"cheaper", "lighter", "longer battery"})

#: A rejection that names a dimension maps onto the direction vocabulary, so the
#: answer is the conversation that already exists for "便宜点" -- derive a bound
#: from what was actually shown, and say the derived number out loud. A rejection
#: with no dimension ("都不喜欢") maps to nothing and is answered with a question.
_REJECTION_KIND: dict[str, str] = {
    "max_price_cents": "cheaper",
    "max_wearing_weight_g": "lighter",
    "min_battery_hours": "longer battery",
}

#: The rules on which reading the user is worth doing. Set by *name* because the
#: bound methods are rebuilt on every access and identity comparison would be a
#: silent no-op.
_PROFILE_READING_RULES: frozenset[str] = frozenset({
    "_rule_rejection", "_rule_refinement", "_rule_question", "_rule_search",
    "_rule_browsing", "_rule_mandate",
})
#: Words that mean the user wants to shop, without saying what they want. This
#: is what separates "我想买个耳机" -- show them something -- from "你好" or
#: "随便看看" -- ask them something. "看看" is deliberately absent: it is an
#: acknowledgement, not a request.
SHOPPING_WORDS: tuple[str, ...] = (
    "耳机", "耳機", "headphone", "earphone", "earbud", "headset",
    "想买", "想買", "要买", "要買", "买个", "買個", "买一副", "買一副",
    "buy", "shopping", "looking for", "want a", "need a", "找一款", "找一副",
)
QUESTION_MARKERS: tuple[str, ...] = (
    "吗", "嗎", "呢", "?", "？", "有没有", "有沒有",
    "any ", "do you have", "is there", "are there", "can i get",
)

#: What a question has to be *about* to count as being about the products.
#: Together with a question marker this separates "有别的颜色吗" -- a question to
#: answer -- from "随便看看" -- a non-committal turn to clarify.
ALTERNATIVE_TOPICS: tuple[str, ...] = (
    "颜色", "顏色", "配色", "colour", "color",
    "款式", "型号", "型號", "版本", "variant",
    "别的", "別的", "其他", "另外", "还有别的", "還有別的",
    "other", "another", "different", "anything else",
)

# ---------------------------------------------------------------------------
# Authorisation vocabulary
# ---------------------------------------------------------------------------
#
# The model path has always been able to return a mandate draft; without these
# the deterministic path could not, so authorising was the one flow that stopped
# working the moment the model was unreachable. The plan's R6 is that the whole
# deterministic flow runs offline, and "the agent cannot be authorised offline"
# is not a flow.

#: What marks a turn as an instruction to *authorise* rather than to search.
#: Deliberately explicit: "不超过 300" is a search filter, and only one of these
#: words turns it into a spending limit.
MANDATE_WORDS: tuple[str, ...] = (
    "授权", "授權", "委托", "委託", "替我买", "替我買", "帮我买", "幫我買",
    "以后都", "以後都", "不用再问", "不用再問", "别再问", "別再問", "全权", "全權",
    "authorize", "authorise", "mandate", "standing instruction", "act on my behalf",
)

#: Signing the summary A just showed. Only read when a draft is on file, so a
#: bare "确认" during a search is not an instruction to spend.
CONFIRM_MANDATE_WORDS: tuple[str, ...] = (
    "确认授权", "確認授權", "就这样", "就這樣", "可以了", "没问题", "沒問題",
    "同意授权", "同意授權", "确认", "確認", "activate", "confirm the mandate", "sign it",
)

#: Withdrawing it. Distinct from cancelling an order, and the reply says so.
REVOKE_MANDATE_WORDS: tuple[str, ...] = (
    "撤销授权", "撤銷授權", "取消授权", "取消授權", "收回授权", "收回授權",
    "revoke", "cancel the mandate", "withdraw the mandate",
)

#: Running the delegated purchase on the product already chosen.
RUN_PURCHASE_WORDS: tuple[str, ...] = (
    "买吧", "買吧", "就买", "就買", "下单", "下單", "执行", "執行", "可以买", "可以買",
    "buy it", "go ahead", "place the order", "run it", "do it",
)

#: Answering an open escalation.
APPROVE_ESCALATION_WORDS: tuple[str, ...] = (
    "同意", "批准", "可以继续", "可以繼續", "确认购买", "確認購買", "approve", "yes",
)
REJECT_ESCALATION_WORDS: tuple[str, ...] = (
    "不要", "不买", "不買", "算了", "拒绝", "拒絕", "取消这笔", "取消這筆",
    "reject", "decline", "no thanks",
)

#: Asking what is left.
STATUS_WORDS: tuple[str, ...] = (
    "还有多少额度", "還有多少額度", "剩多少", "额度", "額度", "状态", "狀態",
    "balance", "how much is left", "status", "remaining",
)

#: A per-purchase limit. "每笔" and "单笔" are the words a user reaches for.
_PER_TRANSACTION_CUES: tuple[str, ...] = (
    "每笔不超过", "每筆不超過", "单笔不超过", "單筆不超過", "每笔", "每筆",
    "单笔", "單筆", "每次不超过", "每次不超過", "per purchase", "per transaction",
)

#: An authorisation deadline. Required by the draft, so it needs its own words:
#: a bare "7 天" is indistinguishable from a rolling window.
_VALIDITY_CUES: tuple[str, ...] = (
    "有效期", "有效期限", "valid for", "valid until", "lasts",
)


@dataclass(frozen=True)
class _Hit:
    """One recognised phrase, with where it was found."""

    value: object
    field: str
    span: str
    start: int


def _find_first(text: str, table: dict[str, tuple[str, ...]], field: str) -> list[_Hit]:
    """All table entries present in ``text``, as hits with their spans."""
    lowered = text.lower()
    hits: list[_Hit] = []
    for canonical, words in table.items():
        for word in words:
            index = lowered.find(word.lower())
            if index >= 0:
                hits.append(_Hit(canonical, field, text[index:index + len(word)], index))
                break
    return hits


def _any(text: str, words: tuple[str, ...]) -> str | None:
    lowered = text.lower()
    for word in words:
        index = lowered.find(word.lower())
        if index >= 0:
            return text[index:index + len(word)]
    return None


def _is_soft(text: str, at: int, window: int = 24) -> bool:
    """Whether a wish-word, rather than a demand, governs position ``at``.

    Proximity decides, not presence. The window has to be wide enough for
    "ideally with noise cancelling" -- where the wish sits thirteen characters
    before the ANC word -- and comparing nearest-wins is what keeps
    "最好便宜点，必须降噪" correct: the wish belongs to the price, the demand to
    ANC, and only the nearer word counts for each.
    """
    nearby = text[max(0, at - window):at].lower()
    soft_at = max((nearby.rfind(w.lower()) for w in SOFT_WORDS), default=-1)
    hard_at = max((nearby.rfind(w.lower()) for w in HARD_WORDS), default=-1)
    return soft_at > hard_at


# ---------------------------------------------------------------------------
# Money and duration
# ---------------------------------------------------------------------------

def _cents(raw: str, scale: str | None = None) -> int:
    """Parse a stated amount into integer cents.

    ``HK$300``, ``300蚊`` and ``300 元`` all mean 30000 cents. A trailing ``k`` or
    ``千`` means thousands of the major unit. Rounded to the nearest cent, because
    a float that reaches a cap comparison is a bug waiting for the wrong locale.
    """
    amount = float(raw)
    if scale:
        amount *= 1000
    return int(round(amount * 100))


#: A number followed by one of these is a measurement, not money. Without this,
#: "30 克以内" reads as a HK$30 budget -- and it did, until this table existed.
_NON_MONEY_AFTER = re.compile(
    r"\s*(?:g\b|gram|克|公斤|kg\b|小时|小時|hour|hrs?\b|h\b|分钟|分鐘|min|"
    r"mm\b|cm\b|m\b|天|day|年|month|个月|個月|%|％)",
    re.IGNORECASE,
)

_CURRENCY_BEFORE = re.compile(
    r"(?:hk\$|hkd|港币|港幣|港元|\$|元|块|塊|蚊|rmb|cny|人民币|人民幣)\s*$",
    re.IGNORECASE,
)

#: The same tokens, but anchored at the start -- "300 蚊以内" puts the currency
#: between the number and the cue, and an end-anchored pattern cannot see it.
_CURRENCY_AFTER = re.compile(
    r"^\s*(?:hk\$|hkd|港币|港幣|港元|\$|元|块|塊|蚊|rmb|cny|人民币|人民幣)",
    re.IGNORECASE,
)

#: What may sit between a bare number and its cue. Anything else means the
#: number belongs to another clause.
_FILLER = re.compile(r"[\s约大概左右上下\-~至到,，、]*")

_NUMBER_WITH_SCALE = re.compile(r"(\d+(?:\.\d+)?)\s*(k|K|千)?")

#: How far around a cue to look for its number. Enough for "预算大约是 500 港币左右",
#: small enough that a weight mentioned in another clause cannot be captured.
_CUE_WINDOW = 28


def _extract_price(text: str, cues: tuple[str, ...]) -> _Hit | None:
    """Find the amount a budget cue refers to, in either word order.

    Word order differs by language and there is no way around handling both:
    Chinese puts the number first ("300 以内"), English puts the cue first
    ("under HK$300"), and Chinese also has leading cues ("预算 500"). The number
    nearest the cue wins, and a number carrying a non-money unit is rejected --
    which is what keeps "30 克以内" from becoming a HK$30 budget.
    """
    lowered = text.lower()
    best: _Hit | None = None

    # Longest cue first, so "不超过" is preferred over a shorter overlapping word.
    for cue in sorted(cues, key=len, reverse=True):
        at = lowered.find(cue.lower())
        if at < 0:
            continue
        cue_end = at + len(cue)
        window_start = max(0, at - _CUE_WINDOW)
        window_end = min(len(text), cue_end + _CUE_WINDOW)
        window = text[window_start:window_end]

        for match in _NUMBER_WITH_SCALE.finditer(window):
            absolute_start = window_start + match.start()
            absolute_end = window_start + match.end()

            after = text[absolute_end:absolute_end + 8]
            before = text[max(0, absolute_start - 8):absolute_start]

            if _CURRENCY_BEFORE.search(before) or _CURRENCY_AFTER.match(after):
                pass  # "HK$300", "300 蚊"
            elif _NON_MONEY_AFTER.match(after):
                continue  # "30 克", "20 小时" -- a measurement, not money
            else:
                # A bare number beside a budget cue. Accept only when nothing but
                # filler sits between them, measured *excluding* the cue itself --
                # "300 以内" is the ordinary Chinese order and its gap is the cue.
                if absolute_end <= at:
                    gap = text[absolute_end:at]
                elif absolute_start >= cue_end:
                    gap = text[cue_end:absolute_start]
                else:  # pragma: no cover - a number cannot straddle its own cue
                    continue
                if not _FILLER.fullmatch(gap):
                    continue

            try:
                value = _cents(match.group(1), match.group(2))
            except ValueError:  # pragma: no cover - the regex guarantees a number
                continue
            else:
                span = text[absolute_start:cue_end if absolute_start < cue_end else absolute_end]
                candidate = _Hit(value, "max_price_cents", span.strip(), absolute_start)
                if best is None or abs(absolute_start - at) < abs(best.start - at):
                    best = candidate
        if best is not None:
            break  # the longest cue that produced anything wins

    return best


def _extract_weight_limit(text: str) -> _Hit | None:
    """``30 克以内`` -> ``max_wearing_weight_g = 30``."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:g|克|公克)\s*(?:以内|以下|under|below)?",
                      text, re.IGNORECASE)
    if not match:
        return None
    return _Hit(float(match.group(1)), "max_wearing_weight_g", match.group(0), match.start())


def _extract_battery_floor(text: str) -> _Hit | None:
    """``续航 20 小时以上`` -> ``min_battery_hours = 20``."""
    match = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:小时|小時|hours?|hrs?)\s*(?:以上|起|at least|\+)?",
        text, re.IGNORECASE)
    if not match:
        return None
    return _Hit(float(match.group(1)), "min_battery_hours", match.group(0), match.start())


# ---------------------------------------------------------------------------
# Authorisation clauses
# ---------------------------------------------------------------------------
#
# Each helper returns the value, the substring it came from, and where it was
# found -- the same three things every other extraction returns, because the
# traceability check reads them and an untraceable money value is refused.

#: How many seconds are in each unit a user might name. Weeks are two characters
#: shorter than a day in Chinese and one word in English, and both are common.
_UNIT_SECONDS: dict[str, int] = {
    "秒": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "分钟": 60, "分鐘": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "小时": 3600, "小時": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "天": 86400, "日": 86400, "day": 86400, "days": 86400,
    "周": 604800, "週": 604800, "week": 604800, "weeks": 604800,
}

_UNIT_PATTERN = "|".join(sorted((re.escape(u) for u in _UNIT_SECONDS), key=len, reverse=True))

#: A quantity of things rather than of money. Excluded from every amount match,
#: because "最多 3 件" reading as HK$3.00 is exactly the kind of silent misread a
#: spending limit must not have.
_QUANTITY_AFTER = r"(?!\s*(?:笔|筆|次|件|个|個|副|只|pairs?|items?|units?|times?))"

#: ``24 小时内总共不超过 500`` -> rolling window and rolling cap together.
_ROLLING = re.compile(
    r"(\d+(?:\.\d+)?)\s*(" + _UNIT_PATTERN + r")\s*(?:内|內|之内|之內)?\s*"
    r"(?:总共|總共|累计|累計|合计|合計|合共|一共|in total|total)?\s*"
    r"(?:不超过|不超過|最多|上限|at most|up to|no more than)?\s*"
    r"(?:hk\$|hkd|港币|港幣|港元|\$)?\s*"
    r"(\d+(?:\.\d+)?)\s*(k|K|千)?" + _QUANTITY_AFTER,
    re.IGNORECASE,
)

#: ``5 分钟最多 2 笔`` -> velocity window and velocity count.
_VELOCITY = re.compile(
    r"(\d+(?:\.\d+)?)\s*(" + _UNIT_PATTERN + r")\s*(?:内|內|之内|之內)?\s*"
    r"(?:最多|至多|不超过|不超過|at most|up to|no more than)?\s*"
    r"(\d+)\s*(?:笔|筆|次|purchases?|transactions?|times?)",
    re.IGNORECASE,
)

#: ``一共买 3 件`` -> the lifetime quantity limit.
_QUANTITY_TOTAL = re.compile(
    r"(?:一共|總共|总共|合计|合計|合共|最多买|最多買|total of|up to)\s*(?:买|買)?\s*"
    r"(\d+)\s*(?:件|个|個|副|只|pairs?|items?|units?)",
    re.IGNORECASE,
)

#: ``有效期 7 天`` and ``7 天内有效`` -> how long the authorisation lasts.
_VALIDITY = re.compile(
    r"(?:有效期|有效期限|有效|valid for|valid until|lasts)\s*(?:是|为|為)?\s*"
    r"(\d+(?:\.\d+)?)\s*(" + _UNIT_PATTERN + r")",
    re.IGNORECASE,
)
_VALIDITY_AFTER = re.compile(
    r"(\d+(?:\.\d+)?)\s*(" + _UNIT_PATTERN + r")\s*(?:内|內)?\s*有效",
    re.IGNORECASE,
)

#: ``超过 280 先问我`` -> the escalation threshold.
_ESCALATE = re.compile(
    r"(?:超过|超過|高于|高於|多于|多於|above|over|more than)\s*"
    r"(?:hk\$|hkd|港币|港幣|港元|\$)?\s*(\d+(?:\.\d+)?)\s*(k|K|千)?\s*"
    r"(?:块|塊|元|蚊)?\s*(?:的话|的話)?\s*(?:先|要|就)?\s*"
    r"(?:问我|問我|问我一声|問我一聲|确认|確認|approve|ask me|check with me)",
    re.IGNORECASE,
)

#: ``送到 addr_01`` / ``商家 demo_audio_store`` -> an identifier the user named.
_ADDRESS = re.compile(
    r"(?:送到|寄到|收货地址|收貨地址|地址|ship to|deliver to)\s*[:：]?\s*"
    r"([A-Za-z0-9][A-Za-z0-9_\-]{2,})",
    re.IGNORECASE,
)
_MERCHANT = re.compile(
    r"(?:商家|商户|商戶|merchant)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9_\-]{2,})"
    r"|(?:从|從|在)\s*([A-Za-z0-9][A-Za-z0-9_\-]{2,})\s*(?:买|買|购买|購買|buy)",
    re.IGNORECASE,
)


def _unit_seconds(unit: str) -> int:
    return _UNIT_SECONDS[unit.lower()] if unit.lower() in _UNIT_SECONDS else _UNIT_SECONDS[unit]


def _scaled_seconds(raw: str, unit: str) -> int:
    """``("1.5", "小时")`` -> ``5400``. Fractional hours are ordinary speech."""
    return int(round(float(raw) * _unit_seconds(unit)))


def _first_word(text: str, words: tuple[str, ...]) -> str | None:
    """The words from ``words`` present in ``text``, longest match first.

    Longest first so that "确认授权" is preferred over the "确认" inside it: the
    span recorded in ``source_spans`` is shown to the user as the evidence for
    the value, and quoting one character of a two-word phrase is not evidence.
    """
    lowered = text.lower()
    for word in sorted(words, key=len, reverse=True):
        if word in lowered:
            return word
    return None


def _amount(raw: str, scale: str | None) -> int:
    amount = float(raw)
    if scale:
        amount *= 1000
    return int(round(amount * 100))


def route_tokens(route_id: str) -> list[str]:
    """The words a user might type for a route identifier.

    ``fps_demo`` -> ``["fps"]``. ``demo`` is dropped because it is scaffolding
    rather than a rail: every route in the sandbox registry carries it, so
    matching on it would make the word "demo" select whichever route came first.
    """
    return [part for part in route_id.lower().split("_") if part and part != "demo"]


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------

class FallbackIntentParser:
    """Deterministic reading of one turn. Implements ``IntentParser``."""

    source = ParseSource.FALLBACK

    def __init__(self, *, route_ids: Sequence[str] = ()) -> None:
        """``route_ids`` are the rails the commerce layer can settle on.

        Passed in rather than hard-coded because they are C's fact, not A's
        vocabulary: a route A invented would be a mandate clause that activates
        and then refuses every purchase. An empty tuple means the word for a rail
        is simply not recognised, and the user is asked.
        """
        self._route_ids = tuple(route_ids)

    def parse(self, text: str, context: SessionContext) -> IntentResult:
        """Read one turn. The first rule that applies wins.

        The rules are ordered by specificity -- a reference to something shown
        beats an answer to an open question, which beats a refinement, which
        beats a question, which beats a fresh search -- and each returns ``None``
        when it does not apply. Keeping the order in one tuple rather than in a
        chain of nested ``if``s means the precedence is visible at a glance and
        every rule can be tested on its own.

        The vocabulary is symmetric with the model's schema: both parsers must be
        able to express the same fields, or a turn that works on one path breaks
        on the other.
        """
        stripped = text.strip()
        if not stripped:
            # ``raw_text`` has min_length 1, so an empty turn cannot be
            # represented as an IntentResult at all. The caller validates
            # ChatRequest first; this is the last line of defence.
            return self._unknown(
                "(empty)", "the message was empty", "message",
                "What would you like to look for?",
            )

        for rule in (
            self._rule_reference,
            self._rule_escalation_answer,
            self._rule_mandate_confirmation,
            self._rule_revoke_mandate,
            self._rule_cancel_selection,
            self._rule_run_purchase,
            self._rule_status,
            self._rule_mandate,
            self._rule_rejection,
            self._rule_refinement,
            self._rule_question,
            self._rule_search,
            self._rule_browsing,
        ):
            outcome = rule(text, stripped, context)
            if outcome is not None:
                # Reading the user is worth doing on the turns that are about
                # choosing something. On a consent turn ("确认", "同意") the same
                # words come back verbatim and a profile built from them would be
                # an echo of A's own question rather than anything the user said.
                if rule.__name__ in _PROFILE_READING_RULES:
                    return self._with_profile(outcome, text)
                return outcome

        described = profile_flow.extract_profile(stripped)
        if described is not None:
            # Nothing to search on and nothing to change, but the user did say
            # something about themselves. A note, not a question: the next turn
            # is where the requirement comes from, and answering a description
            # with "what do you want to buy?" asks them to start over.
            return self._with_profile(IntentResult(
                intent=Intent.SEARCH,
                confidence=0.5,
                raw_text=text,
                parse_source=self.source,
                parser_note="a self-description with no requirement in it",
                search=ConstraintPatch(),
                search_is_new=False,
                source_spans={},
                profile=described,
            ), text)

        return self._unknown(
            text,
            "no recognisable requirement, budget or product reference",
            "message",
            "Could you say what you are looking for, and roughly what you want to spend?",
        )

    def _with_profile(self, result: IntentResult, text: str) -> IntentResult:
        """Attach what this turn said about the user, without changing anything else.

        Done here rather than in each rule because it is orthogonal to intent: a
        search, a refinement and a rejection can all carry a self-description, and
        a rule that forgot to look would silently drop it.

        Inferred facts are added beside the stated ones and are marked as such by
        :mod:`app.agent.profile_flow`. Nothing here may set a traceable field, so a
        profile can never satisfy -- or defeat -- the check for an invented budget.
        """
        stated = result.profile or profile_flow.extract_profile(text)
        if stated is None:
            stated = UserProfile()
        combined = stated.merge(profile_flow.infer_from(stated, text))
        if combined.is_empty():
            return result
        return result.model_copy(update={"profile": combined})

    # -- rules, in precedence order -----------------------------------------

    def _rule_reference(
        self, text: str, stripped: str, context: SessionContext
    ) -> IntentResult | None:
        """A pointer at something already shown: "第二款"."""
        rank = self._rank_reference(stripped, context)
        return self._select(text, rank) if rank is not None else None

    def _rule_escalation_answer(
        self, text: str, stripped: str, context: SessionContext
    ) -> IntentResult | None:
        """A yes or a no to a question C is holding open.

        Read only while an escalation is actually open, which is what keeps a
        bare "同意" during a search from being an authorisation to spend.
        """
        if not context.open_escalation_proposal_id:
            return None
        lowered = stripped.lower()
        if any(word in lowered for word in REJECT_ESCALATION_WORDS):
            return IntentResult(
                intent=Intent.REJECT_ESCALATION, confidence=0.8, raw_text=text,
                parse_source=self.source, parser_note="declining the open escalation",
                source_spans={"escalation": _first_word(stripped, REJECT_ESCALATION_WORDS)},
            )
        if any(word in lowered for word in APPROVE_ESCALATION_WORDS):
            return IntentResult(
                intent=Intent.APPROVE_ESCALATION, confidence=0.8, raw_text=text,
                parse_source=self.source, parser_note="approving the open escalation",
                source_spans={"escalation": _first_word(stripped, APPROVE_ESCALATION_WORDS)},
            )
        return None

    def _rule_mandate_confirmation(
        self, text: str, stripped: str, context: SessionContext
    ) -> IntentResult | None:
        """Signing the summary. Only meaningful with a draft on file.

        The draft is carried **empty**, and that is deliberate. Echoing the
        accumulated draft back would say this turn stated every number in it,
        which would make the confirmation look like an invented budget and trip
        the traceability check that exists to catch exactly that. What the turn
        means is "activate what is already on file", and the orchestrator reads
        the draft from the session rather than from the parse.
        """
        if context.current_mandate_draft is None:
            return None
        lowered = stripped.lower()
        if not any(word in lowered for word in CONFIRM_MANDATE_WORDS):
            return None
        return IntentResult(
            intent=Intent.ACTIVATE_MANDATE, confidence=0.9, raw_text=text,
            parse_source=self.source, parser_note="confirming the mandate summary",
            mandate=MandateDraft(),
            source_spans={"mandate": _first_word(stripped, CONFIRM_MANDATE_WORDS)},
        )

    def _rule_revoke_mandate(
        self, text: str, stripped: str, context: SessionContext
    ) -> IntentResult | None:
        word = _first_word(stripped, REVOKE_MANDATE_WORDS)
        if word is None:
            return None
        return IntentResult(
            intent=Intent.REVOKE_MANDATE, confidence=0.85, raw_text=text,
            parse_source=self.source, parser_note="withdrawing the authorisation",
            source_spans={"mandate": word},
        )

    def _rule_cancel_selection(self, text: str, stripped: str, context: SessionContext):
        if stripped.lower() not in {"取消当前选择", "取消这次购买", "先不买了", "cancel selection", "cancel this purchase"}:
            return None
        return IntentResult(intent=Intent.CANCEL_SELECTION, confidence=1.0, raw_text=text,
                            parse_source=self.source)

    def _rule_run_purchase(
        self, text: str, stripped: str, context: SessionContext
    ) -> IntentResult | None:
        """Executing the purchase for the product already chosen.

        Gated on an authorisation existing, because "买" on its own is a shopping
        request and only means "buy this one" once there is a mandate and a
        shortlist behind it. With a mandate but no selection the turn still
        reaches the purchase handler, which asks which product -- a better answer
        than "what are you looking for?" to somebody who just said "buy it".
        """
        if not (context.selected_product_id or context.has_active_mandate):
            return None
        lowered = stripped.lower()
        word = _first_word(stripped, RUN_PURCHASE_WORDS)
        if word is None:
            return None
        # "不要买" is a refusal, not an instruction to spend.
        if any(negative in lowered for negative in REJECT_ESCALATION_WORDS):
            return None
        return IntentResult(
            intent=Intent.RUN_DELEGATED_PURCHASE, confidence=0.85, raw_text=text,
            parse_source=self.source,
            parser_note="running the delegated purchase for the selected product",
            source_spans={"purchase": word},
        )

    def _rule_status(
        self, text: str, stripped: str, context: SessionContext
    ) -> IntentResult | None:
        word = _first_word(stripped, STATUS_WORDS)
        if word is None:
            return None
        return IntentResult(
            intent=Intent.CHECK_STATUS, confidence=0.7, raw_text=text,
            parse_source=self.source, parser_note="asking about the authorisation",
            source_spans={"status": word},
        )

    def _rule_mandate(
        self, text: str, stripped: str, context: SessionContext
    ) -> IntentResult | None:
        """Authorising the agent to spend, rather than asking it to find something.

        The distinction is a word, and it has to be: "不超过 300" filters a
        catalog and "每笔不超过 300" authorises a spending limit. Only one of
        ``MANDATE_WORDS`` turns the first into the second, so a search is never
        silently promoted into an authorisation.
        """
        marker = _first_word(stripped, MANDATE_WORDS)
        if marker is None and re.search(r"帮我找|幫我找|找.*耳机|找.*耳機|search|find|show me", stripped, re.I):
            return None
        if marker is None and not self._demands_a_device(stripped):
            return None
        # A sentence that demands a device is a clause of the authorisation even
        # when it names no spending limit: the search patch has nowhere to put a
        # device, so the mandate draft is the only place it would survive. The
        # marker stays optional here; every *money* clause still needs one.
        marker = marker or "必须支持"

        draft, spans = self._mandate_draft(stripped)
        spans["mandate"] = marker
        return IntentResult(
            intent=(Intent.UPDATE_MANDATE_DRAFT if context.current_mandate_draft
                    else Intent.CREATE_MANDATE),
            confidence=0.8,
            raw_text=text,
            parse_source=self.source,
            parser_note="a standing authorisation, read into a draft",
            mandate=draft,
            source_spans=spans,
        )

    def _mandate_draft(self, text: str) -> tuple[MandateDraft, dict[str, str]]:
        """The authorisation clauses this turn stated. Nothing is inferred.

        Every value recorded here was written in the sentence; a clause the user
        did not mention stays ``None`` so the caller asks about it. That is the
        same rule the model path follows, and it is what makes an invented
        budget impossible on either path.
        """
        spans: dict[str, str] = {}
        values: dict[str, object] = {}

        price = _extract_price(text, _PER_TRANSACTION_CUES)
        if price is not None:
            values["cap_per_transaction_cents"] = price.value
            spans["cap_per_transaction_cents"] = price.span

        rolling = _ROLLING.search(text)
        if rolling:
            values["rolling_window_seconds"] = _scaled_seconds(rolling.group(1),
                                                               rolling.group(2))
            values["rolling_cap_cents"] = _amount(rolling.group(3), rolling.group(4))
            spans["rolling_window_seconds"] = rolling.group(0)
            spans["rolling_cap_cents"] = rolling.group(0)

        velocity = _VELOCITY.search(text)
        if velocity:
            values["velocity_window_seconds"] = _scaled_seconds(velocity.group(1),
                                                                velocity.group(2))
            values["velocity_max_count"] = int(velocity.group(3))
            spans["velocity_window_seconds"] = velocity.group(0)
            spans["velocity_max_count"] = velocity.group(0)

        quantity = _QUANTITY_TOTAL.search(text)
        if quantity:
            values["max_quantity_total"] = int(quantity.group(1))
            spans["max_quantity_total"] = quantity.group(0)

        validity = _VALIDITY.search(text) or _VALIDITY_AFTER.search(text)
        if validity:
            values["valid_for_seconds"] = _scaled_seconds(validity.group(1),
                                                          validity.group(2))
            spans["valid_for_seconds"] = validity.group(0)

        escalate = _ESCALATE.search(text)
        if escalate:
            values["escalate_above_cents"] = _amount(escalate.group(1), escalate.group(2))
            spans["escalate_above_cents"] = escalate.group(0)

        connection = _find_first(text, CONNECTION_WORDS, "required_connection")
        if connection:
            values["required_connection"] = connection[0].value
            spans["required_connection"] = connection[0].span

        # The device the purchase must work with, and only when the user demanded
        # it. "平时连手机" is a fact about the user and lands in the profile; "必须
        # 支持游戏主机" is a clause of the authorisation, because C has to check it
        # against the product. The two sentences differ by a few characters and by
        # everything that matters, so the requirement words decide -- not the
        # presence of a device word.
        device_span = _any(text, profile_flow.DEVICE_REQUIREMENT_WORDS)
        if device_span is not None:
            device = _find_first(text, profile_flow.DEVICE_WORDS, "required_device")
            if device:
                values["required_device"] = device[0].value
                spans["required_device"] = f"{device_span}{device[0].span}"

        anc_span = _any(text, ANC_WORDS)
        if anc_span is not None and not _is_soft(text, text.lower().find(anc_span.lower())):
            values["anc_required"] = True
            spans["anc_required"] = anc_span

        routes = self._routes_in(text)
        if routes:
            values["allowed_payment_routes"] = routes
            spans["allowed_payment_routes"] = "、".join(routes)

        merchant = _MERCHANT.search(text)
        if merchant:
            values["allowed_merchants"] = [merchant.group(1) or merchant.group(2)]
            spans["allowed_merchants"] = merchant.group(0)

        address = _ADDRESS.search(text)
        if address:
            values["shipping_address_id"] = address.group(1)
            spans["shipping_address_id"] = address.group(0)

        return MandateDraft(**values), spans

    def _routes_in(self, text: str) -> list[str]:
        """Which of C's rails the user named, in the order they named them."""
        lowered = text.lower()
        found: list[tuple[int, str]] = []
        for route_id in self._route_ids:
            for token in route_tokens(route_id):
                at = lowered.find(token)
                if at >= 0:
                    found.append((at, route_id))
                    break
        return [route_id for _, route_id in sorted(found)]

    def _rule_rejection(
        self, text: str, stripped: str, context: SessionContext
    ) -> IntentResult | None:
        """Dissatisfaction with what was recommended, rather than a new criterion.

        Only reached once there is something to be dissatisfied *with*: on a cold
        turn "太重" has no subject and asking is the right answer.

        A turn carrying a number is not a rejection, which is why the number wins:
        "太贵了，200 以内" states a requirement, and reading it as a complaint
        would throw the budget away. That ordering already exists for refinements
        and is reused here rather than re-decided.
        """
        if not context.last_shown_product_ids:
            return None
        patch, _, _ = self._refinement(stripped)
        if patch is not None and not patch.is_empty():
            return None
        # A rejection is signalled by the words themselves, not by the absence of
        # a number. "比较喜欢轻一点的耳机" is a self-description that happens to
        # contain a direction word, and reading it as "your recommendation is too
        # heavy" would answer a complaint the user never made.
        if not profile_flow.looks_like_rejection(stripped):
            return None

        rejection = profile_flow.read_rejection(stripped)
        if not rejection.understood:
            return None
        return IntentResult(
            intent=Intent.REJECT_RECOMMENDATION,
            confidence=0.7,
            raw_text=text,
            parse_source=self.source,
            parser_note=rejection.note,
            profile=rejection.profile if not rejection.profile.is_empty() else None,
            ambiguities=(
                [self._direction_ambiguity(_REJECTION_KIND[rejection.direction])]
                if rejection.direction in _REJECTION_KIND else []
            ),
        )

    def _rule_refinement(
        self, text: str, stripped: str, context: SessionContext
    ) -> IntentResult | None:
        """A change to the criteria already on file."""
        patch, spans, kind = self._refinement(stripped)
        if patch is None:
            return None
        # A stated direction with no number is not a blocking problem, but it is
        # not nothing either: A still has to turn it into a value.
        ambiguities = (
            [self._direction_ambiguity(kind)]
            if kind in _DIRECTION_KINDS and patch.is_empty()
            else []
        )
        return IntentResult(
            intent=Intent.UPDATE_SEARCH,
            confidence=0.7,
            raw_text=text,
            parse_source=self.source,
            parser_note=f"deterministic refinement ({kind})",
            search=patch,
            search_is_new=False,
            source_spans=spans,
            ambiguities=ambiguities,
        )

    def _rule_question(
        self, text: str, stripped: str, context: SessionContext
    ) -> IntentResult | None:
        """A question about the products, which is answered rather than asked back."""
        if not self._is_alternative_question(stripped):
            return None
        return IntentResult(
            intent=Intent.ASK_ALTERNATIVES,
            confidence=0.6,
            raw_text=text,
            parse_source=self.source,
            parser_note="a question about the products already shown",
            search=ConstraintPatch(),
            search_is_new=False,
            source_spans={},
        )

    def _rule_search(
        self, text: str, stripped: str, context: SessionContext
    ) -> IntentResult | None:
        """Requirements were stated, so this is a search or a new one.

        One guard is worth naming: a sentence that *demands* a device ("必须支持
        游戏主机") states an authorisation clause, not a product filter, and the
        mandate rule has to be the one that reads it. Letting the search rule
        answer first would silently drop the requirement -- the search patch has
        nowhere to put it -- so the turn is left to the mandate rule, unless the
        sentence also states criteria the mandate draft cannot carry. Refusing
        *those* would lose a budget, which is the worse of the two losses, and
        the device clause then has to be stated again.
        """
        if self._demands_a_device(stripped):
            patch, _ = self._fresh_search(stripped)
            if patch is None or patch.is_empty():
                return None
        return self._search_result(text, stripped, context)

    def _demands_a_device(self, text: str) -> bool:
        """Whether this sentence demands the product work with a named device."""
        lowered = text.lower()
        if not any(w in lowered for w in profile_flow.DEVICE_REQUIREMENT_WORDS):
            return False
        return any(w in lowered
                   for words in profile_flow.DEVICE_WORDS.values() for w in words)

    def _search_result(
        self, text: str, stripped: str, context: SessionContext
    ) -> IntentResult | None:
        """The patch this sentence states, as a search or a refinement."""
        patch, spans = self._fresh_search(stripped)
        if patch is None or patch.is_empty():
            return None
        return IntentResult(
            intent=Intent.SEARCH,
            confidence=0.6,
            raw_text=text,
            parse_source=self.source,
            parser_note="deterministic search",
            search=patch,
            search_is_new=True,
            source_spans=spans,
        )

    def _rule_browsing(
        self, text: str, stripped: str, context: SessionContext
    ) -> IntentResult | None:
        """Wants to buy, has not said what.

        An empty patch with SEARCH is not "no answer": it says the user asked
        for something and specified nothing. The caller shows a starting point
        and then asks, which is more useful than asking into a silence.
        """
        if not self._wants_to_shop(stripped):
            return None
        return IntentResult(
            intent=Intent.SEARCH,
            confidence=0.4,
            raw_text=text,
            parse_source=self.source,
            parser_note="a shopping request with no requirements stated",
            search=ConstraintPatch(),
            search_is_new=True,
            source_spans={},
            ambiguities=[Ambiguity(
                kind=AmbiguityKind.UNDERSPECIFIED,
                field="message",
                detail="the user wants to buy but stated no requirement",
                question="有特定的款式、预算或用途吗？",
            )],
        )

    @staticmethod
    def _wants_to_shop(text: str) -> bool:
        lowered = text.lower()
        return any(word in lowered for word in SHOPPING_WORDS)

    # -- helpers ------------------------------------------------------------

    def _unknown(self, text: str, detail: str, field: str, question: str) -> IntentResult:
        return IntentResult(
            intent=Intent.UNKNOWN,
            confidence=0.0,
            raw_text=text,
            parse_source=self.source,
            parser_note=detail,
            ambiguities=[Ambiguity(
                kind=AmbiguityKind.MISSING, field=field, detail=detail, question=question,
            )],
        )

    def _rank_reference(self, text: str, context: SessionContext) -> int | None:
        """Which shown product the user means, if any."""
        lowered = text.lower()
        if any(w in lowered for w in CHEAPEST_WORDS):
            return 1  # results are shown cheapest-first only when asked; see below
        if any(w in lowered for w in BEST_WORDS):
            return 1
        for word, rank in RANK_WORDS.items():
            if word in lowered:
                return rank
        return None

    def _select(self, text: str, rank: int) -> IntentResult:
        from app.contracts.agent import TargetReference

        return IntentResult(
            intent=Intent.SELECT_PRODUCT,
            confidence=0.8,
            raw_text=text,
            parse_source=self.source,
            parser_note=f"deterministic reference to rank {rank}",
            target=TargetReference(kind="product", rank=rank),
        )

    @staticmethod
    def _is_alternative_question(text: str) -> bool:
        """Whether this is a question about the products rather than an instruction.

        Two signals have to be present. A question marker alone ("嗯？") is not a
        question about anything; a topic alone ("颜色好看") is not a question.
        Requiring both is what keeps "随便看看" -- which has neither -- on the
        clarify path, where it belongs.
        """
        lowered = text.lower()
        asks = any(marker in lowered for marker in QUESTION_MARKERS)
        about = any(topic in lowered for topic in ALTERNATIVE_TOPICS)
        return asks and about

    def _refinement(self, text: str) -> tuple[ConstraintPatch | None, dict[str, str], str]:
        """A change to the existing criteria, rather than a new search."""
        lowered = text.lower()

        # A number always wins over a direction word: "续航 20 小时以上" is a
        # requirement, and reading it as "the user wants longer battery" would
        # throw the 20 away.
        patch, spans = self._fresh_search(text)
        if patch is not None and not patch.is_empty():
            return patch, spans, "stated criteria"

        if any(w in lowered for w in CHEAPER_WORDS):
            return ConstraintPatch(), {}, "cheaper"
        if any(w in lowered for w in LIGHTER_WORDS):
            return ConstraintPatch(), {}, "lighter"
        if any(w in lowered for w in LONGER_BATTERY_WORDS):
            return ConstraintPatch(), {}, "longer battery"
        return None, {}, ""

    def _direction_ambiguity(self, kind: str) -> Ambiguity:
        """A refinement that states a direction but no number.

        ``UNDERSPECIFIED`` rather than ``MISSING`` on purpose: ``MISSING`` blocks
        the turn and forces a question, and this one should not. "cheaper" is
        answerable from the results already on screen -- A computes a concrete
        budget below the current cheapest -- so the turn proceeds and the
        ambiguity documents what A still has to resolve.
        """
        field, question = {
            "cheaper": (
                "max_price_cents",
                "How much lower would you like to go?",
            ),
            "lighter": (
                "max_wearing_weight_g",
                "What is the heaviest you would accept?",
            ),
            "longer battery": (
                "min_battery_hours",
                "How many hours of battery do you need?",
            ),
        }[kind]
        return Ambiguity(
            kind=AmbiguityKind.UNDERSPECIFIED,
            field=field,
            detail=f"the user asked for {kind} without giving a number",
            question=question,
        )

    def _fresh_search(self, text: str) -> tuple[ConstraintPatch | None, dict[str, str]]:
        spans: dict[str, str] = {}
        values: dict[str, object] = {}

        price = _extract_price(text, MAX_PRICE_CUES)
        if price is not None:
            values["max_price_cents"] = price.value
            spans["max_price_cents"] = price.span

        floor = _extract_price(text, MIN_PRICE_CUES)
        if floor is not None and floor.value != values.get("max_price_cents"):
            values["min_price_cents"] = floor.value
            spans["min_price_cents"] = floor.span

        for hit in _find_first(text, CONNECTION_WORDS, "connection"):
            values["connection"] = hit.value
            spans["connection"] = hit.span

        for hit in _find_first(text, FORM_FACTOR_WORDS, "form_factor"):
            values["form_factor"] = hit.value
            spans["form_factor"] = hit.span

        anc_span = _any(text, ANC_WORDS)
        if anc_span is not None:
            at = text.lower().find(anc_span.lower())
            if not _is_soft(text, at):
                values["anc_required"] = True
                spans["anc_required"] = anc_span
            else:
                # A stated wish is not a requirement. Recorded in the note, not
                # in the constraints -- an unknown spec never satisfies one.
                pass

        weight = _extract_weight_limit(text)
        if weight is not None:
            values["max_wearing_weight_g"] = weight.value
            spans["max_wearing_weight_g"] = weight.span

        battery = _extract_battery_floor(text)
        if battery is not None and any(
            w in text.lower() for w in ("续航", "續航", "电池", "電池", "battery", "小时", "小時", "hour")
        ):
            values["min_battery_hours"] = battery.value
            spans["min_battery_hours"] = battery.span

        colors = {"black": ("黑色", "黑的", "black"), "white": ("白色", "白的", "white"),
                  "blue": ("蓝色", "藍色", "blue"), "pink": ("粉色", "pink"),
                  "silver": ("银色", "銀色", "silver"), "grey": ("灰色", "grey", "gray"),
                  "green": ("绿色", "綠色", "green"), "red": ("红色", "紅色", "red"),
                  "purple": ("紫色", "purple"), "gold": ("金色", "gold")}
        for color, words in colors.items():
            for word in words:
                if (word in text.lower() if not word.isascii() else re.search(r"\b" + word + r"\b", text.lower())):
                    values["color"] = color
                    spans["color"] = word
                    break
        if any(word in text.lower() for word in ("颜色不限", "顏色不限", "不限颜色", "any color", "any colour")):
            values.pop("color", None)
            values["clear_color"] = True
        for device, words in {"phone": ("手机", "手機", "phone"), "computer": ("电脑", "電腦", "computer", "pc"),
                              "tablet": ("平板", "tablet"), "game_console": ("游戏机", "遊戲機", "console", "ps5", "xbox")}.items():
            for word in words:
                if word in text.lower() and any(cue in text.lower() for cue in ("适用", "適用", "兼容", "用于", "用於", "works with", "compatible", "for my")):
                    values["required_device"] = device
                    spans["required_device"] = word
        delivery = re.search(r"(?:最多|以内|within|under)?\s*(\d+)\s*(?:天|days?)\s*(?:内|以内)?\s*(?:送达|送到|到货|delivery|deliver)?", text, re.I)
        if delivery and any(cue in text.lower() for cue in ("送达", "送到", "到货", "配送", "deliver")):
            values["max_estimated_delivery_days"] = int(delivery.group(1))
            spans["max_estimated_delivery_days"] = delivery.group()
        tags = []
        for tag, words in USE_CASE_WORDS.items():
            for word in words:
                match = re.search(r"(?:必须适合|必須適合|必须支持|must support|required tag[: ]+)" + re.escape(word), text, re.I)
                if match:
                    tags.append(tag)
                    spans["tags"] = match.group()
                    break
        if tags:
            values["tags"] = sorted(set(tags))
        if any(word in text.lower() for word in ("不要求有货", "不限库存", "include out of stock")):
            values["in_stock_only"] = False
            spans["in_stock_only"] = text
        if any(word in text.lower() for word in ("仅显示有货", "只要有货", "in stock only")):
            values["in_stock_only"] = True
            spans["in_stock_only"] = text
        if not values:
            return None, {}
        return ConstraintPatch(**values), spans

    #: Exposed so callers can read the vocabulary without importing the tables.
    @staticmethod
    def vocabulary() -> dict[str, object]:
        return {
            "connections": sorted(CONNECTIONS),
            "form_factors": sorted(FORM_FACTORS),
            "use_cases": sorted(USE_CASES),
            "soft_words": list(SOFT_WORDS),
        }


__all__ = [
    "ALTERNATIVE_TOPICS",
    "ANC_WORDS",
    "APPROVE_ESCALATION_WORDS",
    "CHEAPER_WORDS",
    "CONFIRM_MANDATE_WORDS",
    "CONNECTION_WORDS",
    "FallbackIntentParser",
    "FORM_FACTOR_WORDS",
    "MANDATE_WORDS",
    "MAX_PRICE_CUES",
    "QUESTION_MARKERS",
    "REJECT_ESCALATION_WORDS",
    "REVOKE_MANDATE_WORDS",
    "RUN_PURCHASE_WORDS",
    "SOFT_WORDS",
    "STATUS_WORDS",
    "USE_CASE_WORDS",
    "route_tokens",
]
