# A → B 搜索 / 推荐 / 对比 接口规范 v1.1

**交付方**：成员 A（需求理解与任务编排）
**接收方**：成员 B（商品搜索与推荐）
**契约版本**：v1.1
**状态**：B 可据此开始实现，无需等待 A 的 LLM 接入

**代码对应**：`app/contracts/search.py`（本规范与它必须保持一致）

---

## 修订记录

| 版本 | 变更 |
|---|---|
| v1.0 | 初版：`PreferenceProfile` / `ComparisonFrame` / `comparison` / `gaps` / `relax_hints` |
| **v1.1** | **币种 CNY → HKD**；`Product` 字段 18 → **20**（新增 `seller_description`、`shipping_origin`）；价格与文案改英文；fixtures 改由 D 的真实 40 条种子生成；`reasons` 明确为**结构化对象**而非字符串；新增 §3.5 币种与字段、§5.5 汇总接口 |

**与 v1.0 的实质差别只有币种与字段**。过滤、排序、对比、缺口、放宽的规则均未改变。

---

## 0. 本规范解决什么

A 负责把用户自然语言转成结构化判据，B 负责在这些判据下完成**过滤、排序、对比、缺口报告**。

本规范只定义 A 与 B 之间的**数据结构与行为**。它不定义：

- 商品数据从哪来（由 D 的 Repository 提供 `Product`）
- 用户是谁、花多少钱、如何授权（由 C 的 commerce 层负责；A 只提议）
- 最终对用户说什么话（由 A 的 render 层负责）

**B 不生成面向用户的自然语言文案。** B 只返回字段与依据；把字段变成人话是 A 的职责。

**核心分工一句话**：

> **A 提供判断（用户在意的方向、优先级、该比哪几维），B 提供事实（过滤、计数、对比、缺口）。**

### 0.1 三个字段名必须分清

这是最容易出错的地方：**约束字段名**与**商品字段名**不是同一个命名空间。

| 约束字段（A 设定） | 商品字段（B 对比/排序用） |
|---|---|
| `max_price_cents` | `price_cents` |
| `min_price_cents` | `price_cents` |
| `max_wearing_weight_g` | `wearing_weight_g` |
| `min_battery_hours` | `battery_hours` |
| `anc_required` | `anc` |
| `connection` | `connection` |
| `form_factor` | `form_factor` |

**代码中的映射**：`app/contracts/search.py` 的 `CONSTRAINT_TO_ATTRIBUTE`。`comparison.dimensions` 与 `criteria[].attribute` **一律使用右列的商品字段名**。

---

## 1. 边界（先读这一节，避免越界）

### 1.1 B 必须做

1. 校验 `SearchRequest` 的合法性与内部一致性。
2. 用硬约束对 D 的 `list_candidates` 结果做严格过滤，**返回全量合格集**（截取前）。
3. 用 `preferences` 对合格集做**确定性排序**。
4. 按 `comparison` 计算候选之间的**对比矩阵**，标出哪些维度真正有差异。
5. 报告**缺口**：无法评估的偏好、被丢弃的偏好、缺失数据、超预算替代项。
6. 在严格约束下无结果时，计算**逐维度放宽提示**与**最接近候选**。

### 1.2 B 绝对不能做

| 禁止 | 原因 |
|---|---|
| 放宽或忽略 `constraints` 中的硬条件 | 硬条件是用户的明确要求，只有 A 在获得用户同意后才能改 |
| 用 LLM 重新解释、补全或改写条件 | 判据只能来自 A，B 不得二次理解用户意图 |
| 生成面向用户的文案、理由句、推荐语 | B 返回**结构化理由**，A 负责措辞。见 §4.3 |
| 把 `null` 当作 `0`、`false` 或"最差" | 未知 ≠ 不满足，未知 ≠ 最差。见 §5.4 |
| 补造商品参数 | 缺就是 `null`，由 D 的数据决定 |
| 自己建立商品数据副本 | 商品数据统一经 D 的 Repository |
| 返回超过 `limit` 的候选 | 全量数据放在 `total_matches` 与 `comparison` 里 |
| 输出任何形式的"综合损耗分 / 匹配百分比" | 权重不可解释、无法复算，见 §7 |

### 1.3 B 可以否决 A

B 对 `comparison.dimensions` 有**否决权**：若某维度不在 `Product` 字段内、或不适合作对比，B 应丢弃该维度并在 `gaps.dropped_dimensions` 中报告。A 必须尊重这个否决。

---

## 2. 数据流

