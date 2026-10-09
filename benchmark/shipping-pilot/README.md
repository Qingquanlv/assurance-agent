# 发货管理 HTTP Mock 业务试点

在现有 vue-fastapi-admin 中增加“发货管理”：新建发货单、申请运单、查询受理结果、查看处理记录。User、登录和原有模块保持原实现。沿用 Vue 3、Naive UI、FastAPI、Tortoise 以及锁定依赖。

```text
Vue 发货页面 → 主应用 :19999 → HTTP 物流服务 :20000
                   ↓                  ↓
             pilot.sqlite3      carrier.sqlite3
              发货单/历史         运单/幂等记录
```

物流服务是可独立运行的本地示例实现：校验寄件数据、持久化运单、按业务单号去重、支持查询和进程重启。它没有对接商业快递公司，不实际寄件。正常模式不使用 Mock，也不依赖 Mock 才能启动。

## 启动与页面

从本仓库根目录执行：

```bash
uv run --project benchmark/vue-fastapi-admin python benchmark/shipping-pilot/run.py --frontend
```

- 前端：<http://127.0.0.1:13100/shipping>
- 主应用 API：<http://127.0.0.1:19999/docs>
- 物流服务 API：<http://127.0.0.1:20000/docs>
- 全新试点数据库账号：`admin / 123456`。

进入“发货管理”，新建发货单，再在详情里点击“申请运单”。受理成功后会显示 `WB-…` 运单号。物流服务的示例业务规则为单次最多 100 件；填写 101 件可验证业务拒绝及页面原因提示。

启动器仅监听回环地址；`Ctrl-C` 停止它启动的子进程。默认运行目录是 `results/local/`，可通过 `--runtime` 指定；数据库、日志与权限为 0600 的 `credentials.json` 均在该目录，重启沿用。主应用与物流服务使用不同数据库，原 checkout 的数据库不受影响。

可用 `--backend-port`、`--carrier-port`、`--frontend-port` 换端口。不加 `--frontend` 只启动后端。占用的端口会报错，不会杀死其他服务。

## 页面和权限

一个列表页包含单号/状态筛选、分页、创建弹窗、详情抽屉及处理记录。创建弹窗使用稳定的 `client_ref`，网络重试不会重复创建同一草稿。申请运单和查询结果使用同一业务单号；提交后不提供修改寄件内容的入口，需要修正内容时新建发货单。

启动时增量补充发货菜单和五个 API：管理员可写，普通用户默认只读。已有数据库无需清空菜单或角色；后续启动不会重新授予已显式撤销的授权。自定义角色通过原有角色管理页面授权。原鉴权行为未变，本试点仅用于本地验收。

## HTTP Mock 验收边界

Mock 的对象是主应用调用的物流接口，不是主应用自己的发货接口。保持前端、主应用、业务数据库真实运行，将物流地址指向 HTTP Mock：

```bash
uv run --project benchmark/vue-fastapi-admin python benchmark/shipping-pilot/run.py \
  --frontend --carrier-url http://127.0.0.1:18080
```

该模式不启动真实物流进程，也不调用 Mock 的健康检查接口。使用与该运行目录 `credentials.json` 中 `CARRIER_TOKEN` 匹配的服务凭据配置 Mock；前端不接触服务凭据。切换模式前先停止旧启动器，避免端口冲突。建议真实模式和 Mock 模式使用不同 `--runtime`，避免混用受理记录。

主应用对外请求：

```http
POST /shipments
Authorization: Bearer <CARRIER_TOKEN>
Idempotency-Key: SP-<业务单号>
Content-Type: application/json

{"reference":"SP-<业务单号>","recipient":"测试收件人","phone":"13800138000","address":"上海市测试路1号","item_name":"文具","quantity":2}
```

成功响应（字段严格校验，reference 必须与请求相同）：

```json
{"reference":"SP-<业务单号>","waybill_no":"WB-001","status":"ACCEPTED"}
```

