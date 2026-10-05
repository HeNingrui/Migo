"""The user profile, and the soft preferences derived from it.

This module exists because two things that both look like "what the user wants"
are not the same thing, and conflating them is how an agent invents a spending
limit nobody agreed to:

* :class:`~app.contracts.product.HardConstraints` -- **what must be true of the
  product**. Violating one eliminates it. Only the user may state one.
* :class:`~app.contracts.search.PreferenceProfile` -- **how to choose among the
  products that already qualify**. It orders survivors; it never eliminates.

The profile sits between them and is the reason the line can be held. It records
what the user *is like* -- how they commute, what they plug into, what they care
about -- and from that A derives preferences. Everything here is either
``EXPLICIT`` (the user said it, and the quote is kept) or ``INFERRED`` (A worked
it out from something else). A soft preference derived from either one is still
soft: **nothing in this module can produce a hard constraint.** That is not a
convention, it is the shape of the types -- :func:`derive_soft_preferences`
returns :class:`~app.contracts.search.Criterion` objects, and a criterion has no
way to express "eliminate".

Why provenance is stored rather than implied: a later turn, a reviewer or the
user themselves may ask "why does it think I care about weight?". Answering
"the model decided" is not an answer. ``evidence_quote`` is the user's own words,
and ``source`` says whether those words stated the preference or only supported
inferring it.

What is deliberately absent: money. No balance, no cap, no mandate, no payment
route. Those are C's, and a profile that carried them would be a second, stale
copy of an authority A does not have -- see the same rule in
:mod:`app.agent.session`.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .product import DEVICES, Device, UseCase
from .search import Criterion, CriterionAttribute

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: What the profile is allowed to have an opinion about. Chosen so that every
#: entry either maps onto a ``Product`` field or onto a use case the catalog
#: actually carries -- a profile dimension nothing can answer is a question the
#: user should never have been asked.
ProfileField = Literal[
    "use_case",        # commute / study / gaming / sports / calls / music
    "device",          # phone / computer / game_console / tablet
    "wearing",         # prefers in-ear, over-ear, open-ear
    "weight",          # cares about how light it is
    "anc",             # cares about noise cancelling
    "battery",         # cares about battery life
    "portability",     # cares about carrying it around
    "audio_quality",   # cares about how it sounds
]

#: The devices a profile may record. The catalog does not carry a device field
#: today (see ``docs/A_C_contract_changes.md``, CC-10), so this is profile
#: context for ranking and for asking a better question -- never a product fact.
#:
#: Aliased rather than re-listed: two lists of devices that must agree and are
#: not checked against each other is a drift surface, and the profile and the
#: mandate have to mean the same thing by the word "phone".
ProfileDevice = Device

PROFILE_FIELDS: tuple[str, ...] = (
    "use_case", "device", "wearing", "weight", "anc", "battery",
    "portability", "audio_quality",
)

PROFILE_DEVICES: tuple[str, ...] = DEVICES

PROFILE_USE_CASES: tuple[str, ...] = (
    "commute", "study", "gaming", "sports", "calls", "music",
)

PROFILE_WEARING: tuple[str, ...] = ("in_ear", "over_ear", "open_ear")

#: The values a preference-shaped field may hold. ``preferred``/``avoided`` is
#: how a user's own words read; the three ordinals are for when they ranked it.
PROFILE_PREFERENCE_VALUES: tuple[str, ...] = (
    "preferred", "avoided", "high", "medium", "low",
)

#: field -> the values it can hold. Checked here rather than by eight separate
#: models, and checked at all because a profile value nothing can answer would
#: travel all the way to a criterion before anyone noticed.
_VALUE_DOMAINS: dict[str, tuple[str, ...]] = {
    "use_case": PROFILE_USE_CASES,
    "device": PROFILE_DEVICES,
    "wearing": PROFILE_WEARING,
    "weight": PROFILE_PREFERENCE_VALUES,
    "anc": PROFILE_PREFERENCE_VALUES,
    "battery": PROFILE_PREFERENCE_VALUES,
    "portability": PROFILE_PREFERENCE_VALUES,
    "audio_quality": PROFILE_PREFERENCE_VALUES,
}

#: Values for the preference-shaped fields. An ordinal rather than a number:
#: these are the user's own words, and turning "挺在意" into 0.7 would invent a
#: precision nobody supplied.
PreferenceLevel = Literal["low", "medium", "high"]


class FactSource(str, Enum):
    """Where one profile fact came from.

    ``EXPLICIT`` means the user said it and :attr:`ProfileFact.evidence_quote`
    holds the words. ``INFERRED`` means A derived it from something else --
    including from another explicit fact -- and the quote, when present, is the
    evidence it was derived *from*, not a statement of this fact.
    """

    EXPLICIT = "EXPLICIT"
    INFERRED = "INFERRED"


class ProfileGap(str, Enum):
    """Why a profile is not usable for ranking yet.

    A stated reason rather than a bare boolean, because the two cases lead to
    different questions: ``EMPTY`` means the user has told us nothing and the
    interview should start; ``ONLY_INFERRED`` means A has a guess with nothing
    behind it and the honest move is to ask the one question that would confirm
    it rather than to rank on a guess.
    """

    NONE = "NONE"
    EMPTY = "EMPTY"
    ONLY_INFERRED = "ONLY_INFERRED"


# ---------------------------------------------------------------------------
# The profile itself
# ---------------------------------------------------------------------------

class ProfileFact(BaseModel):
    """One thing the profile holds about the user, with its provenance.

    ``value`` is typed loosely on purpose: a use case is a word, a preference is
    an ordinal. Constraining it per field would need eight models to say what one
    validator says, and the validator can also check the two things that matter
    -- that a value is one the field can hold, and that an explicit fact has the
    user's words behind it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: ProfileField
    value: str = Field(min_length=1)

    source: FactSource

    #: The slice of the user's own words. Required for ``EXPLICIT``: a fact
    #: attributed to the user must be quotable back to them.
    evidence_quote: str | None = None

    @model_validator(mode="after")
    def _explicit_is_quotable(self) -> "ProfileFact":
        stated = self.evidence_quote or ""
        if self.source is FactSource.EXPLICIT and not stated.strip():
            raise ValueError(
                "an EXPLICIT fact must carry the user's own words in evidence_quote"
            )
        allowed = _VALUE_DOMAINS[self.field]
        if self.value not in allowed:
            raise ValueError(
                f"profile field {self.field!r} cannot hold {self.value!r}; "
                f"allowed: {list(allowed)}"
            )
        return self


