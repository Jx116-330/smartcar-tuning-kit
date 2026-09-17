# tcp_tool — 智能车调参 PC 工具仓

> **通用调参框架**（2026-08-13 起）：面向多被调对象——通用桥核（config 驱动，
> 核内零被调对象语义）+ schema 驱动 Web 控制台 + TPE 优化器 + 安全护栏 + MCP
> 工具面 + 桥扩展机制 + headless 服务模式。基线版本 `2026-09-14.bridge-ext-v1`。
>
> **2026-09-17 瘦身**：原车（已退役）领域遗产脚本——录制线回传/字节备份、
> 趟归档、板级快照、离线线优化 pipeline（path_pipeline 一族）、XY 影子链、
> 编码器/字库诊断配套、matlab 可视化、attic 存档、四份调车历史文档
> （TUNING_*/HANDOFF/NEXT_SESSION）——已从工作树删除。需要考古时从 git
> 历史恢复，例：`git show 842a1d2:track_dump.py`、`git show 842a1d2:attic/README.md`。

## 桥 / 网页 UI

| 文件 | 用途 |
|---|---|
| `tuning_tool.py` | TCP(8080)↔HTTP(9898) 桥主程序 = `dist/YawTuningTool.exe` 的源码；**改了必重打 exe**(`build.bat`)。HTTP:`/command` `/batch` `/events`(SSE) `/schema` `/snapshot` `/latest` `/shutdown` + 静态托管新前端。支持 `--profile <name>` 加载被调对象 profile;`--headless` 纯服务模式(无 GUI、日志落 `runtime/bridge_headless.log`、`POST /shutdown` 优雅退出)——核内零被调对象语义(桥扩展机制承载领域功能) |
| `bridge_ext.py` | 旧车桥扩展(轨迹 `/trajectory*` 六端点 + 相机 `/camera/*` + CIMG 流解析 + ASR 行桥)；根 `config.json` 的 `"bridge_extension"` 声明加载,契约与钩子见 `docs/2026-09-14-bridge-extension-headless.md`。新被调对象不复用它,写自己的扩展 |
| `config_loader.py` / `config.json` | 桥配置与 TCP 协议 schema（canonical）；config 驱动、通用核不含被调对象语义；`--profile` 决议在 config_loader;`bridge_extension` 键三态 = 路径/显式 null/缺省(缺省仅根布局尝试约定名 `bridge_ext.py`,旧 dist 升级兼容) |
| `control_schema.json` 的 `guardrails` 键 | P0-2 护栏声明（set_patterns/forbidden_commands/max_step/rollback），schema_hash 版本锚覆盖护栏 |
| `control_schema.json` | 被调对象参数 schema（新前端 `/schema` + 调参面板的数据源）；`actions` 键声明安全动作（前端 SafetyActions 消费：常驻 STOP/悬浮/快捷键/重试，通用核不读） |
| `profiles/` | 多被调对象 profile 目录（接入契约见 `docs/protocol_contract_v1.md`）；`profiles/virtual/` = 零硬件演示链（虚拟设备 + 契约 v1 csv 帧；`bridge_extension: null` = 干净核） |
| `firmware_kit/` | 固件侧接入包：`tt_link.h` 单文件 C99 参考实现（行收包/发帧/通道调度/SET·RATE·GET·PING/`!<seq>` 回执）+ `tt_link_test.c` 协议逻辑自测；接入指南见 `firmware_kit/README.md` |
| `web/` | 新前端工程（Vite+React+TS+uPlot，schema 驱动调参面板）；`npm run build` 产物 `web/dist/` 入库供打包；热载 = `dist/web_dist/` 拷目录即生效 |
| `dashboard.html` | 旧版单文件调参台，保留为 `/dashboard` 回退入口（设计文档 §6.4；轨迹页依赖 bridge_ext 的 `/trajectory` 端点） |
| `build.bat` / `smartcar.spec` / `smartcar_console.spec` | PyInstaller 重打 exe（先 `npm run build`，无 npm 打 warning 跳过用旧产物）；双产物 = 窗口版 `YawTuningTool.exe` + 控制台版 `YawTuningToolConsole.exe`（headless/服务场景）；dist 复制策略 = 代码文件覆盖、配置仅缺失时复制 |

## 框架核心

| 文件 | 用途 |
|---|---|
| `session_driver.py` | 会话驱动命令入口（`--do recipe.json` / `--sweep` / `--optimize opt.json` TPE 贝叶斯 / `--diff a b` 跨趟对比 / `--json` / 退出码 0/2/3/4/5；护栏预检 + 趟级自动回滚 + 审计） |
| `optimizer.py` | 预算感知 TPE 贝叶斯优化器（P0-1；stdlib-only；多目标 Pareto 层；确定性 seed） |
| `guardrails.py` | 通用护栏引擎 + append-only 审计（P0-2；黑名单/值域/步长/回滚判定 schema 驱动，桥/session_driver/mcp_server 三层共用） |
| `score_engine.py` + `score_profile.json` | 配置驱动评分引擎 + 分段归因（"丢分在哪"），权重/阈值全在 profile 数据文件 |
| `mcp_server.py` | agent 工具面（stdio MCP：get_snapshot/get_schema/set_params/propose_params/run_experiment/list_runs/get_score/diff_runs） |
| `asr_bridge.py` / `asr_vocab.py` | 讯飞 ASR 代理与命令规整器——`bridge_ext.py` 的运行依赖（凭证 `xfyun_credentials.py` 永不入库；spec hiddenimports 同步）；通用核不引用 |
| `camera_capture.py` | 相机镜像识别——`bridge_ext.py` 的运行依赖（CIMG 流解析/帧识别/会话存档） |
| `test_asr_bridge.py` / `test_asr_vocab.py` / `test_camera_capture.py` | 上述三个依赖模块的单元测试 |

## 目录

| 目录 | 内容 |
|---|---|
| `dist/` | 打包产物 + exe 运行时（runtime 数据自动重建；冻结态验收以 `dist/bridge_ext.py` 为新鲜度标志） |
| `runtime/` | 从源码跑 `tuning_tool.py` 时的运行时目录（护栏审计 audit.jsonl、遥测、相机帧；可清，自动重建） |
| `recordings/` | 趟次库 `runs.jsonl`（session_driver 写，自动重建） |
| `docs/` | 设计文档（现行 = `2026-09-14-bridge-extension-headless.md` 桥扩展机制与 headless；下游 `2026-08-14-p0-smart-optimizer-guardrails-attribution.md`；上游 `2026-08-13-agent-efficiency-and-web-redesign.md` 与 `2026-08-13-agent-auto-tuning-p1-p2-p4.md`） |
| `tests/` | P0 回归套件：`run_p0_checks.py` 十套件一键全跑（冻结态套件缺 exe 产物时显式 SKIP 分级汇总，不算 PASS）；发布验收 = build 后 `python tests/test_frozen_headless.py --require` |
