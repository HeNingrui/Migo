# A/C contract changes

**Scope**: every change this work made to `app/contracts/**` and to the
`CommerceClient` boundary in `app/agent/clients.py`.

**Rule applied**: a shared contract changes only when a real integration needs
it *and* the existing contract cannot express the need. Each entry below states
the issue, the alternatives that were rejected, and the impact on both sides. No
change was made because it was convenient.

**Not changed**: `Product`, `HardConstraints`, `SearchRequest`, `SearchResponse`,
`Quote`, `PaymentRouteEvaluation`, `PurchaseProposal`, `Reservation`,
`MandateDraft`, `Mandate`, `ApprovalGrant`, `PolicyDecision`, `PolicyViolation`,
`DenialReceipt`, `EscalationRequest`, `PaymentReceipt`, `AuditEvent`, `Envelope`,
`ErrorCode`, and every field on all of them. The seven original `CommerceClient`
signatures are unchanged in name, order and kind.

---

## CC-1 — `approve_escalation` narrowed to two identifiers

**Status**: applied.

**Issue.** The signature was

```python
approve_escalation(decision, *, proposal, quote, route) -> ApprovalGrant
```

Three of the four arguments are C's own products, and one of them -- a
`PaymentRouteEvaluation` -- is not obtainable by A at all. The evaluator returns
only a route *id* on the `PolicyDecision`; the object itself is built by C's
payment router. The only thing that ever produced one was
`LocalCommerceClient.evaluate_route`, which its own docstring marked as
existing "for tests and the demo".

So the old shape was not merely awkward. **A could not call it.** The method was
unimplemented on the A side and unimplementable.

**Alternatives considered**

| Option | Why not |
|---|---|
| Keep the shape, add a `get_route()` for A to call first | A would hold and re-submit C's internal object; the coupling the tool is meant to avoid, and nothing stops A substituting a different route |
| Keep the shape, have A construct the route | A cannot: cash total, fee, FX and eligibility are C's derived facts |
| Return the route on the `PolicyDecision` | Changes a frozen C type that the evaluator owns, and puts a derived object on a decision that is meant to record a rule outcome |

**Change**

```python
approve_escalation(proposal_id, *, principal_id) -> ApprovalGrant
```

C looks up the quote, the route and the amount from the escalation it recorded.

**Contract impact**: one Protocol method. Narrowing, not a widening: no caller
gains a capability.

**A impact**: `CommerceTurns.answer_escalation` passes two identifiers. The
session no longer needs to hold a decision, a proposal, a quote *or* a route in
order to answer a question.

**C impact**: `EscalationService.approve` reads the latest escalating decision
for the proposal and binds the grant to it. Slightly more work inside C, which
is where the facts already are.

**Tests**: `tests/commerce/test_service.py::TestTheNarrowedApproval` pins both
halves -- that the decision carries only an id, and that C looks up its own
context. `tests/agent/test_commerce_turns.py` exercises the path end to end.

**Migration**: none outstanding. `LocalCommerceClient` was the only other
implementation and it was deleted in the same change.

---

## CC-2 — `reject_escalation` added

**Status**: applied.

**Issue.** `Intent.REJECT_ESCALATION` has existed since the vocabulary was
written, and the ownership matrix states that `EscalationRequest.resolution` is
the one C object C may update after creating. There was no method to record the
answer. An escalation could therefore be approved or could time out, and "no"
was not expressible.

**Alternatives considered**

| Option | Why not |
|---|---|
| One `resolve_escalation(proposal_id, *, approved: bool)` | A boolean parameter on a money path reads as a flag A owns rather than an answer A relays; two names say which decision was taken |
| Let A record the rejection locally | A would be writing C's object |

**Change**: a new method returning the resolved `EscalationRequest`.

**Contract impact**: additive.

**A impact**: `CommerceTurns.answer_escalation(approve=False)`, reachable from
both the sentence path and the button path.

**C impact**: `EscalationService.reject`, which resolves the record and writes
`ESCALATION_REJECTED` with the principal as the actor.

