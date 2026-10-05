# HacKU 2026 — FinTech Track, Problem Statement 1

**Give a Machine a Wallet — Agentic Commerce** (sponsored by HKT)

**Team Naai_Lung** · `nick-zhang-lohua/HackU-Naai_Lung`

A headphone shopping agent that a user authorises in advance, and a commerce
core that guarantees no money moves outside that authorisation.

```
A proposes.  C decides.  C reserves.  C pays.  C records.  A explains.
```

---

## Repository layout

One repository, one tree. Everything lives at the root, grouped by kind.

```
HackU/
├─ app/
│  ├─ main.py                    A   composition root: builds the objects, mounts the routers
│  ├─ agent/                     A   LLM zone: parsing, orchestration, the mandate and purchase flows
│  ├─ api/                       A   the HTTP edge: envelope, routes, and one demo page
│  ├─ catalog/                   D   product repository, HKD, 40 SKUs
│  ├─ commerce/                  C   policy, mandate, quote, reservation, capability, payment, audit
│  ├─ contracts/                 A   shared types -- everyone reads, only A writes
│  └─ db/                        D   connection, schema, initialisation
│
├─ data/products.seed.json       D   40 HKD demo products (the path the loader reads)
├─ docs/                         all documentation, one layer
├─ fixtures/                     A   6 generated input/output pairs for B
├─ scripts/                      tools (see below)
├─ tests/                        contracts, commerce, agent, api, integration, catalog
└─ var/demo.sqlite3              runtime database, regenerable, not committed
```

`scripts/` holds two kinds of tool, and the difference is deliberate:

| Tool | Imports `app`? | Kind |
|---|---|---|
| `gen_fixtures.py`, `check_fixtures.py` | no | reads the seed JSON directly |
| `init_demo.py`, `inspect_demo.py`, `probe_llm_format.py` | yes | needs the package |
| `inspect_commerce.py` | yes | read-only: mandates, ledger, audit chain, reconciliation |
| `reset_demo.py` | yes | rebuilds a used demo database; backs up first, refuses without `--yes` |
| `demo_conversation.py` | yes | runs the conversation on the command line |

---

## Running it

All commands run from the repository root.

```bash
# dependencies (pinned; do not upgrade individually)
python -m pip install -r requirements.lock.txt

# build the demo database from the seed (idempotent)
python scripts/init_demo.py

# tests
python -m pytest tests -q
```

Configuration is read from the environment. Copy the template for your provider
and fill in the one value it cannot supply:

```bash
cp .env.deepseek.example .env     # or .env.openai.example
# then set LLM_API_KEY in .env
```

`.env` is git-ignored; the `*.example` templates are committed and **hold no
key** — leave the `LLM_API_KEY` line in a template empty, or the working
credential ends up in the repository. `var/*` is git-ignored because it is
regenerable. Each setting in the templates is marked `LIVE` (something reads it
today) or `PLANNED` (the owner has not written the reader yet).

### The demo

```bash
# the whole thing over HTTP, with a page that renders AgentResponse
python -m uvicorn app.main:app --reload      # then open http://127.0.0.1:8000/

# the same conversation on the command line, offline and with no key
python scripts/demo_conversation.py --no-llm --delegated

# what the database actually holds: mandates, ledger, chain, reconciliation
python scripts/inspect_commerce.py

# rebuild a used demo database (backs up first; refuses without --yes)
python scripts/reset_demo.py --yes
```

`--delegated` runs the scenario the problem statement describes: authorise a
spending envelope, sign it, buy inside it, and be stopped once.

---

## Who owns what

The role letters are people, not modules. The boundary between them is the point
of the design.

| | A | B | C | D |
|---|---|---|---|---|
| Owns | `app/contracts/`, `app/agent/`, `app/api/`, `app/main.py` | `app/search/` | `app/commerce/` | `app/catalog/`, `app/db/`, `data/` |
| State | contracts, conversation, mandate and purchase flows, HTTP edge — done | **not started**; contract frozen, fixtures ready | policy, mandate, quote, reservation, capability, payment, audit, reconciliation — done | complete |
| Tests | `tests/contracts/`, `tests/agent/`, `tests/api/`, `tests/integration/` | `tests/search/` (skips until B exists) | `tests/commerce/` | `tests/test_catalog.py` |

**C never asks A for permission, and A can never grant it.** A submits a
`PurchaseProposal`; approving, reserving, signing a capability and paying all
happen inside C. B and C never call each other at all — B is read-only, C holds
the money, and a path between them would bypass the authorisation chain.

`CommerceClient` is the whole of what A can reach. Replacing the in-process C
with a remote one is a change to that client and one line in `app/main.py`;
`tests/integration/test_replacement.py` performs the swap rather than asserting
it, and a second `SearchClient` that ranks the other way round proves A resolves
"the first one" against what B returned instead of re-sorting it.

