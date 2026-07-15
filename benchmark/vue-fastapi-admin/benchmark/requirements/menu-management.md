# 菜单管理模块测试需求

测试 vue-fastapi-admin 的菜单管理（menus）模块，覆盖 API 与 E2E 层。

## 范围

- 后端：`/api/v1/menu/*` — list（树形）、get、create、update、delete
- 前端：`web/src/views/system/menu/index.vue` — 菜单树、新建/编辑、删除、父子层级与排序

## 目标

验证菜单 CRUD、树形列表展示、父子关系与「有子菜单不可删」等业务规则。

## Out of Scope (initial)

- 角色-菜单授权绑定（由角色管理模块覆盖）
- 非 admin 角色的 E2E 细粒度权限矩阵（API 层覆盖 unauthorized 即可）
