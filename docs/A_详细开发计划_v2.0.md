# 成员 A 详细开发计划 v2.0（修订版）

**角色**：需求理解与任务编排（`app/agent/`、`app/contracts/`、`app/main.py`），兼集成负责人
**赛题**：HacKU 2026 FinTech 第 1 题 — Give a Machine a Wallet（HKT 赞助）
**交付窗口**：Day 1 13:00 开赛 → Day 3 13:00 Code Freeze（共 48 小时）

**修订依据**：用户确认的《A/C Delegated Commerce v2.0》总提示词
**取代**：`A_详细开发计划_v1.0.md`（**已删除**；它的流程判断与错误归属记在下节「修订摘要」里，那是它唯一的存留记录）
**当前进度**：Phase 0–2b 已交付；测试数见 `python -m pytest tests -q`，不在此处写死

---

## 修订摘要（v1.0 → v2.0）

v1.0 的**流程判断是对的**（要引入 mandate 层，而不是继续做逐笔确认的聊天机器人），但**职责归属错了**：它把 `authorize()`、令牌签发和审计链放在 `app/agent/` 之下。

按 v2.0 第 四 节，mandate、policy、reservation、capability、payment 全部归 **C**。v1.0 的做法会让 A 既能提出购买、又能批准购买、还能签发支付凭证——**边界形同虚设**。

| # | v1.0 原文 | v2.0 修订 |
|---|---|---|
| 1 | `app/agent/policy/engine.py` 存放 `authorize()` | → `app/commerce/policy_evaluator.py` |
| 2 | 调用图把 `authorize()` 画在 A 的框内 | → 画在 C 的框内；A→C 的连线**穿过**它 |
| 3 | A 签发 `AuthToken`，C 校验 | → **C 签发 `PaymentCapability`**；A 只能提交 `PurchaseProposal` |
| 4 | 9 条检查 | → 24 项检查，顺序固定 |
| 5 | DC5「币种配置项，默认 HKD」 | → 固定 `Literal["HKD"]`（D 已 HKD-only） |
| 6 | `contracts/commerce.py` 定义 `CreateOrderRequest`/`Order`/`PayOrderRequest` | → 定义 `Quote`/`PaymentRouteEvaluation`/`PurchaseProposal`/`Reservation`/`SpendState` |
| 7 | Demo 金额 `269 / 325` | → 真实商品金额 `289 / 309 / 509` |
| 8 | `SpendState` 只累计已结算 | → **计入预留**，否则并发可超预算 |
| 9 | 时间线从 T+0 建仓开始 | → Phase 0–2b 已完成 |
| 10 | "另建初始化流程" | → 使用 D 的 `commerce_schema(conn)` 钩子，**零改 D 文件** |

---

## 0. 这份计划要解决的问题

### 0.1 原四人计划的缺口（v1.0 判断，仍然成立）

原计划把流程设计成「用户说 → 搜索 → 展示 → 用户点确认 → 扣款」，**这是一个安全的聊天机器人，不是题目要的 agentic commerce。**

题目原文的 Scope Requirement：

> Choose one spending decision and **one party delegating it**. Define what the agent may do, what it may not, and **how a limit is actually enforced** — a cap, a mandate, an expiry, a rule, a revocation. Demonstrate **one complete transaction end to end, including at least one case where the agent is stopped.**

要的是「授权**事先**给出 → agent 自主行动 → 被限额拦住」。缺失会导致 25% 的 problem-solution fit 不达标、15% 的 security 分无处落地、最精彩的演示不存在。

### 0.2 v2.0 的分工（本次修订的核心）

```
A 负责让用户清楚地表达并理解授权。
C 负责确保任何一分钱都只能按照这份授权移动。
```

```
A proposes.  C decides.  C reserves.  C pays.  C records.  A explains.
```

### 0.3 工作边界总览

```
用户话语 ──[LLM 区]──► 结构化对象 ──[确定性区]──► 钱
          可错、可重试              不可错、不可重试
          A 的 parse / render      C 的 policy / reservation / capability / payment
```

**一句话原则**：LLM 的输出永远只能是一个「提议」（proposal）或一个「候选策略」，**永远不能是一个「批准」。**

---

## 1. 交付物定义

### 1.1 必须交付（对应题目 EVIDENCE）