受理结果查询使用 `GET /shipments/{reference}`，同样携带服务认证，成功响应结构一致。业务拒绝返回 HTTP 422 和 `{"code":"QUANTITY_LIMIT","message":"单次寄件最多 100 件"}`；另一个允许的拒绝代码为 `ADDRESS_UNSUPPORTED`。不符合约定的错误响应会保留为待确认。

| 物流行为 | 主应用状态与页面结果 |
| --- | --- |
| 正常受理 | ACCEPTED / 已受理，保存运单号 |
| 网络错误、超时、5xx | 最多 3 次，等待 50ms、100ms；耗尽为 UNKNOWN / 待确认 |
| 响应丢失但物流已落库 | 同一单号 GET 查询恢复为已受理，不重建单据 |
| 格式错误、损坏压缩、单号不匹配、重定向、认证失败 | 待确认，不冒充成功或业务拒绝 |
| 约定的 HTTP 422 业务拒绝 | REJECTED / 已拒绝，展示原因，不重试 |
| 查询 404 | 待确认，允许使用原业务单号重新申请 |
| 并发或重复点击申请 | 数据库操作租约限制并发；物流唯一约束保证同一业务单号只有一张运单 |

状态包括 DRAFT / 待申请、PENDING / 处理中、UNKNOWN / 待确认、ACCEPTED / 已受理、REJECTED / 已拒绝。超时不等于物流拒绝。业务操作的 HTTP 200 表示结果已持久化，是否受理必须检查响应中的 `data.status`。

`CARRIER_TIMEOUT` 默认每次 2 秒，允许 `(0, 5]` 秒；单次业务操作总截止时间 15 秒，数据库操作租约 30 秒。进程异常退出后，“处理中”单据可在租约到期后查询或重新申请；不需要手工改库。页面自动刷新保留当前页码。历史记录保存最终状态、原因和调用尝试次数。

## 测试与重建

```bash
uv run --project benchmark/vue-fastapi-admin python -m pytest \
  benchmark/shipping-pilot/test_shipping.py -q -o addopts=''
```

测试启动真实 Uvicorn 和临时数据库；故障响应来自独立 HTTP 服务器。包含真实物流先落库再丢失响应的代理场景、并发申请、重启持久化、租约恢复、权限撤销保留、旧数据库升级，以及启动器端口清理。

本次验证（2026-10-09）：试点 **21 passed**，仓库回归 **4842 passed / 18 skipped**；Ruff、格式、导入契约、生成声明检查、三个 wheel 冒烟、改动前端文件 ESLint 和前端生产构建通过。Pyright 为 0 errors / 1 warning（现有 `jsonschema` 源码解析警告）。前端 ESLint 使用已安装的 pnpm 存储依赖解析，未新增依赖。浏览器完成真实新建与申请运单，并验证第二页自动刷新不会跳回第一页；截图在 `results/local/real-waybill.jpg`。JUnit 结果在 `results/acceptance.xml`。

SUT 被父仓库忽略，因此其改动保存在 `shipping.patch`；物流服务和工具直接保存在本目录。固定基线为本机现有 SUT commit `cc5f05e9148a831d786ac7658bc26bf3bc54ea77`，不保证上游公开仓库含该 commit。另一 worktree 可这样重建：

```bash
uv run python benchmark/shipping-pilot/prepare.py --source /absolute/path/to/vue-fastapi-admin
uv sync --frozen --project benchmark/vue-fastapi-admin
pnpm --dir benchmark/vue-fastapi-admin/web install --frozen-lockfile
uv run --project benchmark/vue-fastapi-admin python benchmark/shipping-pilot/run.py --frontend
```

准备脚本拒绝覆盖已有 SUT 源码，可用 `--destination` 指定空目录。只复制固定版本源码，不复制原数据库、未提交改动或旧测试产物。

本轮不包含支付、库存、回调、实际快递寄送或生产部署。提供独立试点验收入口，尚未把服务编排接入 `benchmark/assurance-product/run_item.py`，也未执行依赖 OpenCode 的 `aa run`。
