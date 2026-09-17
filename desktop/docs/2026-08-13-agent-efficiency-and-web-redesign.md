# tcp_tool 改造设计 — agent 效率 + Web 前端工程化

**日期**：2026-08-13
**状态**：待评审（未动工）
**改动对象**：`E:\ads\tcp_tool\`（tuning_tool.py / session_driver.py / config.json / 新增前端工程）
**不动**：TCP 协议本身、固件侧命令集、PyInstaller 打包链路（仅加静态资源规则）
**前身文档**：`docs/superpowers/specs/2026-05-22-dashboard-cyberpunk-hud-design.md`（旧 dashboard 的 UI 设计，布局与视觉决策仍有效，本文档取代其"单文件 HTML"实现前提）

---

## 1. 目标与痛点

| 痛点 | 现状 | 目标 |
|---|---|---|
| agent 调用效率低 | 每个动作 = 一次 Python 进程启动 + 一次 HTTP 往返；设 10 个参 = 10+ 次调用 | 一次调用完成一批操作并返回结构化结果 |
| UI 不符合设计需求 | dashboard.html 108KB 手写单文件，含已删参数的残留控件（README 已标注），控件与固件参数脱节 | 工程化前端，调参面板由 schema 自动生成 |
| （明确暂缓）安全分级 | FORBIDDEN 硬编码在 session_driver | 本次不做，仅在 /batch 中保留扩展位 |

非目标：不引入 MCP server；不改变"桥独占车端 TCP 8080"的单写者架构；agent 永远不直连车。

## 2a. 通用性边界（常驻约束，2026-08-13 用户指定）

本工具未来要服务多个被调对象（不限于当前车端固件）。策略：**现在不抽象框架，
但所有新代码必须遵守三层契约**，保证未来 profile 化是"加机制"而不是"拆硬编码"：

1. **通用核**（tuning_tool.py 桥）：只认识 prefix、schema、命令字符串。
   铁律——核里**禁止出现被调对象语义的字符串**（MISSION、TELTURN、舵机……）。
   现有 `/trajectory/*` `/camera/*` 是本车历史包袱，标注为"待迁 profile 扩展"，
   本次不动，但新功能不许再走这条路。
2. **Profile 约定**（只定约定，暂缓实施加载机制）：未来每个被调对象一个目录
   `profiles/<name>/{config.json, control_schema.json, recipes/, 领域脚本}`；
   `control_schema.json` 的 `schema_hash` 作版本锚。等第二个被调对象真实出现时
   再加 `--profile` 切换，届时需求才清楚（YAGNI）。
3. **前端零领域知识**：一切参数控件、指标、命令入口从 config/schema 生成。
   换被调对象 = 换 profile 文件，UI 自动跟随。

判别规则：写任何新代码前问"这行知识换一个被调对象还成立吗？"不成立就只许
进 config/schema/领域脚本，不许进通用核。

## 2. 现状盘点（已核实，2026-08-13 读源码确认）

- **桥 HTTP 端点**（tuning_tool.py `do_GET/do_POST`，~L628 起）：
  `/latest` `/snapshot` `/history` `/config` `/status` `/trajectory*` `/paths/*` `/camera/*`，
  POST 仅 `/command` + trajectory/camera 管理。
- **`/command` 是 fire-and-forget**：只发不收，ACK 落在全局唯一的 `latest_command_result`
  （按固件回包里的 `cmd` 回显关联，L387-397）。ACK 关联目前由 **session_driver 客户端轮询
  `/latest` 完成**——这正是"一次调用只干一件事"的结构性根因。
- **`/snapshot` 已存在但是遥测中心视角**（latest_parsed + custom_state + cmd_result），
  不含"全部当前参数值"（参数只在订阅流上报时可见）。
- **`control_schema.json` 是闲置的权威参数 schema**（fields/enums/field_sets/owners/
  cross_field_rules/schema_hash，15 个顶层键），目前**没有任何 Python 或前端代码消费它**。
- **遥测到前端靠轮询 `/latest`**；无推送通道。
- exe 热加载约定：`app_dir()/dashboard.html` 存在则优先于 PyInstaller 内嵌副本（L633），
  改 UI 拷文件即生效。新前端必须保留同等体验。

## 3. 总体路线

```
Phase A  agent 效率（纯 Python，不动前端）
  A1  桥内命令队列 + /batch 端点
  A2  session_driver --do recipe.json + --json + 退出码
  A3  /snapshot 升级（链路/连接/桥自状态聚合）
Phase B  实时推送（前端地基）
  B1  SSE /events 遥测推送（stdlib 实现，零新依赖）
Phase C  前端工程化
  C1  Vite + React + TS 脚手架，schema 驱动调参面板
  C2  桥 serve 静态产物 + 打包规则
```

每期独立可验证、可单独交付；B 必须在 C 的正文开发前完成。

---

## 4. Phase A 详细设计

### 4.1 桥内命令队列（/batch 的地基）

当前 ACK 关联在客户端，多调用方并发时本质不可串行化。把串行化收进桥：

- `app` 新增 `cmd_queue: queue.Queue`、`cmd_cond: threading.Condition`、
  `pending_ack: dict | None`。
- RX 线程在 `_handle_special_packet` 收到 ACK/ERR 时，若 `pending_ack` 的 `cmd`
  与回显匹配（大小写不敏感、去空白），置结果并 `notify`。**保留**现有
  `latest_command_result` 写入（旧客户端行为不变）。
- `/command` 改为入队后立即返回（行为兼容）；`/batch` 是唯一等待 ACK 的调用方。
- 同一时刻只允许一个 /batch 执行（`batch_lock`），其余返回 409。

### 4.2 POST /batch

请求：
```json
{
  "commands": [
    {"cmd": "SET MISSION ADAPT_LA 1", "expect": "ACK", "timeout_ms": 2500},
    {"cmd": "GET STREAMS"},
    {"cmd": "START STREAM MISSION 100", "expect": "ACK"}
  ],
  "stop_on_error": true
}
```
- `expect` 缺省 `"none"`（fire-and-forget）；`"ACK"` 时桥等待匹配回包。
- `timeout_ms` 缺省 2500，上限 10000。

响应（HTTP 200 恒成立，成败看逐项）：
```json
{
  "results": [
    {"cmd": "SET MISSION ADAPT_LA 1", "status": "ack", "data": {...}, "elapsed_ms": 87},
    {"cmd": "GET STREAMS", "status": "sent"},
    {"cmd": "START STREAM MISSION 100", "status": "timeout", "elapsed_ms": 2500}
  ],
  "ok": false
}
```
`status ∈ sent | ack | err | timeout | skipped`（stop_on_error 触发后续为 skipped）。

安全扩展位：请求体预留 `"role": "agent"` 字段，本期不校验，未来接安全分级时
不用改协议。

### 4.3 session_driver `--do` + `--json` + 退出码

配方文件（JSON）：
```json
{
  "label": "adapt_la_trial",
  "set": ["SET MISSION ADAPT_LA 1", "SET TURN KP 3.2"],
  "streams": [["MISSION", 50], ["DRIVE", 50], ["STATS", 1000]],
  "capture_s": 0,
  "report": true
}
```
- `set` 经 `/batch` 一次下发（ACK 验证）；`streams` 订阅；`capture_s=0` 表示只布防
  等用户按菜单发车（沿用现有铁律：脚本永不让车动）。
- `--json`：所有输出（设参结果 / 报告指标 rev、|dStr|、duty_sat、lostsrc…）改吐
  单个 JSON 对象到 stdout，人类可读文本移到 stderr。
- 退出码：`0` 成功 / `2` ACK 超时 / `3` 链路失败 / `4` 配方非法 / `5` 命中禁发命令。

### 4.4 /snapshot 升级

保持**被动**（不发任何车端命令，快照必须无副作用）。响应聚合：
```json
{
  "link":   {"running": true, "mode": "server", "connections": 1, "hb_age_ms": 320},
  "latest": {...}, "custom_state": {...}, "packets": {...},
  "bridge": {"version": "...", "schema_hash": "...", "uptime_s": 1234},
  "_ts": ...
}
```
"全部参数当前值"不在此实现——agent 需要时用 /batch 发 `GET xxx`，
不污染快照的被动性。

---

## 5. Phase B：SSE 实时推送

- 新端点 `GET /events`（Server-Sent Events）：每个遥测包解析后推送一帧
  `data: {"type":"TELTURN","fields":{...},"ts":...}`；链路状态变化推
  `event: link`。
- 选 SSE 不选 WebSocket：遥测是单向流，stdlib `ThreadingHTTPServer` 可直接
  长连接写出，零新依赖；命令仍走 POST，不需要双向。
- 背压：每客户端一个有界队列（满则丢帧并计 `dropped`），客户端断开后清理。
- `/latest` 轮询保留（旧 dashboard 与脚本不受影响）。

## 6. Phase C：前端工程化

### 6.1 技术栈

- **Vite + React + TypeScript**；曲线用 **uPlot**（Canvas，~40KB，遥测帧率下
  性能远超 ECharts）；状态管理用 zustand 或裸 context（规模小，不上 Redux）。
- 工程目录 `E:\ads\tcp_tool\web\`，构建产物 `web/dist/`，**不污染仓库根目录**。

### 6.2 核心设计决策：schema 驱动

- 桥新增 `GET /schema` → 直接返回 `control_schema.json`。
- 调参面板**从 schema 的 fields/enums/field_sets/owners 自动生成控件**
  （枚举→下拉、数值→带范围的输入、field_set→分组页签），禁止手写参数控件。
  这根治旧 dashboard "固件删了参数、UI 还留着按钮"的脱节问题。
- `cross_field_rules` 用于前端即时校验提示（不强制，权威仍在固件）。

### 6.3 布局（四场景，沿用 2026-05-22 文档的视觉方向）

```
┌ HEADER: 链路状态 · agent 状态 · schema_hash · RX/TX ─────────────┐
├──────────────┬───────────────────────────────────────────────────┤
│ 调参面板      │ 主区页签: [实时曲线 uPlot] [轨迹XY] [趟对比分析]    │
│ (schema生成) │                                                    │
├──────────────┴───────────────────────────────────────────────────┤
│ CONSOLE（命令/ACK 滚动日志）                                       │
└───────────────────────────────────────────────────────────────────┘
```
- 数据源全部走 SSE（遥测）+ /batch（命令），不再轮询 /latest。
- 趟对比分析页消费 `session_driver --json` 的结构化输出与 `/paths/*` 现有端点。
- Cyberpunk HUD 视觉语言沿用旧设计文档 §4 的规格。

### 6.4 serve 与打包

- 桥静态托管优先级：`app_dir()/web_dist/`（热加载，拷目录即生效）→
  PyInstaller 内嵌副本。与现有 dashboard.html 约定同构。
- `smartcar.spec` 加 `web/dist` 数据目录；`build.bat` 前先跑 `npm run build`
  （无 node 环境时允许跳过并用旧产物，打 warning）。
- 旧 `dashboard.html` 保留为回退（`/dashboard` 仍可用），新前端挂 `/` 与 `/ui`。

## 7. 验收标准（分期）

- **A**：单条 curl 完成 "设 3 参 + 订阅 2 流"，逐项 ACK 状态正确；
  `session_driver --do recipe --json` 输出可被 `jq`/`json.load` 直接解析；
  旧 dashboard 与旧脚本行为无回归。
- **B**：浏览器 EventSource 收到 ≥ 订阅频率 90% 的帧；断开/重连不泄漏线程。
- **C**：删 schema 中一个字段，调参面板对应控件自动消失（回归测试）；
  exe 打包后 `/ui` 可用；热加载目录替换生效。

## 8. 风险

| 风险 | 缓解 |
|---|---|
| 桥内等 ACK 阻塞 HTTP 线程 | ThreadingHTTPServer 每请求一线程；/batch 互斥锁防并发；超时上限 10s |
| 固件 ACK 回显大小写/空白差异 | 匹配前 normalize；timeout 兜底，不会死等 |
| SSE 在 exe（PyInstaller）下的长连接行为 | B 期先用源码跑验证，再打包验证；保留轮询回退 |
| web/ 工程引入 node 依赖 | 仅构建期需要；运行期仍是纯 Python exe + 静态文件 |
