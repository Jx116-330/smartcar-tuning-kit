# tcp_tool 桥扩展机制 + headless —— 通用核领域债清偿设计

**日期**：2026-09-14
**状态**：已落地并全量验收（tests/test_bridge_ext.py 44 断言全 PASS；GUI/headless
双路冒烟通过；build 后 `test_frozen_headless.py --require` 双 exe 全 PASS——
含 windowed exe 无控制台场景与旧 dist 约定名升级路径；`run_p0_checks.py`
十套件全 PASS）
**改动对象**：`tuning_tool.py`（核机制 + 删领域代码）、`config_loader.py`
（三态解析）、新增 `bridge_ext.py` / `smartcar_console.spec`、
`config.json` + `profiles/virtual/config.json`（声明键）、`build.bat`、
`tests/`（三件）、README
**上游约束**：`docs/2026-08-13-agent-efficiency-and-web-redesign.md` §2a
三层契约——"通用核禁止被调对象语义字符串"
**不动**：TCP 协议、`/trajectory?since=N` 窗口语义、相机 ACK 正常 dispatch
路径、吞吐行为（队列 2000 / 泵 30 条每轮维持现状）

---

## 0. 动机

设计文档 §2a 早已记账："现有 /trajectory/* /camera/* 是本车历史包袱，标注为
'待迁 profile 扩展'"。本次清偿：核内迁出三块旧车代码（TELPT 轨迹、相机
捕获、ASR 行桥），机制化为 config 声明的**桥扩展**；顺带补 `--headless`
（e2e/服务场景去桌面依赖）。

核洁净的证明不靠"端点 404"，靠**源码禁用字符串门**（§5-8）。

## 1. 扩展机制契约

### 1.1 config 三态（合并前区分）

`config.json` 顶层键 `bridge_extension`：

| 值 | 语义 |
|---|---|
| `"path.py"` | 加载 `app_dir()/path.py`（绝对路径原样），`spec_from_file_location` |
| `null` | 显式关闭（干净核） |
| 缺省（UNSET） | **仅根布局（无 --profile）**时尝试约定名 `app_dir()/bridge_ext.py`；命名 profile 缺键 = 无扩展 |

三态在 `_DEFAULTS` 深合并**之前**判定：`_DEFAULTS` 刻意不含此键，
`Config._raw.get('bridge_extension', UNSET)` 得以区分 null 与缺省。
config_loader 导出 `UNSET` 哨兵单例。

**约定名兼容的适用范围**（收窄的理由）：旧 dist（2026-08 前构建）里
profiles/ 目录根本不存在，"缺键形状"只可能是旧根配置；新根 config.json
显式声明（用户拷根配置建自定义 profile 自然继承显式键）；virtual 显式
`null`。命名 profile 一律不享受静默默认——自定义 profile 缺键 = 无扩展，
与显式 null 行为等价，无需迁移文件。

加载校验 `EXT_API == 1`（**等值**比较，非存在性检查）。任何失败
（缺文件/语法错/EXT_API 不符/init 抛异常）→ 降级为无扩展 + 警告日志 +
`/snapshot` bridge.ext 记原因，桥照常启动。

### 1.2 钩子（扩展模块级函数，全部可选）

```python
EXT_API = 1
def init(app): ...                    # 启动期构造（相机管理器等）
def on_start(app): ...                # TCP start 时,后台线程执行（重导入不占 RX/HTTP 线程）
def on_packet(app, parsed): ...       # merge_telemetry_packet 内调用,持 telemetry_lock
def on_line(app, source, line) -> bool  # True=已消费（ASR 行）,False=继续核 dispatch
def on_frame(app, source, frame): ... # 二进制帧（仅扩展解析器会产生）
def create_stream_parser(app): ...    # 流解析器工厂;None/失败 → 核默认 _LineParser
def shutdown(app): ...                # 桥清理链条最后一步
ROUTES = {('GET', '/x'): fn, ...}     # fn(app, qs, body) -> dict | (code, dict)
```

关键语义：

- **on_line 返回 False 是常态**：相机 CAMMETA/CAMSTAT 与 ACK 回显必须
  流经核的正常 dispatch（cmd_result / batch 匹配），不能被扩展吞掉。
  测试 3（ACK 双路径）专门守护这一点。
