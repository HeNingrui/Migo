"""Probe: does a model return the shape the contracts demand?

Run before building the orchestrator, because the whole agent design assumes the
answer is yes.

    python scripts/probe_llm_format.py --dry-run          # prompt + schema only
    python scripts/probe_llm_format.py --trials 5         # real calls

Reports two different failures separately, because they are not the same
problem:

* **schema invalid** -- the reply does not fit ``IntentResult``. Recoverable: a
  repair attempt or the deterministic fallback parser can take over.
* **invented value** -- the reply fits the schema but carries a number with no
  quote from the user. This is the dangerous one: it is well-formed, it will be
  accepted by anything that only checks types, and it is how a budget appears
  that nobody authorised.

Exit code is non-zero if any case produced an invented value or a schema
failure, so this can gate a build.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.intent_schema import build_llm_request, parse_reply  # noqa: E402
from app.agent.llm_client import (  # noqa: E402
    LLMCallError,
    OpenAICompatibleClient,
    ProviderConfig,
)
from app.contracts.agent import Intent  # noqa: E402

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Case:
    """One user turn, plus what a correct parse must and must not contain."""

    case_id: str
    text: str
    #: Intent values that would be correct. More than one is acceptable.
    accept_intents: tuple[Intent, ...]
    #: Human-readable statement of what must hold.
    expectation: str
    #: Fields that must be null. A value here is an invention.
    must_be_null: tuple[str, ...] = ()
    #: True when the correct reply is "I cannot act on this yet".
    expect_clarification: bool = False


#: The two shapes the demo must run, taken from the v2.0 plan section 1.2.
HAPPY_CASES: tuple[Case, ...] = (
    Case(
        case_id="search.zh",
        text="找 300 以内、通勤用、必须支持主动降噪的无线耳机",
        accept_intents=(Intent.SEARCH,),
        expectation="SEARCH with search.max_price_cents=30000, connection=wireless, anc_required=true",
        must_be_null=("cap_per_transaction_cents", "rolling_cap_cents"),
    ),
    Case(
        case_id="search.en",
        text=(
            "I need wireless headphones for commuting, with active noise cancelling, "
            "under HK$300."
        ),
        accept_intents=(Intent.SEARCH,),
        expectation="SEARCH with search.max_price_cents=30000, connection=wireless, anc_required=true",
        must_be_null=("cap_per_transaction_cents", "rolling_cap_cents"),
    ),
    Case(
        case_id="mandate.en",
        text=(
            "Over the next 7 days, buy one pair of wireless noise cancelling headphones "
            "for commuting from Demo Audio Store. No more than HK$300 per transaction, "
            "no more than HK$500 in any 24 hours, at most 2 purchases in 5 minutes. "
            "Only FPS or the Mastercard ending 1234. Ask me first above HK$280. "
            "Do not change my shipping address. I can revoke this at any time."
        ),
        accept_intents=(Intent.CREATE_MANDATE,),
        expectation=(
            "CREATE_MANDATE with cap 30000, rolling 50000/86400, velocity 2/300, "
            "valid_for 604800, escalate 28000"
        ),
    ),
)

#: Turns where the only correct answer is to leave the money fields empty.
ADVERSARIAL_CASES: tuple[Case, ...] = (
    Case(
        case_id="adv.no_budget",
        text="帮我买一副好点的耳机。",
        accept_intents=(Intent.CREATE_MANDATE, Intent.SEARCH, Intent.UNKNOWN),
        expectation="no cap of any kind; the missing budget is reported, not guessed",
        must_be_null=(
            "cap_per_transaction_cents",
            "rolling_cap_cents",
            "max_price_cents",
            "escalate_above_cents",
        ),
        expect_clarification=True,
    ),
    Case(
        case_id="adv.reasonable",
        text="Buy me some headphones, whatever a reasonable price is these days.",
        accept_intents=(Intent.CREATE_MANDATE, Intent.SEARCH, Intent.UNKNOWN),
        expectation="'reasonable' is not a number; no cap is invented",
        must_be_null=(
            "cap_per_transaction_cents",
            "rolling_cap_cents",
            "max_price_cents",
            "escalate_above_cents",
        ),
        expect_clarification=True,
    ),
    Case(
        case_id="adv.implied",
        text="买副耳机，额度跟上次一样就行。",
        accept_intents=(Intent.CREATE_MANDATE, Intent.UPDATE_MANDATE_DRAFT, Intent.UNKNOWN),
        expectation="'same as last time' is not a stated amount; no cap is invented",
        must_be_null=(
            "cap_per_transaction_cents",
            "rolling_cap_cents",
            "max_price_cents",
            "escalate_above_cents",
        ),
        expect_clarification=True,
    ),
)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip("'\"")
    return values


def read_dsh_credential(ref: str) -> str | None:
    """Read one ref from the DSH credentials store.

    Deliberately opt-in. The harness strips its own secrets from spawned
    processes on purpose, so reaching into the store is a decision the operator
    makes explicitly, not something a script does quietly on its own.
    """
    home = os.environ.get("DSH_HOME")
    if not home:
        return None
    path = Path(home) / ".credentials.yaml"
    if not path.is_file():
        return None
    # Parsed line by line rather than with a YAML dependency: the layout is
    # `refs:` then two-space-indented `NAME: value` entries.
    inside_refs = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" ") and line.strip().endswith(":"):
            inside_refs = line.strip() == "refs:"
            continue
        if inside_refs and ":" in line:
            name, _, value = line.strip().partition(":")
            if name.strip() == ref:
                value = value.strip().strip("'\"")
                return value or None
    return None


@dataclass
class Settings:
    """Everything the probe needs, and where each value came from."""

    api_key: str | None
    base_url: str
    model: str
    timeout_seconds: float
    sources: dict[str, str] = field(default_factory=dict)


#: Environment variable -> setting. Read from the process environment first, then
#: from the project .env, so that writing .env and running the probe is enough --
#: a .env that only supplies the key would otherwise leave LLM_MODEL ignored.
ENV_SETTINGS: tuple[tuple[str, str], ...] = (
    ("LLM_API_KEY", "api_key"),
    ("LLM_BASE_URL", "base_url"),
    ("LLM_MODEL", "model"),
    ("LLM_TIMEOUT_SECONDS", "timeout_seconds"),
)


def resolve_settings(
    *, explicit_api_key: str | None, allow_dsh: bool, env_file: Path | None = None
) -> Settings:
    """Resolve configuration. Secret values are never printed, only their source.

    ``env_file`` defaults to ``.env`` in the repository root. Point it at one of
    the committed templates to try a different provider without swapping files:

        python scripts/probe_llm_format.py --env-file .env.openai.example
    """
    from_env = {name: os.environ.get(name) for name, _ in ENV_SETTINGS}
    env_path = env_file if env_file is not None else PROJECT_ROOT / ".env"
    from_file = read_env_file(env_path)
    file_label = env_path.name if env_path.parent == PROJECT_ROOT else str(env_path)

    def pick(name: str) -> tuple[str | None, str]:
        if from_env.get(name):
            return from_env[name], "environment"
        if from_file.get(name):
            return from_file[name], file_label
        return None, "unset"

    values: dict[str, object] = {}
    sources: dict[str, str] = {}
    for name, attribute in ENV_SETTINGS:
        value, source = pick(name)
        values[attribute] = value
        sources[attribute] = source

    if explicit_api_key:
        values["api_key"] = explicit_api_key
        sources["api_key"] = "--api-key"

    if not values["api_key"] and allow_dsh:
        stored = read_dsh_credential("DEEPSEEK_API_KEY")
        if stored:
            values["api_key"] = stored
            sources["api_key"] = "DSH credentials store"

    try:
        timeout = float(values["timeout_seconds"] or 60.0)
    except (TypeError, ValueError):
        timeout = 60.0
        sources["timeout_seconds"] = "invalid, using 60"

    return Settings(
        api_key=values["api_key"],  # type: ignore[arg-type]
        base_url=values["base_url"] or DEFAULT_BASE_URL,  # type: ignore[arg-type]
        model=values["model"] or DEFAULT_MODEL,  # type: ignore[arg-type]
        timeout_seconds=timeout,
        sources=sources,
    )


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

@dataclass
class Trial:
    case_id: str
    ok_schema: bool
    intent: str | None
    invented: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    reply_chars: int = 0

    @property
    def hard_failure(self) -> bool:
        return (not self.ok_schema) or bool(self.invented)


def check_expectations(case: Case, outcome) -> list[str]:
    """Compare a validated outcome against what the case demands."""
    failures: list[str] = []
    if outcome.result is None:
        return ["no result to check"]

    result = outcome.result
    if result.intent not in case.accept_intents:
        accepted = "/".join(i.value for i in case.accept_intents)
        failures.append(f"intent {result.intent.value}, expected one of {accepted}")

    payload: dict = {}
    if result.search is not None:
        payload.update(result.search.model_dump(exclude_none=True))
    if result.mandate is not None:
        payload.update(result.mandate.model_dump(exclude_none=True))

    for name in case.must_be_null:
        if payload.get(name) is not None:
            failures.append(f"{name} was set to {payload[name]!r} but must be null")

    if case.expect_clarification:
        has_ambiguity = bool(result.ambiguities)
        has_draft_ambiguity = bool(result.mandate and result.mandate.ambiguities)
        if not (has_ambiguity or has_draft_ambiguity):
            failures.append("no ambiguity reported for an underspecified turn")

    return failures


def run_case(
    case: Case, client: OpenAICompatibleClient, trials: int, *, verbose: bool
) -> list[Trial]:
    request = build_llm_request(case.text)
    results: list[Trial] = []

    for index in range(trials):
        started = time.perf_counter()
        try:
            response = client.complete(request)
        except LLMCallError as exc:
            results.append(
                Trial(case_id=case.case_id, ok_schema=False, intent=None,
                      problems=[f"{exc.code}: {exc}"],
                      elapsed_ms=int((time.perf_counter() - started) * 1000))
            )
            continue

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        outcome = parse_reply(response.text, raw_text=case.text)
        problems = list(outcome.problems) + check_expectations(case, outcome)

        trial = Trial(
            case_id=case.case_id,
            ok_schema=outcome.schema_valid,
            intent=outcome.result.intent.value if outcome.result else None,
            invented=outcome.invented_values,
            problems=problems,
            elapsed_ms=elapsed_ms,
            reply_chars=len(response.text),
        )
        results.append(trial)

        if verbose:
            mark = "FAIL" if trial.hard_failure or problems else "ok  "
            print(f"    [{mark}] trial {index + 1}: {outcome.summary()} ({elapsed_ms} ms)")
            for problem in problems:
                print(f"           - {problem}")
            if trial.hard_failure:
                print(f"           reply: {response.text[:400]}")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=None,
                        help=f"default: LLM_BASE_URL, else {DEFAULT_BASE_URL}")
    parser.add_argument("--model", default=None,
                        help=f"default: LLM_MODEL, else {DEFAULT_MODEL}")
    parser.add_argument("--api-key", default=None,
                        help="default: LLM_API_KEY from the environment or .env")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--only", default=None, help="case_id substring filter")
    parser.add_argument("--adversarial-only", action="store_true")
    parser.add_argument("--happy-only", action="store_true")
    parser.add_argument("--no-json-mode", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="print the prompt and schema, call nothing")
    parser.add_argument("--allow-dsh-credential", action="store_true",
                        help="permit reading DEEPSEEK_API_KEY from the DSH credentials store")
    parser.add_argument("--show-replies", action="store_true")
    parser.add_argument("--env-file", default=None,
                        help="configuration file; default .env. Point it at "
                             ".env.openai.example to try OpenAI without swapping files")
    args = parser.parse_args()

    cases = list(HAPPY_CASES) + list(ADVERSARIAL_CASES)
    if args.adversarial_only:
        cases = list(ADVERSARIAL_CASES)
    if args.happy_only:
        cases = list(HAPPY_CASES)
    if args.only:
        cases = [c for c in cases if args.only in c.case_id]
    if not cases:
        print("no cases selected")
        return 2

    request = build_llm_request(cases[0].text)
    if args.dry_run:
        print("=" * 78)
        print("SYSTEM PROMPT")
        print("=" * 78)
        print(request.system_prompt)
        print()
        print("=" * 78)
        print("RESPONSE SCHEMA (properties, top level)")
        print("=" * 78)
        print(json.dumps(sorted(request.response_schema.get("properties", {})), indent=2))
        print()
        print("required:", request.response_schema.get("required"))
        print("cases:", ", ".join(c.case_id for c in cases))
        return 0

    env_path = Path(args.env_file) if args.env_file else PROJECT_ROOT / ".env"
    settings = resolve_settings(
        explicit_api_key=args.api_key,
        allow_dsh=args.allow_dsh_credential,
        env_file=env_path,
    )
    if not settings.api_key:
        print("No API key found.")
        print(f"  looked for LLM_API_KEY in: the environment, then {env_path}")
        if not args.allow_dsh_credential:
            print("  (a DSH-stored DEEPSEEK_API_KEY also exists; pass "
                  "--allow-dsh-credential to use it)")
        print()
        print("  Fix, for DeepSeek:")
        print("      Copy-Item .env.deepseek.example .env")
        print("      then set LLM_API_KEY in .env")
        print("  For OpenAI, the same with .env.openai.example.")
        print("  Or point this run at a file directly: --env-file .env.deepseek.example")
        return 3

    config = ProviderConfig(
        base_url=args.base_url or settings.base_url,
        model=args.model or settings.model,
        api_key=settings.api_key,
        timeout_seconds=settings.timeout_seconds,
        json_mode=not args.no_json_mode,
    )
    redacted = config.redacted()
    print(f"endpoint : {redacted['endpoint']}  (from {settings.sources['base_url']})")
    print(f"model    : {redacted['model']}  (from {settings.sources['model']})")
    print(f"api key  : {redacted['api_key']}  (from {settings.sources['api_key']})")
    print(f"timeout  : {redacted['timeout_seconds']}s")
    print(f"json mode: {redacted['json_mode']}")
    print(f"trials   : {args.trials} per case, {len(cases)} cases")
    print()

    client = OpenAICompatibleClient(config)
    by_case: dict[str, list[Trial]] = {}
    for case in cases:
        print(f"  {case.case_id}")
        print(f"    expects: {case.expectation}")
        by_case[case.case_id] = run_case(
            case, client, args.trials, verbose=args.show_replies
        )

    # -- report ------------------------------------------------------------
    print()
    print("=" * 78)
    print(f"{'case':<18}{'schema':>8}{'invented':>10}{'clarified':>11}{'ms':>8}")
    print("-" * 78)

    total = 0
    schema_failures = 0
    inventions = 0
    missed_clarifications = 0

    for case in cases:
        trials = by_case[case.case_id]
        total += len(trials)
        ok = sum(1 for t in trials if t.ok_schema)
        invented = sum(1 for t in trials if t.invented)
        schema_failures += len(trials) - ok
        inventions += invented

        clarified = "-"
        if case.expect_clarification:
            good = sum(1 for t in trials if not t.invented)
            missed_clarifications += len(trials) - good
            clarified = f"{good}/{len(trials)}"

        latencies = [t.elapsed_ms for t in trials] or [0]
        print(
            f"{case.case_id:<18}{ok:>4}/{len(trials):<3}{invented:>6}/{len(trials):<3}"
            f"{clarified:>11}{int(statistics.median(latencies)):>8}"
        )

    print("-" * 78)
    print(f"trials {total}   schema failures {schema_failures}   inventions {inventions}")

    if schema_failures or inventions:
        print()
        print("FAILURES")
        seen: set[str] = set()
        for case in cases:
            for trial in by_case[case.case_id]:
                for problem in trial.problems:
                    key = f"{case.case_id}: {problem}"
                    if key not in seen:
                        seen.add(key)
                        print(f"  {key}")

    print()
    if schema_failures == 0 and inventions == 0:
        print("VERDICT: every reply matched the contract and quoted its evidence.")
        return 0
    print("VERDICT: the contract is not reliably met. Do not build on this yet.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