| # | 交付物 | 归属 | 对应题目要求 |
|---|---|---|---|
| D1 | 签名后的 `Mandate`（含 `policy_hash`、不可变版本历史） | C 存，A 展示 | "the log of what the agent was permitted to do" |
| D2 | 至少一次 `DenialReceipt`（primary reason + observed/limit + 当时的策略哈希） | C 产出 | "then show it being stopped" |
| D3 | 哈希链审计日志 + 只读核验视图 | C 记，A 呈现 | "a log a third party can check without having to trust the operator" |
| D4 | 从 `policy_hash` 指向的规则原文回答"为什么这么做" | C 的规则，A 的文案 | "from the recorded rule, rather than from an explanation written afterwards" |
| D5 | 手工路径 vs agent 路径的步骤/耗时/成本对照 | A | "Compare against the manual route" |
| D6 | 每条外部费率带 `observed_at` + 来源，或标注 `SANDBOX` | A 记录 | "Every rate… must be one you observed and timestamped" |

### 1.2 演示必须跑通的两个场景

**场景 A：购物与推荐（不花钱）** — A + B

```
用户："找 300 以内、通勤用、必须支持主动降噪的无线耳机"
 → parse → 确认需求 → 调 B → 候选 + 对比 → 用户选第一款
```

**场景 B：授权与执行（花钱，且有被拦住的）** ← **题目真正的考点** — A + C

```
用户："未来 7 天内，帮我从 Demo Audio Store 买一件通勤用的无线主动降噪耳机。
      最终每笔不超过 HK$300，24 小时累计不超过 HK$500，5 分钟最多两次，
      只用 FPS 或尾号 1234 的 Mastercard，超过 HK$280 先问我。
      不要修改收货地址，我可以随时撤销。"
 → A 解析 MandateDraft → 展示摘要（可以/不可以/需介入）→ 用户确认
 → C 激活 Mandate v1，签发 policy_hash 与 consent_event
 → A 调 B → 确定性取第一名 → 取 Quote → 提交 PurchaseProposal
 → C：持锁 → 读 mandate/quote/route/SpendState → evaluate_policy() → 原子预留
 → 三种结果（金额取自真实商品，见 §1.4）
```

### 1.3 明确不做（写进 README，避免范围蔓延）

- 真实支付、真实网站抓取、真实登录
- 地址管理、优惠券、退款流程（**撤销 ≠ 退款**）
- 多商家、异步支付回调、自动重试
- 真实费率的生产级采集

### 1.4 演示金额（**必须来自真实商品，不得伪造**）

v1.0 写的 `259 + 10 = 269` / `289 + 36 = 325` 在 D 的 40 条真实商品中**不存在**。按 v2.0 第 一 节 25 条（禁止伪造费率），演示改用真实可核验的组合：

| 场景 | 真实商品 | 单价 | 运费 | 最终现金支出 | 期望结果 |
|---|---|---|---|---|---|
| **通过** | `hp_0018` 无线 ANC | 27900 | 1000 | **28900** | `APPROVE` → 预留 → 支付 → `PaymentReceipt` |
| **超上限** | `hp_0001` 无线 ANC（价格边界商品） | 29900 | 1000 | **30900** | 在 `cap=30000` 下 `DENY`，**不建预留、不调支付适配器** |
| **升格** | `hp_0003` 无线 ANC | 49900 | 1000 | **50900** | 在 `cap=60000, escalate=45000` 下 `ESCALATE` |

运费 HK$10 为 **SANDBOX 虚构参数**（`.env.deepseek.example: SANDBOX_SHIPPING_CENTS=1000`），必须在 `docs/evidence_sources.md`（**待产出**）中标注，**不得声称是实测费率**。

**另有 D 提供的边界商品**：`hp_0007` = 30000 分，正好是「HK$300 含边界」的验证商品；`hp_0005`/`hp_0033`/`hp_0011`/`hp_0022` `stock=0` 用于缺货路径。

---

## 2. 目标架构与模块归属（已修订）

### 2.1 调用图（这张图直接进 Pitch Deck）

