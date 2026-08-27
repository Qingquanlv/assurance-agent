# 部门管理模块测试需求

测试 vue-fastapi-admin 的部门管理（depts）模块，覆盖 API、E2E、Fuzz 与 Performance 层。

## 范围

- 后端：`/api/v1/dept/*` CRUD + 树形列表
- 前端：`web/src/views/system/dept/index.vue` 部门管理页面

## 目标

验证部门创建、查询、更新、删除及树形结构展示的正确性。

## Benchmark 非交互确认

以下范围和数值由 benchmark owner 预先确认，是本 item 的验收输入，不是模型自行选择的默认值：

- Fuzz：覆盖 `POST /api/v1/dept/create`，重点生成部门名称、排序值和父部门 ID 的边界组合；任何输入均不得导致 5xx，成功响应必须保持项目既有响应契约。
- Performance：覆盖 `GET /api/v1/dept/list` 的部门树查询。
  - 并发用户：10
  - 每秒启动用户：2
  - 持续时间：60 秒
  - P95 响应时间不超过 500 ms
  - 错误率不超过 1%
