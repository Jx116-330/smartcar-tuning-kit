# 接入契约 v1(TT 轻量线协议)— 最小被调对象契约

**日期**:2026-08-14
**状态**:v1 已落地(桥解析 + 虚拟被调对象 + virtual profile 演示链)
**适用**:新被调对象(嵌入式设备)接入 tcp_tool 时的固件侧/协议侧约定
**不动**:原车 TCP 协议(`frame_mode: "kv"`,默认 profile 行为完全不变)

---

## 1. 目标与取舍

| 维度 | 决定 | 理由 |
|---|---|---|
| 传输层 | **TCP** | 免排序/免重传代码;断线即重连;SET 幂等 = 天然容错 |
| 编码 | **文本行**,位置式 CSV | curl/telnet 可调试;固件侧一个 snprintf 循环;局域网带宽无压力 |
| 帧定界 | **换行分隔**(`\n`,`\r\n` 都收) | 固件侧最简实现 |
| 订阅机制 | **无**。通道常开,`RATE` 调速率 | 砍掉订阅表/字段级订阅,固件只留一个通道调度器 |
| 命令回执 | **可选**(`!<seq>` 请求才回) | 参数正确性靠"遥测回读"验证,不靠 ACK |
| 心跳 | **无**(可选 D 帧通道自行实现) | 桥看 socket 断连;被调对象无保活义务 |
| 重传/补帧 | **无** | 丢帧 = UI 断点,历史记录自然留缺口 |

**容错语义**:任何一条线(命令/帧)丢了都不会卡死任何一方。命令幂等可重发;
参数值从遥测读回验证;链路断开双方各自重连。

---

## 2. 线格式

### 2.1 下行(PC 桥 → 固件),每行一条命令

```
SET <key> <value>[ !<seq>]        # 设参。幂等。
RATE <chan> <hz>[ !<seq>]         # 通道速率,hz=0 关闭该通道。0~1000。
GET <key>[ !<seq>]                # 读参。固件回一条 P 帧(异步,不进 ACK 关联)。
<领域命令字符串>[ !<seq>]          # 自由透传,固件自己解析。
```

- `!<seq>`:0~2^31-1 整数,桥在 `/batch expect=ACK` 时自动附加。**带了就必须回**
  `OK,<seq>` 或 `ERR,<seq>`;**没带就静默**(可选自由回复,不得干扰)。
- 命令名大小写由固件自行决定是否敏感;契约不做要求,profile 文档写清楚即可。

### 2.2 上行(固件 → PC 桥),每行一帧

```
D,<chan>,<t_ms>,<v1>,<v2>,...     # 遥测帧,位置式
OK,<seq>                          # 命令成功回执
ERR,<seq>[,<reason>]              # 命令失败回执
P,<key>,<value>                   # GET 的应答帧(异步,非 ACK)
```

- `<t_ms>`:固件单调毫秒(建议 32 位回绕即可,桥只用于排序/心跳年龄,不跨包对拍)。
- `<chan>` 与列序、字段名、单位**由 profile 的 config.json `protocol.channels` 定义**;
  固件按自己列序发,PC 侧按 schema 列序映射——两边的"字段表"是一份文档,不是运行时协商。
- 多余的列忽略,缺失的列不填。**没有运行时 schema 协商**,接入检查靠人 + 合规自测脚本。
- `D`/`OK`/`ERR`/`P` 是契约保留帧前缀;profile 的 `telemetry_prefixes`/
  `response_prefixes` 分别登记它们(桥核仍是零领域语义,prefix 全部配置驱动)。

### 2.3 数值口径

- 数值用十进制文本(浮点 `%f`/`%g` 均可),`int`/`float` 由桥自动判定。
- 单位/标定写进 profile 的 control_schema.json(`unit` 字段),协议层不带单位。

---

## 3. 桥的职责边界(通用核零语义铁律)