```
              ┌──────── A ────────┐
 用户话语 ──► │ parse → merge     │
              │   ↓        ↓      │
              │ render   selection │
              └───┬────────┬──────┘
                  │        │  PurchaseProposal（不含任何权威数值）
                  │        ▼
                  │   ┌──────────────────── C ────────────────────┐
                  │   │ MandateRegistry   CanonicalPolicyService  │
                  │   │ QuoteService      PaymentRouter           │
                  │   │ ─────────────────────────────────────────  │
                  │   │ PolicyEvaluator  ← 纯函数，零 IO          │
                  │   │ AuthorityService  ReservationService      │
                  │   │ CapabilityService PaymentService          │
                  │   │ LedgerService     AuditService            │
                  │   └───────┬───────────────────────┬───────────┘
                  │           │                       │
        SearchRequest     ProductRepository      PaymentAdapter
                  │           │                  (MockFPS / MockMC)
                  ▼           ▼                       │
              ┌──── B ────┐ ┌──── D ────┐             ▼
              │ 过滤/排序 │ │ 商品/连接 │          Payment
              │ 对比/缺口 │ └───────────┘
              └───────────┘
   ✗ B → C 不存在        ✗ A 不签发支付凭证
```

### 2.2 修订后的三条架构规则

1. **A 不能批准支付。** A 只能提交 `PurchaseProposal`；批准、预留、签发凭证、扣款全部在 C 内完成。
2. **`PolicyEvaluator` 是纯函数，且不知道 agent 存在。** 它只认 `PolicyInputs(proposal, mandate, quote, route, spend_state, now, approval_grant)`。零 IO、无 LLM、不读时钟、不发凭证。
3. **B 与 C 之间零调用。** B 只读，C 只写钱，一旦连通授权链即被绕过。

### 2.3 目录与文件归属（与当前仓库一致）

工作区已拍平为**单一仓库**：`app/`、`data/`、`docs/`、`fixtures/`、`scripts/`、
`tests/`、`var/` 都在仓库根下平级，不再有嵌套的应用子仓库。

```
HackU/                                ← 仓库根
├─ .env.deepseek.example             A  配置模板（DeepSeek，当前使用）
├─ .env.openai.example               A  配置模板（OpenAI）
├─ AGENTS.md                         A  用户确认的协作约束
├─ README.md                         A  项目总览
├─ requirements.lock.txt             A  ✅ 已锁定
│
├─ app/
│  ├─ main.py                        A  ⬜ Composition root（仅 include_router）
│  ├─ errors.py                      A  ⬜ 结构化错误
│  │
│  ├─ contracts/                     A  ✅ 独占，全员只读
│  │   ├─ common.py                  ✅ Envelope / ErrorCode / 枚举
│  │   ├─ product.py                 ✅ Product(HKD,20字段) / HardConstraints
│  │   ├─ search.py                  ✅ A→B（见 A2B 规范）
│  │   ├─ agent.py                   ✅ ChatRequest / IntentResult / LLMClient
│  │   ├─ mandate.py                 ✅ MandateDraft / Mandate / ApprovalGrant / policy_hash
│  │   ├─ commerce.py                ✅ Quote / PaymentRouteEvaluation / PurchaseProposal
│  │   │                                / Reservation / SpendState
│  │   ├─ policy.py                  ✅ PolicyDecision / PolicyViolation / DenialReceipt
│  │   │                                / EscalationRequest / PaymentReceipt
│  │   └─ audit.py                   ✅ 哈希链审计
│  │
│  ├─ agent/                         A
│  │   ├─ llm_client.py              ✅ OpenAI 兼容客户端（stdlib urllib）
│  │   ├─ intent_schema.py           ✅ schema/提示词生成 + 回复校验
│  │   ├─ orchestrator.py            ⬜ AgentOrchestrator
│  │   ├─ intent_parser.py           ⬜ IntentParser (Protocol)
│  │   ├─ llm_parser.py              ⬜ LLMIntentParser（应并入 intent_schema.py，别留两份）
│  │   ├─ fallback_parser.py         ⬜ FallbackIntentParser（确定性，无网可用）
│  │   ├─ mandate_builder.py         ⬜ MandateBuilder
│  │   ├─ mandate_merge.py           ⬜ MandatePatchService
│  │   ├─ clarification.py           ⬜ ClarificationService
│  │   ├─ response_renderer.py       ⬜ ResponseRenderer
│  │   ├─ session.py                 ⬜ AgentSession
│  │   └─ clients.py                 ⬜ 调用 B/C 的唯一出口（依赖注入）
│  │
│  ├─ commerce/                      C
│  │   ├─ policy_evaluator.py        ✅ 纯函数 evaluate_policy()
│  │   ├─ canonical.py               ⬜ CanonicalPolicyService
│  │   ├─ mandate_registry.py        ⬜
│  │   ├─ quote_service.py           ⬜
│  │   ├─ payment_router.py          ⬜
│  │   ├─ authority.py               ⬜
│  │   ├─ reservation.py             ⬜
│  │   ├─ capability.py              ⬜
│  │   ├─ state_machine.py           ⬜
│  │   ├─ payment_service.py         ⬜
│  │   ├─ ledger.py                  ⬜
│  │   ├─ audit.py                   ⬜
│  │   ├─ schema.py                  ⬜ C 的表定义（独立文件，不回写 db/schema.py）
│  │   ├─ repository/                ⬜ 各 Repository
│  │   └─ adapters/                  ⬜ base.py / mock_fps.py / mock_mastercard.py
│  │
│  ├─ search/                        B  ⬜ 待建
│  ├─ catalog/                       D  ✅ 已完成，31 测试全绿
│  └─ db/                            D  ✅ 已完成
│
├─ data/products.seed.json           D  ✅ 40 条 HKD 英文商品
├─ docs/                             A  ✅ 计划、规范、实测报告
├─ fixtures/                         A  ✅ 6 组输入输出配对（由生成器复算）
├─ tests/
│  ├─ test_catalog.py                D  ✅ 31 测试
│  ├─ contracts/                     A  ✅ 契约测试
│  ├─ agent/                         A  ✅ 58 测试（LLM 格式边界）
│  ├─ commerce/                      C  ✅ 53 测试（policy evaluator）
│  ├─ search/                        B  ⬜ 验收骨架（app/search/ 建出前整体跳过）
│  └─ integration/                   A  ⬜ test_flow
└─ var/demo.sqlite3                  D  运行库，可重建，不入 Git
```