**Tests**: `tests/commerce/test_escalation.py::TestAnswering`,
`tests/integration/test_end_to_end.py::TestTheEscalation`.

**Migration**: none -- the method did not exist, so there is nothing to migrate.

---

## CC-3 — `get_proposal_outcome` added, plus `PaymentFailure` and `ProposalOutcome`

**Status**: applied.

**Issue.** Four types were defined with no path from C to A:
`DenialReceipt`, `EscalationRequest`, `Reservation` and `PaymentReceipt`.
`submit_proposal` returns a `PolicyDecision`, and a decision cannot carry any of
them -- it records a rule outcome, not what then happened. So A could learn
*that* a purchase was refused and not *why*, could not show a receipt, and could
not tell the user which of five things had occurred.

Separately, `ErrorCode` names four payment failures
(`INSUFFICIENT_BALANCE`, `OUT_OF_STOCK`, `PAYMENT_FAILED`,
`PAYMENT_STATUS_UNKNOWN`) and no type carried one to A. That is the gap the
handoff recorded as R-C-04/R-C-05/R-C-07/R-C-08.

**Alternatives considered**

| Option | Why not |
|---|---|
| `submit_proposal` returns a union | Changes a frozen signature, and makes the return type depend on the outcome, which is the shape the codebase avoids elsewhere (`SearchResult` is discriminated deliberately; this would be an undiscriminated one) |
| Four narrow readers (`get_denial`, `get_escalation`, …) | Four round trips to render one reply, and the frontend has to know which to ask for -- it cannot, without already knowing the outcome |
| Raise on payment failure | A purchase the mandate *permitted* would be reported as a failed request. The envelope's `ok=false` would say "you sent something wrong" about a decision C took |
| Put a `payment_failure` inside `PolicyDecision` | Decisions are the evaluator's output and immutable; the payment happens after |

**Change**

* `PaymentFailure` -- one refused or unresolved settlement attempt, carrying the
  code, a message, a `retryable` flag, the rail's reference and the audit event.
* `ProposalOutcome` -- the read model: the latest decision plus whichever of
  denial, escalation, reservation, receipt and payment failure exist, plus
  `settlement_source_type` so a sandbox settlement cannot be read as a real one.
* `CommerceClient.get_proposal_outcome(proposal_id)`.

**Contract impact**: two new types and a read method. Additive.

**A impact**: one read renders an entire reply. `CommerceTurns.explain` maps the
outcome onto `AgentResponse`; nothing is re-derived.

**C impact**: `CommerceService.get_proposal_outcome` assembles it from C's own
rows inside one read connection. No write path changed.

**Validator note**: `ProposalOutcome` refuses incoherent combinations -- a
denial attached to an approval, a receipt without a reservation, a proposal that
is both settled and failed. A read model that can represent an impossible state
is a read model that will eventually be handed one.

**Tests**: `tests/commerce/test_service.py::TestTheReadModel`,
`tests/commerce/test_payment.py::TestTheRailSaysNo`,
`tests/integration/test_end_to_end.py`.

**Migration**: none outstanding.

---

## CC-4 — `merchant_of_record` and `payment_routes` added

**Status**: applied.

**Issue.** This is the D-09 / H-C-03h SPEC GAP, made explicit rather than left
in configuration. A mandate must name the merchants and the payment rails it
permits, and both are required clauses: a draft missing either cannot activate.
A has nowhere to get them.

* `Product` has no merchant field, and neither does `SearchResponse`, so B
  cannot supply one.
* The rail identifiers are C's (`SANDBOX_ROUTES`); A inventing `"fps_demo"`
  would be a mandate clause that activates and then refuses every purchase.

The previous stand-in hard-coded `merchant_id="demo_audio_store"` inside its own
quote method, which the handoff flagged as "**not acceptable**" (F-1) precisely
because A depended on a value A had no right to.

**Alternatives considered**

