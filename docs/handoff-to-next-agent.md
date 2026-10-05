# 交接文档（工程化版）：完整审查 + HTTP 部署 + B/C/D 接入规范

**版本**：v3。v2 描述「当前什么样」与「未来 B/C/D 怎么接」；v3 记录 **A + C 垂直切片已经跑通**，
并把 v2 中已被实现取代的部分逐处标注。

**实测状态（v3）**：`659 passed, 1 skipped, 32 subtests`，36 个提交，工作区干净，130 个受控文件。
**v2 基线**：`469 passed, 1 skipped`，29 个提交，92 个文件。

**适用对象**：下一位接手本仓库的 AI / 开发者；以及未来接入的**真实** B、C、D。

---

# Part 0 — v3 变更摘要（**先读这一节**）

本轮工作限定在 **A + C**：A 负责交互/意图/Proposal/解释，C 负责授权/判定/预留/凭证/支付/审计。
B 与 D 未动（`app/search/` 仍然不存在；`app/catalog/` + `app/db/` 仍是真实实现），
但两边的接入边界、数据交接和替换路径都保持完好并已文档化。

## 0.1 现在有什么

| 模块 | v2 | v3 |
|---|---|---|
| `app/contracts/` | ✅ 9 文件 | ✅ 9 文件，**有改动**：见 `docs/A_C_contract_changes.md` |
| `app/agent/` | 🟡 mandate / 支付未接 | ✅ 对话 + mandate + 报价 + Proposal + 判定/升格/回执全部接通 |
| `app/commerce/` | 🟡 只有 `policy_evaluator.py` | ✅ 16 文件：表、仓储、审计链、mandate/quote/spend_state/authority/escalation/capability/payment/reconcile/service |
| `app/search/` | ⛔ 不存在 | ⛔ **仍不存在**（不是本轮范围） |
| `app/catalog/` + `app/db/` | ✅ 真实 | ✅ **零改动** |
| `app/main.py` + `app/api/` | ⛔ 不存在 | ✅ composition root + 3 个 A 端点 + 2 个只读记录端点 + 演示页 |
| `tests/commerce/` | 1 文件 53 测试 | ✅ 6 文件 188 测试 |
| `tests/agent/` | 5 文件 | ✅ 6 文件（`test_clients.py` → 已删，commerce 部分迁入 `tests/commerce/`） |
| `tests/api/` | ⛔ 不存在 | ✅ 19 测试 |
| `tests/integration/` | ⛔ 不存在 | ✅ 23 测试（含 AC-13 / AC-16） |
| `LocalCommerceClient` | 🟡 C 的替身 | ✅ **已删除**，`CommerceService` 实现同一个 Protocol |

## 0.2 一条真实的 A → C 流程

```
用户：授权你替我买耳机：每笔不超过 320，24 小时内总共不超过 600，5 分钟最多 2 笔，
      一共买 3 件，超过 300 先问我，只用 FPS，送到 addr_demo_01，有效期 7 天
  A  → MandateDraft（每个金额都能追回原话）
  A  → 摘要（A 建议的条款标「我建议的」）
用户：确认授权
  A  → C.activate_mandate(draft, principal_id, agent_id)
  C  → Mandate v1 + policy_hash + consent_event_id（写 CONSENT_RECORDED + MANDATE_ACTIVATED）
用户：找 350 以内、无线、必须支持主动降噪的耳机      → B（替身）→ 候选
用户：第一款
  A  → C.create_quote(product_id, quantity)          ← A 不给金额
  C  → Quote（价格来自 D，运费是 SANDBOX 参数，quote_hash 可复算）
用户：买吧
  A  → PurchaseProposal（13 字段，全是标识与意图）
  A  → C.submit_proposal(proposal)
       BEGIN IMMEDIATE
         读 proposal / mandate / quote / route / SpendState（C 自己的记录）
         evaluate_policy()                        ← 纯函数，30 项检查
         APPROVE → 写 Reservation
       COMMIT
       → 签发 capability → 立刻消费 nonce → 调用 SANDBOX adapter → 扣钱包 → 扣库存
       → PaymentReceipt + AuditEvent
  A  → C.get_proposal_outcome(proposal_id)
  A  → 解释：金额、订单号、余额、规则指纹，并标注 SANDBOX
```

被拦住的那一笔（同一份授权，hp_0003 到手 HK$509 > 单笔上限 HK$320）：
`DENY / CAP_PER_TRANSACTION_EXCEEDED`，写 `DenialReceipt`，**不建预留、不调用支付**，
回复里给出 `实际 HK$509.00，上限 HK$320.00`。

## 0.3 本轮确立的架构事实（可核对）

| 断言 | 怎么核对 |
|---|---|
| A 不能批准、预留、支付、签发凭证 | `tests/integration/test_end_to_end.py::test_the_agent_has_no_call_that_can_pay` |
| A 提交物结构上不含权威数值 | `TestAuthorityBoundaries::test_ac13_a_proposal_cannot_carry_an_amount` |
| B ↛ C | 无 import 路径；`app/commerce/` 不 import `app/agent/` |
| C 是唯一动钱的地方 | `PaymentService` 只被 `CommerceService.submit_proposal` 调用 |
| 判定与预留同一事务 | `AuthorityService.submit` 的 `BEGIN IMMEDIATE`；`SpendStateService.compute` 收连接而非自开 |
| ApprovalGrant 一次性 | `GrantRepository.consume` 的条件 UPDATE；`tests/commerce/test_escalation.py` |
| capability 重放被拒 | `CapabilityService.spend`；`tests/commerce/test_capability.py` |
| 审计链 append-only | `audit_events` 上的两个 trigger；`tests/commerce/test_audit.py` |
| 假值都有标注 | `docs/evidence_sources.md`；`SourceType.SANDBOX` 出现在 quote / outcome / audit / 回复里 |

## 0.4 v2 中已被取代的内容（逐处标注）

| v2 位置 | 状态 |
|---|---|
| §1.2 模块状态表 | ⚠️ 已被 §0.1 取代 |
| §1.3 边界清单 | ⚠️ `CommerceClient` 的方法数由 7 变 11，见 §0.5 |
| §1.4 假实现 | ⚠️ `LocalCommerceClient` 已删除；只剩 `LocalSearchClient` |
| Part 2 `G-1` `G-3` `G-7` `G-8` | ✅ 已解决 |
| Part 3.3 的 7 个方法与 4 个 SPEC GAP | ⚠️ 见 §0.5 与 `docs/A_C_contract_changes.md` |
| Part 5.2 H-C-04（`approve_escalation` 形状） | ✅ 已收窄，见 CC-1 |
| Part 7.1 `FakeCommerceClient` | ✅ 已删除 |
| Part 8 F-1（`merchant_id` 写死） | ✅ 改为 C 的 `merchant_of_record()`，见 CC-4 |
| Part 8 F-5（`spend_state` 恒零） | ✅ 已实现真实记账；**并且修掉了窗口起点导致限额被跳过的问题** |
| Part 11 HTTP | ✅ 已实现，见 §0.6 |
| Part 13 D-03 / D-04 / D-05 / D-06 / D-08 / D-09 | ⚠️ 见 §0.7 |
| Part 16.1 现在 | ⚠️ 已被 §0.2 取代 |
| Part 18 DoD | ⚠️ 见 §0.8 |

## 0.5 `CommerceClient` 的最终形状（11 个方法）

原 7 个签名一字未改：

```python
activate_mandate(draft, *, principal_id, agent_id) -> Mandate
get_mandate(mandate_id) -> Mandate | None
revoke_mandate(mandate_id) -> Mandate
create_quote(*, product_id, quantity=1) -> Quote
spend_state(mandate) -> SpendState
submit_proposal(proposal, *, now=None) -> PolicyDecision
approve_escalation(proposal_id, *, principal_id) -> ApprovalGrant   # ← 收窄
```

新增 4 个：

```python
reject_escalation(proposal_id, *, principal_id) -> EscalationRequest
get_proposal_outcome(proposal_id) -> ProposalOutcome | None
merchant_of_record() -> str
payment_routes() -> list[str]
```

四个可选 `now=` 关键字参数**追加在末尾且 keyword-only**，没有任何位置调用点改变含义。
每条改动的 Issue / Alternative / Impact / Test / Migration 见 `docs/A_C_contract_changes.md`。

## 0.6 HTTP 现状

| 方法 | 路径 | 归属 | 说明 |
|---|---|---|---|
| GET | `/` | A | 演示页（单文件，只渲染，不计算） |
| GET | `/api/v1/health` | A | D 的 `database_status()` + C 的 reconciliation 摘要 |
| POST | `/api/v1/agent/chat` | A | 对话主入口 |
| POST | `/api/v1/agent/actions` | A | 显式动作（按钮），**不经过 parser** |
| GET | `/api/v1/products/{product_id}` | D | D 已实现，A 用 `create_router()` 注入包装与 404 映射 |
| GET | `/api/v1/transactions/{proposal_id}` | C 的只读视图 | `ProposalOutcome` + 审计事件 + reconciliation |
| GET | `/api/v1/audit/{mandate_id}` | C 的只读视图 | 该 mandate 的链 |
| GET | `/api/v1/audit/{mandate_id}/verify` | C 的只读视图 | 重算**整条**链并定位第一个断点 |

⚠️ 计划 §8 写的是 `/audit/{session_id}`；`AuditEvent` 没有 session 字段，
链是按 `mandate_id` / `proposal_id` 归属的，所以用 mandate id，理由写在路由的 docstring 里。

信封、错误码、状态映射与 v2 一致；补了一条 v1 漏掉的：**请求体不合契约**也走信封
（`VALIDATION_ERROR`，`details.problems` 是数组，`details` 仍是对象）。

## 0.7 Decision Log 更新（v2 Part 13）

| ID | v2 状态 | v3 |
|---|---|---|
| D-01 真实 B 形态 | ⬜ 未决 | ⬜ **仍未决**。`SearchClient` 未改，任何一种形态都能接 |
| D-02 真实 C 形态 | ⬜ 未决 | ⬜ **仍未决**。`CommerceService` 结构化满足 Protocol，换成 HTTP client 只改 `app/main.py` 一行 |
| D-03 拒收据/升格如何返回 | ⬜ 未决 | ✅ **已解决（可回退）**：新增只读的 `ProposalOutcome`，写路径未变。见 CC-3 |
| D-04 支付如何从 A 触发 | ⬜ 未决 | ✅ **已解决（安全侧）**：A **没有**触发支付的方法。`submit_proposal` 内部完成预留与结算；A 只读回执 |
| D-05 ApprovalGrant 谁消费 | ⬜ 未决 | ✅ **已解决**：C 在创建预留的同一事务里用条件 UPDATE 消费。见 CC-1 / §0.3 |
| D-06 `approve_escalation` 是否收窄 | ⬜ 未决 | ✅ **已收窄**，理由：旧形状 A 根本无法调用。见 CC-1 |
| D-07 是否加模拟曝光 | ⬜ 未决 | ✅ **已不需要**：C 有真实记账，rolling cap / velocity / quantity 都能真正触发 |
| D-08 `httpx` 是否入锁 | ⬜ 未决 | ✅ **已加入** `httpx==0.28.1`（新增，非升级）。见 CC-9 |
| D-09 `merchant_id` 从哪来 | ⬜ 未决 | 🟡 **已给出可运行答案**：C 的 `merchant_of_record()`，A 在摘要里标注为「我建议的」，用户随摘要一起确认。真正的修法是 B 在候选上带 merchant，见 CC-4 |
| D-10 审计链谁持久化 | ⬜ 未决 | 🟡 **实现为 C 经 D 的 schema hook 持有**；若要改为 D 持有，`AuditRepository` 是接缝 |
| D-11 AC-01…AC-16 内容 | ⛔ SPEC GAP | ⛔ **仍未决**。本仓库只落实了有出处的 AC-13 / AC-16，其余 14 条**没有编造** |
| D-12 `PriorityPreset` 如何参与排序 | ⬜ 未决 | ⬜ 仍未决（B 的范围） |
| D-13 赛道申报 | ❓ | 不变，非本仓库可回答 |
| D-14 B 和 C 是否在实际工作 | ⬜ | C 已由本轮完成；B 仍无实现 |

## 0.8 Part 18 DoD 的现状

| 项 | v3 |
|---|---|
| `/health` `/chat` `/actions` | ✅ |
| `/products/{id}` | ✅ 已挂载 |
| Integration / E2E tests | ✅ `tests/integration/` 23 项 |
| BCD boundary tests | ✅ `tests/commerce/test_service.py`、`tests/agent/test_commerce_turns.py` |
| AC-01…AC-16 | ⛔ 仍缺 14 条定义 |
| 前端 | 🟡 单页演示页 `app/api/demo.html`（**不是**完整前端） |

## 0.9 本轮新增的文档

| 文档 | 内容 |
|---|---|
| `docs/A_C_contract_changes.md` | 每一处共享契约改动的 Issue / Alternative / Impact / Test / Migration，以及若干语义决定 |
| `docs/A_C_data_handoff.md` | A→B、B→A、A→C、C→A、C→D、D→C 的逐字段交接表 + Fake→Real 映射 |
| `docs/evidence_sources.md` | 每一个费率的来源与标注，以及「没有采集什么」 |

## 0.10 本轮新增的工具

```powershell
python scripts/demo_conversation.py --no-llm --delegated   # 授权→购买 的脚本化演示
python scripts/inspect_commerce.py --db var/demo.sqlite3   # 授权/账本/链/对账 只读检查
python scripts/reset_demo.py --db var/demo.sqlite3 --yes    # 协同重建（先备份，拒绝无 --yes）
uvicorn app.main:app --reload                                # HTTP + 演示页
```


