# tcp_tool — 智能车调参 PC 工具仓

> **本目录是 [smartcar-tuning-kit](../README.md) 的桌面调参工具（desktop tool）**，
> 基线版本 `2026-09-14.bridge-ext-v1`。架构 = 通用桥核（TCP↔HTTP）+ config 驱动
> + Web 控制台 + TPE 优化器 + 安全护栏 + MCP 工具面 + 桥扩展机制 + headless 服务模式。
> 私有调车历史档案（TUNING_*/HANDOFF/NEXT_SESSION/attic）与本地凭证
> （`xfyun_credentials.py`）**不随本公开仓库分发**。
> 设计文档见 [`docs/`](docs/)。

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

## 调车会话（原车领域脚本，保留可复用）

| 文件 | 用途 |
|---|---|
| `session_driver.py` | 会话驱动命令入口（`--do recipe.json` / `--sweep` / `--optimize opt.json` TPE 贝叶斯 / `--diff a b` 跨趟对比 / `--json` / 退出码 0/2/3/4/5；护栏预检 + 趟级自动回滚 + 审计） |
| `optimizer.py` | 预算感知 TPE 贝叶斯优化器（P0-1；stdlib-only；多目标 Pareto 层；确定性 seed） |
| `guardrails.py` | 通用护栏引擎 + append-only 审计（P0-2；黑名单/值域/步长/回滚判定 schema 驱动，桥/session_driver/mcp_server 三层共用） |
| `archive.py` | 趟归档系统（数据已清空，脚本通用可复用） |
| `track_dump.py` / `track_upload.py` | 录制线回传 / 优化线上传（原车语义） |

## 离线路径 / 仿真

| 文件 | 用途 |
|---|---|
| `path_pipeline.py` | 线优化一条龙（dump→平滑→优化→κ体检→sim） |
| `path_optimizer.py` / `sim_pure_pursuit.py` / `vehicle_params.py` | pipeline 的依赖（优化器 / 仿真 / 车参） |
| `xy_shadow_analysis.py` | XY 投影影子链离线分析（原车固件配套） |

⚠ sim_pure_pursuit 的控制律仍是旧版：κ/几何体检仍权威，控制行为结论不可信。

## 诊断 / 固件配套

| 文件 | 用途 |
|---|---|
| `enc_diag_run.py` | 转向编码器台架自检驱动（原车固件命令） |
| `asr_bridge.py` | 讯飞 ASR 代理 —— exe 打包依赖（smartcar.spec hiddenimports），别动；凭证 `xfyun_credentials.py` 永不入库 |
| `gen_selfcheck_font.py` | 整车自检菜单中文字库生成器（原车固件配套） |

## 目录

| 目录 | 内容 |
|---|---|
| `dist/` | 打包产物 + exe 运行时（遥测历史已删除，exe 启动自动重建空文件） |
| `runtime/` | 从源码跑 tuning_tool.py 时的运行时目录（已清空） |
| `matlab/` | MATLAB 可视化层（原车配套） |
| `attic/` | 过时代/一次性产物存档（清单 attic/README.md，留档不删） |
| `docs/` | 设计文档（现行 = `2026-09-14-bridge-extension-headless.md` 桥扩展机制与 headless；下游 `2026-08-14-p0-smart-optimizer-guardrails-attribution.md`；上游 `2026-08-13-agent-efficiency-and-web-redesign.md` 与 `2026-08-13-agent-auto-tuning-p1-p2-p4.md`） |
| `tests/` | P0 回归套件：`run_p0_checks.py` 十套件一键全跑（冻结态套件缺 exe 产物时显式 SKIP 分级汇总，不算 PASS）；发布验收 = build 后 `python tests/test_frozen_headless.py --require` |
