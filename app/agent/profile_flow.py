"""Reading the user, as opposed to reading what they want to buy.

Two kinds of turn land here, and both are about the person rather than the
catalogue:

* **A self-description.** "我主要在通勤时用，平时连手机和电脑，喜欢轻一点的"
  states no requirement and eliminates no product. It says what the user is
  like, which is the material soft preferences are derived from.
* **A rejection of what was recommended.** "太重了" / "我更在意续航" is not a new
  criterion either. It is feedback: something about the *user* was wrong, or the
  agent's reading of them was. Answering it by handing B a re-rank request would
  be inventing the requirement on the user's behalf.

Three rules shape this module.

**Nothing here can make a hard constraint.** Every function returns profile facts
or a :class:`~app.contracts.search.Criterion`, and a criterion has no vocabulary
for "eliminate". A user who says "我每天坐地铁上班" has described a commute, not
set a weight limit, and the type system is what stops the second reading -- see
:mod:`app.contracts.profile`.

**Explicit and inferred are different claims, and both are quotable.** A fact the
user stated keeps their words in ``evidence_quote`` and is marked ``EXPLICIT``.
Anything read *out of* those words is marked ``INFERRED`` and carries the quote it
was derived from, so the summary can say "（我推断的）" and the user can correct it
instead of arguing with a ranking whose reason is invisible.

**A rejection re-opens the requirement, it does not re-rank.** "太重" is answered
by asking which of the user's own conditions to give up -- the same conversation
``BrowsingTurns._resolve_direction`` already holds for "便宜点" -- and by recording
the preference that made the recommendation wrong. It is never answered by B
guessing.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.contracts.profile import (
    FactSource,
    ProfileFact,
    ProfileGap,
    UserProfile,
)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Use cases the catalog carries, in the words a user would actually use.
#: Values are ``Product.use_cases`` entries: the profile speaks the catalog's
#: vocabulary so that a stated use case can travel to B unchanged.
USE_CASE_WORDS: dict[str, tuple[str, ...]] = {
    "commute": ("通勤", "上下班", "坐地铁", "地铁", "公交", "commut", "subway", "metro"),
    "study": ("学习", "上课", "图书馆", "自习", "study", "school", "library"),
    "gaming": ("游戏", "打游戏", "手游", "电竞", "gaming", "game", "console"),
    "sports": ("运动", "跑步", "健身", "健身时", "sports", "running", "gym", "workout"),
    "calls": ("开会", "通话", "打电话", "会议", "电话", "calls", "meeting", "call"),
    "music": ("听歌", "音乐", "听音乐", "music", "songs"),
}

#: Devices, as phrases. ``game_console`` deliberately requires an explicit
#: console word ("游戏主机", "ps5", "switch") rather than sharing 游戏 with the
#: use case: owning a console and playing games are different facts, and
#: conflating them would let a use case become a device requirement.
DEVICE_WORDS: dict[str, tuple[str, ...]] = {
    "phone": ("手机", "電話", "电话", "iphone", "android", "安卓", "phone", "mobile"),
    "computer": ("电脑", "筆電", "笔记本", "台式", "mac", "macbook", "pc", "laptop",
                 "computer", "桌面"),
    "game_console": ("游戏主机", "主机", "ps5", "ps4", "playstation", "switch", "xbox",
                     "game console"),
    "tablet": ("平板", "ipad", "tablet"),
}

#: Words that make a device a **requirement** rather than a mention. Only these
#: turn a device into a clause of the authorisation; a bare mention ("平时连手机")
#: stays profile context. This is the line between describing your life and
#: authorising a filter, and it is drawn by the user's own words rather than by
#: the model's judgement.
#:
#: There is deliberately no bare "必须" here. "必须支持游戏主机" names the device
#: and the demand together; "必须有降噪" names no device at all, and a bare demand
#: word would let any sentence containing one become a device clause -- including
#: the ones the deterministic parser is careful to keep as requirements about
#: something else.
DEVICE_REQUIREMENT_WORDS: tuple[str, ...] = (
    "必须支持", "必须能", "必须连", "一定要支持", "一定要能", "只能连", "只支持",
    "只连", "一定要连", "支持游戏主机", "用于游戏主机", "接游戏主机",
    "must support", "must work with", "only works with", "has to support",
    "works with my",
)

#: Preference-shaped facts and the words that express them, per direction.
#: ``preferred`` means "I want more of the good direction"; ``avoided`` means the
#: user named the bad direction ("太重了").
PREFERENCE_WORDS: dict[str, dict[str, tuple[str, ...]]] = {
    "weight": {
        "preferred": ("轻一点", "轻便", "轻的", "轻巧", "不重", "越轻越好", "轻",
                      "重量", "佩戴舒适", "舒服", "lightweight", "light", "lighter",
                      "comfortable"),
        "avoided": ("太重", "好重", "太重了", "沉", "heavy", "too heavy"),
    },
    "anc": {
        "preferred": ("降噪", "主动降噪", "隔音", "安静", "noise cancelling",
                      "noise canceling", "anc"),
        "avoided": ("不需要降噪", "不用降噪", "no anc"),
    },
    "battery": {
        "preferred": ("续航", "电池", "耐用", "用得久", "电量", "battery",
                      "long battery"),
        "avoided": ("续航差", "电池不行", "short battery"),
    },
    "portability": {
        "preferred": ("便携", "好带", "随身", "装包里", "portable", "carry"),
        "avoided": ("不方便带", "不好带", "bulky"),
    },
    "audio_quality": {
        "preferred": ("音质", "好听", "声音好", "hifi", "sound quality", "audio"),
        "avoided": ("音质差", "声音不好", "bad sound"),
    },
}

#: Wearing style is a preference *about the user* ("我习惯入耳"), and it is an
#: enumerated fact rather than a direction -- there is no "more入耳". Kept out of
#: ``PREFERENCE_WORDS`` because the two are different shapes, and folding them
#: together would let a direction word land in a field that only holds styles.
WEARING_WORDS: dict[str, tuple[str, ...]] = {
    "in_ear": ("入耳式", "入耳", "in-ear", "in ear", "earbuds", "earbud"),
    "over_ear": ("头戴式", "头戴", "罩耳", "over-ear", "over ear", "headband"),
    "open_ear": ("开放式", "開放式", "挂耳", "open-ear", "open ear", "open back"),
}

#: Words that mark what follows as a **wish** rather than a demand. Needed here
#: rather than imported: this module has to tell "希望有降噪" from "必须降噪", and
#: it must not import the parser to do it. Beside it sit the words that confirm a
#: demand, because "比较重视降噪" is neither a wish-word nor a demand-word -- it is
#: an explicit statement of what the user cares about, and treating it as neutral
#: is what keeps it out of the hard constraints where it does not belong.
SOFT_CUES: tuple[str, ...] = (
    "最好", "希望", "想要", "如果能", "要是能", "有点", "尽量", "偏好", "倾向于",
    "ideally", "preferably", "would like", "nice to have", "if possible",
)

#: Words that make a preference explicit rather than a passing demand. Present
#: because ``_is_wished`` would otherwise read "比较重视降噪" as a demand: no wish
#: word precedes it, so the wished-vs-demanded comparison comes out equal and the
#: fact would be dropped. The comparative forms are listed separately because the
#: cue has to sit immediately before the dimension for the proximity rule to see
#: it: in "更在意续航" the word before 续航 is 在意, but the phrase that makes the
#: sentence a statement of concern is 更在意.
CARES_CUES: tuple[str, ...] = (
    "重视", "在意", "看重", "注重", "关心", "喜欢", "习惯于", "习惯用",
    "更在意", "更看重", "更重视", "更注重", "最在意", "最看重", "还是", "其实",
    "care about", "value", "matters to me",
)

#: Words that make a stated wish a **requirement** rather than a preference.
#: Shared with the search parser's own vocabulary in spirit: the profile never
#: applies them, it only avoids claiming an explicit preference for something the
#: user demanded (a demand is handled as a hard constraint by the parser).
REQUIREMENT_WORDS: tuple[str, ...] = (
    "必须", "一定要", "只能", "只买", "只要", "must", "only", "required",
)

#: Direction words that mean "the thing you showed me is wrong in this way".
#: These map onto the existing direction vocabulary, so the answer is the same
#: conversation "便宜点" already produces.
REJECTION_DIRECTIONS: dict[str, tuple[str, ...]] = {
    "max_wearing_weight_g": ("太重", "好重", "沉了", "太重了", "heavy", "too heavy"),
    "max_price_cents": ("太贵", "贵了", "超预算", "买不起", "too expensive", "too pricey"),
    "min_battery_hours": ("续航太短", "电池不行", "续航差", "short battery"),
}

#: Plain dissatisfaction, with no dimension named. The honest answer is to ask
#: what is wrong rather than to guess a dimension and move a bound.
REJECTION_WORDS: tuple[str, ...] = (
    "不喜欢", "都不喜欢", "都不太喜欢", "不满意", "不合适", "不要这些", "换一批",
    "换一个", "换几款", "换别的", "再看看别的", "没一个", "不合意", "都不是",
    "don't like", "not what i want", "something else", "none of these",
)

#: Which question to ask when the profile is thin, in the order worth asking.
#: Chosen because each answer changes the ranking: a use case orders by fit, a
#: weight or battery preference orders by measurement.
INTERVIEW_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("use_case", "你主要在什么场景用？通勤、学习、打游戏、运动，还是开会打电话？"),
    ("weight", "佩戴上你更在意轻便，还是可以接受重一点？"),
    ("anc", "降噪对你重要吗？"),
    ("battery", "续航重要吗？"),
)

#: Asking all of them at once is an interrogation. One question per turn, and
#: the turn that has enough stops asking.
MAX_QUESTIONS = 1


# ---------------------------------------------------------------------------
# Reading a self-description
# ---------------------------------------------------------------------------

def _quote(text: str, phrase: str) -> str:
    """The phrase as the user actually typed it, not as it was matched.

    Matching is case-insensitive, so ``iPhone`` matches ``iphone``. Quoting the
    lowercased form back to the user would be quoting something they did not
    write -- small, and exactly the kind of small that makes a transcript
    untrustworthy.
    """
    at = text.lower().find(phrase.lower())
    if at < 0:
        return phrase
    return text[at:at + len(phrase)]


def _find(text: str, table: dict[str, tuple[str, ...]]) -> list[tuple[str, str]]:
    """Every ``(value, matched phrase)`` in ``text``, first match per value."""
    lowered = text.lower()
    found: list[tuple[str, str]] = []
    for value, words in table.items():
        for word in words:
            if word in lowered:
                found.append((value, _quote(text, word)))
                break
    return found


def _requires(text: str) -> str | None:
    lowered = text.lower()
    for word in DEVICE_REQUIREMENT_WORDS:
        if word in lowered:
            return word
    return None


def softness_of(text: str, at: int, window: int = 24) -> bool:
    """Whether a wish, rather than a demand, governs position ``at``.

    Exposed because the deterministic parser answers the same question with its
    own ``_is_soft``, and the two cannot share an implementation: the parser
    imports this module, so this module cannot import the parser. A test asserts
    they agree on the same sentences, which is what keeps two copies of one rule
    from drifting into two rules.
    """
    nearby = text[max(0, at - window):at].lower()
    wished_at = max((nearby.rfind(w.lower()) for w in SOFT_CUES), default=-1)
    demanded_at = max((nearby.rfind(w.lower()) for w in REQUIREMENT_WORDS), default=-1)
    cares_at = max((nearby.rfind(w.lower()) for w in CARES_CUES), default=-1)
    if cares_at > max(wished_at, demanded_at):
        return True
    return wished_at > demanded_at


def _is_wished(text: str, at: int, window: int = 24) -> bool:
    """Internal alias; see :func:`softness_of` for the rule and its rationale."""
    return softness_of(text, at, window)


def extract_profile(text: str) -> UserProfile | None:
    """Read what a turn says about the user, or ``None`` if it says nothing.

    ``None`` rather than an empty profile, because the caller's question is
    "did this turn tell me about the person?" and an empty object answers it
    ambiguously. Every fact returned is either a use case, a device, or a
    preference -- never a number, and never a bound.
    """
    facts: list[ProfileFact] = []
    lowered = text.lower()

    for value, phrase in _find(text, USE_CASE_WORDS):
        facts.append(ProfileFact(
            field="use_case", value=value,
            source=FactSource.EXPLICIT, evidence_quote=phrase,
        ))

    for value, phrase in _find(text, DEVICE_WORDS):
        facts.append(ProfileFact(
            field="device", value=value,
            source=FactSource.EXPLICIT, evidence_quote=phrase,
        ))

    for value, words in WEARING_WORDS.items():
        phrase = next((w for w in words if w in lowered), None)
        if phrase is not None:
            facts.append(ProfileFact(
                field="wearing", value=value,
                source=FactSource.EXPLICIT, evidence_quote=phrase,
            ))
            break

    for field_name, directions in PREFERENCE_WORDS.items():
        for direction, words in directions.items():
            phrase = next((w for w in words if w in lowered), None)
            if phrase is None:
                continue
            at = lowered.find(phrase)
            # A demand is not a preference, and a wish is not a demand.
            # "必须有主动降噪" is a hard constraint and the search parser owns it;
            # recording it here as a soft preference would make one sentence mean
            # two different things. "最好有降噪" is the opposite mistake: it is a
            # preference, and the parser deliberately leaves the constraint unset
            # for it -- so this is the only place that fact is captured at all.
            if field_name == "anc" and not _is_wished(text, at):
                continue
            facts.append(ProfileFact(
                field=field_name, value=direction,
                source=FactSource.EXPLICIT, evidence_quote=_quote(text, phrase),
            ))
            break

    return UserProfile(facts=facts) if facts else None


def infer_from(profile: UserProfile, text: str) -> UserProfile:
    """Facts A works out rather than reads. Always marked ``INFERRED``.

    Kept deliberately small and mechanical. The temptation is to let a model
    reason "commutes daily, therefore cares about ANC", and that is exactly the
    step that must stay visible: the fact is recorded as inferred, with the words
    it came from, so the summary can label it and the user can delete it. A model
    that could mark its own output ``EXPLICIT`` would be able to put words in the
    user's mouth.
    """
    lowered = text.lower()
    inferred: list[ProfileFact] = []
    already = {f.field for f in profile.facts}

    commuting = any(w in lowered for w in USE_CASE_WORDS["commute"])
    if commuting and "anc" not in already and "降噪" not in lowered:
        # A commute is loud. That is a real inference and a weak one, which is
        # why it is marked as such and outranked by anything the user stated.
        inferred.append(ProfileFact(
            field="anc", value="medium", source=FactSource.INFERRED,
            evidence_quote=next(w for w in USE_CASE_WORDS["commute"] if w in lowered),
        ))

    return UserProfile(facts=inferred)


# ---------------------------------------------------------------------------
# Deciding what to do with a profile
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProfilePlan:
    """What the profile is missing, and the one question worth asking about it."""

    gap: ProfileGap
    question: str | None = None
    about: tuple[str, ...] = ()

    @property
    def wants_an_answer(self) -> bool:
        """Whether there is a question to put to the user."""
        return self.question is not None


def plan(profile: UserProfile | None) -> ProfilePlan:
    """Whether to ask about the user before searching for a product.

    A profile is useful but never mandatory: a user who states a budget and a
    form factor gets a search, because their requirement is answerable without
    knowing anything else about them. The interview exists for the turn that
    states *nothing* -- the cold "想买个耳机" -- where the alternative is showing
    a default listing and calling it a recommendation.
    """
    if profile is None or profile.is_empty():
        field_name, question = INTERVIEW_QUESTIONS[0]
        return ProfilePlan(gap=ProfileGap.EMPTY, question=question, about=(field_name,))
    if profile.gap() is ProfileGap.ONLY_INFERRED:
        # Every fact so far is A's guess. Ranking on guesses and presenting the
        # result as the user's preference is the failure this branch prevents.
        found = next_question(profile)
        if found is None:  # pragma: no cover - ONLY_INFERRED implies a gap
            return ProfilePlan(gap=ProfileGap.ONLY_INFERRED)
        field_name, question = found
        return ProfilePlan(gap=ProfileGap.ONLY_INFERRED, question=question,
                           about=(field_name,))
    return ProfilePlan(gap=ProfileGap.NONE)


def _unmentioned(profile: UserProfile | None) -> tuple[str, str] | None:
    """The next question about a dimension the turn never came near."""
    for field_name, question in INTERVIEW_QUESTIONS:
        if profile is None or profile.first(field_name) is None:
            return field_name, question
    return None


def _inferred_only(profile: UserProfile | None) -> tuple[str, str] | None:
    """The next question whose only answer so far is A's own guess."""
    if profile is None:
        return None
    for field_name, question in INTERVIEW_QUESTIONS:
        facts = profile.of(field_name)
        if facts and not any(f.source is FactSource.EXPLICIT for f in facts):
            return field_name, question
    return None


