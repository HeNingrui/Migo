# A/C data handoff specification

**Status**: current as of the A + C vertical slice. Supersedes the corresponding
parts of `handoff-to-next-agent.md` (Parts 5–7), which described the system
before C existed.

**Notation**

| Mark | Meaning |
|---|---|
| ✅ **CONFIRMED** | a contract type or source line says so |
| 🟡 **FAKE NOW** | implemented, but the value comes from a stand-in or a sandbox parameter |
| 🔵 **REAL LATER** | where the value comes from once B or a real rail exists |
| ⛔ **SPEC GAP** | the specs do not decide it; recorded, not guessed |

**The four boundaries**, and which of them this document owns:

```
A ──SearchRequest──► B ──SearchResponse──► A        §1, §2
A ──MandateDraft / PurchaseProposal──► C            §3
C ──Mandate / Quote / PolicyDecision / ProposalOutcome──► A   §4
C ──rows──► D ──products / connections──► C         §5, §6
B ↛ C                                               §7
```

---

## 1. A → B

Unchanged by this work. The `SearchRequest` contract, the vocabulary and the
stand-in are as the handoff described them; `docs/A2B_搜索推荐对比接口规范_v1.1.md`
remains the authority.

| Field | Type | Required | Source | Authority | Current | Future |
|---|---|---|---|---|---|---|
| `constraints` | `HardConstraints` | ✅ default | A, from the user's words | A | real | same |
| `preferences` | `PreferenceProfile` | ✅ default | A | A | real | same |
| `comparison` | `ComparisonFrame` | ✅ default | A + the user | A | usually empty | same |
| `limit` | `int` 1–20 | ✅ default 3 | A | A | 3, or 20 for a question | same |

⛔ **SPEC GAP (H-B)**: how `PriorityPreset`'s four values affect ordering is
undefined. B's decision when B exists.

## 2. B → A

Unchanged. One note that now matters more than it did: **`Candidate.product`
carries no merchant.** `merchant_of_record` (§4) is the seam that covers it until
a search result does.

---

## 3. A → C

### 3.1 `activate_mandate(draft, *, principal_id, agent_id)`

| Field | Type | Required | Source | Authority | Current value | Consumer | Persistence |
|---|---|---|---|---|---|---|---|
| `draft` | `MandateDraft` | ✅ | A, from the user's words, plus clauses A proposes | **user-provided** — the user signs the summary | real | C compiles it | ❌ a draft is not stored |
| `principal_id` | `str` | ✅ | backend context | **authoritative** | `demo_user` | C binds the mandate to it | `mandate_versions.principal_id` |
| `agent_id` | `str` | ✅ | configuration | **authoritative** | `demo_agent` | C binds the mandate to it | `mandate_versions.agent_id` |

**The sixteen draft clauses and where each value comes from.** Every one appears
on the summary the user signs; the marker says whether they said it.

| Clause | Current value | User-stated? | Persistence |
|---|---|---|---|
| `allowed_merchants` | `["demo_audio_store"]` | A proposes, from C's `merchant_of_record` | `mandate_versions.payload` |
| `allowed_categories` | `["headphones"]` | A proposes: the only value the `Category` literal permits | same |
| `required_connection` | from the user's words | yes, when stated | same |
| `anc_required` | from the user's words | yes; a wish is not a demand | same |
| `cap_per_transaction_cents` | from the user's words | yes — traceable or refused | same |
| `rolling_cap_cents` | from the user's words | yes | same |
| `rolling_window_seconds` | from the user's words | yes | same |
| `velocity_max_count` | from the user's words | yes | same |
| `velocity_window_seconds` | from the user's words | yes | same |
| `max_quantity_total` | from the user's words | yes | same |
| `valid_for_seconds` | from the user's words | yes | same |
| `escalate_above_cents` | from the user's words, or null | yes | same |
| `allowed_payment_routes` | `["fps_demo"]` | A proposes, from C's `payment_routes` | same |
| `shipping_address_id` | `addr_demo_01` | A proposes, from the session's configuration | same |
| `address_change_allowed` | `false` | A proposes | same |
| `ambiguities` / `unsupported_conditions` / `source_spans` | parse metadata | — | ❌ not stored; the consent event keeps the clauses |