---

## Verifying the fixtures

Fixture expected values are **generated, not hand-written**, because a
hand-computed expectation cannot be verified: if an implementation disagrees
with it, nobody can tell whether the contract is ambiguous, the fixture is
wrong, or the implementation is.

```bash
python scripts/gen_fixtures.py --check   # is the generation logic self-consistent?
python scripts/check_fixtures.py         # are the committed JSON files intact?
```

Current expected values:

```
search.001.normal        total=4   returned=3   order hp_0007, hp_0001, hp_0008
search.002.relax         total=0   returned=0   hint weight 5 -> 9 (+2)
search.003.anc_unknown   total=6   returned=5   order hp_0001, hp_0008, hp_0007, hp_0034, hp_0003
search.004.invalid       INVALID_CONSTRAINTS
search.005.gaps          total=4   returned=3   unsupported=[audio_quality]
summarize.001.partial    total=36  anc true 6 / false 24 / unknown 6
```

---

## What is being built

The problem statement requires one delegated spending decision, an enforced
limit, and a demonstration of the agent being **stopped**. The system therefore
has two distinct kinds of authorisation:

| | Confirming requirements | Confirming an authorisation |
|---|---|---|
| What it means | "I understand you want X" | "You may spend my money under these rules" |
| Who signs | the user | the user |
| What it enables | a search | autonomous purchases |
| Recorded as | a frozen `SearchRequest` | an immutable `Mandate` version + `policy_hash` |

A denial is a first-class outcome with a durable `DenialReceipt` naming the rule
that fired, the value observed and the limit it met — so "why did it do that?"
is answered from the recorded rule rather than from an explanation written
afterwards.

**Money is always an integer number of minor units. Currency is HKD.** Any rate,
fee or reward value that was not observed from a public source is labelled
`SANDBOX` or `SYNTHETIC_DEMO`; nothing is presented as an observed market rate.

---

## Where to read next

| If you are | Read |
|---|---|
| **picking this up next** | [`docs/handoff-to-next-agent.md`](docs/handoff-to-next-agent.md) — start at Part 0: current state, the end-to-end flow, every architectural claim and how to check it, and every trap already stepped in |
| changing an A/C interface | [`docs/A_C_contract_changes.md`](docs/A_C_contract_changes.md) — each change with its issue, the alternatives rejected, the impact on both sides and the tests |
| wiring a real B, C or payment rail | [`docs/A_C_data_handoff.md`](docs/A_C_data_handoff.md) — every field of every boundary, and the Fake→Real map with a cost per row |
| checking where a number came from | [`docs/evidence_sources.md`](docs/evidence_sources.md) — every rate and parameter, its source, and how it is labelled |
| working on the plan | [`docs/A_详细开发计划_v2.0.md`](docs/A_详细开发计划_v2.0.md) — partially superseded; Part 0 of the handoff says which parts |
| member B | [`docs/handoff-to-B.md`](docs/handoff-to-B.md) |
| touching the LLM boundary | [`docs/LLM_FORMAT_PROBE.md`](docs/LLM_FORMAT_PROBE.md) — how the parser output is measured, and the contract defects that measurement found |
| looking for the catalog module | [`docs/catalog-module.md`](docs/catalog-module.md) — D's module README |

---

## Current status

| Layer | State |
|---|---|
| `app/catalog`, `app/db` (D) | complete; unchanged by the A/C work |
| `app/contracts` (A) | complete: common, product, search, mandate, commerce, policy, agent, audit, **profile** |
| `app/commerce` (C) | complete: schema, repositories, audit chain, mandate registry, quote, spend state, authority, escalation, capability, payment (with a named final check), reconciliation |
| `app/agent` (A) | complete: parsers, orchestrator, mandate and purchase flows, renderer, **profile chain** |
| `app/api`, `app/main.py` (A) | complete: the three A endpoints, two read-only record views, D's product route, one demo page |
| `app/search` (B) | **not started**; contract and fixtures ready, `tests/search/` skips until it exists |
| Payment rail | `SandboxPaymentAdapter`. It moves no money and says so on every settlement it produces |
| Device requirement | carried by the contract and checked fail-closed by C, but **the catalog records no device data yet** — see `docs/A_C_contract_changes.md` CC-10 for the three-part change that belongs to D |
| Catalog data | `data/headphone-database-catalog-v4(1).sqlite3` is D's authoring catalog; `data/products.seed.json` is regenerated from it and is what the loader reads. The two are pinned together by `tests/data/test_catalog_data.py` |
| Acceptance cases AC-01…AC-16 | two have a recorded meaning and are labelled in `tests/integration/`; the other fourteen are undefined in this repository and were **not invented** |

```bash
python -m pytest tests -q
```

Run it for the count rather than trusting a number written here: this table went
stale once already because a count was copied into it.
