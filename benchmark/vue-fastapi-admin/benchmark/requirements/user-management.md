# Requirement: 用户管理模块

测试用户管理模块（User Management）。

## Scope

- 后端 `/api/v1/user/*`：list、get、create、update、delete、reset_password
- 前端 `web/src/views/system/user/index.vue`：用户列表、搜索、新建/编辑弹窗、删除、角色与部门绑定
- 覆盖 CRUD 核心路径、权限、邮箱唯一性、角色绑定、部门关联、密码重置

## Out of Scope (initial)

- 跨模块深度集成（除 dept_id / role_ids 绑定外）
- 非 admin 角色的 E2E 细粒度权限矩阵（API 层覆盖 unauthorized 即可）
