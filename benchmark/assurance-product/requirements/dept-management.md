# 部门管理模块测试需求

测试 vue-fastapi-admin 的部门管理（depts）模块，仅覆盖 API 层。

## 范围

- 后端：`/api/v1/dept/*` CRUD + 树形列表

## 目标

验证部门创建、查询、更新、删除及树形结构展示的正确性。

## Benchmark 非交互确认

以下 API 范围由 benchmark owner 预先确认，是本 item 的验收输入，不是模型自行选择的默认值：

- 覆盖 `POST /api/v1/dept/create` 的创建与输入边界。
- 部门名称长度不超过 20 个字符；21 个及以上字符必须被拒绝。
- 覆盖 `GET /api/v1/dept/list` 的部门树查询。
- 覆盖部门更新、删除以及管理权限校验。