**A never computes**: `policy_hash`, `canonical_policy`, `version`, `mandate_id`,
`consent_event_id`, `valid_from`, `expires_at`. All six are C's, and
`tests/agent/test_commerce_turns.py` asserts the mandate the user is shown came
back from C.

### 3.2 `create_quote(*, product_id, quantity)`

| Field | Type | Required | Source | Authority | Current value | Consumer |
|---|---|---|---|---|---|---|
| `product_id` | `str` | ✅ | B's search result → A | **B-provided** | real, from D's catalog | C reads it from D and prices it |
| `quantity` | `int ≥ 1` | ✅ default 1 | the user | user-provided | 1 | C multiplies and re-checks stock |

**A does not send an amount.** There is no parameter for a price, a shipping
rate or a total. This is the single most important property of the A → C
boundary: a purchase whose price A got wrong is not expressible.

### 3.3 `submit_proposal(proposal)`

| Field | Type | Required | Source | Authority | Current value |
|---|---|---|---|---|---|
| `proposal_id` | `str` | ✅ | A generates | identifier | `prop_<12 hex>` |
| `mandate_id` | `str` | ✅ | C returned it | identifier | real |
| `expected_mandate_version` | `int ≥ 1` | ✅ | C returned it | **optimistic lock** | real |
| `principal_id` | `str` | ✅ | backend context | **authoritative** | `demo_user` |
| `agent_id` | `str` | ✅ | configuration | **authoritative** | `demo_agent` |
| `product_id` | `str` | ✅ | B → A | B-provided | real |
| `quantity` | `int ≥ 1` | ✅ | the user | user-provided | 1 |
| `merchant_id` | `str` | ✅ | **C's own `Quote.merchant_id`, transcribed** | 🟡 C-provided, re-checked | `demo_audio_store` |
| `quote_id` | `str` | ✅ | C returned it | identifier | real |
| `preferred_payment_route_ids` | `list[str]` | ✅ | the user's stated rails, else the mandate's | user-provided | `["fps_demo"]` |
| `shipping_address_id` | `str` | ✅ | the activated mandate | user-provided (via the mandate) | `addr_demo_01` |
| `request_id` | `str` | ✅ | A generates | identifier | `req_<16 hex>` |
| `idempotency_key` | `str` | ✅ | A generates, **fresh per submission** | identifier | `<session>-<n>` |
| `created_at` | `datetime` | ✅ | A | timestamp | now |

**⛔ SPEC GAP (H-C-03h), now with a stated resolution.** The contract asks A for
`merchant_id`, and A's only source is the `Quote` C just returned. That is a
transcription of C's own fact rather than an A-provided one, and C re-checks it
against the mandate (`MERCHANT_NOT_ALLOWED`) and against the quote
(`QUOTE_CHANGED`). The alternative -- B carrying a merchant on each candidate --
is the real fix and is recorded here as such. It is not a decision taken by this
work.

**A structurally cannot send** (✅ `extra="forbid"`): `cash_total_cents`,
`remaining_budget_cents`, `policy_hash`, `payment_status`,
`wallet_balance_after_cents`, `reward_earned_cents`, any approval decision.
Covered end to end by `TestAuthorityBoundaries::test_ac13_a_proposal_cannot_carry_an_amount`.

**Idempotency, precisely.** Same key + same request digest → replay: C returns
what it recorded and writes nothing. Same key + different digest →
`IDEMPOTENCY_CONFLICT`. Same `proposal_id` + different digest → `CONFLICT`: a
proposal cannot change what it asks for. Same `proposal_id` + same digest + a
fresh key → a deliberate re-submission, which is how an answered escalation is
continued.