def next_question(profile: UserProfile | None) -> tuple[str, str] | None:
    """The next question worth asking, or ``None`` when there is nothing to ask.

    Two kinds, in order of value. A dimension the turn never came near is a real
    gap, so it comes first. A dimension A only *guessed* comes second: the guess
    is labelled on the summary and can be corrected there, but the one turn that
    would confirm it should still be offered -- treating a guess as an answer is
    how A ends up never asking about the thing it guessed wrong.
    """
    return _unmentioned(profile) or _inferred_only(profile)


def describe_profile(profile: UserProfile | None) -> str:
    """How the profile is shown back, with inferences labelled.

    Reads the profile's own :meth:`~app.contracts.profile.UserProfile.describe`
    for the wording and only *groups* the two kinds here, rather than formatting
    facts a second time. A profile that reads one way inside the contract and
    another way in a reply is two renderings of one fact, and they drift.

    The grouping is the part that matters to a user: a fact they stated is
    reported as theirs, and a fact A worked out is reported as A's, with the
    legend stated once rather than repeated per line. Being told "you care about
    noise cancelling" when you never said so is being told something false about
    yourself, and the fix is a label, not an argument.
    """
    if profile is None or profile.is_empty():
        return ""
    lines: list[str] = []
    stated = UserProfile(facts=profile.explicit())
    guessed = UserProfile(facts=profile.inferred())
    if stated.facts:
        lines.append(f"我按你说的记下：{stated.describe()}")
    if guessed.facts:
        lines.append(f"我自己推断的（带 * 的，不对请直接说）：{guessed.describe()}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rejection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rejection:
    """What a dissatisfied turn told us, and what to do about it.

    ``direction`` is a field name in the *constraint* namespace, not a bound.
    Turning it into a number is the existing, documented behaviour of
    ``BrowsingTurns._resolve_direction``, which derives it from what the user was
    actually shown and states the derived value in the reply -- so the user sees
    the number they are agreeing to rather than discovering it later.
    """

    profile: UserProfile
    direction: str | None = None
    understood: bool = True
    note: str = ""

    @property
    def asks_nothing_specific(self) -> bool:
        """Whether the user was unhappy without naming a dimension."""
        return self.direction is None


def looks_like_rejection(text: str) -> bool:
    """Whether this turn is complaining rather than describing or specifying.

    The distinction matters more than it looks. "比较喜欢轻一点的耳机" is a
    self-description that happens to contain a direction word; "太重了" is a
    complaint about what was shown. Reading the first as the second answers a
    complaint the user never made, and reading the second as the first throws
    away the answer.

    So the signal is the complaint vocabulary itself -- an explicit grumble, a
    re-prioritisation, or plain dissatisfaction -- and never merely the absence
    of a number.
    """
    lowered = text.lower()
    if any(w in lowered for w in REJECTION_WORDS):
        return True
    if any(w in text for w in ("更在意", "更看重", "优先考虑", "优先")):
        return True
    for words in REJECTION_DIRECTIONS.values():
        if any(w in lowered for w in words):
            return True
    return False


def read_rejection(text: str) -> Rejection:
    """Read dissatisfaction with the recommendation.

    Never returns a hard constraint, and never returns a number. Two outcomes:
    a named dimension ("太重"), which is answerable from what was shown, or plain
    dissatisfaction ("都不喜欢"), which is answerable only by asking.
    """
    lowered = text.lower()
    facts: list[ProfileFact] = []
    direction: str | None = None

    for field_name, words in REJECTION_DIRECTIONS.items():
        phrase = next((w for w in words if w in lowered), None)
        if phrase is None:
            continue
        direction = field_name
        break

    # A complaint about a dimension is a preference the user just revealed, so it
    # is recorded -- as inferred, because "too heavy" is a reaction to what was
    # shown rather than a standing statement about themselves.
    if direction == "max_wearing_weight_g":
        facts.append(ProfileFact(field="weight", value="preferred",
                                 source=FactSource.INFERRED, evidence_quote=text[:40]))
    elif direction == "min_battery_hours":
        facts.append(ProfileFact(field="battery", value="high",
                                 source=FactSource.INFERRED, evidence_quote=text[:40]))
    # A complaint about price records no preference: there is no comparable
    # product attribute the profile could carry for it that would not be
    # re-stating the budget the user is about to give. Inventing one --
    # "portability: low" for "too expensive" -- is exactly the ungrounded
    # inference this module refuses to make.

    # Preferences the rejection implies beyond the dimension named: "我更在意续航"
    # is a re-prioritisation, and it is what makes the next ranking different
    # rather than just cheaper or lighter.
    if "更在意" in text or "更看重" in text or "优先" in text:
        for field_name, directions in PREFERENCE_WORDS.items():
            phrase = next((w for w in directions.get("preferred", ())
                           if w in lowered), None)
            if phrase is None:
                continue
            facts.append(ProfileFact(field=field_name, value="high",
                                     source=FactSource.EXPLICIT, evidence_quote=phrase))
            if direction is None:
                direction = {
                    "weight": "max_wearing_weight_g",
                    "battery": "min_battery_hours",
                    "anc": "anc",
                }.get(field_name)

    plain = next((w for w in REJECTION_WORDS if w in lowered), None)
    if direction is None and plain is None and not facts:
        return Rejection(profile=UserProfile(), understood=False,
                         note=f"unrecognised rejection: {text[:60]}")

    return Rejection(
        profile=UserProfile(facts=facts),
        direction=direction,
        note=(
            f"rejection understood as {direction}" if direction
            else "rejection with no dimension named"
        ),
    )


__all__ = [
    "DEVICE_REQUIREMENT_WORDS",
    "DEVICE_WORDS",
    "INTERVIEW_QUESTIONS",
    "MAX_QUESTIONS",
    "PREFERENCE_WORDS",
    "ProfilePlan",
    "REJECTION_DIRECTIONS",
    "REJECTION_WORDS",
    "Rejection",
    "USE_CASE_WORDS",
    "describe_profile",
    "extract_profile",
    "infer_from",
    "looks_like_rejection",
    "next_question",
    "plan",
    "read_rejection",
    "softness_of",
]