| Option | Why not |
|---|---|
| A holds both as configuration | Same defect in a different file: A would still be asserting a fact about C's catalog, and a mismatch would surface as a denied purchase rather than at the boundary |
| Add `merchant_id` to `Product` | D's schema, D's seed and every fixture would change; that is B/D work, not this |
| Make A read C's `SANDBOX_ROUTES` constant | Reaches past the boundary into C's module |

**Change**: two read-only methods.

**Contract impact**: additive. Both are reads with no authority.

**A impact**: `MandateTurns.draft_mandate` proposes both on the summary, marked
`（我建议的）`, and the user confirms them with everything else. The fallback
parser's rail vocabulary is now C's own list rather than a second copy.

**C impact**: two one-line accessors over values C already owns and already puts
on every `Quote`.

**Deletion path**: when B carries a merchant per search result, `merchant_of_record`
is deleted and A reads it from the candidate. `payment_routes` moves to whatever
route registry replaces the sandbox router. Neither touches A's flow, because
both are already behind the Protocol.

**Tests**: `tests/commerce/test_service.py`, `tests/agent/test_commerce_turns.py`
(the summary marks them as proposals), `tests/commerce/test_quote_service.py`.

**Migration**: none outstanding.

---

## CC-5 — `AgentResponse` gains five payload fields

**Status**: applied.

**Issue.** `AgentResponse` is the frontend contract, and its own docstring
states the rule that makes this a defect rather than a preference:

> **Every number in it is interpolated from a field on this object, never
> generated by a model**

A quote, an activated mandate, a reservation, a spend state and a payment
failure are all things the reply must state numbers from. None of them had a
field. Without them the rule cannot be kept for the price the user is asked to
approve -- which is the one number that matters most.

`requires_user_action()` already promises a control for `CONFIRM_MANDATE` and
`RUN_PURCHASE`, so the gap was reachable from the first turn of the demo.

**Alternatives considered**

| Option | Why not |
|---|---|
| Print the numbers into `message` only | Breaks the rule above, and the frontend would have to parse prose to render a price |
| Reuse `mandate_draft` for the activated mandate | Different types with different meanings; the whole point of the draft/mandate split |
| Add one `outcome: ProposalOutcome` field instead | The four existing payload fields would then duplicate it, and every reader would have to know which of the two to look at |

**Change**: `quote`, `mandate`, `spend_state`, `reservation` and
`payment_failure`, all optional and all defaulting to `None`.

**Contract impact**: additive and backward compatible. A client validating an
older payload still succeeds, because the new fields have defaults.

**A impact**: `TurnOutcome` carries the payload objects, and `_respond` copies
them across. Nothing is looked up from the session at the end of a turn, so what
the user sees is what that turn received.

**C impact**: none.

**Tests**: `tests/api/test_endpoints.py::TestTheResponseIsNeverOnlyText`,
`tests/integration/test_end_to_end.py`.

**Migration**: none.

---

## CC-6 — `SessionContext.selected_product_id` added

**Status**: applied.

**Issue.** `SessionContext` is what a parser is given so that a turn can be read
against the conversation. It carried `last_shown_product_ids` but not the
selection, so a bare "买吧" was indistinguishable from "我想买个耳机": there was
no way to tell that the user was confirming a specific purchase rather than
starting one.

The consequence was user-visible. The deterministic parser refused to read the
confirmation at all, and the agent answered "能说说你想找什么样的耳机吗？" to
somebody who had just said "buy it".

**Alternatives considered**

| Option | Why not |
|---|---|
| Recognise "买吧" unconditionally | Without a subject it is a shopping request, and treating it as a purchase is the guess the parser exists not to make |
| Read the selection from `last_shown_product_ids` | Wrong: being shown is not being chosen |

**Change**: an optional `selected_product_id`.

**Contract impact**: additive; the field is not money and carries no authority.

**A impact**: `_rule_run_purchase` now fires when there is a selection *or* an
active mandate, so "buy it" with a mandate and no shortlist reaches the purchase
handler, which asks which product.