---

## 本文档的记号约定

本文档最核心的纪律是：**只写现有规格、契约与源码能支持的内容。** 无法确定的，一律显式标记，由人裁定。

| 记号 | 含义 |
|---|---|
| ✅ **CONFIRMED** | 有源码 / 契约 / 文档为据，可直接执行 |
| 🟡 **EXPECTED** | 由现有架构必然导出，但尚未被规格明文确认 |
| 🔵 **EXTENSION** | 未来扩展点，当前**不存在**，不得当作已定义 |
| ⛔ **SPEC GAP** | 现有资料不足以决定。**不要猜**，见 §19 Decision Log |
| ❗ **HUMAN DECISION REQUIRED** | 需要人做选择，agent 不得代决 |
| ⚠️ **INTEGRATION COUPLING RISK** | 会让 Fake → Real 替换变贵的耦合 |

**三条场景规则**，后文反复引用：

- **S1 演示期（现在 → Code Freeze）**：Fake B/C/D 可用。目标是**跑通并演示**。
- **S2 替换期（B/C/D 落地时）**：只允许替换 adapter / client / service implementation。
- **S3 目标态**：A → B、A → C、A → D 全部为真实实现，A 的核心逻辑与契约不变。

> **贯穿全文的一句话**：现在可以用假数据把系统跑起来，但**不能因为是假数据，就把未来真实 B/C/D 接入所需的接口、数据交接、返回数据、状态与 authority 边界写死**。

---

# Part 1 — Current State Audit

## 1.1 仓库形态

| 项 | 值 | 依据 |
|---|---|---|
| 工作目录 | `D:\HackU` | ✅ |
| 形态 | **单一扁平仓库**，无嵌套子仓库 | ✅ `git ls-files` 92 文件 |
| 提交数 | 29（两段历史已合并） | ✅ `git rev-list --count HEAD` |
| 测试 | 469 passed, 1 skipped, 32 subtests | ✅ 实测 |
| Python | 3.11.9 | ✅ 实测 |
| 行尾 | 全仓库 LF（`.gitattributes` 强制） | ✅ `git ls-files --eol` |

## 1.2 各模块实现状态

> ⚠️ **v2 快照，已被 Part 0.1 取代。** 保留在此是为了对照，不要据此判断现状。

| 模块 | 归属 | 文件 | 行数 | 状态 |
|---|---|---|---|---|
| `app/contracts/` | A | 9 | 3502 | ✅ 完成，有测试 |
| `app/agent/` | A | 13 | 3608 | 🟡 对话循环完成；mandate 流程与支付**未接** |
| `app/commerce/` | C | 2 | 525 | 🟡 **只有 `policy_evaluator.py`**（30 项检查，纯函数） |
| `app/search/` | B | 0 | 0 | ⛔ **不存在** |
| `app/catalog/` + `app/db/` | D | 9 | 563 | ✅ 完成，31 测试 |
| `app/main.py` / `app/api/` | A | 0 | 0 | ⛔ **不存在** |
| `tests/integration/` | A | 0 | 0 | ⛔ **不存在** |

## 1.3 边界清单（**这是接入的锚点**）

> ⚠️ **v2 快照。** `CommerceClient` 现在是 11 个方法（Part 0.5）；`app/api/` 已存在。

| 边界 | 载体 | 状态 |
|---|---|---|
| **A → B** | `app/agent/clients.py::SearchClient`（1 个方法） | ✅ 已定义 |
| **A → C** | `app/agent/clients.py::CommerceClient`（7 个方法） | ✅ 已定义 |
| **A → D** | 无直接边界；A 经 B 拿商品，经 C 拿报价 | 🟡 见 §3.4 |
| **B → C** | **禁止存在** | ✅ 无调用路径 |
| **C → D** | 经 D 的 `initialize_database` 两个回调 | ✅ 钩子已存在，C 未实现 |
| **D → A（HTTP）** | `app/catalog/routes.py::create_router` | ✅ 已实现 |
| **A → 前端** | `AgentResponse` | ✅ 已定义；前端未写 |

## 1.4 假实现在哪里

> ⚠️ **v2 快照。** `LocalCommerceClient` 已删除；现在只有 `LocalSearchClient` 一个替身。
> 支付是 `SandboxPaymentAdapter`（`app/commerce/adapters/sandbox.py`），标注为 SANDBOX。

| 假实现 | 文件 | 行数 | 替换为 |
|---|---|---|---|
| `LocalSearchClient` | `app/agent/local_search.py` | 330 | 真实 B 的 adapter |
| `LocalCommerceClient` | `app/agent/local_commerce.py` | 343 | 真实 C 的 adapter |

**关键性质 ✅**：`LocalCommerceClient` **不含任何策略**。它调用真实的 `app.commerce.policy_evaluator.evaluate_policy`。所以演示时看到的是**真规则在跑**，只有存储是临时的。

> 这是本次架构最重要的一条：**假的是存储，不是判定。**

---

# Part 2 — Architecture Gaps

> ⚠️ **v2 快照。** G-1 / G-3 / G-7 / G-8 已解决；G-2（`app/search/`）、G-4（AC 定义）、
> G-5（完整前端）仍开放。见 Part 0.4。

按严重度排序。P0/P1/P2/P3 定义见 Part 12。

| # | 缺口 | 严重度 | 影响 | 处置 |
|---|---|---|---|---|
| G-1 | **`app/main.py` / `app/api/` 不存在** | P1 | 无法演示、无法 HTTP 验收 | 任务 B |
| G-2 | **`app/search/` 不存在** | P1（演示期）/ P0（目标态） | A 目前经 `LocalSearchClient` 读 D 的仓库 | 见 §3.2 |
| G-3 | **`app/commerce/` 缺 8 张表与 mandate/quote/reservation/capability/payment** | P1 | A 无法真正花钱 | C 的 Phase 3–8 |
| G-4 | **AC-01…AC-16 未定义** | P1 | Phase 12 无验收标准 | ⛔ **SPEC GAP**，见 Part 10 |
| G-5 | **前端不存在**，但用户明确要求过 | P2 | 演示只能看 CLI | 任务 B 第 4 步 |
| G-6 | **`LocalCommerceClient` 把 `demo_audio_store` 写死在报价里** | ⚠️ P2 | 见 Part 8 F-3 | 接线时改为来自配置 / 真实 B |
| G-7 | **A 的 mandate 流程未接**（`MandateDraft` 可解析，但无 builder/orchestrator 分支） | P1 | 第二种「同意」无法演示 | 需实现 |
| G-8 | **`docs/A_C_contract.md` 等 4 份文档待产出** | P2 | A→C 契约只存在于计划 §5.2 | 见 Part 5 |
| G-9 | **B↔C 之间无协调机制**，若未来 C 需要 B 的商品信息 | 🔵 P2 | 见 §3.5 | 保持零调用，经 A 转交 |

---

# Part 3 — BCD Interface Map

## 3.1 总表

| Module | Current Implementation | Future Real Implementation | Interface Boundary | Current Caller | Future Caller | Status |
|---|---|---|---|---|---|---|
| **B** 搜索/信息 | `LocalSearchClient`（`app/agent/local_search.py`） | 真实 B（`app/search/`） | `app/agent/clients.py::SearchClient` | `app/agent/browsing.py::BrowsingTurns` | 同左，**不变** | 🟡 接口已定义，实现是假的 |
| **C** 商务/授权/交易 | `LocalCommerceClient`（`app/agent/local_commerce.py`） | 真实 C（`app/commerce/` 其余） | `app/agent/clients.py::CommerceClient` | `AgentOrchestrator`（构造注入，**当前未调用**） | 同左，**不变** | 🟡 接口已定义，实现是假的 |
| **D** 数据/持久化 | 真实实现（`app/catalog/`、`app/db/`） | 同左 | `ProductRepository` + `create_router` | `LocalSearchClient`、`LocalCommerceClient` | B、C | ✅ **D 已经是真的** |

> ⚠️ **注意 D 的特殊性**：D **不是假的**。`app/catalog/` + `app/db/` 是完成的真实实现，40 条真实（虚构但结构化）HKD 商品。所以 Part 8 里 D 相关的「硬编码」性质与 B/C 不同。

## 3.2 B — Search / Information Provider

**接口（✅ CONFIRMED，逐字来自源码）**

```python
class SearchClient(Protocol):
    def search(self, request: SearchRequest) -> SearchResponse: ...
```

**A 给 B 什么**：一个 `SearchRequest`，含 `constraints: HardConstraints`、`preferences: PreferenceProfile`、`comparison: ComparisonFrame`、`limit: int`（默认 3）。字段全集见 Part 5。

**B 返回 A 什么**：一个 `SearchResponse`，含 `total_matches`、`returned`、`applied_constraints`、`candidates: list[Candidate]`、`comparison: list[ComparisonRow]`、`gaps: GapReport`、`relax_hints: RelaxHints | None`、`suggestions`、`currency`。

**B 不应该决定什么** ✅ 有明文依据（`docs/handoff-to-B.md` §2、`docs/A2B_..._v1.1.md`）

| B 不做 | 依据 |
|---|---|
| 不放宽条件 | 「B 报告事实，不放宽」 |
| 不判断「301 跟 300 差不多」 | 硬约束含上界，机械可复算 |
| 不生成文案 | B 只返回带 `field` 的 `MatchReason` |
| 不做第 ③ 层判定（`SATISFIED`/`NEEDS_RELAXATION`/`IMPOSSIBLE`） | 那是 A 的，**因为 B 看不到用户** |

**B 不应该访问什么** ✅ 有明文依据

- **不得接触钱**：无 `Quote`、无 `Mandate`、无 `SpendState`
- **不得调用 C**：B/C 零调用是架构规则（计划 §2.2 规则 3）

**B 能否提供价格 / product / availability** ✅ CONFIRMED

| 信息 | 载体字段 | 来源 |
|---|---|---|
| 价格 | `Candidate.product.price_cents`（整数分，HKD） | `Product` ✅ |
| 商品 | `Candidate.product: Product`（20 字段） | `Product` ✅ |
| 可用性 | `Candidate.product.stock: int` | `Product` ✅ |
| 排序分 | `Candidate.match_score` | B 计算 |
| 违反的硬条件 | `Candidate.violated_fields`（只在 `relax_hints.closest_candidates` 里非空） | 契约强制 |

**哪些字段当前是假数据**（⚠️ 见 Part 8）

| 字段 | 当前值来源 | 说明 |
|---|---|---|
| `SearchResponse.candidates` 的顺序 | `LocalSearchClient._rank()` | 简化排序（criteria → 价格 → id），**不是 B 的五级排序** |
| `Candidate.match_score` | `LocalSearchClient._candidate()` | 公式是 B 的（10×用途命中 + 5×ANC），但**在多数用例下全平** |
| `SearchResponse.comparison` | `LocalSearchClient._comparison()` | 只处理 `_COMPARABLE` 里的 6 个维度 |
| `GapReport.unsupported_criteria` | 恒为空 | **真实 B 会填**（例如 `audio_quality` 无法评估） |
| `GapReport.dropped_dimensions` | 恒为空 | **真实 B 会填** |
| `RelaxHints.hints[].to_value` | `LocalSearchClient._minimal_value()` | 简化实现，真实 B 的语义更细 |
| `SearchResponse.suggestions` | 恒为空 | 未使用 |

⛔ **SPEC GAP（B）**：`app/search/` 不存在，因此**没有任何真实 B 的实现细节可依据**。上述「真实 B 会填」是 🔵 EXTENSION 推断，**不是已定义需求**。真实 B 的排序算法、`match_score` 的最终定义、`priority_preset` 如何参与排序——**全部由 B 的实现决定，当前无规格**。

## 3.3 C — Commerce / Authorization / Transaction Authority

> **C 是唯一能移动钱的区域。** 这一节的严格程度高于其它所有节。
>
> ⚠️ **v2 快照，已被实现取代。** 现行接口见 Part 0.5；GAP-C2/C3/C4 已解决，
> GAP-C1（真实 C 是进程内还是 HTTP）**仍未决**。逐条理由见 `docs/A_C_contract_changes.md`。

**接口（✅ CONFIRMED，逐字来自源码，共 7 个方法）**

```python
class CommerceClient(Protocol):
    def activate_mandate(self, draft: MandateDraft, *, principal_id: str,
                         agent_id: str) -> Mandate: ...
    def get_mandate(self, mandate_id: str) -> Mandate | None: ...
    def revoke_mandate(self, mandate_id: str) -> Mandate: ...
    def create_quote(self, *, product_id: str, quantity: int = 1) -> Quote: ...
    def spend_state(self, mandate: Mandate) -> SpendState: ...
    def submit_proposal(self, proposal: PurchaseProposal, *,
                        now: datetime | None = None) -> PolicyDecision: ...
    def approve_escalation(self, decision: PolicyDecision, *,
                           proposal: PurchaseProposal, quote: Quote,
                           route: PaymentRouteEvaluation) -> ApprovalGrant: ...
```

**A 给 C 什么**（详细到字段见 Part 5）

- `activate_mandate`：一个**用户已签字**的 `MandateDraft` + `principal_id` + `agent_id`
- `create_quote`：`product_id` + `quantity`。**A 不给金额。**
- `submit_proposal`：一个 `PurchaseProposal`（13 字段，**全部是标识与意图**）
- `approve_escalation`：`PolicyDecision` + `PurchaseProposal` + `Quote` + `PaymentRouteEvaluation`（都是 C 自己产出后回传的对象）

