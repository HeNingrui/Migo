# 耳机购物 Agent：成员 D 商品数据库（英文 / HKD）

> 这是成员 D 的模块说明。仓库拍平前它是内层应用仓库的 `README.md`，
> 现在放在 `docs/` 下作为模块文档；项目总览见根 `README.md`。
> 文中提到的路径（`app/catalog/`、`data/`、`scripts/`、`var/`）都相对仓库根，已按新布局核对。

本包是依据修改后的 `products.seed.en-HKD.json` 制作的成员 D 模块。40 条商品均为虚构演示商品，发货地点和商家描述也是演示设定。价格是港币演示定价，没有进行汇率换算。

## 你现在拿到了什么

- `var/demo.sqlite3`：已经导入 40 条商品的 SQLite 数据库，可直接使用。
- `data/products.seed.json`：全英文原始数据，用于重建数据库。
- `app/catalog/`：商品模型、种子校验、商品查询、条件筛选、库存更新，以及可选 FastAPI 路由适配器。
- `app/db/`：数据库连接、建表、结构版本、初始化和数据库状态检查。
- `scripts/`：初始化和只读查看工具。
- `tests/test_catalog.py`：31 项数据库与协作边界测试，全程使用临时数据库。
- `docs/`：商品表 SQL、修订后商品 JSON Schema、原始 OpenAPI、响应示例和队友接入说明，供 A 核对。

本包未实现 C 的钱包、订单、支付业务，也未建立四个独立后端。C 的交易表通过统一初始化入口接入。没有正式前端。D 的本地模型位于 `catalog/models.py`，没有代替 A 修改公共 `contracts/`。

## 运行方式

数据库工具仅依赖 Python 标准库。建议按团队约定使用 Python 3.12；本机已在 Python 3.13.14 验证。SQLite 需要 3.37 或以上（使用 STRICT 表）；常规 Python 3.12/3.13 的新版安装通常满足，启动脚本会在不支持时报告数据库错误。

在**仓库根目录**打开终端（脚本会自行定位项目根，不依赖当前目录）。首次建库或以后补导入商品：

```powershell
python scripts/init_demo.py
```

重复运行只补建和补导入，不覆盖现有商品名称、价格、库存或 C 的交易数据。现有 ID 对应的种子参数改变，也不会自动写回运行库。

查看单个商品：

```powershell
python scripts/inspect_demo.py --product hp_0001
```

查看 300 港币以内、无线、必须支持主动降噪的有库存商品：

```powershell
python scripts/inspect_demo.py --max-price-cents 30000 --wireless --anc
```

返回 4 个合格 ID：`hp_0001`、`hp_0007`、`hp_0008`、`hp_0018`。这是全量合格列表，按 ID 保持稳定顺序，不代表推荐顺序；B 应随后排序并截取。

运行测试：

```powershell
python -m unittest discover -s tests -v
```

测试依赖和团队统一依赖由 A 维护；这里使用标准库 unittest，使数据库在未安装 pytest 时也能验证。A 的 pytest 也可以发现这些 unittest 测试。

## 数据库路径

默认路径由代码根据项目目录计算为 `var/demo.sqlite3`，不依赖当前终端位置。路径优先级为：显式 `--db` / 函数参数 > 环境变量 `DEMO_DB_PATH` > 默认路径。相对路径统一相对于项目根目录。

```powershell
$env:DEMO_DB_PATH = 'D:\headphone-demo\demo.sqlite3'
python scripts/init_demo.py
```

`.env.deepseek.example` / `.env.openai.example` 是配置模板（复制成 `.env` 使用）。D 的标准库脚本不自动读取 `.env`；需要由 A 的配置系统设置环境变量，或在终端中设置。

## 商品表

每一行是一款具体 SKU，`product_id` 为稳定主键。相同型号不同颜色使用不同 ID，独立价格和库存。数据库共有两张实际表：`products` 与 `schema_version`。

原有 18 个字段保留，新增：