**`scripts/` 下两类工具的判据**（新加脚本按此放）：

| 工具 | 是否 `import app` |
|---|---|
| `gen_fixtures.py`、`check_fixtures.py` | 否 —— 直接读 JSON 种子 |
| `init_demo.py`、`inspect_demo.py`、`probe_llm_format.py` | 是 —— 需要 `app` 包 |

## 3. 关键设计决策（已修订）

| # | 决策 | 取值 | 修订说明 |
|---|---|---|---|
| DC1 | 模块间通信 | **进程内 Python 服务函数**；FastAPI 仅作演示与调试壳 | 不变 |
| DC2 | 支付凭证算法 | **stdlib `hmac` + `hashlib` + `base64`**，不引入 PyJWT | 签发方由 **A 改为 C** |
| DC3 | 额度账归属 | **C 持有**；A 经只读接口取 `SpendState` | 不变，但 `SpendState` 计入预留 |
| DC4 | 策略判定形态 | **纯函数** `evaluate_policy(PolicyInputs) -> PolicyDecision` | 位置由 `app/agent/policy/` **改为 `app/commerce/`** |
| DC5 | 币种 | **固定 `Literal["HKD"]`**，不是配置项 | D 已 HKD-only；CNY 会被拒绝 |
| DC6 | LLM 供应商 | 配置或适配器，**不渗透到 `contracts/commerce/catalog`** | 不变；另加 `FallbackIntentParser` |
| DC7 | 会话存储 | 单进程内存；**mandate、审计链、预留必须持久化** | 不变，范围扩展 |
| DC8 | 单后端 worker | 第一版固定 1 个 | 不变 |
| DC9 | **新增**：A 的提交物 | 只有 `PurchaseProposal`，**结构上无法携带**金额/额度/哈希/状态 | 由 Pydantic `extra="forbid"` 强制 |
| DC10 | **新增**：合规判定基准 | 一律比较 `cash_total_cents`（商品+运费+手续费+汇兑），**奖励不参与** | 防止返现绕过上限 |
| DC11 | **新增**：未知值语义 | `anc=null` **不满足** `anc_required=true` | 未知 ≠ 满足 |
| DC12 | **新增**：升格超时 | **fail closed**，超时即 `DENY` | 绝不自动放行 |

---

## 4. 进度与里程碑

### 4.1 已完成（Phase 0–2b）

| Phase | 内容 | 状态 |
|---|---|---|
| P0 | git init + baseline commit；依赖安装与锁定；配置模板（现为 `.env.deepseek.example` / `.env.openai.example`） | ✅ |
| 2 | `contracts/` 五个模块 + `__init__` | ✅ |
| 2b | `app/commerce/policy_evaluator.py` + 全量单测 | ✅ |

**已交付 commit**：4 个（可从 `git log` 核对），`156 passed`，**D 的代码零改动**。

