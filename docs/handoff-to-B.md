# 交给 B：搜索/推荐/对比模块

**你要实现的是 `app/search/`。契约已经冻结，不用等任何人。**

现在整个项目就在一个仓库里，直接 clone 即可，不需要再合并任何东西。

```bash
python -m pytest tests/search -q
```

在 `app/search/` 存在之前，这个文件整体跳过，套件保持全绿。你建出
`app/search/__init__.py` 的那一刻它自动启用，并给出 **10 个失败，全部是
"还没实现"**——这是预期状态，不是仓库坏了。把
`tests/search/test_search_fixtures.py` 里标了 TODO 的三处指向你的实现，
它就开始给出真正的反馈。

---

## 1. 先读这三份，顺序别换

| 顺序 | 文件 | 为什么这个顺序 |
|---|---|---|
| **1** | 本文件的 §2、§3 | 先建立心智模型：**B 提供事实，A 做判定** |
| **2** | `docs/A2B_搜索推荐对比接口规范_v1.1.md` | 完整规则：字段语义、过滤、五级排序、对比矩阵、缺口、放宽、28 项验收 |
| **3** | `app/contracts/search.py` | 要 import 的模型。Pydantic 会替你挡住一半的规则 |

`app/contracts/` 里还有 `agent.py` / `commerce.py` / `mandate.py` / `policy.py` /
`audit.py`，那是完整契约的一部分，**你不用读**。

---

## 2. 分工边界（最重要的一节）

**B 提供事实，A 做判定。**

| 层 | 谁 | 内容 | 性质 |
|---|---|---|---|
| ① **定义"什么算符合"** | A | `constraints`（硬条件）+ `criteria`（偏好方向与优先级） | **判断**——来自用户的话 |
| ② **查出"谁符合、有几个、差多少"** | **B** | 过滤、计数、排序、对比、缺口、放宽提示 | **事实**——机械可复算 |
| ③ **决定"所以该怎么办、怎么说"** | A | `SATISFIED` / `NEEDS_RELAXATION` / `IMPOSSIBLE`；对用户说的话 | **判定 + 措辞** |

**为什么 B 不能做第 ③ 层：B 看不到用户。** 用户从没跟 B 说过话，B 手上只有 A
转交的结构化条件。让它决定"要不要放宽条件"，就是替一个它不认识的人做决定。

### 规则一：B 只报告，不放宽

没有任何合规商品时，B 返回**事实**而不是**主张**：

```json
{
  "total_matches": 0,
  "candidates": [],
  "relax_hints": {
    "hints": [
      { "field": "max_wearing_weight_g", "from_value": 5, "to_value": 9,
        "gained_count": 2, "example_product_id": "hp_0001" }
    ]
  }
}
```

B 说的是：**"5 克以内没有商品；把上限放宽到 9 克能多 2 款。"**
B 不说：**"建议放宽到 9 克。"** —— 那是 A 拿到事实后去问用户的话。

### 规则二：B 不决定"300 稍微超一点也可以"

| 用户说了 | B 的职责 | B 不负责 |
|---|---|---|
| "300 以内" | 报告 `hp_0007` = 30000 分，**含入**上界 | 判断"301 也差不多" |
| "必须支持降噪" | `anc = null` 或 `false` 一律淘汰 | 判断"未知的可能也支持" |
| "最好 5 克以内" | 报告"0 款合规；放宽到 9 克 +2" | 决定"那就放宽吧" |

前两列是**规则**（可复算），第三列是**意见**（不可复算）。

### ⚠️ `match_score` 这个名字会误导你

它**不是匹配度判定**，只是**数了几个标签命中**：

```
match_score = 10 × (用户要求的用途里，有几个出现在该商品的 use_cases 里)
            + (prefer_anc 且 anc == true ? 5 : 0)
```

两个实测后果：

1. **常常对全体候选相同。** `search.001.normal` 里都是 10 分——不是算错。真正的排序主力是 `criteria`。
2. **漏掉 `criteria` 会让排序退化**成按 `product_id` 排，而 fixtures 会直接抓到。

觉得该改名（比如 `preference_hits`）就提给 A——现在改最便宜。

---

## 3. 东西都在哪