**C 读取什么** ✅ 由 `PolicyInputs` 决定（判定器文档明文）

```python
PolicyInputs(proposal, mandate, quote, route, spend_state, now, approval_grant)
```

C 从**自己的记录**读 mandate / quote / spend_state，**不从 A 读**。

**C 决定什么** ✅ CONFIRMED（`app/commerce/policy_evaluator.py`，30 项检查）

判定结果三态：`APPROVE` / `DENY` / `ESCALATE`。完整规则见 Part 9。

**C 返回什么** ✅ CONFIRMED

| 返回类型 | 何时 | 关键字段 |
|---|---|---|
| `Mandate` | 激活 / 查询 / 撤销 | `policy_hash`、`version`、`canonical_policy`、`consent_event_id` |
| `Quote` | 报价 | `cash_total` 的组成、`quote_hash` |
| `SpendState` | 查询额度 | `exposure_cents` / `exposure_count` / `exposure_quantity` |
| `PolicyDecision` | 每次提案 | `outcome`、`primary_reason`、`violations`、`observed_values`、`applicable_limits`、`reservation_created`、`payment_adapter_called` |
| `ApprovalGrant` | 用户答复升格 | 绑定 `quote_hash` + `payment_route_id` + `cash_total_cents` |
| `DenialReceipt` | 拒绝时（**当前 Protocol 无此方法** ⛔ GAP-C2） | 见 Part 6 |
| `PaymentReceipt` | 支付后（**当前 Protocol 无此方法** ⛔ GAP-C3） | 见 Part 6 |

**哪些状态只有 C 可以产生** ✅ CONFIRMED（由 `app/contracts/policy.py` 的 validator 强制）

| 状态 | 强制方式 |
|---|---|
| `PolicyDecision` 的 `outcome` | 只有 C 的纯函数产出 |
| `reservation_created` | `DENY`/`ESCALATE` 恒为 `False`（模型校验器） |
| `payment_adapter_called` | 判定器里硬编码为 `False`（只有支付服务会设为真） |
| `Mandate.policy_hash` / `version` / `consent_event_id` | A 从不生成 |
| `Reservation` / `PaymentReceipt` / `DenialReceipt` | A 无构造路径 |

**哪些字段 A 永远不能伪造** ✅ CONFIRMED

```
cash_total_cents · remaining_budget_cents · policy_hash · payment_status
wallet_balance_after_cents · reward_earned_cents · 任何批准决定
```

**强制方式**：`PurchaseProposal` 是 `ConfigDict(extra="forbid", frozen=True)`，提交上述任一字段**直接被拒绝**。

**Fake C 当前怎么模拟** ✅

| 行为 | Fake C 实现 | 真实性 |
|---|---|---|
| 决策 | 调**真实** `evaluate_policy` | ✅ **真** |
| mandate 存储 | 进程内 `dict` | ❌ 假（不持久、不事务安全） |
| 报价计算 | 自己算：`unit × qty + SANDBOX_SHIPPING` | 🟡 公式对，运费是虚构参数 |
| `SpendState` | **恒为全零曝光** | ❌ 假（且**刻意不伪造**，见下） |
| reservation / capability / payment | **完全不存在** | ⛔ 缺失 |
| 审计链 | **完全不写** | ⛔ 缺失 |

> **重要**：`spend_state()` **故意返回全零**，并且注释写明「Reporting a made-up `reserved_cents` would pass the cap check for the wrong reason and hide the fact that C's accounting is not built」。这是**诚实的设计**，不是遗漏。但也意味着：**当前的 rolling cap 检查永远通过**，因为它看不到任何曝光。见 Part 8 F-5。

**Real C 未来如何替换 Fake C** ✅

```python
# 现在
orchestrator = AgentOrchestrator(..., commerce=LocalCommerceClient())

# 未来（唯一需要改的一行）
orchestrator = AgentOrchestrator(..., commerce=HttpCommerceClient(...))
#                                     ^^^^^^^^^^^^^^^^^^ 实现同一个 Protocol
```

⛔ **SPEC GAP（C）**：

| ID | 缺口 | 说明 |
|---|---|---|
| GAP-C1 | **真实 C 是进程内服务还是 HTTP？** | 计划 DC1 说「进程内 Python 服务函数；FastAPI 仅作演示与调试壳」，**但 §8 又列了 11 个 C 的 HTTP 端点**。两者需要澄清。❗ |
| GAP-C2 | **拒绝如何传给 A？** | `DenialReceipt` 类型存在，但 `CommerceClient` 无对应方法。当前 `submit_proposal` 返回 `PolicyDecision`，拒收据需另取。❗ |
| GAP-C3 | **支付如何触发？** | `PaymentReceipt` 类型存在，`CommerceClient` 无 `pay` 方法。计划里是 `POST /api/v1/reservations/{id}/pay`。❗ |
| GAP-C4 | **`ApprovalGrant` 的消费由谁执行？** | 判定器只读不写（✅ 已核实）。「批准一次、只能用一次」需要一个写操作，**当前没有任何地方负责**。❗ |
| GAP-C5 | **capability 的签发与校验** | `ErrorCode` 里有 5 个 `CAPABILITY_*` 码，计划 DC2 说用 stdlib hmac。**无实现、无接口**。🔵 |

## 3.4 D — Data / Persistence / External Record Layer

**⚠️ 首先纠正一个可能的误解**：D **不是假的**。`app/catalog/` + `app/db/` 是真实的、完成度最高的模块。

**谁可以直接访问 D** ✅ CONFIRMED

| 主体 | 能否访问 D | 方式 | 依据 |
|---|---|---|---|
| **C** | ✅ 能 | 经 `initialize_database(commerce_schema=..., wallet_initializer=...)` 两个回调 | D 的 `app/db/initialize.py` 明文预留 |
| **B**（未来） | 🟡 **未定** | 当前 `LocalSearchClient` 直接持 `ProductRepository`；真实 B 是否如此 ⛔ SPEC GAP | — |
| **A** | ❌ **不应直接访问** | A 经 B 拿商品、经 C 拿报价 | 架构规则；当前 A 通过替身间接访问 |

⛔ **SPEC GAP（D-1）**：**真实 B 应该直接读 D 的数据库，还是经 D 的 HTTP 接口，还是 A 把商品数据传给 B？** 计划未定义。当前替身直接 `ProductRepository`，这是**替身的实现细节，不是给 B 的规范**。

**谁可以写 D** ✅

| 表 | 写者 | 依据 |
|---|---|---|
| `products`、`schema_version` | **D 独占** | `app/db/initialize.py` |
| C 的业务表（8 张 + 4 张假设的） | **C 独占** | 计划 §6；经 `commerce_schema(conn)` 钩子建表 |

**哪些数据应该持久化** ✅ CONFIRMED（计划 DC7）

- **必须持久化**：mandate、审计链、reservation
- **可以只在内存**：会话（`AgentSession`）、已展示的商品列表

**当前 Fake D 使用什么替代** → **无替代，D 是真的**。

**未来真实 D 需要什么数据** 🟡 部分已知

计划 §6 列出**需要新增的表**（✅ 明文，但**尚未实现**）：

```
既有假设（不存在）      计划新增（不存在）
──────────────      ──────────────────────────
wallets              mandates          mandate_versions
orders               quotes            purchase_proposals
order_items          reservations      capability_nonces
payments             policy_decisions  audit_events
```

> ⚠️ **本节最容易被误读的地方**：上表里「既有假设」四张表（`wallets`/`orders`/`order_items`/`payments`）在**当前数据库里并不存在**。实测 `var/demo.sqlite3` 只有 `products` 与 `schema_version` 两张表。**不要把计划里的表名当作已存在。**

**哪些数据不应该通过 D 原样暴露给 A** 🟡

| 数据 | 理由 |
|---|---|
| 他人（其他 principal）的订单与钱包 | 越权 |
| 支付凭证 / capability 原文 | 契约明文：「链是给人看的」，payload **不得包含完整 capability、签名密钥或支付凭证** |
| 内部签名密钥 | 同上 |

## 3.5 B 与 C 的边界（零调用）

✅ **CONFIRMED**：架构规则 3 ——「**B 与 C 之间零调用**。B 只读，C 只写钱，一旦连通授权链即被绕过。」

**未来若真实业务需要「B 的信息 + C 的权威」**，允许且唯一允许的形态：

```
✅ A 收到 B 的信息 → A 组装符合契约的请求 → A 发给 C
❌ B 直接告诉 C 该怎么做
❌ A → B → C
```

**为什么**：C 只认 `PurchaseProposal`（标识与意图），它自己重新推导一切金额与哈希。若 B 能直接影响 C，则商品数据的任何偏差都能变成金额偏差。

⚠️ **INTEGRATION COUPLING RISK**：当前 `LocalSearchClient` 与 `LocalCommerceClient` **各自独立**持有一个 `ProductRepository`（`local_search.py` 与 `local_commerce.py` 的 `__init__` 各建一个）。这**不是 B→C 调用**（两者无相互引用 ✅），但意味着商品数据被读了两遍。真实接线时需确认这是否可接受。

# Part 4 — Ownership & Authority Matrix

## 4.1 行动所有权

✅ 依据：计划 §2.2 三条架构规则 + 契约的模型校验器。

| Data / Action | A | B | C | D |
|---|---|---|---|---|
| **Propose**（提出购买） | ✅ | | | |
| **Search**（搜索/过滤/排序） | | ✅ | | |
| **Decide**（批准/拒绝/升格） | | | ✅ | |
| **Reserve**（占用额度） | | | ✅ | |
| **Pay**（支付） | | | ✅ | |
| **Issue capability**（签发支付凭证） | ❌ **永远不可** | | ✅ | |
| **Record**（审计链） | | | ✅ | |
| **Persist**（落库） | | | 经 D 的钩子 | ✅ |
| **Explain**（对用户说话） | ✅ | | | |
| **Read own records** | ❌ 不直连 | 🔵 未定 | ✅ | ✅ |

## 4.2 数据权威矩阵

⚠️ **本矩阵防的是**：A 因为「需要展示」而获得「生成」C 权威信息的能力。

| Data | Who Creates | Who Can Modify | Who Can Read | Who Approves | Who Persists |
|---|---|---|---|---|---|
| `Product` | D（种子） | D | 全员 | — | D |
| `HardConstraints` | **A**（从用户话语） | A | A、B | 用户（间接） | ❌ 不持久 |
| `SearchRequest` | **A** | ❌ 不可变 | A、B | — | ❌ |
| `SearchResponse` | **B** | ❌ 不可变 | A | — | ❌ |
| `MandateDraft` | **A** | A（用户改） | A、C | **用户签字** | ❌（草稿） |
| `Mandate` | **C** | ❌ **不可变**（版本递增） | A、B 读不到、C | 用户签字触发 | C |
| `policy_hash` | **C** | ❌ | 全员 | — | C（在 mandate 内） |
| `Quote` | **C** | ❌ 不可变（重报价 = 新 quote） | A、C | — | C |
| `PurchaseProposal` | **A** | ❌ 不可变 | A、C | — | C |
| `PolicyDecision` | **C** | ❌ 不可变 | A、C | — | C |
| `DenialReceipt` | **C** | ❌ | A、C | — | C |
| `EscalationRequest` | **C** | C（resolution） | A、C | **用户** | C |
| `ApprovalGrant` | **C** | ❌（消费 = 置 `consumed_at`） | C | 用户答复触发 | C |
| `Reservation` | **C** | C（状态机） | A 只读、C | — | C |
| `PaymentReceipt` | **C** | ❌ | A、C | — | C |
| `AuditEvent` | **C** | ❌ **append-only** | 全员（只读核验） | — | C / D |
| `AgentSession` | **A** | A | A | — | ❌ 内存 |

**三条从矩阵直接读出的禁令**

1. **A 不得出现在任何「Who Creates」为 C 的行里。** 已由 `extra="forbid"` 强制。
2. **`Mandate` 与 `AuditEvent` 不可修改。** 撤销 = 新版本（`version + 1`），不是改写。
3. **`EscalationRequest` 是唯一 C 可改的自己产出的对象**，且只改 `resolution` / `resolved_at`。

---

# Part 5 — A → B/C/D Data Handoff Specification

> **范围声明**：下表只列**当前契约里真实存在**的字段。凡计划提到但契约未定义的，标 ⛔ SPEC GAP，不填值。

## 5.1 A → B

**Handoff ID 约定**：`H-B-xx`