- 桥只认识:`D,` `OK,` `ERR,` `P,` 的 prefix、channel 列序表、命令字符串。
- **桥不解析 SET/RATE 的语义**,不做参数合法性校验(权威在固件与 schema 文档)。
- `frame_mode: "csv"` 是第二种解析模式;默认 `"kv"` 保持原车行为。
- `/batch expect=ACK` 在 csv 模式下自动附加 `!<seq>` 并按 seq 匹配回执;
  kv 模式仍按 cmd 回显匹配(旧行为不变)。

---

## 4. 参数读回验证(agent 的正确性口径)

契约**不依赖 ACK 保证设参生效**。约定模式:

1. 被调对象设一个 `params` 通道(建议 1~10Hz),持续广播**当前生效参数值**。
2. agent 设参后读遥测中的 params 帧,值到位即验证通过。
3. `GET <key>` 只作交互式排查用(P 帧落在桥 console/SSE response 流)。

---

## 5. Profile 布局(换工程 = 换文件)

```
profiles/<name>/
  config.json            # 桥配置:frame_mode/channels/UI 面板/quick_commands
  control_schema.json    # 参数 schema(schema_hash 版本锚),前端调参面板数据源
  recipes/               # session_driver --do 的配方(可选)
  <领域脚本>              # 该对象的 PC 侧工具(可选)
  README.md              # 该对象的接入说明(命令表/参数表/单位)
```

- 启动:`python tuning_tool.py --profile <name>`(exe 同:`YawTuningTool.exe --profile <name>`)。
- exe 部署:把 `profiles/` 目录拷到 exe 同目录即热载,无需重打。
- 无 `--profile` 时行为与今天完全一致(根目录 config.json + control_schema.json)。

---

## 6. 新被调对象接入检查清单

1. 固件实现三条义务:**行 RX 缓冲**(按 `\n` 切行)、**通道调度**(按 `RATE` 推 D 帧)、
   **`!<seq>` 回执**(OK/ERR)。→ **直接放 `firmware_kit/tt_link.h` 进工程即可**
   (参数表 + 通道表 + 两个钩子,见 `firmware_kit/README.md`;参数变量本身的
   改造条件=可写标量 + 跨任务可见 + 生效时机,清单同见 README §2)。
2. 写 `profiles/<name>/config.json`:`protocol.channels` 列序表必须与固件**逐列一致**。
3. 写 `control_schema.json`:参数 id/范围/单位/默认值,schema_hash 手工版本号。
4. 联调前用 `profiles/virtual/virtual_device.py` 起假设备,把 UI/agent 流程先跑一遍。
5. 对真设备跑一遍:SET → 遥测读回 → RATE 改频 → `!<seq>` 回执 → 断线重连。

---

## 7. 与旧协议(原车)对比:砍掉什么

| 旧协议机制 | 契约 v1 | 代价(砍掉后) |
|---|---|---|
| 35 个 TEL 前缀逐包 snprintf | 一个 D 帧模板循环 | 固件 TX 代码量一个量级 |
| 流订阅生命周期 + GET STREAMS | RATE 一条命令 | 无订阅表/无"重订流"心智负担 |
| 字段级订阅 SUB/GET CATALOG | 无(列全发) | 带宽敏感场景按通道速率控制 |
| 心跳 HB + cmd_result 单槽保护 | 无心跳 | 桥只看 socket 断连 |
| 每命令强制 ACK 回显关联 | 可选 seq 回执 | /batch 仍可用,靠回读兜底 |
| 42.95s 回绕/跨包对拍口径坑 | 单调 t_ms,不跨包对拍 | 历史教训写进契约 |

---

## 8. 演进预留(本期不做)

- 二进制模式:加帧前缀(如 `B,<chan>,<nbytes>\n` + 定长 payload),给 kHz 级采样场景。
- 多连接命名空间:一个桥同时挂多个被调对象(现在单写者单连接)。
- UDP 模式:真正的高损容忍链路(现在 TCP 重连已够)。