**C impact**: none.

**Tests**: `tests/agent/test_commerce_turns.py::TestFailureIsNotSilent`.

**Migration**: none.

---

## CC-7 — `AgentActionRequest` no longer requires `mandate_id` for `ACTIVATE_MANDATE`

**Status**: applied.

**Issue.** The validator demanded a `mandate_id` for `ACTIVATE_MANDATE`. At the
moment that control is rendered there is no mandate -- the user is being asked to
sign a *draft*, and a draft has no identifier. So the consent gate was not
expressible as an action.

That is a gap rather than a validation, and the contract says so itself:
`AgentResponse.requires_user_action()` includes `CONFIRM_MANDATE`, promising the
client a control; and `NextAction.CONFIRM_MANDATE` is the value that drives it.

**Alternatives considered**

| Option | Why not |
|---|---|
| Give the draft an identifier | A new contract concept -- a draft id -- for one client affordance, and it would have to be persisted to be meaningful |
| Have the client send the mandate id it will receive | It has not received one |
| Force the confirmation through `/agent/chat` as text | Workable, but `A click is a click` is the reason the actions endpoint exists; a consent gate that can only be given by typing is weaker, not stronger |

**Change**: the field is optional for that intent. It is still accepted, and the
orchestrator now *checks* a supplied one against the session rather than ignoring
it -- naming another conversation's mandate is refused with `CONFLICT`.

**Contract impact**: one entry removed from a required-handles map. Strictly a
relaxation of validation, which is why it is recorded here rather than done
quietly.

**A impact**: `handle_action` can sign a draft, and `_reject_foreign_mandate`
guards the field.

**C impact**: none.

**Tests**: `tests/agent/test_commerce_turns.py::TestTheButtonPath`, including
the case where a client names a mandate the session does not hold.

**Migration**: none.

---

## CC-8 — `AuditEventType.QUOTE_ISSUED` added

