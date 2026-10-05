# 整合 v2.1 · 队友交接

## 版本与归属

基础：团队仓库 `nick-zhang-lohua/HackU-Naai_Lung` main，commit `f7debb41eb59c565f4f8de19e50d8f619daa4931`。

B 排序来自 headphone-agent 分支 `a034f9721b213a3149ece0c9c5c628d46a1938d7`，纯评分/排序函数适配在 `app/search/policy.py`，不使用该原型的另一套购买目录。

D 使用已交付 catalog-v4：40 个原 product_id、英文演示品牌/型号、400 条评论、6 份商品及 2 份银行观察。原 `products.seed.json` 与来源 ZIP 保持不变，ZIP SHA256：`4f85f64eedf1a2e7eb4dc76d8c156b27ba70efcb7d8fcd2b9866f0d15a01bc7c`。

按用户这轮授权补全整个项目，未覆盖原下载目录，未上传 GitHub。

| 部分 | 当前模块 | 职责 |
|---|---|---|
| A | app/agent | 解析、多轮、引用选择、授权草稿与解释 |
| B | app/search | 硬条件过滤后的排序、对比、缺口与评论分析 |
| C | app/commerce | 权限最终判定、预留、原子交易、凭证与审计 |
| D | app/catalog、app/db | 商品、评论、观察、只读查询与迁移 |
| 共用 | app/contracts | 所有模块统一输入输出模型 |
| 服务及界面 | app/main.py、app/api、app/web/index.html | 单进程服务及五个操作页面 |

显式 db_path 仅对 C 生效的旧接线错误已修复：搜索、详情、报价、评论、观察、交易与记录使用同一路径。普通启动只导入缺失种子，保留已有库存与钱包。

## 合同

HTTP envelope 保持 `{ok,data,error,request_id}`，`error.details` 为对象。金额为整数 minor units，29900 = HK$299。

Product 原 20 字段增加四个可选字段：supported_devices（null 或设备数组）、color（null 或颜色枚举）、tags（默认空数组）、estimated_delivery_days（null 或非负整数）。原 seed 缺这些字段仍可加载；运行表共 24 列。

HardConstraints 新增 color、required_device、tags、max_estimated_delivery_days。tags 是 **AND**，商品需满足全部标签。null 不通过设备、配送等硬条件。默认 in_stock_only=true。

criteria 接受非空属性名称，使不支持维度进入 gaps，而非假装可评分。支持价格、续航、重量、ANC 和配送天数。推荐先遵守有序偏好，再用 0.5/0.3/0.2 加权。缺评论时去掉评论分并归一化其余权重，不填虚构 0.6。

Candidate 的 SKU 路径始终为 `candidate.product.product_id`，没有顶层 product_id 字段。新增 feature_score、price_score、review_score、composite_score、review_score_source、review_summary。SearchResponse 增加 ranking_policy 与 score_weights。

搜索无结果时返回逐一放宽某一字段的 hints 与注明违反条件的 near misses；原约束保持不变。未支持或未知属性进入 gaps。

新增 CANCEL_SELECTION 仅取消未执行选择与报价，拒绝当前待询问提案，不退款。原 CANCEL_ORDER 枚举兼容保留，不是退款接口。新增 visa_demo/VISA，原 FPS/Mastercard/wallet route ID 保留。

**旧 A/B 的 extra=forbid 会拒绝新字段，所以队友应同步本版共享 contracts、组装入口和相关模块。** 建议整树运行，不把新版 B 响应直接塞给旧版 A。

## 接口

| 方法 | /api/v1 后的路径 | 用途 |
|---|---|---|
| GET | /health | schema、数量、解析模式、审计与对账 |
| POST | /agent/chat、/agent/actions | 原多轮及显式动作格式 |
| POST | /products/search、/products/summarize | 正式 B 输入输出及全部合格商品统计 |
| GET | /products/{id} | 同库实时商品详情 |
| GET | /products/{id}/reviews、/review-summaries | 英文评论与全部评分平均值 |
| GET | /products/{id}/review-analysis | 只读公开文本迹象与依据 |
| POST | /rewards/estimate | 显式条件下的银行卡回赠试算 |
| GET | /demo/overview | 当前演示用户的余额与订单 |
| GET | /transactions/{proposal_id} | 交易判定、凭证、审计与对账 |
| GET | /observations | 来源观察，与虚构目录分开 |