**已实测的关键联调点**：`ProductRepository(product_model=Product)` 返回 A 的 Pydantic 类型；D 的 `HardConstraints.coerce()` 接受 A 的模型；`hp_0007`（30000 分）被含入 300 的上界。

### 4.2 剩余关键路径

```
Phase 3  C 的表结构 ──► Phase 4 MandateRegistry ──► Phase 5 QuoteService
   (2h)                      (3h)                        (2h)
        │
        └──► Phase 7 Authority+Reservation（★事务边界）──► Phase 8 Capability+Payment
                     (4h)                                      (4h)
                          │
                          ▼
             Phase 9-11 A 侧（Mandate/Orchestrator/Renderer）──► Phase 12 集成
                          (8h)                                    (4h)
```

**关键路径上的第一优先项已经完成**（`evaluate_policy` 纯函数 + 53 测试）。剩余最关键的是 **Phase 7 的事务边界**——策略判定与预留创建必须在同一 `BEGIN IMMEDIATE` 内，中间不得释放锁。

### 4.3 逐时段计划（从当前进度起算）

| 时段 | 目标 | 退出条件 |
|---|---|---|
| **T+0 – T+2h** | Phase 3：C 的 8 张表，经 D 的 `commerce_schema(conn)` 钩子接入 | 临时库可建表；`initialize_database(commerce_schema=...)` 通过；D 的文件零改动 |
| **T+2 – T+5h** | Phase 4：`MandateRegistry` + `CanonicalPolicyService` | 激活/修改/撤销/取当前版本；版本不可变；`policy_hash` 可复算 |
| **T+5 – T+7h** | Phase 5：`QuoteService` + `QuoteRepository` | 从 D 读真实价格算出 landed cost；短期过期；`quote_hash` 稳定 |
| **T+7 – T+11h** | **Phase 7：`AuthorityService` + `ReservationService`** ★ | 判定与预留同事务；并发两笔各 600 只过一笔；DENY 零预留 |
| **T+11 – T+15h** | Phase 8：`CapabilityService` + `PaymentService` + Router + Adapters | 凭证篡改/过期/重放被拒；UNKNOWN 不自动重试 |
| **T+15 – T+19h** | Phase 9：A 侧 `IntentParser` + `FallbackParser` + `MandateBuilder` + `MandatePatchService` | 话语→Draft；矛盾追问；确定性 patch；无网可跑 |
| **T+19 – T+23h** | Phase 10–11：`AgentOrchestrator` + `ResponseRenderer` | 场景 B 端到端；SUCCESS/DENY/ESCALATE/UNKNOWN 四种文案 |
| **T+23 – T+27h** | 接 B（Phase 2b 契约）：搜索/对比/缺口 | A2B 规范改 HKD 后，B 侧 fixture 全绿 |
| **T+27 – T+31h** | Phase 12：`tests/integration/` 16 项 AC 用例 | AC-01…AC-16 全绿 |
| **T+31 – T+35h** | `app/main.py` + 只读核验视图 | 端点可调；核验页能重算哈希链 |
| **T+35 – T+39h** | 并发测试（真实 SQLite 事务，非纯函数） | 并发超预算用例全绿 |
| **T+39 – T+44h** | 演示脚本 + 3 分钟视频 + 证据件 | 视频一镜到底，含被拦住的那一笔 |
| **T+44 – T+48h** | Pitch Deck + 冻结提交 | 留 ≥4h 缓冲；仓库公开可访问 |

### 4.4 硬性外部时点

| 时点 | 事项 | 后果 |
|---|---|---|
| **Day 1 20:00** | 赛道申报（Google Form） | 未提交 = 取消资格 |
| **Day 1 15:00–16:00** | **HKT workshop（不录播）** | 评分口径就在那场。**A 作为集成负责人必须去** |
| **Day 3 13:00** | Code Freeze + 提交 | 迟到一律不受理 |

---

## 5. 接口边界规范

### 5.1 A → B（契约已完成，HKD）

见 `docs/A2B_搜索推荐对比接口规范_v1.1.md`，代码对应 `app/contracts/search.py`，测试数据见 `fixtures/`。

- **A 提供判断，B 提供事实**：A 给硬约束 + `PreferenceProfile`（含 `criteria`）+ `ComparisonFrame`；B 返回候选 + `comparison` + `gaps` + `relax_hints`
- **A 负责把 B 的结构化输出变成人话**；B 不写文案，只返回带 `field` 的 `MatchReason` 对象
- **B 报 `dropped_criteria` 非空时，A 的集成测试必须失败**（表示语义已漂移）
- 约束字段名与商品字段名不是同一命名空间，映射见 `CONSTRAINT_TO_ATTRIBUTE`