| ID | From | To | Purpose | Data / Field | Type | Required? | Source | Current Fake Value | Future Real Source |
|---|---|---|---|---|---|---|---|---|---|
| H-B-01 | A | B | 硬约束 | `constraints: HardConstraints` | model | ✅ 有默认 | 用户话语（A 解析） | 真实（用户给的） | 同左 |
| H-B-01a | | | 类别 | `.category` | `Literal["headphones"]` | 默认 | A | `"headphones"` | 同左 |
| H-B-01b | | | 价格上下界 | `.min_price_cents` / `.max_price_cents` | `StrictInt \| None` | 可选 | 用户话语 | 如有 | 同左 |
| H-B-01c | | | 连接方式 | `.connection` | `"wired" \| "wireless" \| None` | 可选 | 用户话语 | 如有 | 同左 |
| H-B-01d | | | 佩戴形式 | `.form_factor` | `"in_ear" \| "over_ear" \| "open_ear" \| None` | 可选 | 用户话语 | 如有 | 同左 |
| H-B-01e | | | 必须降噪 | `.anc_required` | `bool`（默认 `False`） | ✅ | 用户**明确要求** | 如有 | 同左 |
| H-B-01f | | | 续航天花板 | `.min_battery_hours` | `float \| None` | 可选 | 用户话语 | 如有 | 同左 |
| H-B-01g | | | 重量上限 | `.max_wearing_weight_g` | `float \| None` | 可选 | 用户话语 | 如有 | 同左 |
| H-B-01h | | | 品牌白名单 | `.brand_allowlist` | `list[str]`（空 = 不限） | ✅ | 用户话语 | 通常 `[]` | 同左 |
| H-B-01i | | | 只看有货 | `.in_stock_only` | `bool`（默认 `True`） | ✅ | A 默认 | `True` | 同左 |
| H-B-02 | A | B | 偏好 | `preferences: PreferenceProfile` | model | ✅ 有默认 | 用户话语 | 真实 | 同左 |
| H-B-02a | | | 用途 | `.use_cases` | `list[UseCase]` | ✅ | 用户话语 | `["commute"]` 等 | 同左 |
| H-B-02b | | | 偏好维度 | `.criteria: list[Criterion]` | model list | ✅ | 用户话语 | 可能有 | 同左 |
| H-B-02c | | | 排序预设 | `.priority_preset` | 4 个字面量 | 默认 `best_match` | A 默认 | `best_match` | 🟡 **B 如何使用它未定义** |
| H-B-02d | | | 偏好降噪 | `.prefer_anc` | `bool`（默认 `False`） | ✅ | 用户话语（软词） | 用户说「最好有」时 | 同左 |
| H-B-03 | A | B | 对比维度 | `comparison: ComparisonFrame` | model | ✅ 有默认 | 用户话语 + A 判断 | 通常空 | 同左 |
| H-B-04 | A | B | 返回条数 | `limit` | `int`（1–20，默认 3） | ✅ | A 决定 | `3` / 答问时 `20` | 同左 |

**A → B 的保证**

- ✅ A 绝不向 B 发送金额、余额、mandate、报价、哈希
- ✅ `SearchRequest` 是 `extra="forbid"`：多一个字段即被拒绝
- ✅ `SearchRequest` 有跨字段校验（例如：对比维度必须有依据，否则拒绝）

⛔ **SPEC GAP（H-B）**：`PriorityPreset` 的 4 个取值（`best_match` / `lower_price` / `longer_battery` / `lighter_weight`）**如何影响 B 的排序，计划未定义**。真实 B 必须自行裁定，或由人补充规格。❗

## 5.2 A → C

**Handoff ID 约定**：`H-C-xx`

### H-C-01 — 激活授权（`activate_mandate`）

| ID | Data / Field | Type | Required? | Source | 性质 |
|---|---|---|---|---|---|
| H-C-01 | `draft: MandateDraft` | model | ✅ | A 从用户话语解析 + **用户签字** | **user-provided** |
| H-C-01a | `principal_id` | `str` | ✅ | 后端上下文 | **authoritative** |
| H-C-01b | `agent_id` | `str` | ✅ | 配置 | **authoritative** |

⚠️ **`MandateDraft` 的全部字段（16 个）见 §5.4 附表。** 关键：**它们是用户说的，不是 A 推导的权威值**——C 必须自己把它们编译成 `canonical_policy` 并计算 `policy_hash`。

### H-C-02 — 报价（`create_quote`）

| ID | Data / Field | Type | Required? | Source | 性质 |
|---|---|---|---|---|---|
| H-C-02a | `product_id` | `str` | ✅ | **B 的搜索结果 → A 转交** | **B-provided** |
| H-C-02b | `quantity` | `int`（≥1，默认 1） | ✅ | 用户 | user-provided |

✅ **A 不给金额。** C 自己从 D 读价格、自己算运费、自己算总额。**这一条是 A→C 边界最重要的性质。**

### H-C-03 — 提交提案（`submit_proposal`）

| ID | Data / Field | Type | Required? | Source | 性质 |
|---|---|---|---|---|---|
| H-C-03a | `proposal_id` | `str` | ✅ | A 生成 | identifier |
| H-C-03b | `mandate_id` | `str` | ✅ | C 先前返回 | identifier |
| H-C-03c | `expected_mandate_version` | `int` | ✅ | C 先前返回 | **乐观锁**（撤销 = 版本递增） |
| H-C-03d | `principal_id` | `str` | ✅ | 后端上下文 | **authoritative** |
| H-C-03e | `agent_id` | `str` | ✅ | 配置 | **authoritative** |
| H-C-03f | `product_id` | `str` | ✅ | B → A | B-provided |
| H-C-03g | `quantity` | `int` | ✅ | 用户 | user-provided |
| H-C-03h | `merchant_id` | `str` | ✅ | ⚠️ 见下 | B-provided |
| H-C-03i | `quote_id` | `str` | ✅ | C 先前返回 | identifier |
| H-C-03j | `preferred_payment_route_ids` | `list[str]` | ✅（可空列表） | 用户 / mandate | user-provided |
| H-C-03k | `shipping_address_id` | `str` | ✅ | mandate | user-provided |
| H-C-03l | `request_id` | `str` | ✅ | A 生成 | identifier |
| H-C-03m | `idempotency_key` | `str` | ✅ | 调用方 | identifier |
| H-C-03n | `created_at` | `datetime` | ✅ | A | timestamp |

**A 结构上无法提交**（✅ `extra="forbid"` 强制）：

```
cash_total_cents · remaining_budget_cents · policy_hash · payment_status
wallet_balance_after_cents · reward_earned_cents · 任何批准决定
```

⚠️ **`merchant_id` 的来源是缺口**：契约要求 A 提供，但**当前 A 只能从 `Quote.merchant_id` 拿到**——而 `Quote` 是 C 产出的。在 `create_quote` 之前 A 手上没有 merchant。⛔ **SPEC GAP（H-C-03h）**：merchant 应由 B 的搜索结果提供，还是由 C 在报价后回填？当前 `LocalSearchClient` 不返回 merchant（`Product` 无该字段）。❗

### H-C-04 — 答复升格（`approve_escalation`）

| ID | Data / Field | Type | Required? | Source |
|---|---|---|---|---|
| H-C-04a | `decision: PolicyDecision` | model | ✅ | **C 自己产出的**，A 原样回传 |
| H-C-04b | `proposal: PurchaseProposal` | model | ✅ | 同上 |
| H-C-04c | `quote: Quote` | model | ✅ | 同上 |
| H-C-04d | `route: PaymentRouteEvaluation` | model | ✅ | 同上 |

**这个方法的形状本身就是一个缺口**：它要求 A 回传 4 个对象，其中 3 个是 C 的内部产出。⛔ **SPEC GAP（H-C-04）**：更自然的形状应是 `approve_escalation(proposal_id, principal_id)`，由 C 自己查回上下文。当前形状迫使 A 保管 C 的对象，⚠️ **INTEGRATION COUPLING RISK**。❗

## 5.3 A → D

✅ **A 无直接边界。** A 经 B 拿商品、经 C 拿报价。

唯一的间接接触：`app/catalog/routes.py` 的 `create_router` 由 A 挂载，但**业务由 D 实现**，A 只注入包装与错误映射。

| ID | From | To | Purpose | Data | Type |
|---|---|---|---|---|---|
| H-D-01 | A | D | 挂载时注入 | `wrap_success(product_dict, request)` | callable |
| H-D-02 | A | D | 挂载时注入 | `product_not_found(product_id, request)` | callable（**必须抛**） |
| H-D-03 | A | D | 注入仓储 | `ProductRepository(...)` | object |

⚠️ **H-D-01/H-D-02 是 A 给 D 的，方向与直觉相反**——D 的模块定义了接口，A 提供实现。

## 5.4 附表：`MandateDraft` 全集（16 字段）

| 字段 | 类型 | 必填？ | 用户话语示例 |
|---|---|---|---|
| `allowed_merchants` | `list[str] \| None` | 可选 | 「从 Demo Audio Store 买」 |
| `allowed_categories` | `list["headphones"] \| None` | 可选 | — |
| `required_connection` | `"wired"\|"wireless"\|None` | 可选 | 「无线」 |
| `anc_required` | `bool \| None` | 可选 | 「必须降噪」 |
| `cap_per_transaction_cents` | `StrictInt \| None` | 可选 | 「每笔不超过 HK$300」 |
| `rolling_cap_cents` | `StrictInt \| None` | 可选 | 「24 小时累计不超过 HK$500」 |
| `rolling_window_seconds` | `StrictInt \| None` | 可选 | 「24 小时」 |
| `velocity_max_count` | `StrictInt \| None` | 可选 | 「5 分钟最多两次」 |
| `velocity_window_seconds` | `StrictInt \| None` | 可选 | 「5 分钟」 |
| `max_quantity_total` | `StrictInt \| None` | 可选 | 「买一件」 |
| `valid_for_seconds` | `StrictInt \| None` | 可选 | 「未来 7 天内」 |
| `escalate_above_cents` | `StrictInt \| None` | 可选 | 「超过 HK$280 先问我」 |
| `allowed_payment_routes` | `list[str] \| None` | 可选 | 「只用 FPS」 |
| `shipping_address_id` | `str \| None` | 可选 | 「不要改收货地址」 |
| `address_change_allowed` | `bool \| None` | 可选 | 同上 |
| `ambiguities` / `unsupported_conditions` / `source_spans` | 解析质量元数据 | ✅ | — |

**`missing_required_fields()` 要求其中若干项必须非空**才能激活，实测缺项提示为：
`rolling_cap_cents`、`rolling_window_seconds`、`velocity_max_count`、`velocity_window_seconds`、`max_quantity_total`、`valid_for_seconds`、`allowed_merchants`、`allowed_categories`、`allowed_payment_routes`、`shipping_address_id`、`address_change_allowed`，外加 `structural_problems()` 的 `no expiry` / `no shipping address`。

---

# Part 6 — B/C/D → A/System Data Return Specification

> **分级声明**（本 Part 用三级标注，不要混淆）：
> ✅ **Confirmed by existing spec** — 契约里有类型，有字段
> 🟡 **Expected integration requirement** — 架构必然需要，但规格未明文
> 🔵 **Future extension** — 当前不存在

## 6.1 B → A

| Return ID | Provider | Receiver | Purpose | Field | Type | Required? | Current Fake Value | Future Real Meaning | Consumed By | 级别 |
|---|---|---|---|---|---|---|---|---|---|---|
| R-B-01 | B | A | 命中总数 | `SearchResponse.total_matches` | `int` | ✅ | 真实计算 | 截取**前**的数量 | `browsing._present`、渲染 | ✅ |
| R-B-02 | | | 返回数 | `.returned` | `int` | ✅ | = `len(candidates)` | 截取后 | 同上 | ✅ |
| R-B-03 | | | 候选 | `.candidates: list[Candidate]` | model list | ✅ | 真实（简化排序） | B 的五级排序 | 渲染、`session.remember_results` | ✅ |
| R-B-03a | | | 商品 | `Candidate.product: Product` | model(20 字段) | ✅ | 真实 | 同左 | 渲染、前端 | ✅ |
| R-B-03b | | | 排序分 | `Candidate.match_score` | `int` | ✅ | 公式真、常全平 | B 定义 | 🟡 **A 当前不消费** | 🟡 |
| R-B-03c | | | 命中理由 | `Candidate.reasons: list[MatchReason]` | model list | ✅ | 简化的 1–2 条 | B 的完整理由 | 🟡 **A 当前不消费** | 🟡 |
| R-B-03d | | | 偏好未达标 | `Candidate.preference_misses` | `list[str]` | ✅ | 真实 | 同左 | 🟡 **A 当前不消费** | 🟡 |
| R-B-04 | | | 对比矩阵 | `.comparison: list[ComparisonRow]` | model list | ✅ | 简化（6 维度） | B 的完整矩阵 | 渲染（只渲染 `is_distinguishing`） | ✅ |
| R-B-05 | | | 缺口报告 | `.gaps: GapReport` | model | ✅ | **多为空** | 真实 B 会填 | 渲染（`unsupported_criteria`、`missing_data_attributes`） | ✅ |
| R-B-05a | | | 无法评估的判据 | `GapReport.unsupported_criteria` | `list[str]` | ✅ | **恒空** | B 填 | 渲染 | 🟡 |
| R-B-05b | | | 被拒维度 | `GapReport.dropped_dimensions` | `list[str]` | ✅ | **恒空** | B 填 | 🟡 **A 当前不消费** | 🟡 |
| R-B-06 | | | 放宽提示 | `.relax_hints: RelaxHints \| None` | model | ✅ **空结果时必填** | 真实计算 | 同左，语义更细 | 渲染 + `_present` 的提问 | ✅ |
| R-B-06a | | | 近似候选 | `RelaxHints.closest_candidates` | model list | ✅ | 真实（≤3） | 同左 | 🟡 **A 当前不消费** | 🟡 |
| R-B-07 | | | 文案建议 | `.suggestions` | `list[str]` | ✅ | **恒空** | ⛔ **B 不写文案**（规格明文） | 🔵 不应消费 | 🔵 |
| R-B-08 | B | A | 错误 | `ErrorCode.INVALID_CONSTRAINTS` 等 | 异常 | ✅ | `LocalSearchClient` 抛 `AgentError` | 同左 | `browsing._run` | ✅ |

⚠️ **R-B-03b/03c/03d、R-B-05a/05b、R-B-06a 是 INTEGRATION COUPLING RISK 的反面**：A 目前**不消费**它们，所以真实 B 填了也没人用。**这不是错误**（保守设计），但意味着**这些字段的价值尚未被验证**。真实 B 落地时，A 应决定是否渲染 `reasons` 与 `relax_hints.closest_candidates`。