```
A: SearchRequest  ──────────────►  B: search_products()
                                        │
                                        ├─ 校验
                                        ├─ D.list_candidates(constraints)   全量硬过滤
                                        ├─ 排序
                                        ├─ 对比矩阵
                                        ├─ 缺口报告
                                        └─ 无结果时 → 放宽提示
                                        │
A: SearchResponse ◄──────────────  B 返回
   │
   ├─ render：把 comparison / gaps 转成人话
   ├─ session：保存 candidates 顺序，供"第二个"映射
   └─ 无结果时：把 relax_hints 组合成最多三层提议，交给用户确认
```

---

## 3. 请求：SearchRequest

### 3.1 顶层结构

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `constraints` | `HardConstraints` | 是 | 硬约束，违反即淘汰 |
| `preferences` | `PreferenceProfile` | 是 | 偏好画像，只影响排序与对比，不淘汰 |
| `comparison` | `ComparisonFrame` | 否（缺省空） | A 建议的对比维度 |
| `limit` | integer 1–20，缺省 3 | 否 | 返回候选条数，**只影响 candidates 数组长度** |

```json
{
  "constraints": { "...见 3.2..." },
  "preferences": { "...见 3.3..." },
  "comparison": { "dimensions": ["price_cents", "battery_hours", "wearing_weight_g"], "max_columns": 3 },
  "limit": 3
}
```

### 3.2 HardConstraints

| 字段 | 类型 | 语义 |
|---|---|---|
| `category` | `"headphones"` | 第一版固定 |
| `min_price_cents` | integer ≥ 0 \| `null` | 价格下界，**含边界** |
| `max_price_cents` | integer ≥ 0 \| `null` | 价格上界，**含边界** |
| `brand_allowlist` | string[] | 空数组 = 不限制品牌；非空 = 只允许列表内品牌 |
| `connection` | `"wired"` \| `"wireless"` \| `null` | `null` = 不限制 |
| `form_factor` | `"in_ear"` \| `"over_ear"` \| `"open_ear"` \| `null` | `null` = 不限制 |
| `anc_required` | boolean | `true` = 必须有 ANC。**`false` = 不要求，不代表禁止有 ANC** |
| `min_battery_hours` | number ≥ 0 \| `null` | 最低续航 |
| `max_wearing_weight_g` | number > 0 \| `null` | 最高佩戴重量 |
| `in_stock_only` | boolean，缺省 `true` | `true` 时排除 `stock == 0` |

**空值语义（必须严格遵守）**

| 情形 | B 的行为 |
|---|---|
| 字段为 `null` | 该维度不参与过滤 |
| 商品该字段为 `null` | **不得通过**对应的非空硬条件（未知 ≠ 满足） |
| `anc_required=false` | **不排除**有 ANC 的商品 |
| `brand_allowlist=[]` | 不限制品牌 |
| `min_price_cents > max_price_cents` | 返回 `INVALID_CONSTRAINTS` |

### 3.3 PreferenceProfile

```json
{
  "use_cases": ["commute"],
  "criteria": [
    {
      "attribute": "battery_hours",
      "direction": "higher",
      "target": null,
      "priority": "high",
      "source": "explicit",
      "evidence_quote": "续航要长一点"
    }
  ],
  "priority_preset": "best_match",
  "prefer_anc": false
}
```

#### use_cases

枚举数组，取值：`commute` / `study` / `gaming` / `sports` / `calls` / `music`。
**只影响排序与匹配分，不作为硬条件。** 第一版不含"音质""舒适度"等无数据依据的用途。

#### criteria — 偏好判据（本次新增，B 排序的核心输入）

| 字段 | 类型 | 说明 |
|---|---|---|
| `attribute` | string | **必须是 `Product` 的字段名**：`price_cents` / `battery_hours` / `wearing_weight_g` / `anc` / `form_factor` / `connection` |
| `direction` | `"higher"` \| `"lower"` \| `"closer_to"` | 越大越好 / 越小越好 / 越接近 `target` 越好 |
| `target` | number \| `null` | **仅 `closer_to` 时非空** |
| `priority` | `"high"` \| `"medium"` \| `"low"` | 相对重要性 |
| `source` | `"explicit"` \| `"inferred"` | `explicit` = 用户原话；`inferred` = A 推导 |
| `evidence_quote` | string | 用户原话片段，**用于回答"为什么排这个第一"** |

**A 对 B 的保证（B 可以依赖）**

1. `criteria[].attribute` 一定落在上表六个字段内。B 若收到其他字段名，应丢弃并在 `gaps.dropped_criteria` 报告。
2. `criteria` 数组**有序**：越靠前越重要。B 可以用它决定对比维度的展示顺序。
3. `source="inferred"` 的项**不超过一半**，且每项 `evidence_quote` 非空。
4. **A 保证 `criteria` 与 `constraints` 不冲突**：若某维度已在 `constraints` 里被硬性限定（如 `anc_required=true`），A 不会再把它作为 `criteria`（避免"既必须又偏好"的语义重复）。

