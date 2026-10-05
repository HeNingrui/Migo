# Phase 1 — Repository Inspection（修订版 v2）

> **历史文档，描述的是检查当时的状态。**
>
> 本报告作于目录拍平之前，因此里面的 `headphone-database-D-HKD\headphone-database\`
> 路径、以及「两个 git 仓库、fixtures 在应用仓库之外」的描述**都已不再成立**。
> 工作区现在是单一仓库，`app/`、`data/`、`docs/`、`fixtures/`、`scripts/`、`tests/`
> 都在根目录下平级；两个仓库的历史已合并保留。
>
> 内容**故意不改**：这是一份实测记录，改写就等于伪造当时的观测。其中的
> §2 角色映射、§3 可复用组件与接口签名、§5 六条冲突裁定至今有效，仍然是最重要的
> 参考；只有路径和仓库结构需要按上面的说明重新理解。
> 当前布局见根 `README.md`，当前计划见 `A_详细开发计划_v2.0.md` §2.3。
>
> 下文 §7.2 与 §5 X2 提到的 `docs/A_详细开发计划_v1.0.md` **已被删除**，
> 因此那两处引用现在是悬空的。它们的结论（`authorize()` 归 C）已由 v2.0 采纳，
> 见 `A_详细开发计划_v2.0.md` 的「修订摘要」。

**检查人**：Coding Agent
**修订原因**：v1 报告结论「工作区无代码」**是错的**。首次检查使用 `app/` 顶层查找，而 D 的模块位于 `headphone-database-D-HKD\headphone-database\app\`，未命中。
**本阶段约束**：只检查、只输出。未修改任何既有文件。

---

## 0. v1 报告的错误与更正

| v1 结论 | 事实 | 严重性 |
|---|---|---|
| 「工作区无任何业务代码，是零代码任务」 | **错**。D 的 catalog/db 模块已完整存在，31 个测试全绿 | 高——会导致我另起一套不兼容实现 |
| 「不存在可保留/可冲突的代码」 | **错**。D 的 `Product`、`HardConstraints`、`Repository` 签名均已固定 | 高 |
| 「git 未初始化」 | **仍然正确** | — |
| 「pytest 未安装」 | **仍然正确**（本次已安装） | — |

**教训**：检查必须覆盖子目录与压缩包，不能只按提示词假定的路径查找。本报告已改为逐目录实测。

---

## 1. Current Architecture（实测）

### 1.1 工作区真实结构

```
D:\HackU\
├─ HacKU 2026 Official Participant Handbook.md          输入资料
├─ HacKU 2026 — Problem Statements.md                    输入资料
├─ headphone-database-D-HKD.zip                    42 KB   D 的交付压缩包
│
├─ headphone-database-D-HKD\headphone-database\   ★ 成员 D 的完整模块
│   ├─ AGENTS.md                                          D 的协作约束（用户已确认）
│   ├─ README.md                                   11 KB
│   ├─ verification.md
│   ├─ .gitignore / .env.example
│   ├─ app\
│   │   ├─ __init__.py
│   │   ├─ catalog\          D   models.py / repository.py / routes.py / seed.py
│   │   └─ db\               D   core.py / initialize.py / schema.py
│   ├─ data\products.seed.json                     36 KB   40 条商品种子
│   ├─ docs\
│   │   ├─ contract.original-v1.openapi.json       56 KB   原四人计划 v1 契约
│   │   ├─ product.schema.json                             D 修订后的 HKD 字段参考
│   │   ├─ product.responses.examples.json
│   │   ├─ team-compatibility.md                           ★ 接 A 的说明
│   │   └─ schema.sql
│   ├─ scripts\init_demo.py / inspect_demo.py
│   ├─ tests\test_catalog.py                       19 KB   31 tests + 32 subtests
│   └─ var\demo.sqlite3                            53 KB   ★ 已建好的运行库
│
├─ docs\                     A 的契约与计划
├─ fixtures\                 A 的测试夹具（20 条 CNY 商品）
└─ scripts\                  A 的生成器与校验器
```

### 1.2 实测运行环境

| 项 | 值 |
|---|---|
| Python | 3.11.9 |
| pytest | 9.1.1（**本次安装**） |
| pydantic | 2.13.4 |
| fastapi | 0.142.2 |
| D 的测试 | **31 passed, 32 subtests passed**（0.98s） |
| git | **未初始化** |

### 1.3 真实数据库状态（直连 `var/demo.sqlite3` 实测）

| 项 | 值 |
|---|---|
| 表 | `products`、`schema_version`（**尚无交易表**） |
| schema | `catalog_hkd` v1 |
| 商品数 | **40** |
| 币种 | **HKD**（唯一值） |
| 价格区间 | `8900` – `59900`（HK$89 – HK$599） |
| 连接 | `wired` / `wireless` |
| 佩戴形式 | `in_ear` / `over_ear` / `open_ear` |
| `anc` | `0` / `1` / `NULL`（三态，NULL 表示未知） |
| `battery_hours` | 所有 `wired` 商品为 `NULL`，符合 D 的 CHECK 约束 |

**关键边界商品（实测）**

| 商品 | 价格 | 用途 |
|---|---|---|
| `hp_0007` | **30000** | 正好 HK$300 的预算边界商品 |
| `hp_0005` | 19900 | **`stock=0`**，缺货用例 |
| `hp_0033` / `hp_0011` / `hp_0022` | — | 同样 `stock=0` |
| `hp_0016` / `hp_0024` / `hp_0006` / `hp_0017` / `hp_0012` / `hp_0028` / `hp_0036` | — | `anc IS NULL`，未知≠满足 |
| `hp_0001` / `hp_0008` | **29900** | `wireless` + `anc=1` + 有货，价格最优的合规候选 |
| `hp_0003` | 49900 | `wireless` + `anc=1`，超预算用例 |

---

## 2. A / B / C / D Mapping

| 角色 | 目录 | 实存状态 |
|---|---|---|
| **D** | `app/catalog/`、`app/db/`、`data/`、`scripts/init_demo.py` | ✅ **已完成**，31 测试全绿 |
| **A** | `app/agent/`、`app/contracts/`、`app/main.py` | ❌ 未实现；设计文档已就绪 |
| **B** | `app/search/` | ❌ 未实现；契约已冻结（`docs/A2B_...规范_v1.1.md`） |
| **C** | `app/commerce/` | ❌ 未实现；v2.0 规范已给 |

**结论**：这是 **D 已完成、A/C 待建** 的工程，不是零代码任务，也不是「改造现有 A/C」。

---

## 3. Reusable Components（D 提供，可直接复用）

| 组件 | 接口（实测签名） | A/C 如何使用 |
|---|---|---|
| `app.db.core.connect(path, *, readonly, timeout)` | caller 拥有事务 | C 复用同一连接 |
| `app.db.core.resolve_db_path(path)` | 读 `DEMO_DB_PATH` | C 复用 |
| `app.db.core.validate_schema(conn)` | 校验 schema 版本与列 | C 建表后自行扩展校验 |
| `app.db.core.database_status(path)` | 返回 `db_ready`/`product_count` | A 的 `/health` 直接用 |
| `app.db.initialize.initialize_database(path, seed_path, *, reset, commerce_schema, wallet_initializer)` | **`commerce_schema(conn)` 与 `wallet_initializer(conn)` 是 C 的挂载点** | ★ C 的建表入口 |
| `app.catalog.repository.ProductRepository(db_path, *, product_model=None)` | `product_model` 可注入 A 的 Pydantic Product | ★ A 注入公共模型 |
| `ProductRepository.get_product(product_id, connection=None)` | `Product \| None` | C 询价用 |
| `ProductRepository.list_candidates(constraints, connection=None)` | `list[Product]`，**全量、不截断、不排序、不放宽** | B 用；C 复核价格用 |
| `ProductRepository.decrease_stock(product_id, quantity, connection)` | 需 caller 事务，条件更新，返回 `rowcount==1` | ★ C 扣库存直接用 |
| `app.catalog.routes.create_router(repository, *, wrap_success, product_not_found)` | A 注入响应包装与错误映射 | ★ A 挂载路由 |
| `data/products.seed.json` | 40 条 HKD 商品 | 可复现初始化 |

**D 已明确留给 A/C 的挂载点（`initialize_database` 的两个回调）是本次整合的官方接缝，必须使用，不要另建初始化流程。**

---

## 4. Required New Components

### 4.1 A 侧（新建）

`app/contracts/`（`common/product/search/agent/mandate/commerce/policy`）、`app/agent/`（`orchestrator/intent_parser/fallback_parser/mandate_builder/mandate_merge/clarification/response_renderer/session/clients`）、`app/errors.py`、`app/main.py`、`app/api/`

### 4.2 C 侧（新建，按 v2.0 第 四 节归 C）

`app/commerce/`：`mandate_registry`、`canonical`、`quote_service`、`payment_router`、`policy_evaluator`、`authority`、`reservation`、`capability`、`state_machine`、`payment_service`、`ledger`、`audit`、`repository`、`adapters/{base,mock_fps,mock_mastercard}`

### 4.3 B 侧（新建）

`app/search/`：过滤 + 排序 + 对比 + 缺口 + 放宽提示

### 4.4 基础设施

`.gitignore`、`.env.example`、`README.md`、`requirements.lock.txt`、`docs/{A_C_contract,mandate_spec,payment_boundary,evidence_sources}.md`

---

## 5. Existing Conflicts（必须在动代码前裁定）

### X1（阻断级）币种与 Product 字段冲突

| | 我此前冻结的 A2B 规范 / fixtures | **D 的实际实现** |
|---|---|---|
| 币种 | `CNY` | **`HKD`** |
| Product 字段数 | 18 | **20**（新增 `seller_description`、`shipping_origin`） |
| 商品语言 | 中文名 | **英文名** |
| 商品数 | 20（虚构） | **40**（真实库） |
| 价格区间 | 3100–45900 分 | 8900–59900 分 |

D 的 `AGENTS.md` 第 11 条已明确：「由 A 协调同步 Product 与所有金额相关公共模型/示例/契约版本；D 不悄悄丢弃新字段，也不把 HKD 标成 CNY。」

**裁定（采纳）**：**以 D 的 HKD 契约为准**，A 侧的 `Product` 与 A2B 规范、fixtures 全部同步到 HKD/20 字段。

### X2（阻断级）我的 Phase-1 计划把 C 的职责写进了 A

`docs/A_详细开发计划_v1.0.md` 把 `authorize()`、`token.py`、`audit.py`、`lint.py` 放在 `app/agent/policy/`。

v2.0 第 四 节要求 mandate / policy / reservation / capability 全部归 **C**。

**裁定（用户已确认选 3）**：**以 v2.0 为准**。`app/agent/policy/` 取消，迁入 `app/commerce/`。该计划需修订。

### X3 商品 ID 空间不一致

我的 fixtures 用 `hp_0001`–`hp_0020`，含义与真实库的 `hp_0001`–`hp_0040` **完全不同**（例如我的 `hp_0007` 是 ¥290 的头戴，真实库的 `hp_0007` 是 HK$300 的头戴）。若两套并存，A/B/C 的测试会互相污染。

**裁定（建议）**：**fixtures 的测试商品集改用独立 ID 前缀**（如 `fx_0001`），或**直接以 D 的真实 40 条商品为 fixtures 基准**。后者更省事且更真实，推荐后者。

### X4 Demo 金额与真实商品不匹配

v2.0 第 十七 节给的成功用例是 `259 + 10 = 269`、拒绝用例 `289 + 36 = 325`。**真实 40 条商品中不存在 HK$259 或 HK$289 的无线 ANC 商品**（最接近的是 `hp_0001`/`hp_0008` = HK$299）。

**裁定（必须）**：金额**必须由 C 从真实 `price_cents` 算出**，不得为了凑 269/325 而硬编码或伪造商品价格（第 一 节 25 条禁止伪造）。演示三个场景改用真实商品：

| 场景 | 真实商品 | 小计 | 运费 | 最终现金支出 | 期望 |
|---|---|---|---|---|---|
| 通过 | `hp_0001` 或 `hp_0008` | 29900 | 1000 | **30900** | APPROVE |
| 超单笔上限 | `hp_0003`（wireless+ANC） | 49900 | 1000 | **50900** | DENY `CAP_PER_TRANSACTION_EXCEEDED` |
| 升格 | 需 `escalate_above < cash_total ≤ cap` | 调参 | — | — | ESCALATE |

**运费在真实数据中不存在**（商品只有 `price_cents`）。需在 C 的 `QuoteService` 中定义一笔**明确标注为 SANDBOX 的固定运费**（建议 `1000` 分 = HK$10），并写入 `docs/evidence_sources.md` 说明其为虚构演示参数。

### X5 原四人计划 v1 契约仍在仓库内

`docs/contract.original-v1.openapi.json`（56 KB）是 **CNY 版本**，与 HKD 现实冲突。D 已将其命名为 `original-v1` 并说明不代表现状。

**裁定**：保留为历史参考，**A 需产出修订版契约**并明确标注哪个是现行版本，避免队友误用。

### X6 规模 × 时间

v2.0 的 13 Phase × 约 30 class × 8 张新表 × 60+ 测试，与 48 小时窗口冲突。**用户已确认选 2**：先保证第 六十二 节安全边界，DoD 作为持续目标。

---

## 6. Database Changes

现状：仅 `products` + `schema_version`（`catalog_hkd` v1）。需**新增 C 的 8 张表 + 既有假设的 4 张表**：

```
既有假设（不存在）       v2.0 新增（不存在）
──────────────      ──────────────────────────
wallets              mandates
orders               mandate_versions
order_items          quotes
payments             purchase_proposals
                     reservations
                     capability_nonces
                     policy_decisions
                     audit_events