### 5.2 A → C（**唯一入口是 `PurchaseProposal`**）

```jsonc
POST /api/v1/purchase-proposals
{
  "proposal_id": "prop_0001",
  "mandate_id": "man_0001",
  "expected_mandate_version": 1,      // 撤销 = 版本递增，C 校验
  "principal_id": "demo_user",        // 来自后端上下文
  "agent_id": "demo_agent",
  "product_id": "hp_0018",
  "quantity": 1,
  "merchant_id": "demo_audio_store",
  "quote_id": "q_0001",
  "preferred_payment_route_ids": ["fps_demo"],
  "shipping_address_id": "addr_demo_01",
  "request_id": "req_0001",
  "idempotency_key": "idem_0001"
}
```

**A 结构上无法提交**（Pydantic `extra="forbid"` 会直接拒绝）：`cash_total_cents`、`remaining_budget_cents`、`policy_hash`、`payment_status`、`wallet_balance_after_cents`、`reward_earned_cents`、任何批准决定。

**C 的判定结果**回给 A：`PolicyDecision` / `DenialReceipt` / `EscalationRequest` / `PaymentReceipt`。A 只渲染，不重算。

### 5.3 策略判定的固定检查顺序（`evaluate_policy`，24 项）

```
 1 principal_id              13 quote_id 一致
 2 agent_id                  14 quote_hash 自校验
 3 mandate.status            15 quote.issued_at 不在未来
 4 expected_mandate_version  16 quote.expires_at 未过
 5 valid_from                17 route 与 quote 总额一致
 6 expires_at                18 quote.currency == mandate.currency
 7 merchant_id               19 velocity（窗口内笔数，含预留）
 8 quote.product_id          20 cap_per_transaction（cash_total）
 9 quote.merchant_id         21 rolling_cap（当前 exposure + cash_total）
10 category                  22 max_quantity_total（含预留数量）
11 connection                23 escalate_above → ESCALATE
12 anc_required（null 不满足）24 全部通过 → APPROVE
   另：quantity、shipping_address_id、payment_route_id 在 12–18 之间
```

**硬规则**

- **收集全部违规，指定一条 primary reason**：UX 给最可操作的那条，审计日志里全都在。
- **任何硬违规压过 ESCALATE**：升格是"提问"，不是"否决权覆盖"。
- **撤销与过期不产生副作用**：不得因为"看一眼余额"而改变状态。
- **不变量（由模型强制）**：`DENY`/`ESCALATE` 的 `reservation_created` 与 `payment_adapter_called` 恒为 `False`；`APPROVE` 恒无违规、无 primary reason。

### 5.4 事务边界（Phase 7 的核心，尚未实现）

```
BEGIN IMMEDIATE
    读 proposal / mandate / quote / route / SpendState
    decision = evaluate_policy(...)          ← 纯函数
    DENY      : 写 DenialReceipt + AuditEvent，不建预留
    ESCALATE  : 写 EscalationRequest，不签发普通凭证
    APPROVE   : 建 Reservation + AuditEvent
COMMIT
```

**策略判定与预留创建之间不得释放数据库锁。** 否则两个并发提案各自看到全部剩余额度，合起来超额。

### 5.5 撤销的语义边界（必须诚实）

| 撤销到达时 | 正确行为 |
|---|---|
| 已 APPROVE、未提交支付 | 版本落后 → 撤回，释放预留 |
| 已提交支付、结果未知 | 保留预留，转 `UNKNOWN`，**等待对账** |
| 已 `PAID` | **记录 `REVOCATION_MISSED`**，不得假装取消，**不得把 revoke 说成 refund** |

---

## 6. AI 边界清单

### 6.1 允许 vs 禁止

| LLM 可以做 | LLM 绝对不可以 |
|---|---|
| 话语 → `IntentResult` | 决定是否扣款 |
| 话语 → `MandateDraft` 字段值 | 决定 `principal_id` / `agent_id` |
| 生成文案的**连接词** | 生成任何数字（必须插值自 C 的字段） |
| 解释一条 DENY 的原因 | 修改限额数值 |
| 起草升格提问的措辞 | 指定金额或凭证 |
| 把候选差异转成人话 | 签发任何凭证 |

