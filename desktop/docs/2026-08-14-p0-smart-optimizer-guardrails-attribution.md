# tcp_tool 调参助手 P0 设计 — 智能优化器 + 安全护栏 + 分数归因

**日期**：2026-08-14
**状态**：已落地（tests/run_p0_checks.py 七套件全 PASS；桥/driver/MCP 三层护栏
+ 审计 + 回滚生效；TPE 在 8 维 40 趟合成 benchmark 上 9/10 胜随机、+57%；
分段归因与 diff 工具就绪）
**上游文档**：docs/2026-08-13-agent-auto-tuning-p1-p2-p4.md（P1/P2/P4 已落地）
**改动对象**：E:\ads\tcp_tool\（新增 optimizer.py / guardrails.py / tests/；
session_driver.py 加 --optimize/--diff/护栏/回滚；score_engine.py 加分段归因；
tuning_tool.py 桥内强制护栏；mcp_server.py 加 diff_runs 等工具）
**不动**：TCP 协议、固件命令集、桥单写者架构、stdlib-only、发车铁律
（脚本与 agent 永不让车动，发车只能人在车上按菜单）

---

## 0. 动机（红队结论的落点）

竞品视角三大死穴 → P0 三项：

| # | 死穴 | P0 对策 |
|---|---|---|
| 1 | 网格搜索样本效率差（5~15 维空间指数爆炸） | TPE 贝叶斯优化器 + 预算感知 + 多目标 Pareto 层 |
| 2 | 安全护栏只有散落 FORBIDDEN 字符串，桥无强制、无回滚、无审计 | 护栏进 profile 声明，桥/driver/MCP 三层强制 + 自动回滚 + append-only 审计 |
| 3 | 分数不可归因，说不清"丢分在哪" | 分段指标 + per-segment 归因 + 跨趟 diff 工具 |

---

## 1. P0-1 智能优化器（optimizer.py + session_driver --optimize）

### 1.1 算法选型：TPE（Tree-structured Parzen Estimator）

- stdlib-only 可写（只有 exp/log，无矩阵运算）；超参数少、对噪声评分鲁棒；
  是 Hyperopt/Optuna 默认采样器的同族算法，工业界验证充分。
- **多目标**：对每个观察点算 Pareto 非支配层（O(n²)，n≤数百可接受），
  good 组 = 层号 ≤ K 的点（K 自适应取覆盖 ~25% 样本的前若干层）。
  单目标时 Pareto 层退化为一维排序，good = top-γ，与经典 TPE 一致。
- 每维分布 = 截断高斯核 KDE（带宽 = 该组样本 std 的启发式，最小带宽保证探索）；
  候选生成 = 拉丁超立方分层采样 M 个点，取 good 密度 / bad 密度最大者。
- **预算感知**：
  - warmup 阶段（前 W 趟）空间填充采样，为 KDE 提供种子；
  - 候选点与已观察点最小欧氏距离惩罚（避免重复、防卡局部）；
  - spec 声明 budget（max_runs / max_wall_s），超预算停止；
  - 无评分趟（no launch）单独计数，连续 N 趟无结果 → 中止（车/人不在位）。
- 确定性：seed 进 spec，同 spec 同观察序列 → 同提议序列（可复现）。

### 1.2 通用性边界

optimizer.py 是**通用核**：输入 = 参数空间 + objectives（键名/direction）+ 观察
（参数 dict → 指标 dict），输出 = 提议点。不知道什么是 xte、什么是车。
领域知识（参数模板、指标键）全部进 opt.json spec 与 score_profile.json。

### 1.3 opt spec 格式（领域数据文件）

```json
{
  "label": "kp_kd_tune",
  "base_set": [],
  "params": [
    {"name": "kp", "template": "SET kp {value}", "min": 0.0, "max": 50.0, "step": 0.1, "init": 1.0},
    {"name": "kd", "template": "SET kd {value}"}
  ],
  "objectives": [{"key": "score", "direction": "max"}],
  "budget": {"max_runs": 20, "max_wall_s": 3600},
  "warmup": 5,
  "max_no_result_streak": 3,
  "streams": [["ctl", 100], ["stats", 1000]],
  "max_wait_s": 300, "max_run_s": 120,
  "report": true, "seed": 42
}
```