```

**集成方式（关键）**：不得另建初始化流程。使用 D 提供的官方接缝：

```python
from app.db.initialize import initialize_database

def commerce_schema(conn):      # C 提供，在 caller 事务内执行
    for ddl in COMMERCE_DDL:
        conn.execute(ddl)

def wallet_initializer(conn):   # C 提供，只补建缺失用户，不动余额
    ...

initialize_database(commerce_schema=commerce_schema,
                    wallet_initializer=wallet_initializer)
```

D 的实现会校验两个回调**不得结束 caller 事务**（`initialize.py` 第 64–70 行），这正好满足 v2.0 第 十三 节的事务边界要求。

**schema 版本策略**：D 用 `schema_version(component, version)`。C 应使用**独立 component 行**（如 `commerce`），不修改 D 的 `catalog_hkd` 行。

---

## 7. Files To Change

### 7.1 必须新建（A/C）

见 §4。按 Phase 顺序落地。

### 7.2 需要修订的既有文件

| 文件 | 修订 | 是否动 D 的代码 |
|---|---|---|
| `docs/A_详细开发计划_v1.0.md` | `app/agent/policy/` → `app/commerce/`；`authorize()` 归 C | 否（我的文档） |
| `docs/A2B_搜索推荐对比接口规范_v1.1.md` | CNY → HKD；18 → 20 字段 | 否（我的文档） |
| `fixtures/test_catalog.json` + 6 个 fixture | 对齐真实商品数据 | 否（我的文件） |
| `scripts/gen_fixtures.py` | 适配 HKD / 新字段 / 真实商品 | 否（我的脚本） |

### 7.3 **明确不动** D 的文件

`app/catalog/**`、`app/db/**`、`data/**`、`scripts/init_demo.py`、`scripts/inspect_demo.py`、`tests/test_catalog.py`、`docs/contract.original-v1.openapi.json`

---

## 8. Merge Conflict Risks

| 风险 | 文件 | 缓解 |
|---|---|---|
| **极高** | `app/db/initialize.py` | C 只**注入回调**，不修改该文件（D 已预留钩子） |
| **极高** | `app/db/schema.py` | C 的 DDL 放**独立模块** `app/commerce/schema.py`，不回写 D 的文件 |
| **极高** | `app/contracts/*.py` | A 独占；B/C/D 只读 |
| **高** | `app/main.py` | 路由按模块拆 `app/api/routes_*.py`，`main.py` 只 `include_router` |
| **高** | `tests/` | C 的测试放 `tests/commerce/`，不与 `tests/test_catalog.py` 同文件 |
| **中** | `README.md` | 单人维护；D 的 README 在子目录，不冲突 |
| **中** | `data/products.seed.json` | 只读；如需新商品由 D 添加 |

**最重要的两条**：
1. C 通过 `initialize_database(commerce_schema=..., wallet_initializer=...)` 接入 —— **零修改 D 的文件**。
2. C 的 `Product` 使用方式必须是 `ProductRepository(product_model=SharedProduct)` —— **零修改 D 的 repository**。

---

## 9. Phase 1 Implementation Plan（修订）

### 9.1 前置动作（P0）

| # | 动作 | 状态 |
|---|---|---|
| P0-1 | 安装 pytest 等依赖 | ✅ 已完成 |
| P0-2 | **`git init` + 首次 commit**（含 D 的代码作为 baseline） | ⬜ |
| P0-3 | `.gitignore`（排除 `var/*.sqlite3`、`__pycache__`、`.env`、`*.bak`） | ⬜ |
| P0-4 | `.env.example` | ⬜ |
| P0-5 | `requirements.lock.txt` | ⬜ |

**建议落地目录**：`D:\HackU\headphone-database-D-HKD\headphone-database\`（D 的项目目录），因为：
- 它已含 `app/`、`tests/`、`.gitignore`、`.env.example`
- 跑 pytest 需要该目录为 project root（`app` 包相对导入）
- 未来 GitHub 仓库就是这一个 repo

### 9.2 Phase 2 范围（契约层）

```
app/errors.py                  结构化错误（v2.0 第 五四 节）
app/contracts/common.py        Ok/Error 信封、request_id、枚举
app/contracts/product.py       ★ HKD + 20 字段，必须与 D 的 schema 逐字段一致
app/contracts/search.py        按 A2B 规范（改 HKD）
app/contracts/agent.py         ChatRequest/AgentResponse/IntentResult/UserActionType/AgentPhase
app/contracts/mandate.py       MandateDraft + Mandate
app/contracts/commerce.py      Quote/PaymentRouteEvaluation/PurchaseProposal/Reservation/SpendState
app/contracts/policy.py        PolicyDecision/PolicyViolation/DenialReceipt
```

**Phase 2 验收**：`ProductRepository(product_model=Product)` 能成功返回 Pydantic Product（这是与 D 的**第一个真实联调点**）。

### 9.3 Phase 2b（建议优先于 Phase 3–5）

`app/commerce/policy_evaluator.py` —— **纯函数，零外部依赖**，22 项检查 + 全量单测。
理由：不依赖 DB/B/LLM；是安全边界核心；可立即全绿；评委最会追问。

### 9.4 后续 Phase

按 v2.0 第 五十八 节：3 数据库 → 4 MandateRegistry → 5 Quote → 6 Policy → 7 Reservation → 8 Capability+Payment → 9 A Mandate → 10 A Orchestrator → 11 Renderer → 12 Integration → 13 Docs/Git。

---

## 10. 需用户裁定的事项

| # | 事项 | 我的建议 |
|---|---|---|
| **Q1** | 落地目录：在 D 的项目目录内扩展，还是另建？ | **在 D 的目录内**（见 §9.1 理由） |
| **Q2** | fixtures 处理：改造我的 20 条虚构商品，还是以 D 的真实 40 条为基准？ | **以 D 的真实 40 条为基准**（更真实、免维护两套） |
| **Q3** | Demo 金额：v2.0 的 269/325 在真实库中无法构造，是否改用真实商品算出的金额？ | **改用真实金额**（禁止伪造价格） |
| **Q4** | 运费：真实商品无运费字段，是否接受 SANDBOX 固定运费 HK$10？ | **接受**，并写入 `evidence_sources.md` 标注为虚构演示参数 |

---

## 附：本次检查新增文件

| 文件 | 动作 |
|---|---|
| `docs/PHASE1_Repository_Inspection.md` | 覆盖（v1 → v2 修订版） |

**未修改、未删除任何既有文件。D 的代码零改动。**