## 6.2 C → A

| Return ID | Provider | Receiver | Purpose | Field | Type | Required? | Current Fake | Future Real | Consumed By | 级别 |
|---|---|---|---|---|---|---|---|---|---|---|
| R-C-01 | C | A | 授权已激活 | `Mandate` | model | ✅ | Fake 可产出 | 真实 C | ⛔ **A 当前未消费**（mandate 流程未接） | 🟡 |
| R-C-01a | | | 规则指纹 | `Mandate.policy_hash` | `str` | ✅ | `LocalCommerceClient` 算 | C 算 | 展示、D4 回答「为什么」 | ✅ |
| R-C-01b | | | 不可变版本 | `Mandate.version` | `int` | ✅ | 递增 | 同左 | `expected_mandate_version` | ✅ |
| R-C-02 | C | A | 报价 | `Quote` | model | ✅ | Fake 算（公式真） | C 算 | ⛔ A 未消费 | 🟡 |
| R-C-02a | | | 到手总额 | `Quote.merchant_total_cents` | `int` | ✅ | `27900+1000` | 同左 | 渲染 | ✅ |
| R-C-02b | | | 报价指纹 | `Quote.quote_hash` | `str` | ✅ | 真实计算 | 同左 | 判定器校验 | ✅ |
| R-C-03 | C | A | **判定** | `PolicyDecision.outcome` | `APPROVE\|DENY\|ESCALATE` | ✅ | **真实判定器** | 真实 C | 渲染、`requires_user_action` | ✅ |
| R-C-03a | | | 主因 | `.primary_reason: ErrorCode \| None` | enum | ✅ | 真实（**执行顺序第一条**，非严重度） | 同左 | 渲染 | ✅ |
| R-C-03b | | | 全部违规 | `.violations: list[PolicyViolation]` | model list | ✅ | 真实 | 同左 | `describe_denial` | ✅ |
| R-C-03c | | | 观测值/限额 | `.observed_values` / `.applicable_limits` | `dict[str, Any]` | ✅ | 真实 | 同左 | 渲染（`describe_decision`） | ✅ |
| R-C-04 | C | A | 拒收据 | `DenialReceipt` | model | 🟡 | ⛔ **不存在** | C 落库 | ⛔ 无接口 | 🟡 |
| R-C-05 | C | A | 升格请求 | `EscalationRequest` | model | 🟡 | ⛔ **不存在** | C 产出 | ⛔ 无接口 | 🟡 |
| R-C-06 | C | A | 批准凭证 | `ApprovalGrant` | model | ✅ | Fake 产出 | C 产出 | 判定器消费 | ✅ |
| R-C-07 | C | A | 预留 | `Reservation` | model | 🔵 | ⛔ **不存在** | C 产出 | 前端展示 | 🔵 |
| R-C-08 | C | A | 支付回执 | `PaymentReceipt` | model | 🔵 | ⛔ **不存在** | C 产出 | 前端展示 | 🔵 |
| R-C-09 | C | A | 额度状态 | `SpendState` | model | ✅ | **恒零**（刻意） | C 真实记账 | 判定器、前端 | ✅ |

⛔ **SPEC GAP（R-C）**：`CommerceClient` 只有 7 个方法（Part 3.3），**没有**返回 `DenialReceipt`、`EscalationRequest`、`Reservation`、`PaymentReceipt` 的方法。这 4 个类型**有定义、无路径**。❗ 见 GAP-C2/C3。

## 6.3 D → A / C / System

| Return ID | Provider | Receiver | Purpose | Field | Type | 级别 |
|---|---|---|---|---|---|---|
| R-D-01 | D | C | 建表钩子 | `commerce_schema(conn) -> None` | callable | ✅ |
| R-D-02 | D | C | 钱包初始化钩子 | `wallet_initializer(conn) -> None` | callable | ✅ |
| R-D-03 | D | 全员 | 数据库状态 | `database_status()` → dict | — | ✅（`app/db/core.py:52`） |
| R-D-04 | D | A/C | 商品查询 | `get_product(id, connection=None) -> Product \| None` | — | ✅ |
| R-D-05 | D | B/C | 候选列表 | `list_candidates(constraints, connection=None) -> list[Product]` | — | ✅ |
| R-D-06 | D | C | 扣库存 | `decrease_stock(id, qty, connection) -> bool` | — | ✅ |
| R-D-07 | D | C | 事务连接 | `connect(path, *, readonly, timeout)` | — | ✅ |

**D 的钩子契约（✅ 实测自 `app/db/initialize.py`）**

```
执行顺序：创建 D 表 → commerce_schema(conn) → 补导入商品 → wallet_initializer(conn)
          → 检查外键 → 提交

C 的回调不得：提交、回滚、关闭连接、或调用会结束事务的 executescript()
违反会抛：RuntimeError("C's schema hook ended the caller-owned transaction")
```

**不应该通过 D 原样暴露给 A 的** ✅

| 数据 | 理由 |
|---|---|
| 其他 principal 的订单/钱包 | 越权 |
| capability 原文 | 契约明文禁止入审计 payload |
| 签名密钥 | 同上 |

⛔ **SPEC GAP（R-D-08）**：**审计链由 C 写还是 D 写？** `AuditEvent` 类型在 `app/contracts/audit.py`（A 所有），但计划 §2.3 把 `app/commerce/audit.py` 列在 **C** 名下。二者的关系（C 产事件、D 落库？还是 C 自己落库？）**未定义**。❗ 计划 R9 说「审计链单独持久化、reset 默认不删」，暗示有独立的持久层。

---

# Part 7 — Fake → Real Replacement Architecture

> ⚠️ **v2 快照。** `LocalCommerceClient` 已删除；Fake→Real 的现行映射与成本见
> `docs/A_C_data_handoff.md` §8。

## 7.1 替换契约（每个 Fake 必须能说清这六件事）

### FakeSearchClient = `LocalSearchClient`

| 项 | 内容 |
|---|---|
| **实现的接口** | `app/agent/clients.py::SearchClient`（1 方法） ✅ |
| **接受的数据** | `SearchRequest`（Part 5.1 全部字段） ✅ |
| **返回的数据** | `SearchResponse`（Part 6.1） ✅ |
| **被谁替换** | 真实 B —— ⛔ **SPEC GAP**：进程内服务？HTTP？见 GAP-C1 同源问题 |
| **必须保持不变** | `SearchClient` 的方法名、参数名、参数类型、返回类型 |
| **当前调用方** | `app/agent/browsing.py::BrowsingTurns`（唯一） ✅ |

### FakeCommerceClient = `LocalCommerceClient`

| 项 | 内容 |
|---|---|
| **实现的接口** | `app/agent/clients.py::CommerceClient`（7 方法） ✅ |
| **接受的数据** | Part 5.2 全部 |
| **返回的数据** | Part 6.2 |
| **被谁替换** | 真实 C —— ⛔ **SPEC GAP（GAP-C1）** |
| **必须保持不变** | `CommerceClient` 的 7 个签名 |
| **当前调用方** | **无** ⚠️ —— `AgentOrchestrator` 构造注入了但**从不调用**（mandate 流程未接） |

> ⚠️ **注意**：`LocalCommerceClient` 目前**没有任何生产调用方**。它只被测试和 `scripts/demo_conversation.py` 使用。这意味着 **`CommerceClient` 接口从未被真实业务流程验证过**——⚠️ INTEGRATION COUPLING RISK。

## 7.2 替换时的允许与禁止

**允许改动（S2 替换期）**

```
✅ app/agent/local_search.py   → 删除，或改名 real_search.py
✅ app/agent/local_commerce.py → 删除，或改名 real_commerce.py
✅ 构造处的一行注入
✅ app/main.py 的依赖装配
```

**禁止改动**

```
❌ app/agent/clients.py 的两个 Protocol 签名
❌ app/agent/browsing.py 的调用语义
❌ app/agent/orchestrator.py 的调用语义
❌ app/contracts/** 的任何字段
❌ AgentResponse 的字段（前端契约）
```

**除外的唯一情况**：经过**明确的 contract change**（升版本、全员同步、重跑 fixtures），见 `docs/handoff-to-B.md` §9。

## 7.3 替换成本评估（Future Replacement Test）

| 替换 | 预期改动 | 实测风险 | 结论 |
|---|---|---|---|
| **Fake B → Real B** | 1 个文件 + 1 行注入 | 🟢 低。`BrowsingTurns` 只调 `search()` | 可无痛替换 |
| **Fake C → Real C** | 1 个文件 + 1 行注入 | 🟡 中。接口有 4 个缺口（GAP-C2/C3/C4、H-C-04 形状） | **需要 contract 补充后才可替换** |
| **Fake D → Real D** | **不存在此替换** —— D 已经是真的 | 🟢 无 | — |

⚠️ **INTEGRATION COUPLING RISK 汇总**

| ID | 耦合 | 位置 | 影响 |
|---|---|---|---|
| CR-1 | A 需回传 3 个 C 的内部对象来批准升格 | `CommerceClient.approve_escalation` | 接口形状不良 |
| CR-2 | `CommerceClient` 无支付方法 | `app/agent/clients.py` | 支付无法从 A 触发 |
| CR-3 | `CommerceClient` 无拒收据/升格返回值 | 同上 | `DenialReceipt`/`EscalationRequest` 有定义无路径 |
| CR-4 | 两个替身各建一个 `ProductRepository` | `local_search.py` / `local_commerce.py` | 商品数据读两遍 |
| CR-5 | `ApprovalGrant` 无人消费（判定器只读） | 归属未定 | 「一次一用」无法保证 |

---

# Part 8 — Future Integration Freeze Audit

> ⚠️ **v2 快照。** F-1 已解决（改由 C 的 `merchant_of_record()` 提供，A 在摘要里
> 标注为「我建议的」）；F-5 已解决，而且顺手修掉了一个更隐蔽的问题——旧替身把
> `rolling_window_start` 设成整整一个窗口之前，使判定器的「快照是否新鲜」保护恒为假，
> **滚动上限的比较被整个跳过**。现行参数与标注见 `docs/evidence_sources.md`。

## 8.1 硬编码清单

| # | Location | Hard-coded Item | Why It Exists | Safe? | Future Replacement Path | Action |
|---|---|---|---|---|---|---|
| F-1 | `local_commerce.py:210` | `merchant_id="demo_audio_store"` | 替身里没有 merchant 来源 | ⚠️ **不可以接受** | 必须改为来自配置或 B | **P2**：见 H-C-03h |
| F-2 | `local_commerce.py:242` | `route_id` 回退到 `"fps_demo"` | mandate 未指定路线时的默认 | 🟡 可接受（有回退逻辑） | 真实 C 的 PaymentRouter | P3 |
| F-3 | `local_commerce.py:41` | `SANDBOX_SHIPPING_CENTS=1000` | 商品数据无运费字段 | ✅ **可接受**（已标 SANDBOX） | 真实运费须标注来源 | 保持，须写入 `evidence_sources.md` |
| F-4 | `local_commerce.py:241` | `PLATFORM_FEE_CENTS=0` | 同上 | ✅ 可接受 | 同上 | 保持 |
| F-5 | `local_commerce.py:spend_state` | 曝光**恒为零** | **刻意不伪造**（注释明文） | ✅ **可接受** | C 的真实记账 | 保持，但见下 ⚠️ |
| F-6 | `local_search.py:124` | `currency="HKD"` | 硬编码在 `SearchResponse` | ✅ 可接受（DC5 固定 HKD） | 同左 | 保持 |
| F-7 | `orchestrator.py:86-87` | `principal_id="demo_user"`、`agent_id="demo_agent"` 默认值 | 演示身份 | 🟡 可接受（有默认参数） | 来自后端上下文 | 保持 |
| F-8 | `local_search.py` `_rank()` | 简化排序（criteria → 价格 → id） | 替身实现 | ✅ **可接受**（明确的替身） | 真实 B | 删除替身 |
| F-9 | `local_search.py` `GapReport` 多为空 | 替身未实现 | ✅ 可接受 | 真实 B | — |
| F-10 | `browsing.py:38` | `_WIDER_LIMIT = 20` | 答问时取更宽的结果 | ✅ 可接受（有界，契约上限 20） | 同左 | 保持 |
| F-11 | `browsing.py` `_resolve_direction` | 「便宜点」→ 上限 = 最高价 − 1 分 | **A 的判断，且已在回复里声明** | ✅ 可接受 | 同左 | 保持 |

## 8.2 ⚠️ F-5 的深层影响（**这是最重要的一条**）

`LocalCommerceClient.spend_state()` 返回**全零曝光**：

```python
def spend_state(self, mandate) -> SpendState:
    """Nothing has been spent in this stand-in, so exposure is zero.

    Deliberately not faked: reporting a made-up `reserved_cents` would make
    the rolling-cap check pass for the wrong reason and hide the fact that
    C's accounting is not built.
    """
```

**诚实，但后果严重**：

| 检查 | 当前行为 | 真实 C 落地后 |
|---|---|---|
| `rolling_cap`（检查 26） | **永远通过**（曝光=0） | 会真正生效 |
| `velocity`（检查 24） | **永远通过**（笔数=0） | 会真正生效 |
| `max_quantity_total`（检查 27） | 只算单笔 | 会累计 |

**结论 🔵 EXTENSION**：**当前演示无法展示这三条限额被触发**。若演示需要「滚动额度被拦住」，**必须有真实 C 的记账**，或替身必须实现一个**明确标注为模拟**的曝光累计。

