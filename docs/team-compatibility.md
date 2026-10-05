# 与四人总计划的输入输出衔接

## 固定内部接口

| 服务 | 输入 | 输出 |
|---|---|---|
| `get_product(product_id, connection=None)` | 稳定商品 ID、可选同库连接 | 完整 Product；不存在返回 None |
| `list_candidates(constraints, connection=None)` | 完整 HardConstraints、可选同库连接 | 全量合格 list[Product]；无结果返回 [] |
| `decrease_stock(product_id, quantity, connection)` | 商品 ID、正整数数量、C 已开始事务的连接 | 更新一条为 True；库存不足或不存在为 False |

内部接口不套 HTTP 包装。商品价格仍为整数 `price_cents`，可空字段保留 None，`use_cases` 为列表。HardConstraints 的 11 个字段与原计划保持一致；不加入商家描述或发货地点筛选，也不把用途偏好当硬条件。

## 使用 A 的公共模型

```python
from app.contracts.product import Product as SharedProduct
from app.catalog.repository import ProductRepository

repository = ProductRepository(product_model=SharedProduct)
product = repository.get_product("hp_0001")
# product 是 A 的 SharedProduct 实例，B/C 不需要适配 D 的 dataclass。
products = repository.list_candidates(shared_hard_constraints)
```

A 的 Product 需要先采用用户确认的 HKD 与两个新增字段。若 A 仍提供原 CNY/拒绝新增字段模型，验证会直接报冲突；D 不自行删字段或改币种伪装兼容。HardConstraints 可直接传入 A 的 Pydantic 模型，Repository 按其 model_dump() 校验。

模块级函数保留原签名，独立运行时返回 D 本地 Product。集成时推荐 A 注入上述 repository 实例给 B/C，使返回类型成为团队公共类型。数据库基础设施通过 app.db.connect() 和 initialize_database() 共享；这是总计划未固定命名的基础设施函数，C 在集成时按本文接入。

## HTTP 详情接口格式

路径固定为 GET /api/v1/products/{product_id}。调用 create_router 时提供 A 的成功包装函数和公共错误抛出函数。

成功 HTTP 200：顶层字段为 `ok=true`、`data=完整 Product 对象`、`error=null`、非空字符串 `request_id`。完整机器可读的成功和失败示例见本目录的 `product.responses.examples.json`，其中成功示例使用数据库中的 hp_0001。

商品不存在 HTTP 404：

```json
{
  "ok": false,
  "data": null,
  "error": {
    "code": "PRODUCT_NOT_FOUND",
    "message": "Product not found",
    "details": {},
    "retryable": false
  },
  "request_id": "req_..."
}
```

实际包装、request_id 与异常转换由 A 注入。特别注意原 OpenAPI 中 error.details 是对象，未提供详细信息时用 `{}`，不能用 null。路由适配器兼容本地 Product.as_dict() 和公共 Product.model_dump(mode="json")；不要在接口外再套一层包装。

## 唯一已确认的数据契约差异

相对原 Product：18 个原字段名字、必填规则保留；currency 从 CNY 调整为 HKD；新增两个必填字符串 seller_description、shipping_origin。其余连接、佩戴形式、用途、来源枚举不变。发货地点目前是字符串 Hong Kong，不自行改成嵌套对象。

原合同明确拒绝额外字段，因此这三项调整必须由 A 更新全项目契约与对应版本，包括商品、钱包、订单、支付中出现的币种，以及金额单位说明与示例。D 的 product.schema.json 为修订参考，不代表 A 已批准或完成全项目升级。

## 验证边界

已按提供的计划实现参数顺序、商品字段与硬条件语义，并测试公共模型注入方式、外部连接共享、事务回滚。尚未获得 A/B/C 的实际代码和修订后公共模型，不能据此声称跨模块联调已通过。HTTP 详情适配器也需要 A 的团队 FastAPI 环境验证。
