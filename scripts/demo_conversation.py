"""Run a conversation against the agent, on the command line.

    python scripts/demo_conversation.py                  # the scripted demo
    python scripts/demo_conversation.py --interactive    # type your own turns

Uses the real LLM when ``LLM_API_KEY`` is configured and the deterministic
parser otherwise, so it works offline and on a machine with no key. Which one
ran is printed for every turn -- a fallback that takes over silently is how a
system starts being wrong without anyone noticing.

Commerce is real: ``app.commerce.CommerceService`` over the demo database, with
the catalog, mandates, reservations and the hash-chained audit log all persisted.
Only the search side is a stand-in, and only the payment rail is a sandbox --
both are labelled wherever they appear.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.local_search import LocalSearchClient  # noqa: E402
from app.agent.orchestrator import AgentOrchestrator  # noqa: E402
from app.agent.session import SessionStore  # noqa: E402
from app.commerce.schema import (  # noqa: E402
    create_commerce_schema,
    initialize_wallets,
)
from app.commerce.service import CommerceService  # noqa: E402
from app.contracts.agent import ChatRequest  # noqa: E402
from app.db import initialize_database  # noqa: E402

SCRIPTED = [
    "找 300 以内、通勤用、必须支持主动降噪的无线耳机",
    "第二款",
    "便宜点",
    "轻一点",
    "随便看看",
]

#: The scenario the plan's demo describes: authorise a spending envelope, have it
#: signed, buy inside it, and be stopped once.
DELEGATED = [
    "授权你替我买耳机：每笔不超过 320，24 小时内总共不超过 600，5 分钟最多 2 笔，"
    "一共买 3 件，超过 300 先问我，只用 FPS，送到 addr_demo_01，有效期 7 天",
    "确认授权",
    "找 350 以内、无线、必须支持主动降噪的耳机",
    "第一款",
    "买吧",
    "还有多少额度",
]


def build_orchestrator(use_llm: bool,
                       db_path: str | None = None) -> tuple[AgentOrchestrator, str]:
    llm = None
    label = "deterministic parser (--no-llm)"
    if use_llm:
        from app.agent.llm_parser import build_parser_from_env

        llm = build_parser_from_env()
        if llm is None:
            label = ("deterministic parser -- no LLM_API_KEY found, so the model "
                     "path is off. See .env.deepseek.example")
        else:
            config = getattr(llm, "_client").config
            label = (f"LLM: {config.model} at {config.endpoint()} "
                     f"(key from {config.sources.get('api_key')})")
    return AgentOrchestrator(
        sessions=SessionStore(),
        search=LocalSearchClient(),
        # The same database this script just initialized. Pointing the
        # orchestrator at the default while initializing a different file would
        # give a conversation whose writes land somewhere the operator is not
        # looking -- which is exactly what it did until the inspector caught it.
        commerce=CommerceService(db_path=db_path),
        llm_parser=llm,
    ), label


def show(response) -> None:
    print(f"   [{response.intent.value if response.intent else '-'}"
          f" via {response.parse_source.value if response.parse_source else 'action'}"
          f" -> {response.next_action.value}]")
    for line in response.message.splitlines():
        print(f"   agent> {line}")
    payloads = [
        name for name in ("results", "mandate_draft", "quote", "mandate",
                          "decision", "denial", "escalation", "reservation",
                          "receipt", "payment_failure", "spend_state")
        if getattr(response, name) is not None
    ]
    if payloads:
        print("     payload: " + ", ".join(payloads))
    for note in response.notes:
        print(f"     note: {note}")
    if response.trace:
        print("     trace: " + "; ".join(str(t) for t in response.trace))
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--no-llm", action="store_true",
                        help="use only the deterministic parser")
    parser.add_argument("--delegated", action="store_true",
                        help="run the authorise-and-buy scenario instead")
    parser.add_argument("--db", help="Commerce database; defaults to "
                                     "DEMO_DB_PATH or var/demo.sqlite3")
    args = parser.parse_args()

    initialize_database(args.db, commerce_schema=create_commerce_schema,
                        wallet_initializer=initialize_wallets)

    orchestrator, label = build_orchestrator(use_llm=not args.no_llm, db_path=args.db)
    print(f"parser: {label}")
    print()

    session_id = None
    if not args.interactive:
        for text in (DELEGATED if args.delegated else SCRIPTED):
            print(f"user> {text}")
            response = orchestrator.handle(
                ChatRequest(session_id=session_id, message=text)
            )
            session_id = response.session_id
            show(response)
        return 0

    print("type a message, or 'quit' to stop")
    while True:
        try:
            text = input("user> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if text in ("quit", "exit", ""):
            return 0
        response = orchestrator.handle(
            ChatRequest(session_id=session_id, message=text)
        )
        session_id = response.session_id
        show(response)


if __name__ == "__main__":
    raise SystemExit(main())
