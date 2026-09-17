# agent 自动/半自动调参设计 — P1 评分 + P2 试验编排 + P4 工具契约

**日期**：2026-08-13
**状态**：已落地（2026-08-13，run_phase_a/b/c/d_checks 全 PASS；score 权重/阈值为占位值，待实车校准）
**改动对象**：`E:\ads\tcp_tool\`（新增 score_engine.py / score_profile.json / mcp_server.py；
session_driver.py 扩展 --sweep/--score；tuning_tool.py 加 /proposal；web/ 加建议确认条）
**不动**：TCP 协议、固件命令集、桥单写者架构、stdlib-only（桥与 MCP 都不引第三方包）
**上游文档**：`docs/2026-08-13-agent-efficiency-and-web-redesign.md`（Phase A/B/C，已落地）
**通用性边界**：沿用上游 §2a 三层契约——通用核禁被调对象语义；评分权重、阈值、指标键
全部进 `score_profile.json`（领域数据文件），引擎只认配置。

---

## 1. 目标

| 层 | 现状 | 本期目标 |
|---|---|---|
| P1 评分 | `report_metrics()` 产出人读报告，无机器可比的"好坏"数字 | 配置驱动 score 引擎：metrics → 加权 score(0-100) + pass/fail；--do 结果带 score；可独立复评历史趟 |
| P2 编排 | recipe 只能"一组参数一趟"；无参数扫描、无趟次记录 | `--sweep`：参数网格 × 每趟 发车等待→捕获→评分→入 runs.jsonl，支持早停；建议-确认流（agent 提议 → Web 人审 → 一键应用/拒绝） |
| P4 契约 | agent 直连 HTTP 细节，无稳定工具面 | `mcp_server.py`：stdio JSON-RPC 薄壳，7 个工具，全部经桥 HTTP / 读文件实现 |

**非目标（P0 安全层，下一期）**：写参护栏/钳制、速率限制、自动回滚、审计强化。
本期以"建议-确认流 = 人在环路"作为过渡安全手段；`runs.jsonl` 顺带充当审计种子。
**铁律不变**：任何路径都不发发车/录制类命令（FORBIDDEN 表在全链路三处各自兜底）。

## 2. P1 评分引擎

### 2.1 职责切分

- `score_engine.py`（**通用**，零领域字符串）：读 profile → 对一份 metrics dict
  计算 score/pass/breakdown。不知道什么是 xte、什么是车。
- `score_profile.json`（**领域数据**，与 control_schema.json 同级同性质）：
  指标键、权重、方向、归一化基准 ref、pass 阈值。**权重/阈值初值为占位，**
  **由调车人按 TUNING_PLAYBOOK 经验校准**（引擎不背这个锅）。
- 指标来源：session_driver `report_metrics(rows)` 的输出 dict（已存在的计算，不改）。

### 2.2 profile 格式

```json
{
  "profile_version": 1,
  "missing_metric": "fail",
  "metrics": [
    {"key": "xte_mean_m", "weight": 0.35, "direction": "lower", "ref": 0.05, "pass_max": 0.10},
    {"key": "rev",        "weight": 0.25, "direction": "lower", "ref": 6.0,  "pass_max": 12},
    {"key": "duty_sat_pct","weight": 0.15, "direction": "lower", "ref": 20.0},
    {"key": "gnd_mean_ms","weight": 0.25, "direction": "higher","ref": 1.5,  "pass_min": 0.8}
  ]
}
```

- `direction=lower`：贡献 = clamp(1 - value/ref, 0, 1)；`higher`：clamp(value/ref, 0, 1)。
- `score = 100 * Σ(w·contrib) / Σw`，权重不必归一。
- `pass = 全部声明了 pass_max/pass_min 的指标均达标`；无阈值声明的指标只参与 score。
- `missing_metric`: `"fail"`（缺指标→pass=false，缺项贡献按 0）或 `"skip"`（从权重剔除）。
- 输出 breakdown 逐项列出 value/contrib/ok，agent 可解释"分丢在哪"。

### 2.3 集成点

- `session_driver.py --score recordings/run_x.json [--profile p.json] [--json]`：
  对已存捕获重算 metrics + score（复评历史趟、profile 调参后回溯对比）。
- `--do` / `--sweep` 流程内：`report: true` 且 profile 文件存在时，
  结果 JSON 增加 `"score": {...}`。**退出码语义不动**（pass/fail 走 JSON 字段，
  不新增退出码，避免破坏已有调用方）。
- profile 查找顺序：`--profile` 显式指定 > `app_dir()/score_profile.json` > 不打分。

## 3. P2 试验编排

### 3.1 趟次数据库 runs.jsonl

`recordings/runs.jsonl`，append-only，每趟一行：

```json
{"run_id": "20260813-210512_kp1.2", "ts": 1786..., "label": "kp_sweep",
 "sets": ["DATA SET P1 ..."], "capture_file": "recordings/run_xxx.json",
 "metrics": {...}, "score": {"score": 72.5, "pass": true, ...},
 "sweep": {"kp": 1.2}}