#### prefer_anc

boolean，**可选，缺省视为 `false`**。

含义：用户**表达了降噪意愿但没有把它设为硬条件**（例如"最好有降噪"），此时 A 传 `prefer_anc: true`，B 在 `match_score` 上加 5 分。

**注意两点**：

1. 若用户已明确要求降噪，A 会把它放进 `constraints.anc_required = true`，**此时不会再传 `prefer_anc`**（硬条件已经足够，重复表达没有意义）。
2. 该字段只影响 `match_score`，**不影响任何过滤**。它不能让 `anc == false` 或 `anc == null` 的商品通过过滤。

#### priority_preset

`best_match` / `lower_price` / `longer_battery` / `lighter_weight`。
这是**用户显式选择的排序倾向**，优先级低于 `criteria`（见 §5.3）。

### 3.4 ComparisonFrame

| 字段 | 类型 | 说明 |
|---|---|---|
| `dimensions` | string[] | A 建议的对比维度。**A 保证：每一项 ⊆ `constraints` 的非空键 ∪ `criteria[].attribute`** |
| `max_columns` | integer 2–5，缺省 3 | 对比表最多展示几个候选 |

A 不保证 `dimensions` 每个都有差异——**判断有没有差异是 B 的职责**（见 §6）。

### 3.5 币种与 Product 字段（v1.1 新增）

#### 币种

| 项 | 值 |
|---|---|
| 结算币种 | **HKD**，固定 |
| 金额单位 | 整数，**分的百分之一**。`29900` = HK$299.00 |
| 禁止 | 浮点金额。`299.0` 会被拒绝，不是被转换 |

**多币种尚未实现。** `app/contracts/common.py` 预留了接缝（`SETTLEMENT_CURRENCY`、`SUPPORTED_CURRENCIES`、`Money` 占位），但没有任何东西消费它。已定死的两条决策，接 FX 时必须遵守：

1. **只有一个结算币种。** 所有授权上限都在它上面比较——"HK$300 或 US$40"这种授权无法判定是否超额。
2. **汇率用整数 ppm，绝不用 float。** 金额全线整数分；浮点汇率会让 `quote_hash` 依赖平台，从而**不可核验**。

#### Product（20 个字段）

D 的商品数据是英文、HKD，相对 v1.0 的 18 字段**新增两个必填字段**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `product_id` | string | 永久稳定 SKU 标识 |
| `category` | `"headphones"` | — |
| `name` / `brand` / `model` / `variant` | string | 可展示名称与分类 |
| `connection` | `wired`/`wireless` | — |
| `form_factor` | `in_ear`/`over_ear`/`open_ear` | — |
| `price_cents` | integer ≥ 0 | HKD 分 |
| `currency` | `"HKD"` | **仅 HKD**；CNY 会被拒绝，不是被转换 |
| `stock` | integer ≥ 0 | — |
| `anc` | bool \| `null` | `null` = 未知，**不代表不支持** |
| `battery_hours` | number ≥ 0 \| `null` | 有线耳机固定为 `null` |
| `wearing_weight_g` | number > 0 \| `null` | — |
| `use_cases` | enum[] | `commute`/`study`/`gaming`/`sports`/`calls`/`music` |
| `source_type` | `demo`/`real_manual`/`external` | — |
| `source_url` | URL \| `null` | `source_type=demo` 时必须为 `null` |
| `data_note` | string | 数据来源说明 |
| **`seller_description`** | string，1–99 字符 | **v1.1 新增**。商家式短描述 |
| **`shipping_origin`** | string | **v1.1 新增**。发货地，演示设定 |

**字段约束（由 `Product` 模型与 D 的 STRICT 表同时保证）**

- `connection="wired"` 时 `battery_hours` 必须为 `null`
- `source_type="demo"` 时 `source_url` 必须为 `null`
- `use_cases` 不得重复
- `currency` 只接受 `HKD`

**B 不得裁剪返回的 `Product`。** 候选里的 `product` 必须是 D 提供的完整对象——A 的渲染层需要 `seller_description` 与 `shipping_origin`，缺了它无法展示。

---

## 4. 响应：SearchResponse

### 4.1 顶层结构

| 字段 | 类型 | 说明 |
|---|---|---|
| `total_matches` | integer | **截取前的合格商品总数**（不是返回条数） |
| `returned` | integer | `candidates` 的实际长度 |
| `applied_constraints` | `HardConstraints` | 原样回传，供 A 渲染"我按这些条件找的" |
| `applied_criteria` | string[] | **实际参与了排序**的 `criteria[].attribute` 列表 |
| `candidates` | `Candidate[]` | 长度 = `min(limit, total_matches)` |
| `comparison` | `ComparisonRow[]` | 对比矩阵，见 §6 |
| `gaps` | `GapReport` | 缺口报告，见 §4.4 |
| `relax_hints` | `RelaxHints` \| `null` | 仅当 `total_matches == 0` 时非空，见 §7 |
| `suggestions` | string[] | 给 A 的**机械式**放宽建议（如"放宽预算到 33000 可多 2 款"），可空 |