class UserProfile(BaseModel):
    """What A understands about the user, and how it knows.

    Mutable, unlike the contract types that carry authority: a profile is a
    working understanding that the next turn is allowed to correct. Nothing here
    is signed, hashed or submitted to C, so immutability would buy nothing.

    A profile may hold more than one fact for the same field -- "main use:
    commute" and "also uses it for calls" are both true -- except for the
    preference-shaped fields, where two values would be a contradiction rather
    than a richer picture.
    """

    model_config = ConfigDict(extra="forbid")

    facts: list[ProfileFact] = Field(default_factory=list)

    @model_validator(mode="after")
    def _at_most_one_preference_per_field(self) -> "UserProfile":
        seen: set[str] = set()
        for fact in self.facts:
            if fact.field in ("use_case", "device"):
                continue  # several are expected
            if fact.field in seen:
                raise ValueError(
                    f"profile names {fact.field!r} twice; a preference has one value"
                )
            seen.add(fact.field)
        return self

    # -- reading -----------------------------------------------------------

    def of(self, field: str) -> list[ProfileFact]:
        """Every fact recorded about ``field``, in the order they were added."""
        return [f for f in self.facts if f.field == field]

    def first(self, field: str) -> ProfileFact | None:
        """The fact a single-valued field holds, or ``None``."""
        found = self.of(field)
        return found[0] if found else None

    def explicit(self) -> list[ProfileFact]:
        """The facts the user stated, each carrying their own words."""
        return [f for f in self.facts if f.source is FactSource.EXPLICIT]

    def inferred(self) -> list[ProfileFact]:
        """The facts A worked out, each carrying the words it came from."""
        return [f for f in self.facts if f.source is FactSource.INFERRED]

    def use_cases(self) -> list[UseCase]:
        """The use cases the user stated, as the contract's own vocabulary.

        Only ``EXPLICIT`` facts and only the catalog's six use cases: an inferred
        use case is A's guess about the user, and handing a guess to B as a
        *search criterion* is how a guess becomes a filter.
        """
        allowed = {"commute", "study", "gaming", "sports", "calls", "music"}
        found: list[UseCase] = []
        for fact in self.facts:
            if fact.field != "use_case" or fact.source is not FactSource.EXPLICIT:
                continue
            if fact.value in allowed and fact.value not in found:
                found.append(fact.value)  # type: ignore[arg-type]
        return found

    def is_empty(self) -> bool:
        """Whether anything at all is known about the user."""
        return not self.facts

    def gap(self) -> ProfileGap:
        """Whether this profile can rank anything, and if not, why."""
        if not self.facts:
            return ProfileGap.EMPTY
        if not self.explicit():
            return ProfileGap.ONLY_INFERRED
        return ProfileGap.NONE

    # -- writing -----------------------------------------------------------

    def merge(self, incoming: "UserProfile") -> "UserProfile":
        """Fold new facts in, newest wins for the single-valued fields.

        A multi-valued field accumulates without duplicates; a preference-shaped
        field is replaced, because the user saying "actually battery matters more
        than weight" is a correction rather than a second opinion.
        """
        merged: dict[str, list[ProfileFact]] = {}
        for fact in self.facts:
            merged.setdefault(fact.field, []).append(fact)

        for fact in incoming.facts:
            if fact.field in ("use_case", "device"):
                existing = merged.setdefault(fact.field, [])
                if not any(
                    f.value == fact.value and f.source is fact.source for f in existing
                ):
                    existing.append(fact)
            else:
                merged[fact.field] = [fact]

        ordered: list[ProfileFact] = []
        for field in PROFILE_FIELDS:
            ordered.extend(merged.get(field, []))
        return UserProfile(facts=ordered)

    def describe(self) -> str:
        """A one-line summary of these facts, in the user's language.

        Facts A inferred carry a trailing ``*``, because a summary that mixes what
        the user said with what A guessed -- without saying which is which -- is
        the same defect as quoting a model's guess as a requirement.
        """
        if not self.facts:
            return "（还没有你的使用情况）"
        parts = [f"{describe_fact(f)}{'*' if f.source is FactSource.INFERRED else ''}"
                 for f in self.facts]
        return "、".join(parts)