❗ **HUMAN DECISION REQUIRED**：是否要替身实现「模拟曝光」以支撑演示？折中是：实现一个 `_SimulatedLedger`，**在每次 APPROVE 后累加**，并在所有输出里标注 `SIMULATED_LEDGER`。这**不违反**「不伪造」原则，因为它是显式标注的模拟，而不是伪装成真实的记账。**但这需要人批准**，因为它是替身第一次开始「记住状态」。

## 8.3 前端冻结风险（**当前不适用，但需预防**）

前端**尚未编写** 🔵。预防清单：

| 禁止 | 理由 |
|---|---|
| 前端硬编码价格 | 破坏 R-C-02a |
| 前端决定 `outcome` | 破坏 R-C-03 |
| 前端生成 `reservation` / `payment` 结果 | 破坏 Part 4 矩阵 |
| 前端保存「已支付」状态而不经 C | 同上 |

**允许**：前端渲染 `AgentResponse` 的所有字段；用 `requires_user_action()` 决定渲染什么控件；用 `selected_product_id` 绑按钮。

---

# Part 9 — Policy Audit Matrix

**来源**：`app/commerce/policy_evaluator.py`（516 行，✅ 实测）。**22 个不同错误码，30 个比较，落在 27 个编号槽位**（3a/3b/3c 与 16/17 互斥）。

## 9.1 不变量（✅ 逐条实测）

| 不变量 | 强制方式 | 状态 |
|---|---|---|
| deterministic | 纯函数，无 IO | ✅ |
| no IO | 不打开文件/网络/数据库 | ✅ |
| **no clock** | 时钟由 `inputs.now` 传入，文件内**无 `datetime.now()`** | ✅ |
| no transaction | 不开/不提交/不回滚 | ✅ |
| no capability access | `CAPABILITY_*` 码未被使用 | ✅ |
| no DB | 不 import 任何仓储 | ✅ |
| **unknown ≠ satisfied** | `quote.anc is not True`（`None` 判失败） | ✅ |
| stable reject reason | `primary_reason = check.order[0]`（**执行顺序第一条，非严重度**） | ✅ |
| sequence 是否为 contract | 计划 §5.3 声明顺序固定；实测与计划**有出入**（见 9.3） | ⚠️ |
| 违规全量累积、无短路 | 每个检查独立 `if` | ✅ |
| 升格超时 fail closed | DC12；`ESCALATION_TIMEOUT` 码存在但判定器不发 | ✅ |

## 9.2 规则矩阵

✅ = 测试文件里有引用 / **无** = 测试文件从未提及（实测）

| Rule ID | Rule | 输入 | Reject Code | 测试 | 状态 |
|---|---|---|---|---|---|
| 1 | 归属人一致 | `proposal.principal_id` vs mandate | `PRINCIPAL_MISMATCH` | ✅ | ✅ |
| 2 | agent 一致 | `proposal.agent_id` | `AGENT_MISMATCH` | ✅ | ✅ |
| 3a | mandate 已撤销 | `mandate.status` | `MANDATE_REVOKED` | ✅ | ✅ |
| 3b | mandate 已被取代 | 同上 | `MANDATE_VERSION_STALE` | ✅ | ✅ |
| 3c | mandate 已过期 | 同上 | `MANDATE_EXPIRED` | ✅ | ✅ |
| 4 | 版本未落后 | `expected_mandate_version` | `MANDATE_VERSION_STALE` | ✅ | ✅ **乐观锁** |
| 5 | 尚未生效（容 5s 时钟偏移） | `valid_from` | `MANDATE_NOT_ACTIVE` | ✅ | ✅ |
| 6 | 未过期（**严格，无偏移宽容**） | `expires_at` | `MANDATE_EXPIRED` | ✅ | ✅ **fail closed** |
| 7 | 商户在白名单 | `proposal.merchant_id` | `MERCHANT_NOT_ALLOWED` | ✅ | ✅ |
| 8 | 报价商品与提案一致 | `quote.product_id` | `QUOTE_HASH_MISMATCH` | ✅ | ✅ |
| 9 | 报价商户与提案一致 | `quote.merchant_id` | `QUOTE_CHANGED` | ✅ | ✅ |
| 10 | 类别在白名单 | `quote.category` | `CATEGORY_NOT_ALLOWED` | **无** | ⚠️ 见 9.4 |
| 11 | 连接方式符合 | `quote.connection` | `CONNECTION_NOT_ALLOWED` | ✅ | ✅ |
| 12 | **降噪：未知不满足** | `quote.anc is not True` | `ANC_REQUIREMENT_NOT_MET` | ✅ | ✅ **DC11** |
| 13 | 数量一致 | `quote.quantity` | `QUOTE_CHANGED` | ✅ | ✅ |
| 14 | 单笔数量上限 | `proposal.quantity` | `QUANTITY_LIMIT_EXCEEDED` | ✅ | ✅ |
| 15 | 收货地址一致 | `shipping_address_id` | `ADDRESS_CHANGE_NOT_ALLOWED` 或 `PRINCIPAL_MISMATCH` | ✅ | ✅ 双码 |
| 16 | 路线在白名单 | `route.route_id` | `PAYMENT_ROUTE_NOT_ALLOWED` | ✅ | ✅ |
| 17 | 路线可用 | `route.eligible`（`elif`） | `PAYMENT_ROUTE_NOT_ALLOWED` | ✅ | ✅ 与 16 互斥 |
| 18 | 报价 ID 一致 | `quote.quote_id` | `QUOTE_CHANGED` | ✅ | ✅ |
| 19 | 报价哈希自校验 | `quote.hash_matches()` | `QUOTE_HASH_MISMATCH` | ✅ | ✅ |
| 20 | 报价未来自未来（容 5s） | `quote.issued_at` | `QUOTE_CHANGED` | ✅ | ✅ |
| 21 | 报价未过期（严格） | `quote.expires_at` | `QUOTE_EXPIRED` | ✅ | ✅ |
| 22 | 路线与报价总额一致 | `route.merchant_total_cents` | `QUOTE_CHANGED` | ✅ | ✅ |
| 23 | 币种一致 | `quote.currency` | `QUOTE_CURRENCY_MISMATCH` | **无** | ⚠️ 见 9.4 |
| 24 | 速度限制（含预留） | `exposure_count >= velocity_max_count`（`>=`） | `VELOCITY_LIMIT_EXCEEDED` | ✅ | ✅ 但见 F-5 |
| 25 | 单笔上限 | `route.cash_total_cents > cap`（**严格 `>`，等于放行**） | `CAP_PER_TRANSACTION_EXCEEDED` | ✅ | ✅ |
| 26 | 滚动上限 | `exposure_cents + cash_total > rolling_cap` | `ROLLING_CAP_EXCEEDED` | ✅ | ✅ 但见 F-5 |
| 27 | 终身数量 | `exposure_quantity + quantity` | `QUANTITY_TOTAL_EXCEEDED` | ✅ | ✅ 但见 F-5 |
| 28 | 升格 | `cash_total > escalate_above` 且凭证不覆盖 | `ESCALATION_REQUIRED` | ✅ | ✅ 见下 |

**升格子检查 `_grant_problem`（7 步，首个命中者胜）**：无凭证 → 不可用/已消费 → 提案不符 → mandate 不符 → 版本过期 → 归属人不同 → 报价/路线/金额已变。✅ 但 ⚠️ **返回的原因是字符串，且被调用方丢弃**（`_grant_problem` 只用了 `is not None`）。用户因此看不到「你的批准为何失效」。

**结果判定（✅ 实测）**

```
无违规              → APPROVE
有违规且全是 ESCALATION_REQUIRED → ESCALATE
否则                → DENY（任何硬违规压过升格）
```

## 9.3 ⚠️ 文档与代码的不一致（已确认）

`docs/A_详细开发计划_v2.0.md` §5.3 声称是「24 项」，实测是 **30 个比较 / 27 个槽位**。该表从第 13 项起与代码错位，且漏掉了两项独立检查（**数量一致性 #13** 与 **收货地址 #15**）。源码自己的分组注释也不准（`# -- 7-9 scope` 实际是 6 项）。

**P2 建议**：修正计划 §5.3 以对齐代码。**注意**：v1 的交接文档已指出此点，但只修了一半。

## 9.4 ⚠️ 两条结构性不可达的检查（**新发现**）

| 检查 | 为何不可达 | 实测结果 |
|---|---|---|
| #10 `CATEGORY_NOT_ALLOWED` | `Quote.category` 是 `Literal["headphones"]`，**造不出别的值** | 正常构造 → `ValidationError` |
| #23 `QUOTE_CURRENCY_MISMATCH` | `Quote.currency` 与 `Mandate.currency` **都是** `Literal["HKD"]` | 正常构造 → `ValidationError` |

**但用 `model_construct()` 绕过校验器后，两条都正确触发**（✅ 实测：`outcome=DENY`，码在 `violations` 里）。

**结论：这不是死代码，是有效的第二道防线。** 判定器是安全边界，**不应假设上游校验一定跑过**——这与「A 的提交物结构上不含权威值」在两处强制是同一种思路。

**P2 建议**：为这两条补测试，用 `model_construct()` 构造被篡改的 payload，断言判定器仍然拒绝。当前**测试文件从未提及这两个码**，所以防线的存在是**未被验证的**。

---

# Part 10 — E2E Acceptance Matrix AC-01 ~ AC-16

## 10.1 ⛔ 首先：这 16 项在仓库里**不存在**

实测全仓库检索结果：

| 出处 | 内容 |
|---|---|
| `docs/A_详细开发计划_v2.0.md:311` | 「Phase 12：`tests/integration/` **16 项 AC 用例**」 |
| `docs/A_详细开发计划_v2.0.md:486` | 「集成 AC-01…AC-16 \| **见 v2.0 §四十七**；含 **AC-13**（A 篡改金额被 C 拒）与 **AC-16**（注入无效）」 |
| `tests/integration/` | **目录不存在** |

**「v2.0 §四十七」不在本仓库内**（那是用户给的总提示词，未提交）。

> ⛔ **SPEC GAP（AC）**：**14 项验收用例的内容无人知晓。** 本文档**不发明它们**——发明出来的验收标准会让「全绿」失去意义。
>
> ❗ **HUMAN DECISION REQUIRED**：由了解 §四十七 的人补齐，或由团队现场定义。

## 10.2 已知的两项（✅ 有出处）

| ID | 描述 | 出处 | 当前可否执行 |
|---|---|---|---|
| **AC-13** | A 篡改金额 → 被 C 拒 | 计划 §7.2 明文 | 🟡 **理论可执行**：`PurchaseProposal` 无法携带金额（`extra="forbid"`），所以「篡改」表现为**提交被拒绝**。可测 |
| **AC-16** | 注入无效 | 计划 §7.2 明文 | 🔵 **前端未写，无法端到端验证**。可测的是：商品 `seller_description` 含注入文本时，**决策链不受影响**（描述是数据，不是指令） |

## 10.3 建议的矩阵骨架（🔵 **提议，不是规格**）

> ⚠️ 下表是**结构建议**，供人填充。**它不是 AC-01…AC-16 的定义**，不得当作验收标准使用。

| 场景类别 | 应覆盖 | 跨越的边界 | 当前可测？ |
|---|---|---|---|
| **搜索** | 有结果 / 无结果 / 边界价 | A → B | ✅ 可（替身） |
| **提案** | 提交 / 幂等 / 版本落后 | A → C | 🔵 需 C |
| **选择商品** | 序号解析 / 越界 / 未展示 | A → Session | ✅ 可 |
| **策略拒绝** | 每一类违规至少一次 | C（纯函数） | ✅ 可（判定器直接测） |
| **策略批准** | APPROVE 全路径 | C | 🔵 需 C |
| **升格** | 触发 / 用户批准 / 超时 | A ↔ C | 🔵 需 C |
| **预留** | 创建 / 并发 / 释放 | C | 🔵 需 C |
| **Capability** | 签发 / 篡改 / 过期 / 重放 | C | 🔵 需 C |
| **支付** | 成功 / 失败 / UNKNOWN | C | 🔵 需 C |
| **审计** | 追加 / 验链 / 断裂检测 | C + D | 🟡 可（`verify_chain` 已有） |
| **未知数据** | `anc=null` 不满足 | A + B | ✅ 可 |
| **错误** | 信封格式 / 404 映射 | A（HTTP） | 🔵 需 HTTP |
| **重试** | UNKNOWN 不自动重试 | C | 🔵 需 C |

**当前可测的 5 类应优先落地**——它们不依赖 B/C 的真实实现，且能立刻锁住已经修好的行为。

# Part 11 — HTTP/API Detailed Plan

> ✅ **v3：已实现。** 端点表见 Part 0.6；实现是 `app/main.py`（composition root）、
> `app/api/routes_agent.py`、`app/api/routes_records.py`、`app/api/envelope.py`，
> 演示页 `app/api/demo.html`。测试在 `tests/api/`（19 项）与 `tests/integration/`（23 项）。
> 本节其余内容是 v2 的设计依据，仍然适用。

## 11.1 端点归属（✅ 计划 §8 明文）

### A 的（**本次要做的**）

| 方法 | 路径 | 请求 | 响应 | 边界 |
|---|---|---|---|---|
| `GET` | `/api/v1/health` | — | `Envelope` | 复用 D 的 `database_status()` |
| `POST` | `/api/v1/agent/chat` | `ChatRequest` | `Envelope[AgentResponse]` | → `AgentOrchestrator.handle()` |
| `POST` | `/api/v1/agent/actions` | `AgentActionRequest` | `Envelope[AgentResponse]` | **不经过解析器**（按钮就是按钮） |

### D 的（**已实现，只需挂载**）