### 4.2 Candidate

| 字段 | 类型 | 说明 |
|---|---|---|
| `product` | `Product` | D 提供的**完整标准商品对象**，不得裁剪字段 |
| `match_score` | number | 偏好匹配分，见 §5.2。**只表示偏好匹配，不表示音质或绝对性能** |
| `reasons` | `MatchReason[]` | **结构化依据**，见 §4.3 |
| `preference_misses` | string[] | 未满足的偏好（**不是硬条件**） |
| `violated_fields` | string[] | 该商品违反的硬约束字段名。**严格过滤下恒为 `[]`**；仅在 `relax_hints.closest_candidates` 中可能非空 |

### 4.3 reasons 必须是结构化对象（**v1.1 重要变更**）

`reasons` **不是字符串数组**，而是 `MatchReason` 对象数组：

| 字段 | 类型 | 说明 |
|---|---|---|
| `field` | string | **必填**。这条理由来自 `Product` 的哪个字段 |
| `observed` | any | **必填**。该字段被观测到的值 |
| `text` | string | 给 A 渲染用的一句话，可本地化 |
| `kind` | `"constraint"` \| `"preference"` \| `"spec"` | 这条理由的性质 |

```json
{
  "field": "battery_hours",
  "observed": 32,
  "text": "标称续航 32 小时",
  "kind": "spec"
}
```

**为什么必须是对象而不是字符串。**

字符串理由无法被机器检查。`"续航 40 小时"` 到底是从 `battery_hours` 读的，还是模型编的？看不出来。加上 `field` 与 `observed` 之后，**"每条理由都能追溯到某个商品字段"从一句约定变成了可断言的事实**——`scripts/check_fixtures.py` 现在就检查这一点。

**允许与禁止**

| 允许（附 `field` / `observed`） | 禁止 |
|---|---|
| `field="anc", observed=true, text="支持主动降噪"` | `text="音质出色"`（无字段可依） |
| `field="battery_hours", observed=32` | `text="性价比很高"`（无定义） |
| `field="wearing_weight_g", observed=218` | `text="通勤首选"`（营销语） |
| `field="price_cents", observed=30000` | `text="用户会喜欢"` |

**判定方式**：任何一条理由都应能回答"这条是从 `Product` 的哪个字段读出来的"。答不出来，就删掉——或者说明它其实不是理由。

### 4.4 GapReport

| 字段 | 类型 | 说明 |
|---|---|---|
| `unsupported_criteria` | string[] | A 提交了但 B 无法评估的判据（如无数据依据的"音质"）。**必须报告，不得静默忽略** |
| `dropped_criteria` | string[] | B 未用于排序的 `criteria[].attribute`。**非空即表示 A/B 语义已漂移，A 的集成测试会失败** |
| `dropped_dimensions` | string[] | B 否决掉的 `comparison.dimensions` 项 |
| `missing_data_attributes` | string[] | 候选集中存在 `null` 的对比维度 |
| `over_budget_alternatives` | `OverBudgetAlternative[]` | 超出 `max_price_cents` 但其他硬条件都合格的候选，**仅供 A 提示，不得进入 `candidates`** |

```json
"over_budget_alternatives": [
  { "product_id": "hp_0034", "price_cents": 33900, "delta_cents": 3900 }
]
```

**同一个未知判据要在三处同时出现**（例：`audio_quality`）：

| 字段 | 作用 |
|---|---|
| `unsupported_criteria` | 告诉 A"用户提了但我评估不了"，A 据此向用户明说 |
| `dropped_criteria` | 告诉 A"这一项没参与排序"。**A 的集成测试在本字段非空时失败** |
| `dropped_dimensions` | B 对 `comparison.dimensions` 行使否决权的结果 |

缺任何一处都算实现不合格。

### 4.5 ComparisonRow

| 字段 | 类型 | 说明 |
|---|---|---|
| `attribute` | string | 维度名（`Product` 字段名） |
| `direction` | `"higher"` \| `"lower"` \| `"closer_to"` | 该维度的"更好"方向。来自 `criteria`；若维度只来自 `constraints`，用该字段的自然方向 |
| `values` | `ComparisonCell[]` | **与 `candidates` 同序同长** |
| `spread` | number \| `null` | 极差。**有任一 `known=false` 时为 `null`** |
| `is_distinguishing` | boolean | 候选之间该维度是否真有差异，见 §6.2 |
| `is_criterion` | boolean | 是否来自用户的 `criteria`（而非仅来自 `constraints`） |

