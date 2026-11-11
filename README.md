# MoviePilot-Plugins

个人 MoviePilot V3 插件仓库。

在 MoviePilot「设定 → 插件 → 插件市场仓库」里加入：

```text
https://github.com/lddcc/MoviePilot-Plugins/
```

## 插件

### Webhook增强版（WebhookPlus）

基于官方 [Webhook](https://github.com/jxxghp/MoviePilot-Plugins/tree/main/plugins/webhook) 插件改写，事件发生时向第三方地址发送请求。

相比官方版：

- **自定义请求头**：每行一个 `Key: Value`，可以带 `Authorization: Bearer xxx` 之类的鉴权头，直接对接 Dagu webhook 等需要 Token 的接收端。
- **事件类型过滤**：多选要发送的事件（如只发 `transfer.complete` 整理完成），不选则发送全部事件。
- **标题过滤**：「标题包含 / 标题排除」两个正则，只作用于事件数据里带 `title` 的事件（如插件通知 `notice.message`），
  其它事件不受影响；例如只转发 `【115网盘】.*(STRM|删除)` 的通知、排除 `失败|异常`。正则写错会记日志并视为不过滤。
- **异步发送**：处理器是协程，跑在宿主主事件循环上，复用宿主共享的 HTTP 连接池；先过滤再序列化，被过滤的事件几乎零开销。
- **超时可配**，详情页显示最近 50 条发送记录（时间 / 事件 / 结果）。
- 序列化事件数据时生成新对象，**不原地改写原事件数据**（官方版会把事件里的对象就地替换成 dict，后续处理器拿到的数据被改动）。

请求格式与官方版一致：

- POST：`Content-Type: application/json`，body 为 `{"type": "<事件类型>", "data": {...}}`
- GET：查询参数 `type` 与 `data`（JSON 字符串）

### Dagu任务面板（DaguPanel）

在 MoviePilot 里看 [Dagu](https://github.com/dagucloud/dagu) 任务的运行情况，并一键运行。只做展示和触发，任务本身仍在 Dagu 里执行。

- **仪表板组件**「Dagu 任务」：每个 DAG 一行，显示最近一次运行的状态、开始时间、耗时、触发方式、下次运行时间，
  以及结果摘要（最后执行那一步 stdout 的最后一行，失败时取 stderr 里的失败说明），带「立即运行」按钮。
- **详情页**：每个 DAG 一张运行记录表（状态 / 开始 / 耗时 / 触发与重试次数 / 结果），开始时间链接到 Dagu 的运行详情。
- **立即运行**：调 Dagu `POST /api/v1/dags/<dag>/enqueue`，任务正在跑时排在后面；只允许配置里列出的 DAG。

配置：Dagu 地址、API Key（在 Dagu「设置 → API Keys」创建，`operator` 角色即可）、要显示的 DAG（每行 `DAG名=显示名`）。

性能：

- 不注册定时服务，打开页面时按需拉取，结果放进宿主缓存（MoviePilot 配了 Redis 就在 Redis 里）：
  DAG 列表缓存到最早的下次运行时间（30 秒～1 小时），运行列表 15 秒（有运行中 / 排队中的运行时 5 秒），
  **已结束运行的摘要缓存 7 天**——同一次运行的日志只取一次。点「立即运行」后立刻让该 DAG 的运行列表缓存失效。
- 多个 DAG 的列表与摘要用小线程池并发请求，keep-alive 复用连接；Dagu 连不上时 30 秒内直接显示错误，不逐个等超时。

## 许可

插件改写自 [jxxghp/MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)，按 GPL-3.0 发布。
