# Evidence sources

Every rate and every parameter this system uses, where it came from, and how it
is labelled. The problem statement is explicit that a fabricated rate is
fabrication, so the rule here is that **a number either has a source or says it
does not.**

The categories are the contract's own (`SourceType`), and only one of them may
ever be presented as data about the world:

| `SourceType` | Meaning | May be presented as observed? |
|---|---|---|
| `OBSERVED_PUBLIC_SOURCE` | a human read it from a public source and recorded the date | ✅ yes |
| `SYNTHETIC_DEMO` | constructed for the demo | ❌ no |
| `SANDBOX` | a stand-in for a real rate or a real movement | ❌ no |

The integrated v2.1 edition imports dated public-source observations separately
from the 40 fictional SKUs. See `docs/integration-v2.1.md`: six JD product
snapshots and two bank-rule observations are read-only evidence. They do not
replace unknown prices/stock or become payment authority. The original parameter
register below describes C's sandbox settlement; its reward value remains zero.
The conditional card calculator is a separate read-only estimate.

---

## 1. Parameter register

| Value | Where | Amount | `SourceType` | Origin | Future |
|---|---|---|---|---|---|
| Flat shipping | `app/commerce/config.py::SANDBOX_SHIPPING_CENTS` | HK$10.00 | `SANDBOX` | chosen for the demo; the catalog has no shipping field and no carrier was consulted | a real rate with an `evidence_id` and a `valid_until` |
| Platform fee | `SANDBOX_PLATFORM_FEE_CENTS` | HK$0.00 | `SANDBOX` | no fee schedule exists; recorded as zero rather than omitted so the receipt still reconciles | a real schedule |
| Tax | `SANDBOX_TAX_CENTS` | HK$0.00 | `SANDBOX` | no tax rule exists for the fictional catalog | a real rule |
| Discount | `SANDBOX_DISCOUNT_CENTS` | HK$0.00 | `SANDBOX` | no discount programme is implemented | — |
| FX cost | `SandboxPaymentRouter.price` | HK$0.00 | `SANDBOX` | the settlement currency is pinned to HKD, so there is no conversion to cost. Reported as zero rather than omitted | the reserved FX seam in `app/contracts/common.py` |
| Reward value | `SandboxPaymentRouter.price` | HK$0.00 | `SANDBOX` | no reward programme is implemented; reported as zero so `effective_cost_cents` still reconciles | an OPTIONAL work centre |
| Wallet opening balance | `DEMO_WALLET_OPENING_BALANCE_CENTS` | HK$5,000.00 | `SANDBOX` | chosen so the demo can make several purchases; seeded once and never reset | a real ledger |
| Payment settlement | `SandboxPaymentAdapter` | — | `SANDBOX` | the adapter moves no money and opens no socket; it is deterministic and returns a reference derived from the attempt id | a real rail client |

Every one of these is reachable by grep from this table, and each appears with
its label on the surface that shows it:

* the quote's `source_type` is `SANDBOX`, and `describe_quote` prints
  `（运费与手续费来源于 SANDBOX 参数，不是实测费率）`;
* a settlement's `ProposalOutcome.settlement_source_type` is `SANDBOX`, the audit
  payload records `adapter: "sandbox"`, and the reply says
  `（支付由 SANDBOX 沙箱通道完成，没有真实扣款）`;
* `/api/v1/health` reports `commerce.settlement_source_type`, and the demo page
  carries the same statement in its banner.

## 2. Product data

**Source**: `data/products.seed.json`, 40 rows, owned by D.

| Property | Value |
|---|---|
| Currency | HKD, integer hundredths (`27900` = HK$279.00) |
| Origin | synthetic, written for the demo |
| `source_type` on every row | `demo` |
| `source_url` on every row | `null` — enforced by the schema: a demo row may not carry a URL |
| `seller_description` | a fictional vendor blurb, fewer than 100 characters |
| `shipping_origin` | a fictional location |

The catalog is the one thing in the system that is *real in structure and
synthetic in content*: it is a real SQLite table with real constraints, populated
with rows that are explicitly marked as demo data. Nothing in it claims to
describe a product for sale.

⛔ **SPEC GAP**: `data_note` on each row says what the row is; there is no
recorded decision about which real products (if any) should replace them.

## 3. Rates the demo does *not* claim

| Rate | Status |
|---|---|
| A real FPS or card fee schedule | not collected. `SANDBOX_PLATFORM_FEE_CENTS` stands in and is labelled |
| A real FX rate | not collected. The settlement currency is pinned to HKD and there is no conversion |
| Real reward or cashback values | not collected. Rewards are reported as zero |
| Real shipping quotes | not collected |
| Timestamps for any of the above | not applicable, because none of them was observed |

The manual comparison referenced by the problem statement's EVIDENCE requirement
is `docs/manual_path_baseline.md`, which predates this work and is unchanged.

## 4. What is genuinely observed, and therefore unlabelled

These are measurements rather than parameters, and they need no source label
because they are reproducible from the repository:

| Measurement | Where it is recorded |
|---|---|
| Test counts | this document's sibling, `docs/A_C_data_handoff.md` §9, and the commit messages |
| The evaluator's rule set (30 comparisons, 27 slots) | `docs/handoff-to-next-agent.md` Part 9, unchanged by this work |
| The policy hash of a mandate | computed at activation and printed to the user: `describe_mandate` shows `policy_hash`, and `/api/v1/audit/{mandate_id}/verify` recomputes the chain |
| LLM format behaviour | `docs/LLM_FORMAT_PROBE.md`, from the probe script |

## 5. How a real rate would be added

The seams already exist, so this is a data change rather than a redesign:

1. record the rate with its source and the date it was read, and set
   `source_type = OBSERVED_PUBLIC_SOURCE`;
2. give it an `evidence_id`, which `Quote.source_ref` and
   `PaymentRouteEvaluation.evidence_id` already carry;
3. for FX specifically, `app/contracts/common.py` reserves the whole design:
   integer parts-per-million rates, an `as_of`, a `valid_until`, and the two
   policy checks that would go with them. None of it is implemented, and none of
   it needs renaming when it is.
