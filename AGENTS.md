# 用户确认的协作约束

本项目按《耳机购物 Agent：四人模块化开发计划 v1.0》与用户提供的 OpenAPI、示例组织协作。用户明确要求输入输出格式与队友兼容。

- D 负责 catalog/、db/、商品种子、初始化脚本与自身测试；A 维护公共 contracts/、响应包装、入口和全项目契约。不要替 A 另造共享模型或改团队锁定依赖。
- Repository 服务签名保持：get_product(product_id: str, connection=None) -> Product | None；list_candidates(constraints: HardConstraints, connection=None) -> list[Product]；decrease_stock(product_id: str, quantity: int, connection) -> bool。
- 读取返回完整标准 Product，未知值保留 null，用途返回数组。list_candidates 返回全量硬条件合格商品，不提前截断、不自行排序推荐、不放宽条件。
- 内部服务直接返回商品/列表/布尔值；HTTP 才用 {ok, data, error, request_id} 包装，错误对象包含 {code, message, details, retryable}。error.details 必须为对象，无详情用 {}，不能用 null。不存在的商品由 A 的错误处理输出 404 PRODUCT_NOT_FOUND。
- HTTP 商品详情路径保持 GET /api/v1/products/{product_id}。接 A 的现有应用，不新建独立后端。
- 价格字段名仍为 price_cents，金额始终为整数百分之一单位，商品 ID、其他枚举和 HardConstraints 字段沿用计划。
- 用户已明确修订商品数据为英文、币种为 HKD，并新增 seller_description 与 shipping_origin。原 CNY 契约不能原样接受这些数据。由 A 协调同步 Product 与所有金额相关公共模型/示例/契约版本；D 不悄悄丢弃新字段，也不把 HKD 标成 CNY。商家描述少于 100 个字符，发货地点为虚构演示设定。
- 集成时通过 ProductRepository(product_model=SharedProduct) 使用 A 的修订后公共 Pydantic Product。独立运行的 D 本地模型只是开发后备模型，不声称与未提供的 A/B/C 实际代码已经联调。
- 商品、订单、钱包、支付共用同一个配置数据库。C 传入连接时，Repository 必须使用该连接，不另开连接、不提交、不回滚、不关闭。扣库存必须有调用方事务并按库存充足条件更新。
- 交易表、支付规则和钱包初始化由 C 提供。不要复制 C 的业务实现。默认初始化只补建/补导入，不覆盖当前库存、价格、钱包或订单；显式重置不得擅自破坏 C 的交易记录。
- 运行 SQLite 文件不提交 Git。测试使用临时数据库。公共契约和实际队友代码未提供时，明确列出尚需联调的项目，不承诺已经完全兼容。