| 方法 | 路径 | 挂载方式 |
|---|---|---|
| `GET` | `/api/v1/products/{product_id}` | `create_router(repo, wrap_success=..., product_not_found=...)` |

✅ 实测签名：
```python
def create_router(repository, *, wrap_success, product_not_found)
# router = APIRouter(prefix="/api/v1/products")
# @router.get("/{product_id}")
```

⚠️ **路径冲突**：D 的 router 前缀是 `/api/v1/products`，路由是 `/{product_id}`。若 B 未来加 `/api/v1/products/search`，**必须注册在 `/{product_id}` 之前**，否则 `search` 会被当成 product_id。出处：`docs/catalog-module.md:195`。

### C 的（**尚未实现，不要替它实现**）

计划 §8 列了 11 个 C 的端点：`POST /mandates/activate`、`GET /mandates/{id}`、`POST /mandates/{id}/amend`、`POST /mandates/{id}/revoke`、`POST /quotes`、`POST /purchase-proposals`、`POST /purchase-proposals/{id}/authorize`、`POST /reservations/{id}/pay`、`GET /transactions/{id}`（含只读对账）、`GET /wallet`、`GET /audit/{session_id}`、`GET /audit/{session_id}/verify`。

⛔ **SPEC GAP（HTTP-C）**：**A 是否应该代理这些端点？** 计划 DC1 说「进程内服务函数，FastAPI 仅作演示壳」，但 §8 又列了 HTTP 端点。若 C 是进程内服务，这些 HTTP 路径只是**调试壳**；若 C 是独立服务，A 需要 client。**未定义**。❗ 与 GAP-C1 同源。

## 11.2 响应信封（✅ 团队契约，必须遵守）

```json
{ "ok": true,  "data": {...}, "error": null,  "request_id": "req_..." }
{ "ok": false, "data": null,  "error": {
    "code": "PRODUCT_NOT_FOUND", "message": "...",
    "details": {}, "retryable": false }, "request_id": "req_..." }
```

**实现要点（✅ 实测可用）**

```python
from app.contracts.common import Envelope, ErrorDetail, new_request_id
from app.errors import detail_from, AgentError

Envelope.success(agent_response, agent_response.request_id)
Envelope.failure(detail_from(exc), rid)
```

⚠️ **`error.details` 永远是对象**，无详情用 `{}`，**绝不能用 `null`**（团队契约）。
✅ `detail_from()` 会把非 `AgentError` 归为 `INTERNAL_ERROR`，且 `details` 只含异常**类型名**，**不泄露 traceback**。

## 11.3 实施步骤（每步可独立验证）

| 步 | 产出 | 验证 |
|---|---|---|
| 1 | `app/api/routes_agent.py` | 三个 A 的端点，薄壳 |
| 2 | `app/main.py` | composition root，**只 `include_router`** |
| 3 | 依赖装配 | `SessionStore` / `SearchClient` / `CommerceClient` / `llm_parser` **构造一次**，放应用状态 |
| 4 | 全局异常处理器 | 兜住未捕获异常 → `INTERNAL_ERROR` 信封 |
| 5 | `tests/api/` | 信封格式、404 映射、`details` 非 null |
| 6 | 演示页面 | 单页，调 `/chat` |

**依赖装配的现成材料**

```python
from app.agent.local_search import LocalSearchClient      # 将来换 Real B
from app.agent.local_commerce import LocalCommerceClient  # 将来换 Real C
from app.agent.llm_parser import build_parser_from_env     # 无 key 时返回 None，不抛
from app.agent.session import SessionStore
```

⚠️ **`SessionStore` 必须单例** —— 每请求新建会丢掉会话（`SessionStore.get_or_create` 依赖它跨请求存在）。

## 11.4 HTTP 层**不得**吃掉未来 B/C/D

**必须避免**（本节对应用户的 §14）

```json
❌ { "message": "selected" }        // 只表达得了文本，未来无法容纳结构化载荷
```

**必须做到**：`AgentResponse` 已经携带结构化载荷，HTTP 层**原样透传**：

| 载荷 | `AgentResponse` 字段 | 状态 |
|---|---|---|
| 搜索结果 | `.results: SearchResponse \| None` | ✅ 已定义 |
| 约束 | `.constraints: HardConstraints \| None` | ✅ |
| 选中的商品 | `.selected_product_id: str \| None` | ✅ |
| 判定 | `.decision: PolicyDecision \| None` | ✅ |
| 拒绝收据 | `.denial: DenialReceipt \| None` | ✅ 已定义（C 未实现） |
| 升格 | `.escalation: EscalationRequest \| None` | ✅ 已定义（C 未实现） |
| 回执 | `.receipt: PaymentReceipt \| None` | ✅ 已定义（C 未实现） |
| 澄清 | `.clarification: Clarification \| None` | ✅ |
| 歧义 | `.ambiguities: list[Ambiguity]` | ✅ |
| **降级说明** | `.notes: list[str]` | ✅ |
| **解析轨迹** | `.trace: list[ParserAttempt]` | ✅ |

⚠️ **不要自行发明新字段**。若 HTTP 层发现需要契约没有的东西 → ⛔ **SPEC GAP**，记入 Part 13，**不要偷偷加**。

---

# Part 12 — Review Severity Policy

| 级别 | 定义 | 例子 | 期限 |
|---|---|---|---|
| **P0** | **Authority / 安全 / 事务边界被破坏** | A 能支付 · A 能批准 C 的决策 · B 绕过 A/C 直连 · 前端伪造 payment · **假结果被当作权威结果** | **立即修，阻塞一切** |
| **P1** | 功能正确性 / 契约违反 | 字段类型错 · 校验器不触发 · 测试断言恒真 · 文档与代码不一致 | acceptance 前必修 |
| **P2** | 架构 / 可维护性 | 上帝类 · 重复实现 · 耦合风险 · 未断言的防线 | 记录，视时间修 |
| **P3** | 风格 / 外观 | 命名 · 注释 · 排版 | **48h 内不得阻塞交付** |

**本次审查已定的级别**

| 发现 | 级别 | 状态 |
|---|---|---|
| `_RELAXED_VALUE` 漏 `anc_required`，被宽 `except` 吞掉 | **P1** | ✅ 已修 |
| `_canonical_policy` 与 `executable_policy()` 可能漂移 | **P1** | ✅ 已加测试守卫 |
| 12 个未使用 import | P3 | ✅ 已修 |
| `orchestrator.py` 上帝类（639 行） | P2 | ✅ 已拆 |
| `clients.py` 两个不相干替身同文件 | P2 | ✅ 已拆 |
| `parse()` 93 行嵌套 if | P2 | ✅ 已重构为规则链 |
| **两个错误码结构性不可达且无测试** | **P2** | ⬜ **未修**，见 §9.4 |
| `CommerceClient.approve_escalation` 形状不良（CR-1） | **P2** | ⬜ 未修，需 C 参与 |
| `CommerceClient` 无支付方法（CR-2） | **P2** | ⬜ 未修，需 C 参与 |
| `spend_state` 恒零导致 3 条限额永不触发（F-5） | **P1（若演示需要展示限额）** | ⬜ ❗ 需人决定 |
| AC-01…AC-16 未定义 | **P1** | ⬜ ⛔ SPEC GAP |

---

# Part 13 — Decision Log

❗ 全部标记为 **HUMAN DECISION REQUIRED** —— agent 不得代决。

> ⚠️ **v3：D-03 / D-04 / D-05 / D-06 / D-07 / D-08 已解决或已不再需要，
> D-09 / D-10 给出了可运行答案；D-01 / D-02 / D-11 / D-12 仍未决。**
> 逐条更新见 Part 0.7，理由见 `docs/A_C_contract_changes.md`。
> 下表是 v2 的原始记录，保留以便追溯。

| ID | Topic | Options | Constraint | Decision | Owner | Deadline | Impact |
|---|---|---|---|---|---|---|---|
| D-01 | **真实 B 如何接入？** 进程内服务 / HTTP / 直接读 D | (a) 进程内 `app/search/` (b) HTTP 服务 (c) A 传商品给 B | 计划 DC1 倾向 (a)，但 §8 又列 HTTP 端点 | ⬜ **未决** | 团队 / B | Phase 12 前 | 决定 `SearchClient` 的实现形态 |
| D-02 | **真实 C 如何接入？** | 同 D-01 | 同 GAP-C1 | ⬜ **未决** | 团队 / C | Phase 7 前 | 决定 `CommerceClient` 的实现形态 |
| D-03 | **`DenialReceipt` / `EscalationRequest` 如何返回给 A？** | (a) `submit_proposal` 返回联合类型 (b) 新增 `get_denial(proposal_id)` (c) 并入 `PolicyDecision` | 类型已存在，无路径 | ⬜ **未决** | 团队 / C | Phase 7 前 | 影响 A 的渲染与前端 |
| D-04 | **支付如何从 A 触发？** | (a) 新增 `pay_reservation` 到 Protocol (b) C 内部自动支付 (c) A 调 C 的 HTTP | 计划列了 `POST /reservations/{id}/pay` | ⬜ **未决** | 团队 / C | Phase 8 前 | 决定前端「确认支付」按钮的行为 |
| D-05 | **`ApprovalGrant` 的消费由谁执行？** | (a) C 的 AuthorityService (b) 新增 `consume_grant` | 判定器只读（已核实） | ⬜ **未决** | 团队 / C | Phase 8 前 | 「批准一次、只能用一次」能否保证 |
| D-06 | **`approve_escalation` 的形状是否收窄？** | (a) 保持现形状 (b) 改为 `(proposal_id, principal_id)` | CR-1 | ⬜ **未决** | 团队 / A+C | Phase 7 前 | 决定 A 是否需保管 C 的对象 |
| D-07 | **是否给替身加「模拟曝光」以演示滚动限额？** | (a) 不加，承认限额演示不了 (b) 加 `_SimulatedLedger`，全链路标注 `SIMULATED_LEDGER` | F-5；「不伪造」原则 | ⬜ **未决** | 团队 | 演示前 | 决定演示能否展示滚动额度被拦 |
| D-08 | **`httpx` 是否加进 `requirements.lock.txt`？** | (a) 加（新增非升级） (b) 测试改用 urllib (c) 不加，文档注明 | 锁文件是 A 的所有物 | ⬜ **未决** | A | 写 API 测试前 | 决定队友能否跑 API 测试 |
| D-09 | **`merchant_id` 从哪来？** | (a) B 的搜索结果提供 (b) C 报价后回填 (c) 配置 | H-C-03h；`Product` 无 merchant 字段 | ⬜ **未决** | 团队 / B+C | Phase 7 前 | 影响 `PurchaseProposal` 能否填满 |
| D-10 | **审计链由 C 还是 D 持久化？** | (a) C 自持久化 (b) C 产事件、D 落库 | R-D-08 | ⬜ **未决** | 团队 / C+D | Phase 7 前 | 影响 D 的表设计 |
| D-11 | **AC-01…AC-16 的内容** | — | ⛔ 只有 AC-13/AC-16 有描述 | ⬜ **未决** | 了解 §四十七 的人 | Phase 12 前 | **Phase 12 无法验收** |
| D-12 | **`PriorityPreset` 如何影响排序？** | — | H-B；4 个取值无语义定义 | ⬜ **未决** | 团队 / B | B 实现前 | 影响搜索质量 |
| D-13 | **`HacKU 2026` 赛道申报（Day 1 20:00 截止）提交了吗？** | 是 / 否 | 手册：未提交 = 取消资格 | ⬜ **未决** | 团队 | **已过期** | **取消资格** |
| D-14 | **B 和 C 是否在实际工作？** | — | commit log 显示 C 1 次提交（19:13）、B 0 次 | ⬜ **未决** | 团队 | 现在 | 决定整份排期是否成立 |

---

# Part 14 — Critical Path

## 14.1 时间账（✅ 实测）

```
开赛   Day 1 13:00
现在   Day 1 23:11   （T+10h11m）
冻结   Day 3 13:00   （剩 37h49m）
```

## 14.2 我的判断

⛔ 计划 §4.2 自估剩余关键路径 **27 小时**（Phase 3→4→5→7→8→9-11→12），加 `main.py`、并发、演示、Deck、缓冲约 **48 小时工作量，剩 38 小时**。

**结论：计划按原形态做不完。但架构是对的，不需要改架构——需要改的是顺序。**

## 14.3 关键路径（建议顺序）

```
①  钱的脊梁（C 的 Phase 3+7）          4h   ← 真正的阻塞点
      ↓
②  支付闭环（C 的 Phase 8）            4h
      ↓
③  A 的 mandate 流程接通               3h   ← 第二个「同意」
      ↓
④  HTTP 层 + 页面                      3h   ← 本次任务 B
      ↓
⑤  集成测试（先落 5 类可测的）         3h
      ↓
⑥  演示脚本 + 视频 + Deck              6h
      ↓
   ────────────────────────────────
   小计 23h，留 15h 缓冲 / 睡眠 / 意外
```

**为什么 ① 排第一**：没有真实 C 的记账，**三条限额永远不触发**（F-5），而「agent 被限额拦住」是题目的核心要求。当前演示只能展示**单笔上限**与**升格**，展示不了滚动与速度限制。

## 14.4 与 v2.0 计划的分歧

| 项 | v2.0 计划 | 我的建议 | 理由 |
|---|---|---|---|
| 顺序 | 分层（所有表 → 所有 registry → …） | **垂直切片**（先一条路走通） | 48h 里杀死项目的是集成太晚，不是功能太少 |
| Phase 12 集成 | 放在最后 | 提前，且先落 5 类不依赖 B/C 的用例 | 同上 |
| AC 用例 | 假定已定义 | **先定义再写** | 当前无定义（D-11） |

