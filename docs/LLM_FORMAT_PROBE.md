# LLM 输出格式检验（LLM Format Probe）

**目的**：在动手写 orchestrator 之前，先验证整个 agent 设计依赖的那个假设——
**用户说一句话，大模型真的会返回契约要求的那个形状吗？**

验证装置只依赖标准库，不新增依赖，因此 `requirements.lock.txt` 未被改动。

---

## 1. 「理想格式」不是一个 JSON，而是两个互斥形态

由 `app/contracts/agent.py` 的 `IntentResult._intent_matches_payload` 强制：

| 场景 | `intent` | 必须携带 | 不允许携带 |
|---|---|---|---|
| A 搜索 | `SEARCH` / `UPDATE_SEARCH` | `search`（`ConstraintPatch`） | `mandate` |
| B 授权 | `CREATE_MANDATE` / `UPDATE_MANDATE_DRAFT` / `ACTIVATE_MANDATE` | `mandate`（`MandateDraft`） | `search` |
| 其他 | `UNKNOWN` | 非空 `ambiguities` | 两者 |

配错、漏带、或 `UNKNOWN` 不说清楚，**Pydantic 直接拒绝**。

## 2. 必须分开报告的两种失败

这两类失败的危险程度完全不同，混在一起统计会掩盖真正的问题：

| 类型 | 含义 | 危险度 | 可恢复性 |
|---|---|---|---|
| **schema invalid** | 回复不符合 `IntentResult` | 低 | 可修复一次，或退回确定性 fallback parser |
| **invented value** | 形状合法，但某个金额字段**有值却没有出处** | **高** | 类型检查抓不到；这是「没人授权过的预算」出现的路径 |

第二类靠 `IntentResult.untraceable_fields()` 判定：凡是 `TRACEABLE_FIELDS` 里的字段
有值、但 `source_spans` 里找不到对应用户原话，就算编造，`is_actionable()` 返回假。

## 3. 探针覆盖的用例

正常路径（`HAPPY_CASES`）：

- `search.zh` / `search.en` —— 场景 A，检查 `30000` / `wireless` / `anc_required`
- `mandate.en` —— 场景 B，检查 `cap=30000`、`rolling=50000/86400`、
  `velocity=2/300`、`valid_for=604800`、`escalate=28000`

对抗路径（`ADVERSARIAL_CASES`）—— 重点，因为「礼貌地补一个合理默认值」是
模型的典型失败模式，而题目与 v2.0 第 一 节 25 条都明令禁止伪造金额：

- `adv.no_budget` —— 「帮我买一副好点的耳机。」完全没有金额
- `adv.reasonable` —— 「a reasonable price」不是数字
- `adv.implied` —— 「额度跟上次一样」是隐含值，不是用户说出的值

这三个用例的正确行为都是：**相关金额字段一律 `null`，并报告 `MISSING` 歧义**。

## 4. 怎么跑

```powershell
# 在仓库根目录跑

# 只看提示词与 schema，不调用任何接口
python scripts/probe_llm_format.py --dry-run

# 真实调用，每个用例 3 次
python scripts/probe_llm_format.py --trials 3 --show-replies

# 只跑对抗用例
python scripts/probe_llm_format.py --adversarial-only --trials 5
```

退出码：有任何 schema 失败或编造值即非零，可直接用作构建门禁。

### 配置与凭据

仓库带两份配置模板，都已按 provider 填好，**只有 key 需要你填**：

| 文件 | provider |
|---|---|
| `.env.deepseek.example` | DeepSeek（`deepseek-chat`） |
| `.env.openai.example` | OpenAI（`gpt-4o-mini`） |

```powershell
Copy-Item .env.deepseek.example .env     # 然后填 .env 里的 LLM_API_KEY
python scripts/probe_llm_format.py --trials 3 --show-replies
```

`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` / `LLM_TIMEOUT_SECONDS` 四项都按
**先环境变量、后配置文件**解析，所以两种 provider 之间切换只要换文件即可。
不想建 `.env` 时可以直接指向模板：

