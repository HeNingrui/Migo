"""Reading the catalog, and saying what was found.

This is the half of a turn that touches B: running a search, answering a
question about what is already on screen, and turning "cheaper" into a number.
It is separated from the orchestrator because those four jobs share one
responsibility -- *presenting search outcomes* -- and because they were the bulk
of a class that had grown to hold eleven of them.

The contract with the caller is deliberately narrow: every method returns a
:class:`TurnOutcome`, or ``None`` to mean "this turn is not mine to answer".
``None`` is what keeps the decision to clarify in one place instead of being
duplicated into each handler.

What it never does: decide whether a purchase may happen. It reads.
"""

from __future__ import annotations

from pydantic import ValidationError

from app.agent import response_renderer as render
from app.agent.clients import SearchClient
from app.agent.session import AgentSession
from app.agent.turn import TurnOutcome
from app.contracts.agent import Ambiguity, AmbiguityKind, Clarification, ConstraintPatch
from app.contracts.common import AgentPhase, ErrorCode, NextAction
from app.contracts.product import HardConstraints
from app.contracts.search import SearchRequest, SearchResponse

#: How many alternatives to list when answering "is there anything else?".
MAX_ALTERNATIVES = 3

#: One cent below, one cent above: a direction turned into an exclusive bound.
_ONE_CENT = 1

#: A wider look, used when answering a question about what is on screen. The
#: truncated list the user saw is not enough to answer "any others?" from.
_WIDER_LIMIT = 20