---

# Part 15 — 48h Scope

## MUST（Demo / 架构 / 安全，缺一不可）

| 项 | 依据 |
|---|---|
| 一条完整的 APPROVE→预留→支付→回执 | 题目：*"one complete transaction end to end"* |
| **至少一次被限额拦住** | 题目：*"including at least one case where the agent is stopped"* |
| 哈希链审计 + 只读核验 | 题目：*"a log a third party can check"* |
| 从 `policy_hash` 回答「为什么」 | 题目：*"from the recorded rule"* |
| 手工路径对比 | 题目 EVIDENCE 点名 |
| 费率标注 `SANDBOX` | 题目：*"Every rate… must be one you observed and timestamped"* |
| HTTP 三端点 | 演示入口 |
| 演示页面 | **用户明确要求** |

## SHOULD

| 项 |
|---|
| A 的 mandate 流程（第二个「同意」） |
| 前端完整的控件（选品 / 确认授权 / 确认支付 / 批准升格） |
| 两个错误码的防御性测试（§9.4） |
| `docs/evidence_sources.md` |

## OPTIONAL

| 项 |
|---|
| 奖励/积分计算（题目标为「合法的工作中心」之一，非必须） |
| 多路线比价与 `effective_cost_cents` 排序 |
| `summarize` 先探后问 |
| `relax_hints` 的完整前端渲染 |

## DEFERRED TO REAL B/C/D

| 项 | 为何推迟 |
|---|---|
| Real B integration | B 未实现（D-14） |
| Real C integration | C 只完成判定器 |
| Real D integration | **D 已是真的**，无此项 |
| real payment | 题目 §1.3 明确不做 |
| real reservation / capability / persistence | C 的 Phase 7/8 |
| 真实费率采集 | 题目 §1.3 明确不做 |

> **区分**：`Fake-compatible architecture`（接口、契约、边界）是 **MUST**；`Real B/C/D` 是 **DEFERRED**。**前者不能因为后者未完成而省略。**

---

# Part 16 — Current → Target Architecture

> ⚠️ **v2 快照。** 现行调用图见 Part 0.2；Http 层与 C 都已落地。

## 16.1 现在

```
User
 ↓
A  (app/agent/ — 13 文件 3608 行)
 ├── SearchClient ──► LocalSearchClient ──► ProductRepository ──► SQLite ✅真
 ├── CommerceClient ─► LocalCommerceClient ─┬─► evaluate_policy() ✅真判定
 │                                          └─► 内存 dict        ❌假存储
 └── (HTTP 层不存在 ⛔)
```

## 16.2 目标（S3）

```
User
 ↓
HTTP (app/main.py + app/api/)          ⛔ 待建
 ↓
A  (app/agent/ — 逻辑不变)
 ├── SearchClient ──► Real B (app/search/)        ⛔ 待建
 └── CommerceClient ─► Real C (app/commerce/其余)  🟡 部分
                        ↓
                       D (app/catalog + app/db)   ✅ 已是真
```

## 16.3 状态标注

| 区域 | 标注 |
|---|---|
| `app/contracts/**` | ✅ **现在已有**（真实契约，非假） |
| `app/catalog/` `app/db/` | ✅ **现在已有**（真实实现） |
| `app/commerce/policy_evaluator.py` | ✅ **现在已有**（真实判定） |
| `app/agent/**` | ✅ **现在已有**（真实逻辑） |
| `LocalSearchClient` | 🟡 **现在是假实现**，接口已预留 |
| `LocalCommerceClient` | 🟡 **现在是假实现**，接口已预留 |
| `SearchClient` / `CommerceClient` | ✅ **接口已预留** |
| `AgentResponse` 的载荷字段 | ✅ **接口已预留**（decision/denial/escalation/receipt 已定义） |
| Real B | 🔵 **未来需要真实实现** |
| Real C 其余 | 🔵 **未来需要真实实现** |
| 8 + 4 张表 | 🔵 **未来需要持久化** |
| 运费 / 手续费 | 🔵 **未来需要外部数据**（当前是 SANDBOX 参数） |
| HTTP 层 / 前端 | ⛔ **现在缺失，属于 MUST** |

---

# Part 17 — BCD Handoff Package

## 17.1 What We Give B

✅ 全部有契约依据（`SearchRequest`）

```
SearchRequest
├─ constraints: HardConstraints      ← A 从用户话语解析
│   category / min_price_cents / max_price_cents / brand_allowlist /
│   connection / form_factor / anc_required / min_battery_hours /
│   max_wearing_weight_g / in_stock_only
├─ preferences: PreferenceProfile
│   use_cases / criteria[attribute,direction,target,priority,source,evidence_quote] /
│   priority_preset / prefer_anc
├─ comparison: ComparisonFrame
│   dimensions / max_columns
└─ limit: int
```

**B 需要知道的三件事**（✅ 来自 `docs/handoff-to-B.md`）

1. **B 看不到用户。** 只拿到 A 转交的结构化条件。
2. **B 提供事实，A 做判定。** 无解时返回 `relax_hints`（事实），不说「建议放宽」（主张）。
3. **`match_score` 全平是正常的**，真正的排序主力是 `criteria`。

## 17.2 What We Give C

⚠️ **特别详细。按性质分类。**

### authoritative（C 必须采信，A 无权改）

| 字段 | 来源 |
|---|---|
| `principal_id` | 后端上下文 |
| `agent_id` | 配置 |
| `expected_mandate_version` | C 先前返回 |

### user-provided（用户说的，C 编译成规则）

| 字段 | 注意 |
|---|---|
| `MandateDraft` 全部 16 字段 | **C 编译为 `canonical_policy` 并算 `policy_hash`**，A 不算 |
| `quantity` | 用户要几件 |
| `preferred_payment_route_ids` | 用户偏好 |
| `shipping_address_id` | 来自 mandate |

### B-provided（A 只是转交）

| 字段 | 注意 |
|---|---|
| `product_id` | 来自 B 的搜索结果 |
| `merchant_id` | ⛔ 来源未定（D-09） |

### derived / 非权威（C 自行推导，**A 不给**）

```
cash_total_cents · merchant_total · fee · fx · policy_hash
quote_hash · spend_state · reservation_id · capability · receipt
```

> ✅ **这张「不给」清单是 A→C 边界的核心。** A 提交物**结构上**不含它们（`extra="forbid"`）。

## 17.3 What We Give D

| 给什么 | 形式 | 状态 |
|---|---|---|
| 建表回调 | `commerce_schema(conn) -> None` | ✅ 钩子已存在 |
| 钱包初始化回调 | `wallet_initializer(conn) -> None` | ✅ 钩子已存在 |
| 交易/预留/支付/审计记录 | ⛔ SPEC GAP（D-10） | ⬜ |
| 对账信息 | ⛔ SPEC GAP | ⬜ |

**D 的回调约束（✅ 实测）**：不得提交、回滚、关闭连接、`executescript()`。

---

# Part 18 — Final Definition of Done

> ⚠️ **v2 快照。** 现状见 Part 0.8：HTTP、集成测试、BCD 边界测试已完成；
> AC-01…AC-16 仍缺 14 条定义；前端只有一个演示页。

## Architecture

| 项 | 状态 |
|---|---|
| A/B/C/D boundaries 明确 | ✅ **是**（`app/agent/clients.py` + D 的 `routes.py`） |
| B/C/D interfaces 已预留 | ✅ **是**（2 个 Protocol） |
| B ↛ C | ✅ **是**（无调用路径） |
| A 无 C authority | ✅ **是**（`extra="forbid"` 结构强制） |
| D ownership 明确 | ✅ **是**（`initialize_database` 钩子） |
| HTTP 层不绕过 B/C/D | ⬜ **待建** |

## Data

| 项 | 状态 |
|---|---|
| A→B data 明确 | ✅ **是**（Part 5.1） |
| A→C data 明确 | ✅ **是**（Part 5.2，含 1 个 SPEC GAP） |
| A→D data 明确 | ✅ **是**（Part 5.3，方向是 A 给 D 回调） |
| B/C/D→system data 明确 | 🟡 **部分**（Part 6；4 个 C 的类型有定义无路径） |
| Fake data 与 future real data 有映射 | ✅ **是**（Part 7） |

## Implementation

| 项 | 状态 |
|---|---|
| Fake implementations 可运行 | ✅ **是**（469 测试） |
| Fake implementations 通过正式 interface | ✅ **是**（Protocol） |
| 不存在关键业务逻辑依赖 Fake details | ✅ **是**（判定器是真的） |
| Real implementation 可通过 adapter 替换 | 🟡 **B 可；C 需先补 4 个接口缺口** |

## Testing

| 项 | 状态 |
|---|---|
| Unit tests | ✅ 469 |
| Contract tests | ✅ 有 |
| Integration tests | ⛔ **不存在** |
| E2E tests | ⛔ **不存在** |
| Regression tests | 🟡 部分 |
| **BCD boundary tests** | ⛔ **不存在** ⬅ 建议优先 |

## HTTP

| 项 | 状态 |
|---|---|
| `/health` | ⛔ 待建 |
| `/chat` | ⛔ 待建 |
| `/actions` | ⛔ 待建 |
| `/products/{product_id}` | ✅ D 已实现，待挂载 |
| HTTP 不绕过未来 B/C/D boundary | ⬜ 设计已定（Part 11.4） |

## Documentation

| 项 | 状态 |
|---|---|
| Architecture | ✅ 本文档 Part 16 |
| Ownership | ✅ Part 4 |
| Data handoff | ✅ Part 5 |
| API | 🟡 Part 11（待实现） |
| BCD integration | ✅ Part 3、7、17 |
| E2E acceptance | ⛔ **AC 未定义**（D-11） |
| Policy audit | ✅ Part 9 |
| Decision log | ✅ Part 13（14 项待决） |
| Current → Target roadmap | ✅ Part 16 |

---

# 附录 A — 已知陷阱（踩过的，别重复踩）

| 症状 | 根因 | 处理 |
|---|---|---|
| `ModuleNotFoundError: app` | pytest 需在**仓库根**跑 | 永远 `cd D:\HackU` 再 `python -m pytest tests -q` |
| 生成文件显示为「已修改」 | `pathlib.write_text()` 在 Windows 把 `\n` 翻成 CRLF；`.gitattributes` 要求 LF | 写字加 `newline="\n"`；**用 Python 脚本生成文件后**跑 `git rm --cached -r -q . ; git reset --hard -q` 归一化 |
| 提示词渲染 `KeyError: '"kind"'` | 提示词里有 JSON 例子，被 `str.format()` 当占位符 | 花括号写 `{{` `}}`。**此 bug 曾让每一轮 LLM 都失败**，全靠兜底撑住 |
| `DataValidationError: anc_required: expected a boolean` | 约束经过**两个**校验器：Pydantic 的，与 D 的 `app.catalog.models.DataValidationError` | 两个都要抓。只抓 Pydantic 会让真 bug 藏起来 |
| 编辑文件报 `ReplaceFileW EIO (Win32 1175)` | Windows 文件锁 | 重试，通常第二次成功 |
| `git add -A` 把嵌套仓库加成 gitlink | 目录里有 `.git` | 已扁平化，不该再出现。若出现：`git rm --cached -r <path>` 后 amend |
| PowerShell 不支持 `-Encoding utf8NoBOM` | 这是 PS **5.1** 不是 pwsh 7 | 用 `[System.IO.File]::WriteAllText($p,$s,(New-Object System.Text.UTF8Encoding($false)))` |
| PowerShell 不支持 heredoc `<<'PY'` | 同上 | 把脚本写成文件再跑 |
| `Get-Content` 行号与 read 工具对不上 | PS 5.1 默认编码非 UTF-8 | 以 read 工具为准 |
| **派出去做代码审查的子代理会失败** | 前人派过两个，都 `failed before it finished`（无原因） | 审查自己做，或拆得更小 |
| `TestClient` 在队友机器上 ImportError | **`httpx` 不在 `requirements.lock.txt`** | 见 D-08 |

# 附录 B — 验收命令速查

```powershell
cd D:\HackU

python -m pytest tests -q                    # 469 passed, 1 skipped
python scripts/gen_fixtures.py --check       # exit 0
python scripts/check_fixtures.py             # exit 0
python scripts/init_demo.py                  # 幂等
python scripts/probe_llm_format.py --dry-run # exit 0

# 对话演示（离线，不需要 key）
python scripts/demo_conversation.py --no-llm

# 对话演示（真实 LLM）
$env:PYTHONIOENCODING="utf-8"
python scripts/demo_conversation.py
python scripts/demo_conversation.py --interactive
```

**实测环境**：Python 3.11.9 · fastapi 0.142.2 · uvicorn 0.54.0 · starlette 1.7.0 · pydantic 2.13.4 · pytest 9.1.1 · **httpx 0.28.1（不在锁文件）**

# 附录 C — 禁止事项（绝对不要）

- 把 Fake B/C/D 写死成它们的最终形态
- 让 A 直接依赖 Fake implementation 的细节
- 让前端模拟 C authority，或伪造 payment
- 让 B 直接调用 C
- 把当前临时字段变成未经确认的正式 contract
- 为了 Day 1 Demo 删除未来 B/C/D 的接口
- 把「现在没有真实数据」理解成「不需要数据接口」
- **凭空创造当前规格不存在的业务规则** —— 遇到就标 ⛔ SPEC GAP + ❗ HUMAN DECISION REQUIRED
- 写死测试数量（README 已因此过期过一次）