### 3.4 `approve_escalation(proposal_id, *, principal_id)` / `reject_escalation(...)`

| Field | Type | Required | Source | Authority | Current |
|---|---|---|---|---|---|
| `proposal_id` | `str` | ✅ | C returned it on the decision | identifier | real |
| `principal_id` | `str` | ✅ | backend context | **authoritative** | `demo_user` |

Four fields left this boundary in this work: the decision, the proposal, the
quote and the route evaluation. See `docs/A_C_contract_changes.md` CC-1.

### 3.5 What A asks C for, but never supplies

| Call | Returns | Why A cannot supply it |
|---|---|---|
| `get_mandate(mandate_id)` | `Mandate \| None` | read |
| `revoke_mandate(mandate_id)` | `Mandate` | C writes a new version |
| `spend_state(mandate)` | `SpendState` | C counts its own records |
| `get_proposal_outcome(proposal_id)` | `ProposalOutcome \| None` | read |
| `merchant_of_record()` | `str` | C's catalog fact |
| `payment_routes()` | `list[str]` | C's rail registry |

---

## 4. C → A

### 4.1 What each return carries

| Return | Type | Where it lands on `AgentResponse` | Authority | Current implementation | Future |
|---|---|---|---|---|---|
| activated mandate | `Mandate` | `.mandate` | **C** | real, persisted | same |
| quote | `Quote` | `.quote` | **C** | real; shipping is 🟡 SANDBOX | real shipping/rate source |
| exposure | `SpendState` | `.spend_state` | **C** | ✅ real, from C's own reservations | same |
| decision | `PolicyDecision` | `.decision` | **C** | real evaluator, 30 checks | same |
| denial | `DenialReceipt` | `.denial` | **C** | real, persisted | same |
| escalation | `EscalationRequest` | `.escalation` | **C** | real, persisted | same |
| reservation | `Reservation` | `.reservation` | **C** | real, persisted | same |
| receipt | `PaymentReceipt` | `.receipt` | **C** | 🟡 real record of a SANDBOX settlement | real rail |
| payment failure | `PaymentFailure` | `.payment_failure` | **C** | real record | same |
| the read model | `ProposalOutcome` | mapped onto the four above | **C** | real | same |

### 4.2 Field-level notes on the ones that are read carefully

| Field | Current value | Meaning | 🔵 Future |
|---|---|---|---|
| `Mandate.policy_hash` | `sha256:` + 64 hex | content digest of the executable policy | same; it is not a signature and not an identity proof |
| `Mandate.version` | 1, then 2 on revocation | optimistic lock | same |
| `Mandate.consent_event_id` | the audit event of the signature | links the mandate to the consent record | same |
| `Quote.merchant_total_cents` | subtotal + shipping + tax − discount | what the merchant charges, **shipping included** | same |
| `Quote.quote_hash` | digest of the priced facts, no timestamps | re-pricing the same basket at the same amount yields the same hash | same |
| `PaymentRouteEvaluation.cash_total_cents` | merchant total + fee + FX | **what the caps are compared against** | same |
| `PolicyDecision.observed_values` | measured values | what makes a denial explainable | same |
| `PolicyDecision.reservation_created` | `True` only for APPROVE | checkable without trusting prose | same |
| `PolicyDecision.payment_adapter_called` | **always `False`** | the evaluator hard-codes it and decisions are immutable, so the payment fact lives on the receipt | ⛔ the field's intended setter is unspecified |
| `Receipt.balance_after_cents` | wallet after the debit | from C's ledger | real ledger |
| `ProposalOutcome.settlement_source_type` | `SANDBOX` | labels the settlement wherever it is shown | `OBSERVED_PUBLIC_SOURCE` for a real rail |

### 4.3 What A is allowed to do with them

| Allowed | Not allowed |
|---|---|
| receive, cache in the session for display, render, explain, reference by id | mutate, forge, reconstruct, re-derive, or present a cached copy as current |