class BrowsingTurns:
    """Searches, and the replies they produce."""

    def __init__(self, search: SearchClient) -> None:
        self._search = search

    # -- entry points -------------------------------------------------------

    def more(self, session: AgentSession, *, show_all=False) -> TurnOutcome:
        request = SearchRequest(constraints=session.constraints or HardConstraints(),
            preferences=session.soft_preferences(), limit=20 if show_all else 6)
        page_search = getattr(self._search, "search_page", None)
        if page_search is None:
            return TurnOutcome(message="当前搜索服务还不支持翻页。", phase=session.phase)
        response = page_search(request, exclude_product_ids=() if show_all else session.browse_seen_ids)
        if not response.candidates:
            # Keep the last visible list, so a rank still refers to that list.
            return TurnOutcome(message=(f"当前条件下共 {response.total_matches} 件商品，符合条件的商品已经看完。可以修改条件继续找。"
                                        if response.total_matches else "当前条件没有符合的商品，可以修改筛选条件。"),
                phase=session.phase, next_action=NextAction.SELECT_PRODUCT if session.last_results and session.last_results.candidates else NextAction.NONE,
                produced=["results"] if session.last_results else [], notes=["catalog_navigation"])
        if show_all:
            session.browse_seen_ids.clear()
        session.browse_limit = request.limit
        session.remember_results(response)
        outcome = self._present(response, ["catalog_navigation"])
        outcome.message = render.describe_results(response, limit=response.returned) + f"\n当前条件共 {response.total_matches} 件，累计已展示 {len(session.browse_seen_ids)} 件。" + ("还有其他商品，可以继续点“换一批”。" if len(session.browse_seen_ids) < response.total_matches else "已展示全部符合条件的商品。")
        return outcome

    def opening(self, session: AgentSession) -> TurnOutcome | None:
        """Cold start: show something, then ask.

        "我想买个耳机" gives nothing to filter on, and replying "what do you
        want?" is the least useful answer available -- it asks the user to
        compose a specification from nothing. Showing three products and asking
        for a reaction is easier to answer, and it demonstrates what the agent
        actually does.

        The ordering is stated, because with no stated preference there is no
        "best match" -- there is only a default, and saying which one it is
        keeps it from looking like a recommendation.
        """
        response = self._search.search(
            SearchRequest(constraints=HardConstraints(), limit=3)
        )
        if not response.candidates:
            return None  # nothing to show, so the caller should ask instead

        session.constraints = HardConstraints()
        session.remember_results(response)
        return TurnOutcome(
            message="\n".join([
                "还没说具体要求，先按价格从低到高给你看三款：",
                render.describe_results(response),
                "有特定的款式、预算或用途吗？说了我就按你的条件重新找。",
            ]),
            next_action=NextAction.ANSWER_QUESTION,
            phase=AgentPhase.SHOWING_PRODUCTS,
            produced=["results"],
            notes=["no requirements stated; showing the default listing"],
            clarification=Clarification(
                question="有特定的款式、预算或用途吗？",
                about=["max_price_cents"], blocking=False,
            ),
        )

    def answer_question(self, session: AgentSession) -> TurnOutcome | None:
        """Answer a question about the products instead of asking one back.

        "有别的颜色吗" is a question with a subject, and everything needed to
        answer it is already held: the products just shown and the criteria used.
        Turning that into "what are you looking for?" is the same defect as
        asking for a budget already given.

        ``None`` when nothing has been shown -- then the question genuinely has
        no subject and asking is correct.
        """
        if not session.last_results or not session.last_shown_product_ids:
            return None

        shown = session.last_results.candidates
        shown_ids = {c.product_id for c in shown}
        shown_models = {c.product.model for c in shown}

        # First: the user may already be looking at the answer. "Are there other
        # colours?" asked while two colours of one model are on screen is
        # answered by pointing at them, not by hunting for ones not yet shown.
        by_model: dict[str, list] = {}
        for candidate in shown:
            by_model.setdefault(candidate.product.model, []).append(candidate)
        already_varied = {m: cs for m, cs in by_model.items() if len(cs) > 1}

        wider = self._search.search(SearchRequest(
            constraints=session.constraints or HardConstraints(), limit=_WIDER_LIMIT,
        ))
        variants = [
            c for c in wider.candidates
            if c.product_id not in shown_ids and c.product.model in shown_models
        ]

        lines = [
            f"你现在看到的就有同一型号（{model}）的不同版本："
            + "、".join(c.product.variant for c in group) + "。"
            for model, group in already_varied.items()
        ]
        elaboration, offers_a_choice = self._elaborate(
            variants, wider, shown_ids, bool(already_varied)
        )
        lines += elaboration

        # The wider set replaces what is on screen, so a following "第二款"
        # resolves against what the user is now looking at.
        session.remember_results(wider)

        return TurnOutcome(
            message="\n".join(lines),
            next_action=(NextAction.SELECT_PRODUCT if offers_a_choice
                         else NextAction.ANSWER_QUESTION),
            phase=AgentPhase.SHOWING_PRODUCTS,
            produced=["results"],
            clarification=(
                Clarification(question="要换其中哪一款吗？", about=["product_id"],
                              blocking=False)
                if offers_a_choice else None
            ),
        )

    def search(
        self,
        patch: ConstraintPatch,
        *,
        is_new: bool,
        ambiguities: list[Ambiguity],
        session: AgentSession,
        preferences_changed: bool = False,
    ) -> TurnOutcome | None:
        """Apply a patch and present the result.

        ``None`` means the turn carries nothing to search on *and* has nothing
        to fall back to; the caller decides what to ask. A cold "我想买个耳机"
        arrives here with an empty patch and no criteria, which is what the
        opening exists for.

        ``preferences_changed`` is the one case where an empty patch still means
        "search again": the user described themselves rather than a product, so
        the *ordering* is what moved. Without it, a turn like "我平时通勤，喜欢轻
        一点的" would either be dropped or answered with the identical list it was
        a reaction to -- and the profile would silently change nothing.

        ``previous`` is an optional snapshot of what the user was looking at
        before this call, so a result that comes back empty does not erase the
        fact that there *was* a list. Without it, one empty search makes the next
        "太重了" unanswerable: there would be nothing left to be too heavy
        relative to.
        """
        base = HardConstraints() if is_new else (session.constraints or HardConstraints())
        previous = session.capture_results()
        notes: list[str] = []

        if patch.is_empty():
            resolved, explanation = self._resolve_direction(ambiguities, session)
            if resolved is not None:
                patch, notes = resolved, explanation
                base = session.constraints or HardConstraints()
            elif render.has_criteria(session.constraints) or preferences_changed:
                # Nothing new was said about *products*, but the profile moved --
                # or something is already on file and this is "show me again",
                # not "I do not understand you".
                base = session.constraints or HardConstraints()
                if render.has_criteria(base):
                    notes.append(
                        "（没有新条件，按之前记下的再看一遍："
                        f"{render.describe_constraints(base)}）"
                    )
                if preferences_changed:
                    notes.append("（按你说的使用情况重新排了一遍）")
            else:
                return None

        try:
            constraints = patch.apply_to(base)
        except ValidationError as exc:
            # The patch passed the contract but cannot be merged, which means a
            # value is valid in isolation and wrong in context. That is a parsing
            # failure rather than a server error: report it as a question instead
            # of letting a validation error end the conversation.
            return TurnOutcome(
                message="这次的条件我没能合并进去，可能是某个取值不在允许范围内。换个说法再说一次？",
                next_action=NextAction.ANSWER_QUESTION,
                phase=AgentPhase.AWAITING_REQUIREMENTS,
                notes=[f"constraint merge failed: {type(exc).__name__}: {exc}"[:200]],
                clarification=Clarification(
                    question="这次的条件没听懂，能换个说法再说一次吗？",
                    about=["message"], blocking=False,
                ),
            )

        response = self._run(constraints, session)
        session.constraints = constraints
        session.remember_results(response, previous=previous)
        return self._present(response, notes)

    # -- the parts ----------------------------------------------------------

    def _run(self, constraints: HardConstraints,
             session: AgentSession) -> SearchResponse:
        """Ask B, translating an unexpected failure into a named error.

        The request carries the user's own preferences as well as the hard
        constraints, and they arrive by two different routes on purpose:

        * ``constraints`` -- what the user said must be true of the product.
        * ``preferences`` -- derived from the profile (see
          :meth:`AgentSession.soft_preferences`), and able only to order the
          products that already qualify.

        Nothing that happens in this method can turn the second into the first.
        B decides the order; it does not decide eligibility, and it is told so by
        the shape of what it receives.
        """
        from app.errors import AgentError

        request = SearchRequest(
            constraints=constraints,
            preferences=session.soft_preferences(),
            limit=session.browse_limit,
        )
        session.browse_seen_ids.clear()
        try:
            return self._search.search(request)
        except AgentError:
            raise
        except Exception as exc:  # noqa: BLE001 - any other failure is B's
            raise AgentError(
                ErrorCode.SEARCH_UNAVAILABLE,
                "the search could not be completed",
                details={"exception": type(exc).__name__},
                retryable=True,
            ) from exc

    def _present(self, response: SearchResponse, notes: list[str]) -> TurnOutcome:
        """One place that decides what a set of results looks like."""
        lines = [render.describe_results(response, limit=response.returned)]
        for extra in (render.describe_relax_hints(response),
                      render.describe_comparison(response)):
            if extra:
                lines.append(extra)
        lines.extend(notes)
        message = "\n".join(line for line in lines if line)

        if response.candidates:
            return TurnOutcome(
                message=message, next_action=NextAction.SELECT_PRODUCT,
                phase=AgentPhase.SHOWING_PRODUCTS, produced=["results"],
                notes=list(notes),
            )

        # Nothing matched. The question is not "what do you want?" -- the user
        # already said. It is which of their own conditions to give up, and the
        # options come from B's hints rather than from A's judgement.
        return TurnOutcome(
            message=message, next_action=NextAction.ANSWER_QUESTION,
            phase=AgentPhase.AWAITING_REQUIREMENTS, produced=["results"],
            notes=list(notes),
            clarification=Clarification(
                question="要放宽其中哪一条吗？如果都不改，也可以直接说一个新的条件。",
                about=[h.field for h in (response.relax_hints.hints
                                         if response.relax_hints else [])],
                blocking=False,
            ),
        )

    def _elaborate(
        self, variants: list, wider: SearchResponse, shown_ids: set[str], already_varied: bool
    ) -> tuple[list[str], bool]:
        """What to say beyond "you are already looking at them".

        Returns the lines and whether they offer the user a choice, so the caller
        does not have to infer the next action from the wording. Sniffing the
        text for a question mark is the kind of shortcut that breaks the first
        time the copy changes.
        """
        if variants:
            lines = [f"另外还有 {len(variants)} 个同型号版本："]
            lines += [render.describe_candidate(i, c)
                      for i, c in enumerate(variants[:MAX_ALTERNATIVES], start=1)]
            lines.append("要换其中哪一款吗？")
            return lines, True

        if already_varied:
            return ["除此之外，同型号没有更多的版本了。"], True

        others = [c for c in wider.candidates if c.product_id not in shown_ids]
        if others:
            lines = [f"刚看的那几款没有别的版本。条件不变的话，还有 {len(others)} 款可以选："]
            lines += [render.describe_candidate(i, c)
                      for i, c in enumerate(others[:MAX_ALTERNATIVES], start=1)]
            return lines, True

        lines = ["按现在的条件，没有别的选择了。"]
        relaxation = render.describe_relax_hints(wider)
        if relaxation:
            lines.append(relaxation)
        return lines, False

    def _resolve_direction(
        self, ambiguities: list[Ambiguity], session: AgentSession
    ) -> tuple[ConstraintPatch | None, list[str]]:
        """Turn "cheaper" into a number, derived from what is on screen.

        Only reached when the parser reported a direction and no value, which it
        signals with an ``UNDERSPECIFIED`` ambiguity. The chosen number is
        returned as an explanation so the reply can state it: a derived budget
        the user never sees is a budget the user never agreed to.

        If there is nothing to derive from -- no results yet -- the honest answer
        is a question, not a guess.
        """
        kinds = {a.field for a in ambiguities if a.kind == AmbiguityKind.UNDERSPECIFIED}
        shown = session.last_results.candidates if session.last_results else []
        if not kinds or not shown:
            return None, []

        notes: list[str] = []
        values: dict[str, object] = {}

        if "max_price_cents" in kinds:
            highest = max(c.product.price_cents for c in shown)
            if highest <= _ONE_CENT:
                notes.append("（已经是能找到的最低价了）")
            else:
                values["max_price_cents"] = highest - _ONE_CENT
                notes.append(
                    f"（按「便宜点」，我把上限设成 {render.money(highest - _ONE_CENT)}，"
                    f"即低于刚看到的最高价 {render.money(highest)}；不对请直接说数字）"
                )

        if "max_wearing_weight_g" in kinds:
            weights = [c.product.wearing_weight_g for c in shown
                       if c.product.wearing_weight_g is not None]
            if weights:
                bound = max(min(weights) - 1, 1.0)
                values["max_wearing_weight_g"] = bound
                notes.append(f"（按「轻一点」，我把重量上限定为 {render.grams(bound)}）")

        if "min_battery_hours" in kinds:
            batteries = [c.product.battery_hours for c in shown
                         if c.product.battery_hours is not None]
            if batteries:
                bound = max(batteries) + 1
                values["min_battery_hours"] = bound
                notes.append(f"（按「续航久一点」，我把下限提到 {render.hours(bound)}）")

        if not values:
            return None, notes
        return ConstraintPatch(**values), notes


__all__ = ["BrowsingTurns", "MAX_ALTERNATIVES"]
