# User 创建：API 与 SQLite 独立验证

本规格回链原始需求 `RET-user-management`，并将本次机器验证范围收窄到一个 User 创建流程。

仅覆盖 API 层的 User 创建，POST /api/v1/user/create；不宣称整个 User 管理模块已覆盖。
使用本轮冻结的 username、email、is_active、is_superuser、dept_id，认证由执行 host 提供。
机器计划与生成测试必须使用 api_db.v1，只有 API family。必须通过已安装的 execute_case 桥接发送一次创建请求；请求、DB 观察、判定均由父级 host 负责。

本需求的八条业务断言：

1. HTTP status 等于 200。
2. HTTP JSON envelope 的 code 等于 200。
3. SQLite user 表中按本轮唯一 username 查询，行数等于 1。
4. 落库 username 等于冻结输入 username。
5. 落库 email 等于冻结输入 email。
6. 落库 is_active 等于冻结输入 is_active。
7. 落库 is_superuser 等于冻结输入 is_superuser。
8. 落库 dept_id 等于冻结输入 dept_id。

不得以 HTTP 成功推断事务提交，不得从 DB 反向生成预期值。SQLite 独立连接必须在 HTTP 请求结束后观察。