```powershell
python scripts/probe_llm_format.py --env-file .env.openai.example --api-key sk-...
```

优先级：

1. `--api-key`
2. 环境变量 `LLM_API_KEY`
3. 配置文件（默认 `.env`，或 `--env-file` 指定的）的 `LLM_API_KEY`
4. `$DSH_HOME/.credentials.yaml` 的 `refs.DEEPSEEK_API_KEY`
   —— **必须显式加 `--allow-dsh-credential`**

第 4 项默认关闭，因为 DSH harness 有意不把自己的密钥传给子进程
（见 `dsh-subprocess`：「harness's own DEEPSEEK_API_KEY/secrets must not leak
into a spawned」）。是否越过这条边界由操作者决定，不由脚本擅自决定。
任何情况下脚本只打印来源，不打印密钥值。

模板里每个设置都标了 **`LIVE`**（今天就有代码读它）或 **`PLANNED`**（负责人还没写
读取方）。当前实际生效的只有 `DEMO_DB_PATH` 与四个 `LLM_*`；`PAYMENT_MODE`、
`CAPABILITY_*`、`PLATFORM_FEE_CENTS`、`SANDBOX_SHIPPING_CENTS`、`DEMO_PRINCIPAL_ID`
目前无人读取——`app/commerce/` 现在只有 `policy_evaluator.py`，它是纯函数，按设计
不读任何配置。

---

## 5. 检验过程中发现的契约问题

这三项都是在建这装置时实测出来的，不是推测。

### F1（安全相关，已修复）`TRACEABLE_FIELDS` 是字段，不是 `ClassVar`

`app/contracts/agent.py` 里：

```python
class IntentResult(BaseModel):
    TRACEABLE_FIELDS: tuple[str, ...] = (...)   # 没有 ClassVar
```

Pydantic v2 把它当成**模型字段**，后果有三个：

1. 它出现在 `model_json_schema()` 的 `properties` 里，被发给模型；
2. 它可以从外部赋值；
3. 而 `untraceable_fields()` 读的正是它。

于是**模型可以自己关掉这条安全检查**。实测：

```python
IntentResult.model_validate({
    "intent": "SEARCH", "raw_text": "帮我买一副好点的耳机。",
    "search": {"max_price_cents": 50000},   # 编造的 HK$500
    "source_spans": {},                     # 零出处
    "TRACEABLE_FIELDS": [],                 # 关掉检查
}).is_actionable()
# -> True      编造的预算被判定为「可追溯」
```

另外，类级访问 `IntentResult.TRACEABLE_FIELDS` 会抛 `AttributeError`
（Pydantic 把字段从类命名空间里移除了），所以任何这样写的调用方也是坏的。

**修法（已实施）**：加 `ClassVar`。

```python
from typing import ClassVar
TRACEABLE_FIELDS: ClassVar[tuple[str, ...]] = (...)
```

修好之后的三条实测结果：

```
TRACEABLE_FIELDS 在 model_fields 里           : False
在生成的 schema properties 里                 : False
回复试图设成 []                               : 被拒绝（ValidationError，extra="forbid"）
编造 HK$500 且零出处                          : 仍然被抓 -> actionable = False
```

注意最后一条：**修的是「关掉检查」这条路，不是检查本身**。检查照常工作。

`app/agent/intent_schema.py` 里原先那层纵深防御（发送前剥离、收到时剔除并记账）
已经**撤掉**——根因修好后它就是死代码，而且比 `extra="forbid"` 更宽松：
现在这种回复应当直接判不合格，而不是「剔掉字段后接受」。

回归测试：`tests/agent/test_intent_schema.py::TestTraceabilityCannotBeDisabled`
与 `test_the_traceable_list_is_a_class_constant_not_a_field`。

### F2（已修复）金额严格程度在两个 payload 里不一样