```json
{ "attribute": "battery_hours", "direction": "higher",
  "values": [ {"value": 40, "known": true}, {"value": 35, "known": true} ],
  "spread": 5, "is_distinguishing": true, "is_criterion": true }
```

`ComparisonCell`：`value`（原值，未知为 `null`）、`known`（boolean）。

---

## 5. 过滤与排序规则

### 5.1 过滤（严格模式）

1. 校验 `SearchRequest` 合法性：枚举合法、数值范围合法、`min_price_cents ≤ max_price_cents`。失败返回 `INVALID_CONSTRAINTS`。
2. 调用 `D.list_candidates(constraints, connection=None)` 获取**全部**硬条件合格商品。
3. **不得在排序前截取。** `total_matches` 必须是截取前的数量。
4. `in_stock_only=true` 时排除 `stock == 0`。
5. **未知参数不得通过对应的非空硬条件。** 例：`anc_required=true` 且商品 `anc == null` → 淘汰。

### 5.2 匹配分（确定性，非 LLM）

```
match_score = 10 × |{ u ∈ use_cases : u ∈ product.use_cases }|
            + (prefer_anc 且 product.anc == true ? 5 : 0)
```

- `prefer_anc` 缺省为 `false`。当用户明确要求降噪时，A 使用 `constraints.anc_required=true`，不再传 `prefer_anc`（见 §3.3）。
- `match_score` **只表示偏好匹配**，不得被描述为"音质分""综合得分"。
- **`match_score` 常常对全体候选相同，这不是错误。** 它只在候选对用途命中的程度不同、或 `prefer_anc` 区分了 `anc` 时才有区分度。**真正的排序主力是 `criteria`。** 见 `fixtures/search.001.normal.json`（全部候选 `match_score` 均为 10）。

### 5.3 排序（严格按此顺序，逐级打破平局）

```
第 1 级  match_score 降序
第 2 级  按 priority_preset 附加：
           best_match      → 无附加（保持 match_score 顺序）
           lower_price     → price_cents 升序
           longer_battery  → battery_hours 降序
           lighter_weight  → wearing_weight_g 升序
第 3 级  criteria 有序生效：按 criteria 数组顺序，逐项用 direction 排序
           （仅当该维度在候选中已知；未知排在该维度已知商品之后）
第 4 级  product_id 升序
```

**未知值处理（第 3 级）**：某维度为 `null` 的候选，**排在该维度已知候选之后**，且不得被当作最小值或最大值参与排序。

**稳定性要求**：相同输入必须产生**逐位相同**的输出顺序。禁止使用非确定性排序（如依赖字典遍历顺序、时间戳、随机数）。

### 5.4 截取

排序完成后按 `limit` 截取，写入 `candidates`，`returned = len(candidates)`。**`comparison` 与 `suggestions` 基于截取后的 `candidates` 计算；`total_matches` 与 `relax_hints` 基于全量。**

---

## 6. 对比矩阵规则

### 6.1 计算范围

`comparison` 的行 = `comparison.dimensions` 中 B 接受的维度；列 = `candidates`（截取后）。
**若 `candidates` 少于 2 个，`comparison` 返回空数组**（没有可对比的对象）。

### 6.2 is_distinguishing 判定

```
已知值集合 K = { cell.value : cell.known }
若 K 为空                      → is_distinguishing = false（全部未知，无法区分）
若 |K| == 1                    → is_distinguishing = false（所有已知值相同）
若 |K| >= 2                    → is_distinguishing = true
```

- `spread`：仅当**所有** cell 都 `known` 时计算（`max − min`），否则为 `null`。
- **禁止**把 `null` 折算成 `0` 参与 `spread` 或比较。若存在未知值，`spread = null`，由 A 在文案中说明"部分商品缺少该数据"。

### 6.3 展示优先级（B 只负责计算与标记，排序由 A 决定，但 B 需按此顺序输出数组）

```
1. is_criterion=true  且 is_distinguishing=true    ← 用户在意 + 真有差异
2. is_criterion=false 且 is_distinguishing=true    ← 用户没提，但这是决策点
3. is_criterion=true  且 is_distinguishing=false   ← 用户在意，但都满足（可折叠）
```

同组内按 `comparison.dimensions` 的原始顺序。

**第 2 组是 B 提供价值最高的部分**：用户没提、但候选之间真的有差异的维度，正是他真正要做决定的地方。B 必须把它算出来并交给 A，**由 A 决定是否向用户点出**。

---

## 7. 无结果时的放宽提示（RelaxHints）