# ---------------------------------------------------------------------------
# Soft preferences
# ---------------------------------------------------------------------------

#: Which profile field steers which comparable product attribute, and in which
#: direction. Only attributes the catalog can actually evaluate appear here:
#: ``portability`` and ``audio_quality`` are profile dimensions but not product
#: fields, so they influence how the user is asked and how candidates are
#: explained -- never a criterion B would have to guess at.
PROFILE_TO_ATTRIBUTE: dict[str, tuple[CriterionAttribute, str]] = {
    "weight": ("wearing_weight_g", "lower"),
    "battery": ("battery_hours", "higher"),
    "anc": ("anc", "higher"),
}

#: An explicit fact is the user's own priority; an inferred one is a guess and
#: must not outrank something the user actually said.
_PRIORITY_BY_SOURCE: dict[FactSource, str] = {
    FactSource.EXPLICIT: "high",
    FactSource.INFERRED: "medium",
}


def derive_soft_preferences(profile: UserProfile) -> list[Criterion]:
    """Turn a profile into ranked criteria. Pure, deterministic, and soft.

    The result is handed to B as ``PreferenceProfile.criteria``, whose only power
    is to order products that already satisfy every hard constraint. There is no
    code path from here to ``HardConstraints``, which is the property the design
    rests on: a user who says "我经常通勤" has described their life, not set a
    weight limit.

    Ordering: explicit facts first, then inferred, each in the profile's own
    field order. B is told the order matters and must not re-sort it, so this is
    the user's stated priority expressed as a list rather than as weights nobody
    could justify.
    """
    derived: list[Criterion] = []
    seen: set[str] = set()

    for fact in profile.facts:
        mapping = PROFILE_TO_ATTRIBUTE.get(fact.field)
        if mapping is None:
            continue
        attribute, direction = mapping
        if attribute in seen:
            continue
        # "avoided" inverts the natural direction: the user wants less of it.
        if fact.value == "avoided":
            direction = {"lower": "higher", "higher": "lower"}[direction]
        elif fact.value not in ("preferred", "high", "medium", "low"):
            continue
        seen.add(attribute)
        derived.append(Criterion(
            attribute=attribute,
            direction=direction,  # type: ignore[arg-type]
            priority=_PRIORITY_BY_SOURCE[fact.source],  # type: ignore[arg-type]
            source="explicit" if fact.source is FactSource.EXPLICIT else "inferred",
            evidence_quote=fact.evidence_quote,
        ))

    # ``wearing`` and ``portability`` are profile dimensions with no comparable
    # product attribute: the catalog carries a form factor and a weight, not a
    # "how portable is it" field. They shape which questions are asked and how
    # results are explained, and produce no criterion -- a criterion B cannot
    # evaluate would come back as a gap at best and a guess at worst.
    derived.sort(key=lambda c: 0 if c.source == "explicit" else 1)
    return derived