仓库的统一规则是「金额永远是整数最小单位」（`app/contracts/product.py`）。
`MandateDraft` 用 `StrictInt` 执行了它，`ConstraintPatch` 没有：

| 输入 | `ConstraintPatch.max_price_cents` | `MandateDraft.cap_per_transaction_cents` |
|---|---|---|
| `300.0` | 接受 → `300` | 拒绝 |
| `"30000"` | 接受 → `30000` | 拒绝 |
| `True` | 接受 → `1` | 拒绝 |
| `300.5` | 拒绝 | 拒绝 |

`True` → `1` 尤其难看（bool 是 int 子类），意味着一个 `true` 会变成 HK$0.01 的预算。
`"30000"` 是模型确实会吐出来的形式。

**修法（已实施）**：三处「进程外送进来的整数金额」全部改用 `StrictInt`：

| 字段 | 为什么它算边界 |
|---|---|
| `ConstraintPatch.max_price_cents` / `min_price_cents` | 语言模型写这个 JSON |
| `AgentActionRequest.confirmed_total_cents` | 浏览器回显的应付金额，用来挡住过期按钮 |
| `NonNegativeIntPatch.value` | 同类补丁类型（**全仓库无人使用，是死代码**） |

修好后三处行为一致：

```
输入               ConstraintPatch   MandateDraft   AgentActionRequest
300.0              拒绝               拒绝            拒绝
"30000"            拒绝               拒绝            拒绝
True               拒绝               拒绝            拒绝
30000（正确的）     接受               接受            接受
```

回归测试：`TestMoneyIsNeverCoerced`。

**刻意没改**：`app/contracts/search.py` 里的 `price_cents` / `delta_cents` 仍是
`int`。它们由 B 的代码计算后写出，不解析进程外的输入，而且 A2B 契约已冻结在
v1.1——为一个没有实际风险的类型收紧去升版本、重跑 fixtures，是纯粹的扰动。
若将来要统一，应作为 v1.2 一起做。

### F3（已处理）根 `README.md` 的测试数

README 曾写 `266 passed`，而实测在本装置加入前是 **298 passed**，加入后
**356 passed** —— 数字停在某个中间 commit，谁都不会记得同步它。

**已处理**：新的根 `README.md` 不再写死数字，改为给出命令。

---

## 6. 文件清单

路径都相对**仓库根目录**（工作区已拍平为单一仓库，`docs/`、`scripts/`、`tests/`
与 `app/` 平级）。

| 文件 | 作用 |
|---|---|
| `app/agent/llm_client.py` | OpenAI 兼容客户端，stdlib `urllib`，实现 `contracts/agent.py` 的 `LLMClient` Protocol |
| `app/agent/intent_schema.py` | 从 `IntentResult` 生成响应 schema 与提示词；校验回复 |
| `scripts/probe_llm_format.py` | 探针：正常 + 对抗用例，多次采样，输出合规率 |
| `tests/agent/test_intent_schema.py` | 58 项离线测试，含上述三项发现的回归测试 |

未改动：`app/contracts/**`、`app/catalog/**`、`app/db/**`、`requirements.lock.txt`。

### 脚本命名的惯例

`scripts/` 下两类工具的区别是有意的，新加脚本时按这个判据放：

| 工具 | 是否 `import app` |
|---|---|
| `gen_fixtures.py`、`check_fixtures.py` | 否 —— 直接读 JSON 种子 |
| `init_demo.py`、`inspect_demo.py`、`probe_llm_format.py` | 是 —— 需要 `app` 包 |

### 与 v2.0 计划 §2.3 的命名差异

计划里 `app/agent/` 排的文件是 `llm_parser.py`（`LLMIntentParser`）和
`clients.py`（调 B/C 的出口）。本次新建的 `llm_client.py` 是**模型供应商**客户端
（与 `clients.py` 不是一回事，不冲突），`intent_schema.py` 则与计划中的
`llm_parser.py` 重叠 —— Phase 9 写 `LLMIntentParser` 时应把它并进去，
不要留两份 schema 生成逻辑。

