# 角色管理模块测试需求

测试 vue-fastapi-admin 的角色管理（roles）模块，覆盖 API 与 E2E 层。

## 范围

- 后端：`/api/v1/role/*` — list、get、create、update、delete、authorized（读/写菜单与 API 权限）
- 前端：`web/src/views/system/role/index.vue` — 角色列表、搜索、新建/编辑、删除、权限配置（菜单 + API）

## 目标

验证角色 CRUD、名称唯一性、分页查询，以及菜单/API 授权读写（M2M）核心路径。

## Out of Scope (initial)

- 用户-角色绑定（由用户管理模块覆盖）
- 非 admin 角色的 E2E 细粒度权限矩阵（API 层覆盖 unauthorized 即可）