**Status**: applied (separate commit, with C's persistence layer).

**Issue.** The requirement is that quoting is auditable. `AuditEventType` had no
value for a priced offer, and its own docstring explains why free text is not an
option:

> The list is deliberately explicit rather than free text: a verifier and a UI
> both need to switch on it, and free text guarantees they will disagree.

So a quote could not be recorded at all without violating the type's contract.

**Alternatives considered**

| Option | Why not |
|---|---|
| Record the quote inside the proposal's event payload | The quote exists before the proposal does, and a price that appears only as a nested field cannot be listed or verified on its own |
| Leave it unrecorded | "The log a third party can check" would be missing the price |

**Change**: one enum value. **Additive**: every existing value is unchanged, and
an older reader that does not know it fails loudly rather than silently.

**A impact**: none.

**C impact**: `QuoteService.create` writes the event inside the transaction that
persists the quote.

**Tests**: `tests/commerce/test_quote_service.py::test_issuing_a_quote_is_recorded`,
`tests/integration/test_end_to_end.py::TestTheHappyPath`.

**Migration**: none.

---

## CC-9 — `requirements.lock.txt` gains `httpx==0.28.1`

**Status**: applied.

**Issue.** This is the handoff's D-08. `fastapi.testclient.TestClient` imports
`httpx`, and it was not in the lock file, so the API tests could not run on a
machine that installed only the locked dependencies.

**Alternatives considered**: rewriting the tests against `urllib` (loses the
ASGI transport, so the exception handlers and the envelope wiring would go
untested), or documenting it as a manual install (the failure mode is a
collection error that looks like a broken test).

**Change**: one new pin. A new dependency, not an upgrade: no existing pin moved.

**Impact**: A's file; C, B and D are unaffected.

---

## Not contract changes, but semantic decisions worth recording

These were under-specified rather than wrong, and each is now stated in code and
pinned by a test.

### The `SpendState` window start

`rolling_window_start` is the caller's record of *when the supplied counts
begin*, not the start of the window being judged. The evaluator uses it for one
thing: to decide whether the snapshot is current at all.

```
if now - rolling_window_start < rolling_window_seconds:
    compare exposure against the cap
else:
    treat exposure as zero
```

The stand-in set it one full window in the past, so the comparison was skipped
and the rolling cap could not fire whatever the exposure was. C now sets it to
the earliest contributing record, or to `now` when there is nothing to count,
and filters the window strictly at the lower edge so the earliest contributing
timestamp is always strictly inside it. Written up in `app/commerce/spend_state.py`
and pinned by `tests/commerce/test_spend_state.py::TestTheWindowStartGuard`.

### `PaymentStatus.UNKNOWN` is not retryable

`is_retryable()` returns `True` for `PAYMENT_STATUS_UNKNOWN`, because the code is
absent from `NON_RETRYABLE`. For an attempt that may already have moved the money
that is the wrong answer, and the plan is explicit that an unknown outcome is
never retried automatically. `PaymentService` therefore reports
`retryable=False` for that one code and keeps the shared table's answer for every
other, with the reason in the code and a test at the site.

### `PolicyDecision.payment_adapter_called` is always `False`

The evaluator hard-codes it, and the payment service never rewrites the decision
because decisions are immutable. The flag therefore reads `False` on every
recorded decision, and the payment fact lives on `PaymentReceipt`. Reporting it
any other way would mean C editing its own record of what a rule decided.

### One reservation per proposal, and re-submission

A proposal is one purchase attempt. `reservations.proposal_id` is unique, so a
released attempt is not re-usable and a retry needs a new proposal -- which is
what stops a retry loop from becoming a second charge.

An approval is the exception, and it has to be: an `ApprovalGrant` names one
`proposal_id`, so a purchase the principal has just approved is *re-submitted*
with a fresh idempotency key rather than replaced. Recorded in
`AgentSession.pending_proposal` and in `AuthorityService`'s docstring.

The key itself carries a random suffix. A key built only from the session id and
a per-session counter collides across restarts -- session ids come from a counter
too -- and C correctly refuses the collision with `IDEMPOTENCY_CONFLICT`. Fixed
in `AgentSession.next_idempotency_key`, with the regression in
`tests/agent/test_commerce_turns.py::TestTwoConversationsShareOneDatabase`.

### `AuditEventType.CAPABILITY_REJECTED` is never written

Declared in the contract, and unreachable today. C issues a capability and spends
it inside one transaction; a refusal would need the nonce to be already consumed
or the context to have moved, and both are bugs rather than states. The refusal
*is* loud -- `CapabilityService.spend` raises one of five named `CAPABILITY_*`
errors, and each is tested at the service -- but it is not recorded in the chain,
because an event written inside a transaction that is about to roll back is an
event that does not survive.

Left as it is, deliberately. Recording it would mean a second transaction on a
path nothing can reach, and the honest place for a value like this is the same
one the evaluator's two structurally unreachable checks occupy (see the handoff's
§9.4): a defensive second line, tested where it can be, and documented rather
than dressed up. It becomes reachable the moment something other than
`PaymentService` can present a capability -- a rail callback, or an HTTP endpoint
that accepts a token -- and that is exactly the change the SPEC GAP in
`app/commerce/capability.py` is reserved for.

---

## CC-10 — the device an authorised purchase must work with

**Status**: applied, and **not yet satisfiable by the catalog**.

**Issue.** The product requirement that reached this round names four starting
conditions: headphone type, price range, **connection device type**, and the
extent of the authorisation. Three of the four had a home. The device did not:
`Connection` is wired-or-wireless, and no field anywhere in the system said what
a product can be plugged *into*. A requirement nothing can check is not a
requirement, and inferring one from a product's use cases ("打游戏" therefore
"works with a console") is exactly the fabrication the problem statement
forbids.

**Alternatives considered**