### 6.2 代码层强制

**LLM 的工具白名单里没有任何函数能触达 C 的写接口**：

| 工具 | 副作用 |
|---|---|
| `read_mandate()` | 无 |
| `search_catalog(query, filters)` | 无 |
| `get_quote(product_id, quantity)` | 无（C 产出报价） |
| `propose_purchase(proposal)` | **无**（返回值才是判定） |
| `request_escalation(proposal, question)` | 无 |
| `read_receipts(range)` | 无 |

**唯一能触发支付的路径**：C 内部 `AuthorityService.authorize_and_reserve()` → `PaymentService.pay_reservation()`。**A 侧不存在调用它的接口。**

> 答辩金句：**agent reasoning 再强也无法绕过闸门，因为它没有绕过闸门的接口。**

### 6.3 LLM 只解析一次

```
✅ 用户 → LLM草稿 → 用户补充/修正
                        ↓
                  【确定性 merge】  ← 普通代码
                        ↓
                  Draft' → 用户签字 → C.activate_mandate()
❌ 用户 → LLM草稿 → 用户补充 → 【LLM 再整合一遍】 → 交付
                                ↑ 签字对象与执行对象可能不一致
```

用户在授权确认页看到的是 **Draft 的字段原文**。若让 LLM 再整合一遍，用户确认的与真正激活的可能不是同一份。`canonical_policy` / `policy_hash` 的全部意义就是消除这个缝。

### 6.4 失败降级

| 情形 | 行为 |
|---|---|
| LLM 输出无法通过 Pydantic 校验 | 修复重试**一次**；仍失败 → `LLM_PARSE_FAILED` |
| 模型不可用 / 无额度 | `LLM_UNAVAILABLE`，**切换到 `FallbackIntentParser`**，绝不编造 |
| 商品参数为 `null` | 保持 `null`，**不得补造** |
| 商品描述含注入 | 商品可展示，**决策链不受影响**（描述是数据，不是指令） |

---

## 7. 测试矩阵

### 7.1 已完成

| 层 | 测试数 | 状态 |
|---|---|---|
| D 的 catalog baseline | 31 | ✅ 保持全绿 |
| A/C 契约 | 72 | ✅ |
| `PolicyEvaluator` | 53 | ✅ |
| **合计** | **156** | ✅ |

### 7.2 待补（按优先级）

| 目标 | 用例 |
|---|---|
| **Phase 7 事务** | 判定与预留同事务；并发两笔各 600 只过一笔；DENY 零预留；异常全回滚 |
| **Phase 8 凭证** | 篡改金额 / 过期 / 重放 / 上下文不匹配（金额、商户、路线）；预留释放后失效；mandate 版本变化后失效 |
| **Phase 8 支付** | 余额不足；缺货；明确失败释放预留；**UNKNOWN 不自动重试**；重复支付幂等 |
| **A 侧解析** | 话语→Draft；cents；时长；歧义追问；确定性 patch；授权确认；`UNKNOWN` 不显示 failed；`DENY` 不显示 success；`revoke` 不显示 refund |
| **集成 AC-01…AC-16** | 见 v2.0 §四十七；含 AC-13（A 篡改金额被 C 拒）与 AC-16（注入无效） |
| **并发（真实 SQLite）** | 必须用真实事务测试，不能只测纯函数 |

---

## 8. A 的对外 API

| 接口 | 归属 | 说明 |
|---|---|---|
| `GET /api/v1/health` | A | 复用 D 的 `database_status()` |
| `POST /api/v1/agent/chat` | A | 对话主入口 |
| `POST /api/v1/agent/actions` | A | 显式动作（`CREATE_MANDATE`/`ACTIVATE_MANDATE`/`REVOKE_MANDATE`/`RUN_DELEGATED_PURCHASE`/`APPROVE_ESCALATION`/`CHECK_STATUS`…） |
| `GET /api/v1/products/{product_id}` | D | **D 已实现**，A 用 `create_router()` 注入包装与错误映射 |
| `POST /api/v1/mandates/activate` | C | — |
| `GET /api/v1/mandates/{mandate_id}` | C | — |
| `POST /api/v1/mandates/{mandate_id}/amend` | C | — |
| `POST /api/v1/mandates/{mandate_id}/revoke` | C | — |
| `POST /api/v1/quotes` | C | — |
| `POST /api/v1/purchase-proposals` | C | — |
| `POST /api/v1/purchase-proposals/{id}/authorize` | C | A 不调用其判定逻辑 |
| `POST /api/v1/reservations/{id}/pay` | C | — |
| `GET /api/v1/transactions/{id}` | C | 含只读对账入口 |
| `GET /api/v1/wallet` | C | — |
| `GET /api/v1/audit/{session_id}` | C | 哈希链 |
| `GET /api/v1/audit/{session_id}/verify` | C | 第三方核验（重算哈希） |