`total_matches == 0` 时，`relax_hints` 必须非空，且遵守以下规则。

### 7.1 结构

```json
{
  "hints": [
    { "field": "max_price_cents", "from_value": 30000, "to_value": 33000,
      "gained_count": 2, "example_product_id": "hp_0007" },
    { "field": "max_wearing_weight_g", "from_value": 200, "to_value": 260,
      "gained_count": 5, "example_product_id": "hp_0012" }
  ],
  "closest_candidates": [
    { "product": { "...": "..." }, "match_score": 10,
      "reasons": ["支持主动降噪"],
      "preference_misses": [],
      "violated_fields": ["max_price_cents"] }
  ]
}
```

| 字段 | 说明 |
|---|---|
| `field` | 被放宽的硬约束字段名 |
| `from_value` | 用户原值 |
| `to_value` | **最小必要放宽值**——恰好使 `gained_count ≥ 1` 的那个值 |
| `gained_count` | 单独放宽这一维后，新增的合格商品数量 |
| `example_product_id` | 一个具体受益商品的 ID，让 A 能说出"例如 hp_0007" |
| `closest_candidates` | 违反硬约束**最少**、且数值差距**最小**的前 N 条（N ≤ limit），每条**必须**带非空的 `violated_fields` |
### 7.2 计算方式（机械操作，无需权重）

```
对 constraints 中每一个非 null 字段 F：
   「单独放宽」= 只改 F，其他字段全部保持不变
   从小到大（价格/重量）或从大到小（续航）遍历该维度在商品集中出现过的取值 v，
     取第一个使「新增合格商品数 ≥ 1」的 v 作为 to_value
   gained_count = 在该 v 下的新增商品数
   若遍历完仍无法新增任何商品 → 该维度不输出 hint

hints 排序：1) gained_count 降序
            2) field 名升序（保证确定性）

closest_candidates：
   对每个商品计算 violated_fields（违反的硬约束字段名集合）与数值差距之和
   排序： 1) |violated_fields| 升序
          2) 数值型维度的实际差距之和升序（用原始单位，如 battery_hours 差 8、price 超支 2900）
          3) match_score 降序
          4) price_cents 升序
          5) product_id 升序
   取前 N 条（N = limit）
```

**`to_value` 的三条定义要点（容易做错）**

1. **必须是「单独放宽」的结果**，其他约束一律冻结。不得为了凑出一个正数而同时放松两个维度。
2. **必须是「最小必要值」**，即从当前值向放宽方向扫到的**第一个**能新增商品的取值。例：`max_wearing_weight_g` 从 190 放宽，若商品集中第一个超过 190 克的合格商品是 195 克，则 `to_value = 195`，不是 200 或 250。
3. **`gained_count == 0` 的维度不出现在 `hints` 中。** 宁可只给一条有用的建议，也不要给一条"放宽了也没用"的建议。

### 7.3 三条铁律

1. **B 只报告，不放宽。** B 返回严格模式下的空结果 + hints，**绝不**把放宽后的商品放进 `candidates`。
2. **`violated_fields` 必须非空且准确。** 这是 A 向用户坦白"这一款没有主动降噪"的唯一依据。漏报即为 consent 风险。
3. **不使用任何标量评分。** 不得输出 `loss_ratio`、`similarity`、`match_percent` 等无法复算的数值。

### 7.4 谁决定"哪些维度可以放宽"

这是**职责分离**，不要混淆：

| 角色 | 职责 |
|---|---|
| **B** | 机械计算**每个**可放宽维度单独放宽能得到什么（`hints`）。**不做价值判断**，不猜用户愿不愿意让步 |
| **A** | 依据用户原话（`source="explicit"` 且用户用过"最好/尽量/希望"，或 `source="inferred"`）**筛选**出最多三层提议，交给用户显式确认 |

**因此 `hints` 必须包含全部可放宽维度，哪怕某些维度在语义上不该由 agent 主动提出。** 例：`anc_required` 是用户说"必须支持主动降噪"得到的，B 仍应报告"取消强制 ANC 可多 3 款"这个事实，但**由 A 决定是否把它摆到用户面前**——A 在默认情况下不会主动提议放宽用户明确说过的硬要求。

两个理由：

1. B 看不到用户原话，无法做这个判断。`constraints` 里没有"这一条是硬要求还是随口一说"的信息。
2. 即使 A 不该提议，B 把这个事实报告出来也无害，而且对 A 的日志与审计有价值。

**可放宽维度白名单**（B 只对这几个字段产出 hint）：

```
max_price_cents, min_price_cents, max_wearing_weight_g, min_battery_hours, anc_required
```

