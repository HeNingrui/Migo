# fixtures 说明（A → B）

写给成员 B：这里是可以直接拿去写测试的输入输出配对。

---

## 0. 先读这一节：分工边界

**B 提供事实，A 做判定。**

这句话决定了 `app/search/` 里该有什么、不该有什么。越界不会报错，只会让用户收到一个没人替他做过的决定。

### 0.1 三层分工

| 层 | 谁 | 内容 | 性质 |
|---|---|---|---|
| ① **定义"什么算符合"** | **A** | `constraints`（硬条件）+ `criteria`（偏好方向与优先级） | **判断**——来自用户的话 |
| ② **查出"谁符合、有几个、差多少"** | **B** | 过滤、计数、排序、对比、缺口、放宽提示 | **事实**——机械可复算 |
| ③ **决定"所以该怎么办、怎么说"** | **A** | `SATISFIED` / `NEEDS_RELAXATION` / `IMPOSSIBLE`；对用户说的话 | **判定 + 措辞** |

**为什么 B 不能做第 ③ 层：B 看不到用户。** 用户从没跟 B 说过话，B 手上只有 A 转交的结构化条件。让它决定"要不要放宽条件"，就是在替一个它不认识的人做决定。

### 0.2 两条硬规则

**规则一：B 只报告，不放宽。**

没有任何合规商品时，B 返回的是**事实**而不是**主张**：

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

B 不说的是：**"建议放宽到 9 克。"** —— 那是 A 拿到这个事实后，去问用户的话。

判定本身是 `classify_outcome()`（住在契约里，但**语义上属于 A**）：`total_matches > 0` → 满足；`> 0` 且有 hints → 需要放宽；都没有 → 不可能。**它不是 B 的调用点。**

**规则二：B 不决定"300 稍微超一点也可以"。**

| 用户说了 | B 的职责 | B 不负责 |
|---|---|---|
| "300 以内" | 报告 `hp_0007` = 30000 分，**含入**上界 | 判断"301 也差不多" |
| "必须支持降噪" | `anc = null` 或 `false` 一律淘汰 | 判断"未知的可能也支持" |
| "最好 5 克以内" | 报告"0 款合规；放宽到 9 克 +2" | 决定"那就放宽吧" |

前两列是**规则**，第三列是**意见**。规则可以复算，意见不能。

### 0.3 ⚠️ `match_score` 这个名字会误导你

`match_score` **不是匹配度判定**，它只是**数了几个标签命中**：

```
match_score = 10 × (用户要求的用途里，有几个出现在该商品的 use_cases 里)
            + (prefer_anc 且 anc == true ? 5 : 0)
```

它回答的是"这个商品的用途标签命中了几个"，**不是**"这个商品合不合适"。

两个后果，都是实测过的：

1. **`match_score` 常常对全体候选相同。** `search.001.normal` 里所有候选都是 10 分——这不是算错。真正的排序主力是 `criteria`。
2. **漏掉 `criteria` 会让排序退化。** 分数全平时若没实现 `criteria`，顺序会变成按 `product_id` 排，而 fixtures 会直接抓到。

**如果你觉得这个名字应该改成 `preference_hits` 之类**，提给 A——现在改的成本远低于以后（fixtures 是生成的，重跑即可）。

### 0.4 B 绝对不能做的事（完整清单）

| 禁止 | 原因 |
|---|---|
| 放宽或忽略 `constraints` 里的硬条件 | 只有 A 在用户同意后才能改条件 |
| 用 LLM 重新解释、补全或改写条件 | 判据只能来自 A；B 不得二次理解用户意图 |
| **生成面向用户的文案、理由句、推荐语** | B 返回**带 `field` 的结构化理由**，措辞是 A 的事 |
| 把 `null` 当作 `0`、`false` 或"最差" | 未知 ≠ 不满足，未知 ≠ 最差 |
| 补造商品参数 | 缺就是 `null`，由 D 的数据决定 |
| 自己建立商品数据副本 | 商品数据统一经 D 的 Repository |
| 返回超过 `limit` 的候选 | 全量数据放在 `total_matches` 与 `comparison` 里 |
| 输出任何"综合损耗分 / 匹配百分比" | 权重不可解释、无法复算 |
| 判断"要不要向用户提议放宽" | 那是 A 的决策，见 §0.2 |