| Option | Why not |
|---|---|
| Infer devices from `use_cases` in A | An unverifiable claim presented as a product fact. `gaming` is what a person does; a console is what they own |
| Put it on `HardConstraints` only | D's repository is the filter, and its table has no such column, so the constraint would be silently ignored -- a filter the user believes is on |
| Leave it unimplemented and document the gap | Then the round ships a requirement it cannot enforce, and C has nothing to fail closed on |

**Change**

* `app/contracts/product.py` — `DEVICES`, `Device`, and
  `Product.supported_devices: list[Device] | None = None`. `None` means **not
  recorded**, and a not-recorded list never satisfies a requirement.
* `app/contracts/commerce.py` — `Quote.supported_devices`, copied by C from the
  catalog at pricing time, so the evaluator stays a pure function over recorded
  facts. It is **not** in `Quote.hashable()`: the hash commits to what was
  priced, and a device list is not priced.
* `app/contracts/mandate.py` — `MandateDraft.required_device` and
  `Mandate.required_device`, in `executable_policy()` so the clause is inside the
  signed hash.
* `app/contracts/common.py` — `ErrorCode.DEVICE_REQUIREMENT_NOT_MET`, in
  `POLICY_DENIAL_CODES`, so it is a handled business outcome rather than a 4xx.
* `app/commerce/policy_evaluator.py` — rule 9b, immediately after the ANC check
  and with the same fail-closed shape: a product that declares a different device,
  **or declares none**, is refused.

**A impact**: `required_device` is parsed only when the user *demands* a device
("必须支持游戏主机"). A mention ("平时连手机") stays profile context, which is what
keeps an inferred fact out of a hard constraint. See
`app/agent/profile_flow.py::DEVICE_REQUIREMENT_WORDS`.

**C impact**: one evaluator rule and one quote field. No new table, no new
transaction, no new service.

**B impact — read this before wiring a real B**: `Product.supported_devices` is
now part of the product contract and **every row in the seed leaves it null**.
B may report it; B must not invent it. Until D records the data, any mandate that
requires a device refuses every purchase with
`DEVICE_REQUIREMENT_NOT_MET`, and that refusal is the correct behaviour rather
than a bug to work around.