- `params[].min/max/step` 缺省时从当前 profile 的 control_schema.json fields 取
  （id == name），两个都没有 → spec 非法（exit 4）。
- 每趟流程与 --sweep 相同（设参 ACK → 布防等发车 → 捕获 → metrics+score →
  runs.jsonl），仅提议来源不同；记录加 `"optimize": {param: value}`。
- 结果 JSON 汇总：best（含参数+分数）、pareto_front、convergence（每趟参数/分数）、
  budget_used、stopped_reason。退出码沿用 0/2/3/4/5。

---

## 2. P0-2 安全护栏 schema 化 + 回滚 + 审计（guardrails.py）

### 2.1 声明位置：control_schema.json 顶层键 `guardrails`

与 fields 同文件 → schema_hash 版本锚天然覆盖护栏，换 profile 自动带护栏。

```json
"guardrails": {
  "set_patterns": ["SET {param} {value}", "DATA SET P{p} {param} {value}"],
  "forbidden_commands": ["MISSION RETURN", "MISSION EVENT", "MISSION LEARN"],
  "unmatched_policy": "allow",
  "max_step": {"default": null, "per_param": {"kp": 10.0}},
  "rollback": {
    "on_apply_fail": true,
    "score_min": 30.0,
    "metrics": [{"key": "xte_mean_m", "max": 0.3}]
  },
  "audit_path": null
}
```

- `set_patterns`：设参命令正则模板（{param}/{value}/{p} 占位）。通用核只认配置，
  不认识参数名语义；解析出 param 后对照 fields[].id 做值域/步长校验。
- `forbidden_commands`：全链路黑名单收进 profile。schema 未声明时调用方
  回退各自内置 FORBIDDEN 常量（行为不变）。
- `max_step`：单次写入相对当前值的最大变化量（需要当前值，缺失时跳过该项）。
- `rollback`：跑完触发条件（score 下限 / metrics 键值比较）+ 设参失败回滚。

### 2.2 强制点（三处共用 guardrails.py）

| 层 | 强制时机 | 违规处置 |
|---|---|---|
| 桥（强制点，本次新增） | /command、/batch、proposal apply 前逐命令检查 | 拒绝整批，返回 violations；audit 记 rejected |
| session_driver | --do/--sweep/--optimize/--arm 下发前预检 | fail fast 对应退出码；audit 记 |
| mcp_server | set_params / propose_params / run_experiment 预检 | 抛错返回 agent；audit 记 |

桥是唯一能拿到"参数当前值"的层（latest_parsed），max_step 校验以桥为准；
driver/MCP 层无当前值时跳过步长项（值域/黑名单仍强制）。

### 2.3 自动回滚（driver 内，趟级）

- 每趟下发前快照参数当前值（GET <param> 优先，params 遥测通道兜底）；
- 趟结束 → check_metrics(metrics, score)：
  触发（score < score_min 或 metric 超限）或设参部分失败（on_apply_fail）
  → 下发恢复命令（SET 回旧值，经 /batch ACK）→ audit 记 `{"event":"rollback"}`；
- 回滚本身失败 → audit 记 `{"event":"rollback_failed"}`（留给 Web 告警，P0 不阻塞）。

### 2.4 审计日志

append-only `runtime/audit.jsonl`（app_dir/runtime/），每行：
```json
{"ts": ..., "actor": "bridge|driver|mcp", "action": "batch|command|proposal|set|sweep|optimize|rollback",
 "commands": [...], "verdict": "ok|rejected", "violations": [...], "role": "agent|human", "profile": "..."}
```
单写者 = 各进程串行 append（与 runs.jsonl 同约定）。

### 2.5 与发车铁律的关系

FORBIDDEN 表位置不变（driver/MCP 常量兜底），profile 声明时以声明为准。
桥新增强制后铁律变三层真兜底（此前桥不查 = 设计文档与实现不符的缺口，本次修复）。

---

## 3. P0-3 分数归因 + 跨趟 diff

### 3.1 分段指标（report_metrics 扩展，不破坏现有键）

`metrics["segments"] = {"count": 10, "metrics": [{"seg": 0, "xte_mean_m": ..., "rev": ..., ...}, ...]}`