完整格式见 contract.integrated-v2.1.openapi.json、integration.examples.json 及运行时 `/docs`。LLM、商家描述、评论和观察均不能批准、预留或扣款。C 只在 submit_proposal 内执行交易，现金上限不减预计回赠。

## SQL 与记录

catalog schema v1 → v2 只加列，评论与观察组件 version=1。迁移和导入共用初始化事务，不支持的版本/异常原列集合拒绝转换。D 原 get_product/list_candidates/decrease_stock 签名保留，调用方传 connection 时不提交或关闭其事务。

product-metadata.demo.json 仅补齐演示商品缺失扩展，不覆盖已有扩展或价格、库存，不为真实观察臆造参数。设备能力、配送天数均明确为虚构设定。reviews/observations 只追加，已有 ID 改内容被拒绝。

运行库在 var 下，未打包测试交易、钱包余额、聊天会话或密钥。来源 v4 ZIP 内有原静态 SQLite 快照，独立于运行库。显式 reset 工具先备份再重建 C/D，并保留评论及观察；正常启动从不调用它。

## 银行试算及证据

固定观察日 2026-10-03，京东耳机、HKD、直接过卡。用户规则与本次核对的官方条款作为计算依据。

HSBC 学生金卡该场景基本每 HK$250 得 HK$1 并滚存余数。MMPOWER 其他网上零售需要登记月份覆盖、15 日内志账、学生证明或月度门槛，额外部分按月度净额 4.6% 取整数 HKD，月度额外封顶 HK$500，再加基本奖励。钱包渠道排除推广奖励；基础资格也必须明确，不能从缺条件推定可得。

银行观察保留原始证据，不被计算结果改写。C 的模拟通道 reward_value_cents 仍为 0；独立计算器的奖励不是支付余额或真实银行入账。UI 预填学生、已登记等均标注为可修改的演示假设。未知必要条件返回 conditional。

官方资料：[HSBC 学生卡](https://www.hsbc.com.hk/zh-hk/credit-cards/products/student-visa-gold/)、[HSBC 奖赏 FAQ](https://www.hsbc.com.hk/zh-hk/help/faq/credit-cards/rewards/)、[恒生 MMPOWER 英文条款](https://www.hangseng.com/content/dam/wpb/hase/rwd/personal/cards/pdfs/everyday_tnc_en.pdf)。

## 范围及未验证项

本版是完整可运行的本地模拟应用。默认规则解析有限词表，支持常见条件与多轮修改；可选接 A 已实现的联网 parser，其 schema 校验、来源可追溯与失败降级保留。没有用真实 key 实测外部模型调用。

聊天会话在进程内存，重启需新对话和新授权，交易/余额/旧授权审计仍持久保存。真实京东结算、银行核验、退款、生产多用户认证，以及真实评论的识别效果不属于已完成的模拟范围。

## 2026-10-04 模型解释补充

共享 AgentResponse、Product、金额单位及响应 envelope 不变。A 可选 GroundedExplainer 读取 B 的只读 review_context(product_id)，结合已经筛选的候选生成简短说明，仅替换 message 和追加 trace。解释成功 trace detail 为 grounded_explanation，失败为 grounded_explanation_failed，按钮路径跳过模型。评论追问保留原排序与候选。GET /api/v1/health 新增 llm 状态（配置模型、实际返回模型、成功收到的响应次数），从不返回 key。

LLM API 只收到本轮输入、有限浏览历史、候选目录事实和公开的模拟评论样本，不包含 key、钱包余额或付款凭证。评论分析目前仍是 B 的确定性文本检测；模型解释检测信号，不宣称已证实付费水军。定量筛选、排序与支付判定保持原实现。