```
HackU/
├─ app/
│  ├─ catalog/  db/          D 的商品库，只读参考
│  ├─ contracts/             A 的契约（你只读 search.py / product.py / common.py）
│  ├─ commerce/              C 的钱
│  ├─ agent/                 A 的编排
│  └─ search/                ← 你建这个
├─ data/products.seed.json   40 条 HKD 真实商品，期望值的计算来源
├─ fixtures/                 6 组输入输出配对，直接跑
│  ├─ search.001.normal.json      4 款合格、返回 3 款；排序；对比矩阵
│  ├─ search.002.relax.json       无解：0 款；放宽提示；近似候选
│  ├─ search.003.anc_unknown.json anc=null 不得满足必须降噪
│  ├─ search.004.invalid.json     非法约束 → INVALID_CONSTRAINTS
│  ├─ search.005.gaps.json        缺口必须三处同时报告
│  └─ summarize.001.partial.json  先探后问的分布统计
├─ scripts/gen_fixtures.py   期望值生成器（唯一事实来源）
├─ scripts/check_fixtures.py 结构校验器
└─ tests/search/             你的验收骨架
```

---

## 4. 这条链路你要实现的部分

```
A ──SearchRequest──► B ──SearchResponse──► A
                      │
                      ├─ 1 校验请求（含对比维度是否有依据）
                      ├─ 2 按 constraints 严格过滤（全量，不提前截取）
                      ├─ 3 算 match_score
                      ├─ 4 五级排序
                      ├─ 5 截取 limit 条
                      ├─ 6 算对比矩阵（含 is_distinguishing）
                      ├─ 7 算缺口报告
                      └─ 8 无命中时算放宽提示与近似候选
```

**没有第 9 步。** 不生成文案、不建议、不判断用户该不该接受。

---

## 5. 六个最容易做错的地方

| # | 规则 | 为什么容易错 |
|---|---|---|
| 1 | `total_matches` 是**截取前**的数量 | `search.001` 里 `total=4` 而 `returned=3`，看起来像 bug，其实是对的 |
| 2 | `anc = null` **不得**满足 `anc_required=true` | 把未知当满足，`total_matches` 会明显偏大 |
| 3 | `match_score` **全平是正常的** | 见 §2 |
| 4 | 某维度有未知值时 `spread` **必须为 `null`** | 不能把 `null` 当 0 参与极差计算 |
| 5 | 无解时**绝不**把放宽后的商品放进 `candidates` | 只能放进 `relax_hints`，且每条近似候选必须带非空 `violated_fields` |
| 6 | 无法评估的判据要**同时**出现在三处 | `unsupported_criteria` / `dropped_criteria` / `dropped_dimensions`，缺一即不合格 |

---

## 6. 期望值不要手改

发现对不上时按这个顺序查：

1. 跑 `python scripts/gen_fixtures.py`，看输出是否与 `fixtures/` 里的文件一致
2. **一致** → 是你的实现与规范不符，对照规范相应条款
3. **不一致** → 文件被改过或转写有误，以生成器输出为准
4. 认为**规范本身**有问题 → 提给 A，由 A 升版本并重跑生成器

```bash
python scripts/gen_fixtures.py --check   # 生成逻辑是否自相矛盾
python scripts/check_fixtures.py         # 现有 JSON 是否完好
```

两个都应通过。生成器内置自检，会拦下这些自相矛盾的输出：`returned` 与候选
数量不符、`total_matches=0` 却有候选、有未知值却给了 `spread`、放宽提示的
`gained_count` 不是正数、近似候选没写 `violated_fields`。

---

## 7. 数据源

`data/products.seed.json`（40 条 HKD 商品）就是期望值的计算来源。测试里用
`tests/search/test_search_fixtures.py` 的 `SeedCatalog` 直接喂给你的实现，
**不需要**跑 D 的初始化脚本。

真实数据已经覆盖了需要的边界：

| 边界 | 商品 |
|---|---|
| 价格正好 HK$300 | `hp_0007`（30000 分，含边界） |
| 缺货 | `hp_0005`、`hp_0011`、`hp_0022`、`hp_0033` |
| `anc = null` | 6 条在库商品 |
| `wearing_weight_g = null` | `hp_0017`、`hp_0034` |
| `battery_hours = null` | 全部有线耳机 + `hp_0013`、`hp_0026`、`hp_0039` |

---

## 8. 有问题就问

规范有歧义、字段含义不清、fixtures 与实现不符——都直接提给 A。
**契约在冻结期内不允许单方面改字段名、类型、枚举或语义**；需要改就提出来，
由 A 升版本、重跑生成器、全员同步。