`main.py` 只做 composition root：按模块 `include_router`，不写业务。

---

## 9. 集成负责人职责

| 职责 | 动作 | 状态 |
|---|---|---|
| 契约冻结 | `contracts/` 落地 + 升版本流程 | ✅ 进行中 |
| 依赖锁定 | `requirements.lock.txt`，全员沿用 | ✅ |
| 交付目录 | 统一在 D 的项目目录内扩展 | ✅ 已裁定 |
| 桩管理 | 未就绪模块用假实现，**交付版本不得残留桩** | ⬜ |
| 联合验收 | `tests/integration/` 全场景 | ⬜ |
| 契约变更仲裁 | 任何字段/语义变更经 A 升版本 + 重跑生成器 | ✅ 流程已定 |
| 证据件 | 公开仓库、live demo 或 3 分钟视频、Pitch Deck | ⬜ |

---

## 10. 风险与对策

| # | 风险 | 影响 | 对策 |
|---|---|---|---|
| R1 | **做成聊天机器人，没有 mandate** | 25% problem fit 归零 | 场景 B 是**必做项** |
| R2 | 编造费率/积分/汇率 | 题目明文视为造假 | 只用真实商品价格 + `SANDBOX` 标注 |
| R3 | 真实下单 | 资金/合规 | 全程 mock 适配器 + sandbox |
| R4 | 让 LLM 当最终裁量者 | 核心考点塌陷 | `evaluate_policy` 纯函数 + 工具白名单 |
| R5 | **A 被错误实现或篡改** | 越权扣款 | C 独立校验一切；A 的提交物结构上不含权威数值 |
| R6 | 无网络 / LLM 不可用 | 演示中断 | `FallbackIntentParser`；确定性流程全程可跑 |
| R7 | 撤销只做成 UI 开关 | 空按钮 | 版本递增 + 在途三态处置 |
| R8 | UNKNOWN 无限占额度 | 额度被幽灵占满 | Phase 8 至少实现只读对账入口 |
| R9 | 审计链未持久化 | 重启即丢证据 | 单独持久化；reset 默认不删 |
| R10 | 范围蔓延 | 48h 做不完 | §1.3 不做清单写进 README |
| R11 | D 的文件被误改 | 破坏队友成果 + merge 冲突 | 只用 D 的官方钩子；每次提交前用 `git log --name-only` 自查 |

---

## 11. 24 小时内必须完成的最小可用版本

时间不够时，**砍功能不砍这三样**：

1. **`evaluate_policy` + 单测**（✅ 已完成）
2. **至少一条真实的 `DenialReceipt`**（题目明文要求 "at least one case where the agent is stopped"）
3. **哈希链审计日志 + 只读核验**（15% security 分的落点）

可砍：`ResponseRenderer` 的精美度、升格完整流程、`summarize` 先探后问、全部非核心端点。

---

## 12. 参考文件

| 文件 | 状态 |
|---|---|
| `docs/PHASE1_Repository_Inspection.md` | ✅ 实测报告 + 整合裁定（**当前权威**） |
| `docs/A2B_搜索推荐对比接口规范_v1.1.md` | ✅ 已完成，HKD / 20 字段，与代码和 fixtures 一致 |
| `docs/A_详细开发计划_v1.0.md` | 🗑️ 已删除（被本文件取代；理由见「修订摘要」） |
| `docs/A_C_contract.md` | ⬜ 待产出 |
| `docs/mandate_spec.md` | ⬜ 待产出 |
| `docs/payment_boundary.md` | ⬜ 待产出 |
| `docs/evidence_sources.md` | ⬜ 待产出（含运费为 SANDBOX 参数的声明） |
| `app/contracts/*` | ✅ 已实现 |
| `app/commerce/policy_evaluator.py` | ✅ 已实现 |
| `fixtures/` 6 组 | ✅ 已以真实 40 条商品为基准，由 `scripts/gen_fixtures.py` 复算 |