| 字段 | 类型 | 示例 |
|---|---|---|
| `seller_description` | 非空文本，少于 100 个字符 | Meet Air C: wireless in-ear listening... |
| `shipping_origin` | 非空文本 | Hong Kong |

金额字段仍为整数 `price_cents`，现在表示港币的百分之一：`29900 = HK$299.00`。`currency` 只能为 `HKD`。未知参数保留 `null`，不得由商家文案或模型补造。

SQLite 中 `anc` 用 `1 / 0 / NULL` 保存，Repository 返回 `true / false / null`。`use_cases` 在数据库中保存 JSON 文本，Repository 返回 Python 列表；其他业务模块不用处理字符串。

数据库约束保护主键、非负价格/库存、枚举、布尔值、JSON 数组、商品描述长度及有线续航为空等条件。导入校验进一步保证字段完整、没有额外字段、ID 与用途标签不重复、数字有限且范围合法、来源 URL 格式合理。导入器拒绝错误数据，不会部分导入。

## B 如何接入

```python
from app.catalog import HardConstraints, ProductRepository

repository = ProductRepository()  # 共用 DEMO_DB_PATH
constraints = HardConstraints(
    max_price_cents=30000,
    connection="wireless",
    anc_required=True,
)
products = repository.list_candidates(constraints)
for product in products:
    print(product.product_id, product.price_cents, product.as_dict())
```

全部硬条件均执行：类别、预算上下界（含边界）、品牌列表、连接方式、佩戴形式、ANC、最低续航、最高重量与库存。未知参数不能通过相应硬性要求。空品牌列表不限制品牌，`anc_required=False` 不排除支持 ANC 的商品。无匹配时返回 `[]`，不放宽条件。

支持 D 的 `HardConstraints`、完整字段字典、或 A 的 Pydantic 模型（通过 `model_dump()`）。返回 D 的 `Product`，A 后续可以通过 `product.as_dict()` 转成修订后的公共模型。C 也可通过属性读取价格、库存和商品快照字段。

正式集成时可以直接注入 A 的公共 Product，让 Repository 返回该公共类型：

```python
from app.contracts.product import Product as SharedProduct
repository = ProductRepository(product_model=SharedProduct)
```

该公共模型必须已采用用户确认的 HKD 和两个新增字段；旧 CNY 契约冲突会明确报错，不会静默改数据。接口与类型衔接细节见 `docs/team-compatibility.md`。

模块级函数保留计划中的三个签名：

```python
from app.catalog.repository import get_product, list_candidates, decrease_stock

get_product(product_id: str, connection=None)  # Product 或 None
list_candidates(constraints, connection=None)  # 全量 list[Product]
decrease_stock(product_id: str, quantity: int, connection)  # bool
```

模块级函数使用统一配置路径；需要测试或依赖注入时使用 `ProductRepository(db_path)` 实例。

## C 如何接入同一个事务

```python
from app.catalog import ProductRepository
from app.db import connect

repository = ProductRepository()
conn = connect()
try:
    conn.execute("BEGIN IMMEDIATE")
    product = repository.get_product("hp_0001", connection=conn)
    # C 在这里完成订单/归属/状态/确认金额/幂等/钱包余额校验。
    updated = repository.decrease_stock("hp_0001", 1, connection=conn)
    if not updated:
        raise RuntimeError("OUT_OF_STOCK")
    # C 使用同一连接写钱包、订单和支付记录；任一异常由 C 回滚。
    conn.commit()
except Exception:
    conn.rollback()
    raise
finally:
    conn.close()
```

这仅展示库存如何加入事务，不是完整支付实现。付款先后顺序和失败记录仍按 C 的计划实现。

传入连接后，所有 Repository 方法使用该连接，不另开连接、不提交、不回滚、不关闭。无外部连接的读取方法自行打开只读连接并关闭。扣库存要求已有事务、数量为正整数，并使用 `stock >= quantity` 的条件更新；库存不足或商品不存在返回 `False`。

所有连接开启外键和 5 秒忙等待；数据库忙异常由 A/C 的统一错误处理映射为 `DB_BUSY`。连接为自动提交模式，因此 C 必须显式开始事务；没有事务的库存扣减会被拒绝。