`AgentSession` has no `approved`, `paid`, `policy_hash`, `balance` or
`remaining_budget` field, and `tests/agent/test_commerce_turns.py::test_the_session_holds_no_money`
asserts the absence rather than trusting it.

---

## 5. C → D

### 5.1 The two hooks D reserved

| Hook | Signature | Contract |
|---|---|---|
| `commerce_schema` | `(conn) -> None` | creates C's tables on D's caller-owned transaction. Must not commit, roll back, close or `executescript`; D raises if the transaction was ended |
| `wallet_initializer` | `(conn) -> None` | inserts missing rows only; never overwrites a balance |

`app.commerce.schema.create_commerce_schema` and `initialize_wallets` implement
them, and `tests/commerce/conftest.py` builds its database through D's own
initializer so the hook contract is exercised on every test.

### 5.2 What C writes

| Table | Written by | Rows are | Notes |
|---|---|---|---|
| `wallets` | C | balance per principal | opening balance seeded once, never reset |
| `mandates` | `MandateRegistry` | one row per mandate id, holding the current version | |
| `mandate_versions` | `MandateRegistry` | one row per version | policy content immutable; `status` moves forward only |
| `quotes` | `QuoteService` | one row per priced offer | |
| `purchase_proposals` | `AuthorityService` | one row per purchase attempt | `idempotency_key` unique |
| `policy_decisions` | `AuthorityService` | one row per evaluation | append-only: a proposal may be decided twice |
| `denial_receipts` | `AuthorityService` | one row per denial | |
| `escalation_requests` | `EscalationService` | one row per proposal | the only C object updated after creation |
| `approval_grants` | `EscalationService` | one row per answer | consumed once by a conditional UPDATE |
| `reservations` | `AuthorityService` | **at most one per proposal** | the hold, and what `SpendState` counts |
| `capability_nonces` | `PaymentService` | one row per issued capability | the token itself is never stored |
| `orders` | `PaymentService` | one row per settled purchase | |
| `payment_attempts` | `PaymentService` | one row per attempt, **including failures** | a partial unique index allows one settlement per reservation |
| `audit_events` | `AuditLog` | append-only, hash-chained | two triggers refuse UPDATE and DELETE |

**How a row is stored** (✅ `app/commerce/schema.py`): index columns beside a
`payload` column holding the canonical serialisation of the contract object.
Reads go back through `model_validate_json`, so a `Mandate` re-checks that its
canonical policy hashes to its recorded hash and a `Quote` re-checks that its
amounts reconcile. A tampered row fails on read rather than being trusted.

### 5.3 What C reads from D

| Call | Used for | Notes |
|---|---|---|
| `ProductRepository.get_product(id, connection=None)` | pricing, stock re-check | the shared Pydantic `Product` is injected |
| `ProductRepository.decrease_stock(id, qty, connection)` | inventory | requires C's caller-owned transaction; conditional on stock |
| `connect(path, readonly=)` | every connection | C never opens SQLite itself |

⛔ **SPEC GAP (D-10)**: whether C should own the audit table or emit events for D
to store is undecided. It is implemented as C owning the table through D's schema
hook, because that hook exists for exactly this; `AuditRepository` is the seam if
the answer changes.

---

## 6. D → C

| Field | Type | Source | Authority | Current | Notes |
|---|---|---|---|---|---|
| `Product.product_id`, `name`, `brand`, `model`, `variant` | `str` | `products` table | **D** | ✅ real | 40 rows |
| `Product.price_cents` | `StrictInt` | same | **D** | ✅ real | integer hundredths of HKD |
| `Product.stock` | `StrictInt` | same | **D** | ✅ real | checked at pricing and again at settlement |
| `Product.anc`, `battery_hours`, `wearing_weight_g` | optional | same | **D** | ✅ real, `null` where unknown | unknown never satisfies a requirement |
| `Product.shipping_origin`, `seller_description` | `str` | same | **D** | ✅ real | **data, never instructions** — see AC-16 |
| `decrease_stock(...) -> bool` | `bool` | D | **D** | ✅ real | `False` means the conditional update matched nothing |