- engaged 帧按 m_idx/(m_pts) 归一化十等分（count 可配置），每段算与全局同键
  的段级指标（xte_mean/xte_max/rev/dstr/duty_sat/gnd_mean）。
- 现有调用方（--score/--sweep/runs.jsonl）无感：segments 是附加键，
  原指标键不变。capture 文件中的 rows 已含 m_idx，重算历史趟自动获得分段。

### 3.2 per-segment 归因（score_engine 扩展）

score_profile.json 指标可声明 `"per_segment": true` → 引擎取各段值，
breakdown 附加 `"segment_values": [...]`、`"worst_segment": n`，
贡献按 aggregation（`mean` 段均值 / `worst` 最差段）计算。
归因口径：分数丢在哪项 → 哪段 → 哪个值，agent 可解释。

### 3.3 diff 工具

- `session_driver.py --diff a.json b.json [--json]`：对两个 capture 文件
  各自 report_metrics（含 segments），输出逐键 delta + pct + 段级 diff。
- `mcp_server.py diff_runs {run_a, run_b}`：按 run_id 查 runs.jsonl 的
  capture_file 再复用 --diff 逻辑（薄壳转发）。

---

## 4. 实施顺序与验证

1. optimizer.py（纯函数，先行单元验证：合成黑盒 vs 网格/随机收敛对比）
2. guardrails.py + control_schema.json(s) guardrails 段 + 桥/driver/MCP 三处集成
3. score_engine 分段 + session_driver 分段指标 + --diff
4. session_driver --optimize + MCP 工具扩展
5. tests/run_p0_checks.py 全量回归（护栏违规矩阵 / 优化收敛 / 归因 / diff / 审计）
6. README + 设计文档状态更新

**验收**（2026-08-14 实测）：tests/run_p0_checks.py 七套件全 PASS；
虚拟 profile 下桥内护栏拒绝用例可复现（test_p0_e2e，桥 /command 与 /batch
双端点 + max_step 步长强制 + 审计记录）；--optimize 合成 benchmark：
8 维 40 趟 TPE mean=74.8 vs 随机 mean=47.7（9/10 胜场，+57%），同预算网格
（2 点/维=256 趟）不可用；audit.jsonl append-only 记录桥/driver/MCP 三方
rejected/rollback 事件。

---

## 5. v2 增量（2026-08-14）：参数类型声明 + 穷举路由 + init 起点

按"简单稳定生效"原则的最小增量，白盒、可回退、旧 spec 零破坏：

1. **type/choices 声明**：param 可声明 `"type": "continuous"|"categorical"`；
   categorical 用 `choices` 列表（缺省 type=continuous，旧 opt.json 不变）。
   分类维在 TPE 里用拉普拉斯平滑类别频率建模（good/bad 两组各算类别概率），
   只有计数除法，无新调参面。
2. **穷举路由**：全空间有限组合数 ≤ 24（`_exhaust_max`）且 ≤ 预算 → 直接
   穷举（效果等同 --sweep 网格）；否则 TPE。路由是纯算术白盒规则，用户永远
   只写一个 opt.json，不选器。
3. **init 起点**：全部参数声明 init 时，第一趟提议 = init 组合（当前可工作
   参数的基线复测），后续照常优化。零成本 warm-start 简化版。

验证：tests/test_optimizer_v2.py 12 项全 PASS（混合 2 连续+分类空间锁定正确
类别且连续维收敛；穷举 2×3 空间 6 趟无重复；非法声明拒收）。旧连续空间
benchmark 数值不变（test_optimizer_smoke/robust 回归一致）。

**遗留与校准项**：
- TPE 超参（带宽 0.10 / 探索 0.15 / 候选 1500 / warmup 4）在合成二次形上锁定，
  实车数据积累后可再校准（同 score 权重性质）；
- 回滚快照依赖 params 遥测通道读回（GET 兜底）；真实固件若无参数读回通道，
  回滚退化为"无法恢复"并审计记录（rollback_failed）；
- 原车 metrics 提取器（report_metrics）仍是车语义，virtual 等异构对象的
  提取器 profile 化是下一期（异构对象 #2）工作；
- /proposal decide 的 apply 时刻护栏拒绝返回 409 但提案状态已标 rejected，
  前端展示 violations 的路径待前端接入。