`category`、`connection`、`form_factor`、`brand_allowlist`、`in_stock_only` **不产出 hint**（放宽它们等于换一个商品品类，不是"让步"，而是"改需求"）。

### 7.4 A 侧如何使用（供 B 理解上下文，B 不需实现）

A 会把 `hints` 组合成**最多三层放宽提议**，交给用户显式确认后才重新发起检索。A 只会放宽 `source="inferred"` 或用户用过"最好/尽量/希望"的维度；`anc_required` 这类明确硬条件**不会被 A 自动放宽**。

---

## 8. 探测接口（支撑"先探后问"）

A 在用户条件**部分指定**时，需要在不取全量的情况下问对问题。

```
POST /api/v1/products/summarize        （或 search 请求附 "summary_only": true）
```

请求体：与 `SearchRequest` 相同（`limit` 被忽略）。

响应：

```json
{
  "total_matches": 40,
  "dimension_stats": {
    "price_cents":       { "min": 8900, "max": 89900, "known_count": 40, "unknown_count": 0 },
    "battery_hours":     { "min": 20,   "max": 60,    "known_count": 31, "unknown_count": 9 },
    "wearing_weight_g":  { "min": 180,  "max": 320,   "known_count": 31, "unknown_count": 9 }
  },
  "boolean_stats": {
    "anc":           { "true_count": 12, "false_count": 28, "unknown_count": 0 },
    "connection":    { "wired": 16, "wireless": 24 }
  }
}
```

| 字段 | 说明 |
|---|---|
| `dimension_stats` | 仅数值型维度。`min`/`max` **只统计已知值**；全部未知时两者为 `null` |
| `boolean_stats` | 布尔型与枚举型维度的取值分布 |

**要求**：`summarize` 与 `search` 必须使用**同一套过滤逻辑**。相同 `constraints` 下，`summarize.total_matches` 必须等于 `search.total_matches`。

---

## 9. 错误与空结果

### 9.1 区分"错误"与"空结果"

| 情形 | 返回 | 说明 |
|---|---|---|
| 请求字段非法（枚举越界、数值范围错） | `INVALID_CONSTRAINTS` / 422 | 是错误 |
| `min_price_cents > max_price_cents` | `INVALID_CONSTRAINTS` | 是错误 |
| 硬条件合法但无商品 | HTTP 200，`total_matches=0`，`candidates=[]` | **是正常结果，不是错误** |

### 9.2 空结果要求

`total_matches == 0` 时必须：

1. `candidates == []`、`comparison == []`
2. `relax_hints` 非空（除非确实没有任何可放宽维度）
3. `gaps` 中说明原因（如 `unsupported_criteria` 非空）
4. **不得编造商品，不得自动放宽条件**

---

## 10. 验收清单（B 的自测必须全绿）

| # | 场景 | 期望 |
|---|---|---|
| 1 | 恰好 HK$300 的商品（`hp_0007` = 30000 分），`max_price_cents=30000` | 被包含（边界含） |
| 2 | 高于上界的商品，`max_price_cents=30000` | 被排除 |
| 3 | `anc_required=true`，商品 `anc=null` | 被排除 |
| 4 | `anc_required=true`，商品 `anc=false` | 被排除 |
| 5 | `anc_required=false`，商品 `anc=true` | **不被排除** |
| 6 | `connection=null` | 有线与无线都返回 |
| 7 | `brand_allowlist=[]` | 不限制品牌 |
| 8 | `min_price_cents > max_price_cents` | `INVALID_CONSTRAINTS` |
| 9 | `in_stock_only=true`，`stock=0` | 被排除 |
| 10 | 所有合格商品都缺货 | `total_matches=0`，`candidates=[]`，不编造 |
| 11 | 相同输入连续两次 | 候选顺序**逐位相同** |
| 12 | 30 款合格、`limit=3` | `total_matches=30`，`returned=3` |
| 13 | 候选在 `battery_hours` 上 40/35/30 | `is_distinguishing=true`，`spread=10` |
| 14 | 候选 `battery_hours` 全部为 40 | `is_distinguishing=false`，`spread=0` |
| 15 | 候选之一 `battery_hours=null` | 该 cell `known=false`，该行 `spread=null` |
| 16 | 某维度全部 `null` | `is_distinguishing=false`，`spread=null` |
| 17 | `criteria` 含 `attribute="audio_quality"` | 进 `gaps.dropped_criteria`，不参与排序 |
| 18 | `comparison.dimensions` 含非法字段 | 进 `gaps.dropped_dimensions`，不进 `comparison` |
| 19 | `criteria` 有一项未影响排序 | 进 `gaps.dropped_criteria` |
| 20 | 存在超预算但其他条件合格的商品 | 进 `over_budget_alternatives`，**不进入 `candidates`** |
| 21 | 严格条件下 `total_matches=0` | `relax_hints.hints` 非空，`to_value` 为最小必要值 |
| 22 | `closest_candidates` 每一条 | `violated_fields` 非空且准确 |
| 23 | 某维度放宽也无法新增商品 | 该维度不出现在 `hints` 中 |
| 24 | `summarize` 与 `search` 相同 `constraints` | `total_matches` 一致 |
| 25 | `use_cases=["commute","music"]` 且商品两个都命中 | `match_score` 加 20 |
| 26 | 任一 `reason` | 可追溯到 `Product` 的某个字段 |
| 27 | `candidates` 长度 < 2 | `comparison == []` |
| 28 | 请求含非法枚举值 | 422 或 `INVALID_CONSTRAINTS` |