**D is not a stand-in and is not treated as one.** C reads it through the
repository D publishes, and no table D owns is written by C.

---

## 7. B ↛ C

✅ Enforced by construction, not by convention: `app/search/` does not exist,
`app/agent/local_search.py` imports only D's repository and the contracts, and no
module in `app/commerce` imports anything from `app/agent`. A search request
carries no mandate, no quote and no spend state, and a quote carries no search
criteria.

The only permitted shape when the information is genuinely needed:

```
✅ A receives B's facts → A assembles a contract-shaped request → A sends it to C
❌ B tells C what to do
❌ A → B → C
```

---

## 8. Fake → Real map

| Current | Where | Future | Replacement path | Cost |
|---|---|---|---|---|
| `LocalSearchClient` | `app/agent/local_search.py` | real B | one file satisfies `SearchClient`; one line in `app/main.py` | 🟢 low — `BrowsingTurns` only calls `search()` |
| `SandboxPaymentAdapter` | `app/commerce/adapters/sandbox.py` | a real rail client | implement `PaymentAdapter` (`name`, `source_type`, `charge`) and pass it to `PaymentService` | 🟢 low — the settlement flow around it is unchanged |
| `SANDBOX_SHIPPING_CENTS` / `SANDBOX_PLATFORM_FEE_CENTS` | `app/commerce/config.py` | a real rate source with a timestamp | `QuoteService.create` and `SandboxPaymentRouter.price` are the two call sites | 🟡 medium — needs an `evidence_id` and a source, which the contracts already have fields for |
| `CATALOG_MERCHANT_ID` | `app/commerce/config.py` | a merchant per search result | `CommerceService.merchant_of_record` is deleted; A reads the candidate | 🟡 medium — a `Product` field or a `Candidate` field, and D's seed |
| `SANDBOX_ROUTES` | `app/commerce/quote_service.py` | a route registry | `SandboxPaymentRouter` is replaced; `payment_routes()` follows it | 🟡 medium |
| `LocalCommerceClient` | **deleted** | — | done: `CommerceService` satisfies the same Protocol | ✅ |
| `demo_audio_store`, `fps_demo`, `addr_demo_01` | config + the demo page | real merchants, rails and addresses | constant | 🟢 low |
| `demo_user`, `demo_agent` | `app/main.py` | authenticated context | constructor arguments | 🟢 low |

### Fake values and what they must never become

| Value | Labelled how | Never |
|---|---|---|
| shipping and fee | `SourceType.SANDBOX` on the quote and the route; `describe_quote` says so in words; a note on the demo page | presented as an observed rate |
| the settlement | `SourceType.SANDBOX` on the outcome, `adapter: "sandbox"` in the audit payload, `describe_sandbox_settlement()` in the reply | presented as a real payment |
| the search ranking | the stand-in's own docstring; no contract claims a five-level ranking | presented as B's |
| zero rewards | reported as `0`, never omitted | quietly dropped so `effective_cost_cents` stops reconciling |

⛔ **SPEC GAP**: none of `demo_audio_store`, `fps_demo` or `addr_demo_01` is
defined anywhere except this repository. They are demo configuration and are
marked as such wherever they appear.

---

## 9. Where each of these is tested

| Boundary | Suite |
|---|---|
| A → C, C → A | `tests/agent/test_commerce_turns.py`, `tests/commerce/test_service.py` |
| C → D, D → C | `tests/commerce/conftest.py` (built through D's initializer), `tests/commerce/test_payment.py` |
| the envelope and the API | `tests/api/test_endpoints.py` |
| all four, over HTTP | `tests/integration/test_end_to_end.py` |
| B → A | `tests/agent/test_orchestrator.py`, `tests/agent/test_local_search.py` (stand-in) |