---

## 1. 数据来源

**fixtures 不再维护自己的测试商品集。** 全部期望值由生成器从**成员 D 的真实商品种子**复算：

```
data/products.seed.json   （40 条，HKD）

```

之前有过一份独立的 20 条虚构商品集，已删除。两套商品数据必然漂移——同样的 `hp_0001` 在两份数据里是不同的东西，迟早有人按错误的那个写代码。D 的种子本来就覆盖了需要的全部边界：

| 边界 | 商品 |
|---|---|
| 价格正好 HK$300 | `hp_0007`（30000 分，含边界） |
| 缺货 | `hp_0005`、`hp_0011`、`hp_0022`、`hp_0033` |
| `anc = null`（未知） | 6 条 |
| `wearing_weight_g = null` | `hp_0017`、`hp_0034` |
| `battery_hours = null` | 全部 9 条有线耳机 + `hp_0013`、`hp_0026`、`hp_0039` |
| 最轻 / 最重 | `hp_0001`/`hp_0008`（9 克） / `hp_0003` 等（230 克） |

---

## 2. 文件清单

| 文件 | 覆盖 |
|---|---|
| `search.001.normal.json` | 正常路径：4 款合格、返回 3 款；排序；对比矩阵；超预算替代项 |
| `search.002.relax.json` | **无完美解**：0 款；放宽提示；最接近候选的 `violated_fields` |
| `search.003.anc_unknown.json` | **边界**：`anc=null` 不得满足 `anc_required=true`；`lower_price` 排序 |
| `search.004.invalid.json` | **错误**：`min_price_cents > max_price_cents` → `INVALID_CONSTRAINTS` |
| `search.005.gaps.json` | **缺口**：无法评估的判据必须被报告，不得静默忽略 |
| `summarize.001.partial.json` | 先探后问的分布统计（支撑 A 生成带数字的追问） |

当前期望值摘要：

```
search.001.normal        total=4   returned=3   顺序 hp_0007, hp_0001, hp_0008
search.002.relax         total=0   returned=0   hint 重量 5 -> 9 (+2)
search.003.anc_unknown   total=6   returned=5   顺序 hp_0001, hp_0008, hp_0007, hp_0034, hp_0003
search.004.invalid       INVALID_CONSTRAINTS
search.005.gaps          total=4   returned=3   unsupported=[audio_quality]
summarize.001.partial    total=36  anc true 6 / false 24 / unknown 6
```

---

## 3. 期望值是怎么来的

**`expected` 不是手写的，而是由生成器复算的。**

```bash
python scripts/gen_fixtures.py            # 重新生成全部 fixture
python scripts/gen_fixtures.py --check    # 只校验、不写文件
python scripts/gen_fixtures.py --report   # 只打印推导
```

**为什么必须这样：** 手算的期望值无法被验证。一旦你的实现与样例不一致，双方都无法判断是规范有歧义、样例算错，还是实现有 bug。有了生成器，期望值天然正确，而且你能自己重跑复现。

生成器内置 `self_check()`，自动拦下这些自相矛盾的输出：

- `returned != len(candidates)`
- `total_matches == 0` 但 `candidates` 非空，或缺 `relax_hints`
- 某对比维度有未知值，但 `spread` 不是 `null`
- `closest_candidates` 的 `violated_fields` 为空
- `hints` 的 `gained_count` 不是正数
- 候选或超预算替代项引用了种子里不存在的商品，或币种不是 HKD

---

## 4. 怎么用

### 4.1 路径

fixtures、脚本和数据现在都在同一个仓库里，路径是平的：

```
HackU/                            <- 仓库根
├─ fixtures/                      <- 就是这里
├─ scripts/gen_fixtures.py
├─ data/products.seed.json        <- fixtures 的数据来源
├─ app/
└─ tests/                         <- 测试在这里跑
```