- **on_packet 在 telemetry_lock 内被调**，扩展自有状态天然受锁保护；
  `/trajectory` 读取端点同样先取 `app.telemetry_lock`。
- **解析器接口** `feed(data)->[(type,value)] | timed_out()->bool | close()`；
  扩展解析器的协议错误记账内聚在自身 feed/close 内（record 后 re-raise，
  核统一捕获关连接）。
- **扩展禁止 `import tuning_tool`**（脚本态 `__main__` 双导入陷阱）；
  依赖经 app 参数注入（`app.data_dir` / `app.send_command` /
  `app.queue_log` / `app.telemetry_lock`）。
- HTTP 派发在内建路由之后、404 之前；POST body 已由核解析（坏 JSON → 400），
  状态码由扩展返回值保留原端点语义（400/404/500 分支原样迁移）。

### 1.3 核默认 `_LineParser`

无扩展时每个 TCP 连接用纯行解析器：`\n` 定界、rstrip `\r`、
utf-8(replace) 解码、strip、空行丢弃、16384 行帽（超限 =
`StreamProtocolError` → 关连接）。行语义与相机扩展的混排解析器逐条对齐
（camera_capture.py 的行分支），virtual profile 的 csv 帧流经它无感。

## 2. --headless 契约

- **导入门**：tkinter/ttkbootstrap 全部惰性导入；毒化 GUI 库后
  `import tuning_tool` 必须成功且不启动任何相机 worker（测试 9）。
- **变量替身** `_PlainVar`（get/set 接口同 tk 变量），核内读点零改动。
- **遥测泵**：headless 常驻 daemon 线程，节奏沿用 process_queue 自适应
  间隔（100ms 常态 / 50ms 积压）；**泵退出条件独立于 TCP `running`**
  （独立 `_pump_stop`——GUI 模式 Stop TCP 不停泵，与原行为一致）。
- **日志**：headless 恒写 `runtime/bridge_headless.log`（5MB 轮转
  `.log.1`）；stdout 尽力而为（windowed exe 无控制台，写失败静默）。
  这正是冻结态必须验 windowed exe 的原因：console=False 下 stdout/stderr
  不可用，只有日志文件能证明契约成立。
- **副作用保序**：`_write_custom_state_file` 从 GUI 刷新路径提出到
  process_queue 主路径（headless 仍写 custom_state 文件）。

## 3. POST /shutdown 线程契约

```
HTTP 线程:  _json_response({'status': 'shutting down'}) → wfile.flush()
            → app.shutdown_event.set()          # 只置事件,绝不清理
owners 线程: headless = 主线程(wait(event) → 清理 → sys.exit(0))
             GUI    = Tk 泵线程(process_queue 轮询 event → 清理 →
                       root.destroy()——Tk 对象只在 Tk 线程销毁)
```

- **先响应后置事件**（flush 显式调用），客户端必收到应答。
- **清理链条幂等且各步容错**：`pump → tcp → sim → http → ext` 每步独立
  try/except，单步失败记 stderr 不阻断后续。
- Ctrl+C（控制台场景）走同一清理函数。
- 与既有 API 同威胁模型（localhost 工具，`/command` 本就同暴露面；
  默认 `http_host=127.0.0.1`）。

## 4. 打包与旧 dist 升级

- `build.bat`：`bridge_ext.py` 进**无条件覆盖**循环（代码文件先例），
  冻结验收会核对其内容与源码一致；`profiles/` 逐文件 **if-not-exist**（配置
  保留用户修改；不用 robocopy——`/IS` 语义 ≠ 仅缺失复制）；双 spec 构建
  （`YawTuningTool.exe` + `YawTuningToolConsole.exe`）。
- 旧 dist 升级路径：新 exe + 无条件复制的 `bridge_ext.py` + 旧
  config.json（无键）→ 约定名默认加载 → 轨迹/相机功能保持。已发 dist
  的 virtual 配置缺扩展键无需迁移（命名 profile 缺键 ≡ 显式 null）。
- `smartcar.spec` 不动：`camera_capture`/`asr_*` 已在 hiddenimports，
  冻结态下扩展模块的 import 走 bundle；扩展本体经 `spec_from_file_location`
  从 exe 旁加载（免重打包热换，与 dashboard.html/web_dist 同哲学）。

