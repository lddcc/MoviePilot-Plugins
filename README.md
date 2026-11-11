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
- **超时可配**，详情页显示最近 50 条发送记录（时间 / 事件 / 结果）。
- 序列化事件数据时生成新对象，**不原地改写原事件数据**（官方版会把事件里的对象就地替换成 dict，后续处理器拿到的数据被改动）。

请求格式与官方版一致：

- POST：`Content-Type: application/json`，body 为 `{"type": "<事件类型>", "data": {...}}`
- GET：查询参数 `type` 与 `data`（JSON 字符串）

## 许可

插件改写自 [jxxghp/MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)，按 GPL-3.0 发布。