所以在 `tests/` 下用 `ROOT / "fixtures"` 就是对的，不需要 `../..` 那种绕法。
**现成的骨架就在 `tests/search/test_search_fixtures.py`，直接改它，不必照抄示例。**
环境变量 `HACKU_FIXTURES` / `HACKU_CATALOG` 仍可覆盖默认值：

```python
import json, os, pathlib, pytest
from app.contracts.search import SearchRequest, SearchResponse

ROOT = pathlib.Path(__file__).resolve().parents[1]     # tests/xxx/ -> 仓库根
FIXTURES = pathlib.Path(os.environ.get("HACKU_FIXTURES", ROOT / "fixtures")).resolve()
CATALOG = pathlib.Path(os.environ.get("HACKU_CATALOG",
         ROOT / "data" / "products.seed.json")).resolve()
```

### 4.2 数据源

fixtures 的期望值来自 D 的 `data/products.seed.json`（**就在应用仓库里**）。用假的 Repository 直接喂给你的实现，**不需要跑 D 的初始化脚本**：

```python
class SeedRepo:
    def __init__(self, seed_path=CATALOG):
        self.products = json.loads(seed_path.read_text(encoding="utf-8-sig"))
        if isinstance(self.products, dict):
            self.products = self.products["products"]

    def list_candidates(self, constraints, connection=None):
        return list(self.products)      # 过滤是 B 的职责

    def get_product(self, product_id, connection=None):
        return next((p for p in self.products if p["product_id"] == product_id), None)
```

---

## 5. 必须理解的期望行为

### 5.1 `match_score` 全平是正常的

见 §0.3。`search.001.normal` 里所有候选的 `match_score` 都是 `10`——**这不是算错**，它只是数了标签命中。

正因分数全平，这个用例才成为**检验 `criteria` 是否真的生效**的好样例：漏掉 `criteria` 时顺序会退化成按 `product_id` 排。

### 5.2 `total_matches` 是截取前的数量

`search.001.normal` 的 `total_matches=4` 但 `returned=3`。看到这个差异不要以为哪里错了。

### 5.3 未知值必须是未知

`search.003.anc_unknown` 的硬条件是 `anc_required=true`，命中 6 款。若你的实现把 `anc=null` 当成满足，`total_matches` 会明显偏大。这是最容易暴露的实现错误。

### 5.4 无结果时不得把放宽结果塞进 candidates

`search.002.relax` 的 `candidates` 与 `comparison` 必须是空数组；放宽后的商品只能出现在 `relax_hints` 里。而且每条 `closest_candidates` 都要带**非空且准确**的 `violated_fields`——这是 A 向用户坦白"这一款差在哪"的唯一依据。

### 5.5 同一个未知判据要在三处同时出现

`search.005.gaps` 里 `audio_quality` 同时出现在 `unsupported_criteria`、`dropped_criteria`、`dropped_dimensions`。缺任何一处都算实现不合格。

### 5.6 空结果不是错误

`total_matches == 0` 是 HTTP 200 的正常结果，且必须带 `relax_hints`。而 `INVALID_CONSTRAINTS` 是错误，两者不可混淆。

---

## 6. 发现期望值与实现不符时

按这个顺序排查，**不要直接改 JSON**：

1. 跑 `python scripts/gen_fixtures.py`，看输出是否与仓库里的文件一致。
2. 重跑后**一致** → 是你的实现与规范不符，对照 `docs/A2B_搜索推荐对比接口规范_v1.1.md` 相应条款。
3. 重跑后**不一致** → 仓库里的 JSON 被手工改过或转写有误，以生成器输出为准。
4. 认为**规范本身**有问题 → 提给 A，由 A 更新规范与生成器后升版本。

---

## 7. 契约变更

本目录与 `docs/A2B_搜索推荐对比接口规范_v1.1.md`、`app/contracts/search.py` 三者配套。任何字段或语义变更都必须**同时**更新这三处，再重跑生成器。**不要手工编辑 `fixtures/*.json`。**
