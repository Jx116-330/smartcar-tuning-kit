# MATLAB 集成层 — `matlab/` 目录

Python 一侧负责数据录制、指标计算、参数扫描；MATLAB 一侧专注**可视化、对比、报告生成**，可选做多目标优化。两侧用同一份 JSON / CSV 当交换格式，schema 由 Python 的 `record_session.py` / `param_sweep.py` 定义、MATLAB 这边只读。

## 文件清单

| 文件 | 角色 | 依赖输入 | 实车安全性 |
|---|---|---|---|
| `load_recording.m` | TELMISSION JSON → MATLAB table | 实车录制 JSON | ✅ 完全干净 |
| `compute_metrics.m` | 4 大指标（XTE / duration / 平稳 / 安全），镜像 Python 版 | 录制 table | ✅ 完全干净 |
| `plot_trajectory.m` | 单次 run 的 XY + 3 个时序图 | 录制 table | ✅ 完全干净 |
| `compare_runs.m` | 多 run 对比，含 Pareto-style 散点 + 平行坐标 | `runs/_index.jsonl` | ✅ 完全干净 |
| `pareto_explorer.m` | sim 出的 Pareto 3D 可视化 + 重算 | `sweeps/*/master.csv` | ⚠️ **依赖 sim** — 启动时打 SIM TRUST 横幅 |
| `generate_report.m` | 一键 PDF：cover + compare + trajectory + Pareto | 以上所有 | 安全（PDF cover 列出每页的可信度）|

## 实车安全性约定

任何"基于 sim 的输出"在脚本顶部都明示警告。`vehicle_params.WHEELBASE_M` **2026-05-14 起从 USER_SIM 0.80m 切到 FITTED 0.957m**（由 `calibrate_vehicle_params.py` 从 4 份实车录制 336 cornering 帧拟合，IQR 0.91-1.01）。这是 sim 应该用的"等效轴距"，影响：

- `sim_pure_pursuit.simulate` 的 yaw-rate 增益 → `param_sweep.py` 的所有 XTE/duration 绝对值
- 派生量 `MIN_TURN_RADIUS_M`（轴距 / tan(δ)）：1.443 m → **1.726 m**

WB 切换后 sim XTE 绝对值上偏（e.g. xte_test3 sim：2.28cm → 13.60cm），但 **Pareto 排序保持**（已知好 KP=2.5/LA=500/CAP=400/BLD=0.0 仍在前沿）。原因：原 0.80m 是经验上"装合 sim 跟实车 XTE 吻合"的值，0.957m 是真物理值；sim 还缺 servo lag (`SERVO_TAU_S` PLACEHOLDER) + Turn PID 闭环。要让绝对 XTE 重新对齐，需补 sim fidelity。

`pareto_explorer.m` 加载 master.csv 时打印 SIM TRUST 横幅，generate_report 的 cover 页也注明。**不要把 Pareto 推荐当成"上车直接用的参数"**——它只是相对排序参考。

`compare_runs` / `plot_trajectory` / `compute_metrics` 全部基于实车录制，不依赖 `vehicle_params`，安全。

## 典型工作流

### 1. 把一次录制画出来看

```matlab
addpath('E:/ads/tcp_tool/matlab');
T = load_recording('E:/ads/tcp_tool/recordings/tmp_run_xte_test3.json');
plot_trajectory(T, 'title', 'xte_test3 baseline');
% 注意：name-value 参数（不是 struct）。所有 matlab/*.m 的 opts.* 字段都同样调用方式。
```

### 2. 拿到指标（跟 Python 端的 `replay_to_dataframe.py` 给出的相同）

```matlab
M = compute_metrics(T, 'xte_test3');
fprintf('XTE rms = %.2f cm, completion %.0f%%, flips=%d\n', ...
        M.xte_rms_m * 100, M.completion_ratio * 100, M.sign_flips);
```

### 3. 多 run 横评（调车闭环跑完几次之后）

```matlab
out = compare_runs('E:/ads/tcp_tool/runs/_index.jsonl');
disp(out.summary);
```

### 4. 看 sim 出的 Pareto 前沿（带 SIM TRUST 横幅）

```matlab
pareto_explorer('E:/ads/tcp_tool/sweeps/<timestamp>/master.csv');
```

### 5. 一键出 PDF 给老板看

```matlab
generate_report( ...
    'runs_index', 'E:/ads/tcp_tool/runs/_index.jsonl', ...
    'recording',  'E:/ads/tcp_tool/recordings/tmp_run_xte_test3.json', ...
    'sweep_csv',  '');   % 留空跳过 Pareto 页
```

## Schema 双向兼容

`compute_metrics.m` 跟 `replay_to_dataframe.compute_metrics` (Python) **必须**保持同样的字段名和定义。任何一边改了，另一边也要改。当前同步的字段：

```
n_frames        n_engaged          duration_s
xte_rms_m       xte_max_m          xte_mean_signed_m
steer_dstr_std_deg                 sign_flips
lost_fired      completion_ratio   kp / la / blnd / pts / file
```

两侧约定：
- 只统计 `eng == 1` 的帧
- XTE 过滤 `|x| > 1e-6`（早于 cursor 推进的 0 不算）
- duration 用 engaged 段的 `ms_last - ms_first`，单位秒
- sign_flips 跳过 0（只数真正的左右交替）

## 后续可加

1. **多目标 GA**：`pareto_explorer` 加 `--optimize` 标志，调用 `gamultiobj` / `paretosearch` 跳出网格扫描的离散点，给"在 KP=2.2, LA=420, CAP=550 试试"这种内插推荐。一旦 WB 上车量出来，这条路才能真正开放。
2. **实时 MATLAB dashboard**：跟 `tuning_session.py` 联动，每完成一次 run 自动调 MATLAB 出图——目前实际上 MATLAB 启动慢，更适合事后批量出 PDF，不是实时。
3. **接 `subject1_sim.m` 的样条规划器**：阿克曼仿真里有 Pure Pursuit 跟 spline 的整套实现，可以借来做 Stage 2 路径几何重规划的 MATLAB 端。