## C 的建表与钱包初始化接入

```python
from app.db import initialize_database

initialize_database(
    commerce_schema=c_create_tables,
    wallet_initializer=c_init_demo_wallet,
)
```

两个适配函数都接收一个 `sqlite3.Connection`。执行顺序是：创建 D 表 → 创建 C 表 → 补导入商品 → 初始化钱包 → 检查外键 → 提交。C 的函数只通过传入连接执行语句，不能提交、回滚、关闭或调用会结束事务的 `executescript()`。钱包初始化应仅补建缺失用户，不能重置余额。交易表和钱包初始化当前尚未提供，命令行脚本仅初始化商品部分。

`schema_version` 为组件版本表：`component / version / updated_at`；D 的组件名为 `catalog_hkd`、版本 1。C 若使用此表管理版本，应使用自己的组件名，由团队协调。现有无版本数据库或不支持的 D 版本会报错，不自动覆盖或迁移。

## 明确重置

```powershell
python scripts/init_demo.py --reset
```

只有显式 `--reset` 才恢复初始商品及库存。重置前会将当前完整数据库备份为同目录下的 `demo.sqlite3.before-reset-时间戳.bak`。

当前重置工具只处理 D 的独立商品演示库。一旦已接入 C 的业务表或其他组件版本，重置会拒绝执行，以免留下旧订单和新库存不一致的状态。全系统重置需要 D 与 C 一起接入 C 的重置逻辑；本工具不擅自删除交易记录或重置钱包。

## A 的接口与契约衔接

`app/catalog/routes.py` 提供 `create_router()` 工厂，挂载 `GET /api/v1/products/{product_id}`。A 注入统一成功包装函数和抛出 `PRODUCT_NOT_FOUND` 的公共错误函数，再在现有应用挂载；数据库异常交给 A 的全局异常处理。

```python
from app.catalog.routes import create_router
from app.catalog import ProductRepository

router = create_router(
    ProductRepository(),
    wrap_success=a_wrap_success,           # (product_dict, request) -> 通用响应
    product_not_found=a_product_not_found, # (product_id, request) -> 抛出公共 404 错误
)
app.include_router(router)
```

当前环境没有 FastAPI/Pydantic，因此该可选 HTTP 适配器未实际运行；商品服务和数据库已经验证。接入时注意 B 的静态 `/search` 路由应先于 `/{product_id}` 挂载，或使用统一路由注册避免路径冲突。

本包遵从用户明确修改的 HKD 数据。原 v1 契约仍为 CNY，且不允许新增商品字段。A 必须同步修改公共 Product、商品响应、金额/订单/钱包/支付的货币定义、示例和契约版本。`docs/product.schema.json` 仅是 D 的修订字段参考，不是已冻结的新全项目 OpenAPI。接入前不能把本包直接称为与原 v1 全量兼容。

## 已验证与剩余工作

31 项测试覆盖全部种子读回、英文/HKD/描述、商品查询、SQL 参数绑定、全部硬筛选、预算边界、未知参数、错误种子原子拒绝、重复初始化保留当前值、版本冲突、外键、只读连接、调用方事务、库存不足、并发最后一件、钱包与库存共同回滚、C 初始化接入、初始化失败回滚、备份重置及路径配置，另验证公共 Product 模型注入与旧 CNY 契约冲突不被静默修改。

剩余协作事项：A 修订公共契约并运行 HTTP 详情接口；B 进行推荐排序联调；C 提供正式交易表/钱包初始化、进行完整支付与幂等验收，并共同完成集成后的全系统重置。当前测试里的钱包是临时验证表，不存在于交付的实际数据库中。

## 交给队友与 Git

交付包附带一个已经建好的演示 SQLite 文件，方便立即查看。开发协作时只提交源码、种子、测试和说明；`.gitignore` 已忽略 `var/` 下的运行数据。每个人用同一份源码和种子生成自己的本地库，不在 Git 中互相覆盖 SQLite 文件。