```

- 写入方：session_driver（--arm/--do/--sweep 捕获后都写，--sweep 必写）。
- 读取方：mcp_server（list_runs/get_score）、未来 Web 历史页。
- jsonl 而非 sqlite：零依赖、可手改、可 git diff；量级（日数十趟）不需要索引。

### 3.2 --sweep

```json
{
  "label": "kp_sweep",
  "streams": [["MISSION",100],["DRIVE",100],["STATS",1000]],
  "base_set": ["DATA SET P2 diff_gain 0.3 rev=..."],
  "sweep": [{"name": "kp", "template": "DATA SET P1 turn_kp {value} rev=...",
             "values": [0.8, 1.0, 1.2]}],
  "max_wait_s": 300, "max_run_s": 120,
  "early_stop": {"consecutive_worse": 2},
  "report": true
}
```

- 多个 sweep 条目 = **笛卡尔积**（grid）；`{value}` 占位替换；name 仅作记录键。
- 每趟流程（复用 --arm 的 eng 1→0 检测，抽成 `capture_run()`）：
  FORBIDDEN 检查 → /batch 下发 base_set+本趟 set（ACK 验证，失败记趟并继续下一趟）
  → 订阅 streams → 布防等人按菜单发车（**铁律：脚本永不让车动**）→ eng 1→0 收尾
  → metrics+score → 写 runs.jsonl。
- **早停**：按 values 给定顺序逐趟跑；score 相对历史最佳连续变差 N 趟 → 中止
  （只中止本次 sweep，已跑趟次全部保留）。`consecutive_worse` 缺省 0 = 不早停。
- 单趟超时（max_wait_s 没等到发车）：记 `{"skipped": "no launch"}` 行，继续下一趟。
- 退出码沿用现有语义：0 全部跑完（含早停正常中止）/ 2 首趟设参 ACK 失败 /
  3 链路失败 / 4 sweep 文件非法 / 5 禁发命令。

### 3.3 建议-确认流（半自动 UX）

桥加内存提案队列（bounded 20，重启即清空——提案是短寿命对象，不落盘）：

- `POST /proposal` `{commands: [...], rationale?, expected?}` →
  `{id, status:"pending"}`；SSE 广播 `event: proposal`（Web 实时弹出）。
- `GET /proposal/list` → 全部提案及状态。
- `POST /proposal/decide` `{id, decision: "apply"|"reject"}` →
  apply 走**与 /batch 完全相同的执行路径**（抽 `_execute_batch()` 共用），
  结果写回提案；reject 仅标记。SSE 再广播一次状态变更。
- Web：监听 `proposal` 事件 → 顶部确认条：命令列表（经 planner 解析成
  参数/旧值(默认或遥测)/新值 diff 展示）+ rationale + [应用]/[拒绝]。
- 桥对提案命令**只当字符串**；diff 解析全部在前端用 schema 做（通用性边界）。
- 这是 P0 护栏落地前的过渡安全：agent 不能直接 set_params 生效调参参数，
  走 propose → 人确认。（MCP 的 set_params 工具仍保留直写能力，供非调参类
  命令与信任场景用；P0 落地时统一收编。）

## 4. P4 MCP 工具契约（mcp_server.py）

stdlib 实现最小 MCP：stdio 上 JSON-RPC 2.0，支持 `initialize` /
`notifications/*`（吞掉）/ `tools/list` / `tools/call` / `ping`。
不引 mcp SDK（stdlib-only 约定）；协议面刻意只覆盖 tools 能力子集。

| 工具 | 实现 | 说明 |
|---|---|---|
| `get_snapshot` | GET /snapshot | 链路+遥测+桥状态聚合 |
| `get_schema` | GET /schema | 参数 schema（含 schema_hash 版本锚） |
| `set_params` | POST /batch (expect ACK) | 直写；FORBIDDEN 表兜底拒绝 |
| `propose_params` | POST /proposal | 半自动主通道：提议→人在 Web 确认 |
| `run_experiment` | 子进程 `session_driver.py --do/--sweep --json` | 返回结构化结果（含 score） |
| `list_runs` | 读 runs.jsonl 尾部 N 行 | 趟次历史检索 |
| `get_score` | 读 runs.jsonl 按 run_id 或取最新 | 单趟 score+breakdown |

- 薄壳原则：MCP 内**零业务逻辑**，全部转发桥 HTTP / 读文件 / 调 driver 子进程。
- FORBIDDEN 表第三处兜底（桥、driver、MCP 各自独立拒绝，不互相依赖）。
- 启动：`python mcp_server.py`（ Claude/Desktop/agent 配置 stdio 接入）。
- 未来 P0 落地时在此加 `role`/权限声明，协议不变。

## 5. 验证（attic/phase_a_verify/run_phase_d_checks.py）

沿用 sandbox + FakePeer 套路，新增：

1. score_engine 单测：lower/higher/clamp/缺指标 fail/skip/权重不归一。
2. --score 复评已存捕获（用现成 recordings 样本或合成 rows）。
3. --sweep 文件校验 + 网格展开 + 早停逻辑（stub capture，注入 score 序列）。
4. 桥 /proposal 全生命周期（建→SSE 事件→list→decide apply 走 /batch→状态回写）。
5. mcp_server stdio：initialize → tools/list（7 工具）→ get_snapshot →
   propose_params → decide 后 list_runs/get_score。
6. 回归：run_phase_a/b/c_checks.py 必须仍全 PASS。

## 6. 风险与残余

- score 权重/阈值是占位值，**未校准前 pass/fail 无物理意义**（文档与 profile 注释标注）。
- --sweep 每趟仍要人按菜单发车（铁律），"全自动"目前=自动迭代+人工发车；
  真闭环发车属 P0 之后、且必须实车验证安全链。
- 提案不落盘，桥重启丢 pending 提案（可接受：短寿命；P0 审计期再评估）。
- runs.jsonl 无并发写保护（单写者=driver 串行跑趟，成立）。