## 5. 验收矩阵（tests/test_bridge_ext.py，44 断言）

1. **5000 点窗口算术**：500/批节奏受控发送 + 轮询 count 确认（队列
   2000/泵 30 条每轮是现行为，突发吞吐不在本套件断言，不为过测试改吞吐）；
   since=0（count=5010/窗口 5000/首点序=10）、since=5 低于窗起点全窗、
   since=4995 末 15 点、clear 后新旧游标均空。
2. **TELPT→SSE**：按 SSE 空行组帧、提取 data: 行、`json.loads` 断言
   type/字段——不做紧凑字符串匹配（json 序列化空格不定）。
3. **相机 ACK 双路径**：status subscribed 变化 **且** cmd_result 携带
   该 ACK（on_line 未吞证明）。
4. **CIMG 帧管线**：CAMMETA+CAMSTAT+帧（`<4sIII`+38400B）分 3 片发送，
   received/latest_sequence 前进。
5. **帧错误**：坏 payload_len → 关连接+protocol_errors 前进+桥存活可再连；
   断连半帧 → partial 计数；半帧 >5s → 超时关连接。
6. **扩展加载失败**：缺文件 / 语法错 / EXT_API=2 / init 抛异常 → 全部
   failed 降级 + 桥存活 + 端点 404 + snapshot 记原因。
7. **配置三态**：缺键根布局→约定名加载 200；显式 null→404；命名 profile
   缺键→404。
8. **源码禁用字符串门**（tuning_tool.py 全文）：
   `TELPT TELPTDUMP CAMMETA CAMSTAT CIMG CAMERA_MAGIC
   START_STREAM_CAMERA_FRAME STOP_STREAM_CAMERA_FRAME "ASR " trajectory
   camera CameraCapture MixedCameraStream`
   （不含 `bridge_ext`——会误伤通用标识符 `bridge_extension`）。
9. **无 GUI 导入门**：毒化 tkinter/ttkbootstrap/tkinter.messagebox 后
   import 成功且无相机 worker。
10. **headless 生命周期**：/shutdown 5s 内退出码 0；bridge_headless.log
    非空；custom_state 文件已写（副作用保序）。
11. **持久化往返**：save→list→load→delete + name 清洗 + /camera/save；
    边界 = 坏 JSON→500、路径穿越→400、不存在→404。

GUI 路径（非 headless）手动冒烟：窗口启动 + HTTP 可用 + /shutdown 经
Tk 线程清理退出码 0。

## 6. 冻结态验收（tests/test_frozen_headless.py）

- **双 exe 都验**：windowed `YawTuningTool.exe` 是 console=False 的目标
  场景（无 stdout 下日志文件契约 + /shutdown 退出）；Console exe 同套
  + stdout 有内容。
- 冻结态走**约定名兼容路径**（dist 旧 config 无键 + exe 旁 bridge_ext.py）
  ——升级场景即验收场景。
- 日常回归：缺产物/过期 → 打印 `FROZEN-SKIP` 清单 + `FROZEN-RESULT: skip`
  行，退出 0；**run_p0_checks.py 解析该行分级汇总**："源码测试通过；
  冻结态 SKIP"，不把 SKIP 报成 PASS。
- 发布门：`python tests/test_frozen_headless.py --require`（或 `RELEASE=1`）
  → 任一产物缺失即 FAIL。build.bat 末尾提示该命令。
- 新鲜度门：exe 不得早于对应源码/spec，`dist/bridge_ext.py` 必须与源码
  逐字节一致，运行后还要核对 `/snapshot` 的 `bridge.version`；任一不符都
  必须跳过/重建，发布模式直接失败。

## 7. 遗留与后续

- **IMU 状态横幅逻辑**（`update_status_banner` 的 bias_ok/gxyz 语义）
  仍在核内：纯 GUI 内部展示、零 API 面，留作后续小额债务。
- `/paths/*` 端点保留核内：离线数据目录服务，无领域字符串，新前端
  CompareTab 在用。
- 解析器错误日志文案微调（"camera protocol error"→"stream error" 等）：
  仅日志文本，非契约。
- windowed exe headless 的硬杀退出 = 文档化兜底（daemon 线程，最坏丢
  尾部缓冲遥测）；服务场景用 Console 构建 + /shutdown。