def preference_profile_from(
    profile: UserProfile, *, base_criteria: list[Criterion] | None = None,
    priority_preset: str = "best_match",
):
    """Build the B-facing preference profile from a profile plus stated criteria.

    ``base_criteria`` are criteria the user stated directly ("我就要最轻的").
    They keep their own place ahead of anything derived, because a stated
    criterion is more specific than a standing preference -- and a dimension
    named by both is kept once, with the stated version winning.
    """
    from .search import PreferenceProfile

    stated = list(base_criteria or [])
    stated_attributes = {c.attribute for c in stated}
    soft = [c for c in derive_soft_preferences(profile)
            if c.attribute not in stated_attributes]

    return PreferenceProfile(
        use_cases=profile.use_cases(),
        criteria=[*stated, *soft],
        priority_preset=priority_preset,  # type: ignore[arg-type]
    )


#: Field and value names as a person reads them. The contract's own vocabulary is
#: for code; a summary a user sees has to be in their language, and one table is
#: enough for every rendering of a profile.
_FIELD_LABEL: dict[str, str] = {
    "use_case": "用途",
    "device": "设备",
    "wearing": "佩戴形式",
    "weight": "重量",
    "anc": "降噪",
    "battery": "续航",
    "portability": "便携",
    "audio_quality": "音质",
}

_VALUE_LABEL: dict[str, str] = {
    "commute": "通勤",
    "study": "学习",
    "gaming": "游戏",
    "sports": "运动",
    "calls": "开会通话",
    "music": "听歌",
    "phone": "手机",
    "computer": "电脑",
    "game_console": "游戏主机",
    "tablet": "平板",
    "in_ear": "入耳式",
    "over_ear": "头戴式",
    "open_ear": "开放式",
    "preferred": "在意",
    "avoided": "不喜欢",
    "high": "很在意",
    "medium": "一般",
    "low": "不太在意",
}


def describe_fact(fact: ProfileFact) -> str:
    """One profile fact in the user's language, e.g. ``用途通勤``."""
    field_name = _FIELD_LABEL.get(fact.field, fact.field)
    return f"{field_name}{_VALUE_LABEL.get(fact.value, fact.value)}"


__all__ = [
    "FactSource",
    "PROFILE_DEVICES",
    "PROFILE_FIELDS",
    "PROFILE_TO_ATTRIBUTE",
    "PreferenceLevel",
    "ProfileDevice",
    "ProfileFact",
    "ProfileField",
    "ProfileGap",
    "UserProfile",
    "derive_soft_preferences",
    "describe_fact",
    "preference_profile_from",
]