**D impact — the one thing this round could not do for you**: `products` has no
`supported_devices` column, and this round deliberately did not add one (the seed
is D's, and inventing device support for 40 demo SKUs would be inventing data).
`Product.to_row()` therefore **drops** the key rather than writing a column that
does not exist. Wiring it up is a three-part change, all of it D's: a column in
`app/db/schema.py` (JSON array, `NULL` allowed), the values in the catalog data,
and `Product.supported_devices` in `app/catalog/models.py`. Until then the field
is carried and checked but always unknown, which is the honest state.

**Tested against the current catalog**: `tests/data/test_catalog_data.py` asserts
the column is *still* absent, so the day device data lands, that test fails and
points at the three files to change.

### The catalog moved to v4, and the seed is now derived from it

`data/headphone-database-catalog-v4(1).sqlite3` is D's authoring catalog. It
carries the same 40 products with the same identifiers, the same prices, stock,
ANC flags, connections, form factors, battery figures, weights, use cases, seller
descriptions and shipping origins — and **rewritten brands and product names**
(`Yunsheng Demo` → `Auralis Audio`, and so on). It also adds two datasets that
nothing imports yet: `product_reviews` (400 synthetic reviews) and
`catalog_observations` (8 records whose `source_type` is
`OBSERVED_PUBLIC_SOURCE`, with a `record_hash` and `evidence_status = 'partial'`).

`data/products.seed.json` remains **the path the code loads** — `initialize_database`
reads it on every start — and it is now regenerated from the authoring database.
The two are pinned together by the tests above, so an edit to one without the
other fails there rather than in a demo.

Two consequences worth knowing before the next demo:

* the runtime database `var/demo.sqlite3` is **not** reconverged by the loader:
  `insert_missing_products` only inserts, deliberately, so an existing row keeps
  its original brand and name. Its 40 display fields were refreshed once, by a
  surgical update that left stock, price, orders, mandates, the wallet balance
  and the 24-event audit chain untouched (reconciliation and chain verification
  both still pass). A future catalog rename needs the same treatment, and there
  is no tool for it.
* the 8 observation records are the first `OBSERVED_PUBLIC_SOURCE` data in this
  repository. `docs/evidence_sources.md` says nothing in the system carries that
  label; that statement is now out of date, and the observations are D's to wire
  into a product-snapshot path before any of it reaches a quote.


**Tests**: `tests/commerce/test_final_check.py::TestAHardConstraintMismatchIsRefusedByC`,
`tests/agent/test_profile.py::TestTheParserCarriesTheProfile`.

---

## CC-11 — the profile chain, and why it is not a second source of authority

**Status**: applied.

**Issue.** The round's product requirement asks A to collect a user profile, derive
soft preferences from it, keep stated and inferred facts apart, and re-open the
requirement when a user rejects a recommendation. None of that existed: the
session had an unwritten `preferences` field, and the only "no" the system
understood was an answer to an escalation.

**Change**

* `app/contracts/profile.py` (new) — `ProfileFact` (field, value, `FactSource`,
  `evidence_quote`), `UserProfile`, `derive_soft_preferences`,
  `preference_profile_from`. **Nothing in this module can produce a
  `HardConstraints`**, which is the property the design rests on; a test asserts
  the module does not even import the type.
* `app/contracts/agent.py` — `IntentResult.profile`, `SessionContext.current_profile`,
  `AgentResponse.profile`, `Intent.REJECT_RECOMMENDATION`,
  `NextAction.DESCRIBE_PREFERENCES`.
* `app/agent/profile_flow.py` (new) — reading a self-description, the one
  question worth asking next, and reading a rejection.
* `app/agent/session.py` — `profile`, `remember_profile`, `soft_preferences()`
  (derived on every read; the unwritten `preferences` field is gone rather than
  duplicated).
* `app/agent/browsing.py` — `SearchRequest.preferences` is now populated from the
  profile, so a stated preference actually re-orders what B returns.

**Why provenance is in the contract rather than in a comment**: a user who is
told "you care about noise cancelling" when they never said so has been told
something false about themselves, and the fix has to be one field on a model
rather than an argument about a ranking.

**A impact**: three response fields and one intent. Nothing in the chain can set
a traceable field, so a profile can never satisfy or defeat the check for an
invented budget.

**C impact**: none. A payment is authorised by a mandate, never by a profile.

**B impact**: `SearchRequest.preferences` now arrives populated and ordered,
explicit facts before inferred ones. B must not re-sort it, must not treat a
criterion as a filter, and must report a criterion it cannot evaluate in
`GapReport` rather than guessing -- all of which the existing contract already
says. `use_cases` is populated only from facts the user stated.

---

## Open questions this work did not decide

| ID | Question | Why it is still open |
|---|---|---|
| D-01 | Is real B in-process, HTTP, or does A hand it product data? | B's module does not exist; the `SearchClient` Protocol is unchanged either way |
| D-02 | Is real C in-process or HTTP? | Same. `CommerceService` satisfies the Protocol structurally, so an HTTP client replaces it in `app/main.py` and nowhere else |
| D-10 | Does C persist the audit chain, or does C emit events and D store them? | Implemented as C owns the table through D's schema hook, because that is what the hook is for. If D is to own it, the `AuditRepository` is the seam |
| D-11 | The content of AC-01 … AC-16 | Fourteen have no definition in this repository. Two do, and are labelled in `tests/integration/test_end_to_end.py` |
| D-12 | How `PriorityPreset` affects ordering | B's decision, and B is not implemented |
| D-15 | Does D record `supported_devices` on the seed? | CC-10 carries and checks the field; the values are D's data, and this round did not invent them. Until they exist, a device requirement refuses every purchase |
| — | Whether `approve_escalation`'s new shape should also return the reservation | It returns the grant, which is what the answer produces. The reservation follows on re-submission and is read back through `get_proposal_outcome` |
