# API 管理模块测试需求

测试 vue-fastapi-admin 的 API 管理（apis）模块，覆盖 API 与 E2E 层。

## 范围

- 后端：`/api/v1/api/*` — list、get、create、update、delete、refresh
- 前端：`web/src/views/system/api/index.vue` — API 列表、搜索、新建/编辑、删除、刷新 OpenAPI 注册

## 目标

验证 API 元数据的 CRUD、分页与条件查询，以及 refresh 同步路由注册表的行为。

## Out of Scope (initial)

- 角色/菜单授权绑定（由角色管理模块覆盖）
- 非 admin 角色的 E2E 细粒度权限矩阵（API 层覆盖 unauthorized 即可）