---

## 11. 完整样例（以 fixtures 为准）

**本规范不内嵌完整样例。** 全部可测试的输入输出配对在 `fixtures/` 目录，期望值由 `scripts/gen_fixtures.py` 从**成员 D 的真实商品种子**复算生成：

```
data/products.seed.json   （40 条，HKD）
```

规范里同时放一份手写样例，就会产生**第二份事实来源**。一旦两者不一致，B 无法判断该信哪一份。

| fixture | 覆盖 | 当前期望值 |
|---|---|---|
| `search.001.normal.json` | 正常路径；`total_matches` 截取前计数；五级排序；对比矩阵；超预算替代项 | `total=4 returned=3`，顺序 `hp_0007, hp_0001, hp_0008` |
| `search.002.relax.json` | 无完美解；`relax_hints` 与 `closest_candidates`；`violated_fields` | `total=0`，hint 重量 `5 → 9 (+2)` |
| `search.003.anc_unknown.json` | `anc=null` 不得满足 `anc_required=true`；`lower_price` 排序 | `total=6 returned=5` |
| `search.004.invalid.json` | `INVALID_CONSTRAINTS` 错误形状 | `expected_error_code=INVALID_CONSTRAINTS` |
| `search.005.gaps.json` | `unsupported_criteria` / `dropped_criteria` / `dropped_dimensions` | `total=4`，`unsupported=[audio_quality]` |
| `summarize.001.partial.json` | 先探后问的分布统计 | `total=36`，anc `6/24/6` |

用法、约定与排查顺序见 `fixtures/README.md`。

**本规范只定义字段与规则，期望值一律以重跑生成器后的结果为准。**

验证命令：

```bash
python scripts/gen_fixtures.py --check    # 生成逻辑是否自相矛盾
python scripts/check_fixtures.py          # 仓库里的 JSON 是否完好
```

---

## 12. 待定项（B 反馈后冻结）

以下两项由 A 与 B 商定后写入 v1.1，**在此之前以本节描述为准**：

| # | 待定项 | 当前默认 | 说明 |
|---|---|---|---|
| 1 | `prefer_anc` 的去留 | **保留，缺省 `false`** | 用户明确要求降噪时 A 用 `anc_required=true`，不传 `prefer_anc`。若 B 认为该字段无实际用例，可提议删除 |
| 2 | `summarize` 的形态 | **独立端点** `POST /api/v1/products/summarize` | 备选是 `search` 的 `summary_only: true` 开关。独立端点让 `search` 的响应形状保持确定，A 的代码不必写分支 |

---

## 13. 契约变更记录

| 版本 | 变更 | 提出方 |
|---|---|---|
| v1.0 | 初版：`preferences.criteria`、`priority_preset`、`prefer_anc`、`comparison`、`candidates[].violated_fields`、`total_matches` 语义明确化、`returned`、`applied_criteria`、`comparison[]`、`gaps`、`relax_hints`、`/products/summarize` | A |
| v1.0 | 配套产出 `fixtures/` 与 `scripts/gen_fixtures.py` | A |
| **v1.1** | **币种 CNY → HKD**；`Product` 18 → **20** 字段（`seller_description`、`shipping_origin`）；商品文案改英文；`reasons` 由字符串数组改为 **`MatchReason` 对象数组**；fixtures 改为从 D 的真实 40 条种子生成（删除独立的 `test_catalog.json`）；新增 §3.5 币种与字段、§0.1 字段命名空间对照 | A |

**变更流程**：任何人对本规范有异议或需扩展，必须先向 A 提出，由 A 更新本文件、`app/contracts/search.py` 与 `scripts/gen_fixtures.py` 后升版本、重跑生成器，全员同步再开发。**冻结期内不允许单方面改字段名、类型、枚举或语义。**

**三者必须一致**：`docs/A2B_...规范`（本文件）、`app/contracts/search.py`（代码）、`fixtures/*.json`（数据）。任何一处变更都要同时改另外两处。
