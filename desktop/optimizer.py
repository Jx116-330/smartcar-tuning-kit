# -*- coding: utf-8 -*-
"""optimizer.py - 预算感知的 TPE 贝叶斯优化器(2026-08-14, P0-1, 设计文档 §1)

通用核:零被调对象语义。输入 = 参数空间 + objectives(键名/direction) +
观察(参数 dict -> 指标 dict),输出 = 下一个提议点。不知道什么是 xte、
什么是车。领域知识(参数模板、指标键)全部在 opt.json spec 与
score_profile.json,不进本文件。

算法(设计文档 §1.1):
  - TPE:观察按 Pareto 非支配层分组(单目标退化为 top-γ 分位数),
    good 层覆盖 ~25% 样本;每维用截断高斯核 KDE 估 p(x|good)/p(x|bad);
    候选 = 拉丁超立方采样,取 good/bad 密度比最大者。
  - 预算感知: warmup 阶段空间填充采样;候选与已观察点最小距离惩罚;
    max_runs/max_wall_s 预算;连续无结果趟中止。
  - 确定性:seed 进 spec,同 spec 同观察序列 -> 同提议序列。
  - stdlib-only:math.erf/exp/log 足够,零第三方依赖。

  from optimizer import Optimizer
  opt = Optimizer(spec)
  obs = []                       # [{"params": {...}, "objectives": {...}}]
  while (p := opt.propose(obs)):
      score = run(p["params"])   # 调用方跑真实/仿真趟
      obs.append({"params": p["params"], "objectives": {"score": score}})
  print(opt.summary(obs))        # best + pareto_front + convergence
"""
import math
import random


# ---------------------------------------------------------------- 数值工具
def _clamp(v, lo, hi):
    return min(max(v, lo), hi)


def _norm_pdf(x, mu, sigma):
    z = (x - mu) / sigma
    return math.exp(-0.5 * z * z) / (sigma * math.sqrt(2.0 * math.pi))


def _norm_cdf(x, mu, sigma):
    # 标准正态 cdf 经 erf 实现
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


def _trunc_pdf(x, mu, sigma, lo, hi):
    """截断高斯核密度: N(x;mu,sigma) / (Phi(hi)-Phi(lo))。"""
    if sigma <= 0:
        return 1.0 if abs(x - mu) < 1e-12 else 0.0
    z_hi = _norm_cdf(hi, mu, sigma)
    z_lo = _norm_cdf(lo, mu, sigma)
    denom = max(z_hi - z_lo, 1e-12)
    return _norm_pdf(x, mu, sigma) / denom


def _lhs_points(n, dims, rng, lo=0.0, hi=1.0):
    """拉丁超立方采样:每维分 n 层,层内均匀随机,层序随机。"""
    pts = []
    for _ in range(n):
        p = []
        for _d in range(dims):
            strata = rng.sample(range(n), 1)[0]
            v = lo + (hi - lo) * (strata + rng.random()) / n
            p.append(v)
        pts.append(p)
    return pts


# ---------------------------------------------------------------- Pareto 层
def _pareto_layers(vecs):
    """对数值向量列表算非支配分层(层 0 = 前沿,值越大越优)。含 None 的向量不
    参与分层(视为未知)。返回 (layers, unknown) 两组下标。"""
    n = len(vecs)
    valid = [i for i, v in enumerate(vecs)
             if v is not None and all(x is not None for x in v)]
    if not valid:
        return [], [i for i in range(n) if i not in valid]
    dim = len(vecs[valid[0]])
    dominates = [[False] * n for _ in range(n)]
    for i in valid:
        oi = vecs[i]
        for j in valid:
            if i == j:
                continue
            oj = vecs[j]
            # i 支配 j:i 每个目标都不差于 j,且至少一个严格更好
            ge = all(oi[k] >= oj[k] for k in range(dim))
            gt = any(oi[k] > oj[k] for k in range(dim))
            dominates[i][j] = ge and gt
    remaining = set(valid)
    layers = []
    while remaining:
        front = [i for i in remaining
                 if not any(dominates[j][i] for j in remaining)]
        layers.append(front)
        remaining.difference_update(front)
    unknown = [i for i in range(n) if i not in valid]
    return layers, unknown


# ---------------------------------------------------------------- Optimizer
class Optimizer:
    """TPE 优化器。spec:
      params:     [{name, min, max, step?}]   参数空间(连续)
      objectives: [{key, direction: max|min}] 优化目标(方向在构造时归一)
      budget:     {max_runs?, max_wall_s?}
      warmup:     int                          前 N 趟空间填充(缺省 0)
      seed:       int                          复现锚
    """

    def __init__(self, spec):
        if not isinstance(spec, dict):
            raise ValueError("optimizer spec must be a dict")
        params = spec.get("params")
        if (not isinstance(params, list) or not params
                or not all(isinstance(p, dict) and isinstance(p.get("name"), str)
                           for p in params)):
            raise ValueError("params must be a non-empty list of {name,...}")
        # v2: type=continuous(缺省)|categorical;连续带 min/max/step;
        # 分类带 choices;init 为第一趟提议的起始组合
        checked = []
        for p in params:
            name = p["name"].strip()
            ptype = p.get("type", "continuous")
            init = p.get("init")
            if ptype == "continuous":
                lo = float(p.get("min", 0.0))
                hi = float(p.get("max", 1.0))
                if hi <= lo:
                    raise ValueError("param %s: need min < max" % name)
                step = p.get("step")
                if step is not None:
                    step = float(step)
                    if step <= 0:
                        raise ValueError("param %s: step must be > 0" % name)
                if init is not None:
                    if not isinstance(init, (int, float)) or isinstance(init, bool):
                        raise ValueError("param %s: init must be numeric" % name)
                    init = float(init)
                    if not (lo <= init <= hi):
                        raise ValueError("param %s: init %s outside [%s, %s]"
                                         % (name, init, lo, hi))
                checked.append({"name": name, "type": "continuous",
                                "min": lo, "max": hi, "step": step,
                                "choices": None, "init": init})
            elif ptype == "categorical":
                choices = p.get("choices")
                if (not isinstance(choices, list) or len(choices) < 2
                        or not all(c is not None for c in choices)):
                    raise ValueError("param %s: categorical needs choices "
                                     "(non-empty list of >=2)" % name)
                if init is not None and init not in choices:
                    raise ValueError("param %s: init %s not in choices"
                                     % (name, init))
                checked.append({"name": name, "type": "categorical",
                                "min": 0.0, "max": float(len(choices) - 1),
                                "step": None, "choices": list(choices),
                                "init": init})
            else:
                raise ValueError("param %s: type must be continuous|categorical"
                                 % name)
        objectives = spec.get("objectives")
        if not isinstance(objectives, list) or not objectives:
            raise ValueError("objectives must be a non-empty list")
        checked_obj = []
        for o in objectives:
            if (not isinstance(o, dict) or not isinstance(o.get("key"), str)
                    or o.get("direction", "max") not in ("max", "min")):
                raise ValueError("objective needs {key, direction: max|min}")
            checked_obj.append({"key": o["key"].strip(),
                                "sign": 1.0 if o.get("direction") == "max" else -1.0})
        budget = spec.get("budget", {}) or {}
        mr = budget.get("max_runs")
        mw = budget.get("max_wall_s")
        if mr is not None and (not isinstance(mr, int) or isinstance(mr, bool) or mr < 1):
            raise ValueError("budget.max_runs must be int >= 1")
        if mw is not None and (not isinstance(mw, (int, float)) or isinstance(mw, bool)
                               or mw <= 0):
            raise ValueError("budget.max_wall_s must be > 0")
        warmup = spec.get("warmup", 0)
        if not isinstance(warmup, int) or isinstance(warmup, bool) or warmup < 0:
            raise ValueError("warmup must be int >= 0")
        seed = spec.get("seed", 0)
        self.params = checked
        self.objectives = checked_obj
        self.max_runs = mr
        self.max_wall_s = float(mw) if mw is not None else None
        self.warmup = warmup
        self.rng = random.Random(seed)
        self._n_candidates = 1500         # 每轮 TPE 候选点数(高维下 256 太稀)
        self._bw_base = 0.10               # KDE 带宽基数(单位空间, 8维40趟扫描锁定)
        self._good_frac = 0.25            # good 组目标覆盖比例
        self._min_bandwidth_frac = 0.02   # 相对范围的最小带宽(单位空间)
        self._min_dist_frac = 0.1         # 距离惩罚的目标最小距离(单位空间)
        self._explore_prob = 0.15         # epsilon-greedy:纯探索候选概率
        # v2 穷举路由:全空间有限组合数 <= 上限且 <= 预算 -> 直接穷举(白盒规则)
        self._exhaust_max = 24
        self._mode = "tpe"
        self._exhaust_points = None
        self._exhaust_idx = 0
        counts = []
        for p in checked:
            if p["type"] == "categorical":
                counts.append(len(p["choices"]))
            elif p["step"] is not None:
                counts.append(int(round((p["max"] - p["min"]) / p["step"])) + 1)
            else:
                counts.append(None)
        if all(c is not None for c in counts):
            total = 1
            for c in counts:
                total *= c
            if (total <= self._exhaust_max
                    and (self.max_runs is None or total <= self.max_runs)):
                self._mode = "exhaust"
                self._total_exhaust = total
                self._exhaust_points = self._build_exhaust_points()
        # v2 init 组合(全部参数声明了 init 时第一趟用之)
        self._init_point = None
        if all(p["init"] is not None for p in checked):
            self._init_point = {p["name"]: p["init"] for p in checked}

    def _build_exhaust_points(self):
        """穷举点集(笛卡尔积,顺序确定)。"""
        import itertools
        dims = []
        for p in self.params:
            if p["type"] == "categorical":
                dims.append(list(p["choices"]))
            else:
                n = int(round((p["max"] - p["min"]) / p["step"])) + 1
                dims.append([p["min"] + i * p["step"] for i in range(n)])
        pts = []
        for combo in itertools.product(*dims):
            pts.append({p["name"]: combo[i] for i, p in enumerate(self.params)})
        return pts

    # -------------------------------------------------------------- helpers
    def _objective_vector(self, obs):
        """单观察 -> 统一方向的数值向量(越大越 good);任一键缺失/None 返回 None。"""
        vec = []
        for o in self.objectives:
            v = obs.get(o["key"])
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                return None
            vec.append(float(v) * o["sign"])
        return vec

    def _ranges(self):
        return [(p["min"], p["max"]) for p in self.params]

    def _to_unit(self, point):
        out = []
        for i, p in enumerate(self.params):
            if p["type"] == "categorical":
                try:
                    idx = p["choices"].index(point[i])
                except (ValueError, TypeError):
                    idx = 0
                out.append(idx / max(len(p["choices"]) - 1, 1))
            else:
                lo, hi = p["min"], p["max"]
                out.append(_clamp((point[i] - lo) / (hi - lo), 0.0, 1.0))
        return out

    def _from_unit(self, u, apply_step=True):
        out = []
        for i, p in enumerate(self.params):
            if p["type"] == "categorical":
                idx = int(round(u[i] * (len(p["choices"]) - 1)))
                idx = _clamp(idx, 0, len(p["choices"]) - 1)
                out.append(p["choices"][idx])
            else:
                lo, hi = p["min"], p["max"]
                v = lo + u[i] * (hi - lo)
                if apply_step and p["step"] is not None:
                    v = round(v / p["step"]) * p["step"]
                out.append(_clamp(v, lo, hi))
        return out

    def _min_dist_unit(self, u, observed_unit):
        """候选点到已观察点集的最小欧氏距离(单位空间,按维度归一)。"""
        if not observed_unit:
            return 1.0
        return min(math.sqrt(sum((a - b) ** 2 for a, b in zip(u, o))
                             / max(len(u), 1))
                   for o in observed_unit)

    # -------------------------------------------------------------- propose
    def propose(self, observations, wall_s=None):
        """observations: [{"params": {name: value}, "objectives": {key: value}}]。
        返回 {"params": {...}, "phase": "init"|"exhaust"|"warmup"|"tpe",
        "reason": str} 或 None(预算耗尽)。"""
        obs = list(observations or [])
        n = len(obs)
        if self.max_runs is not None and n >= self.max_runs:
            return None
        if (self.max_wall_s is not None and wall_s is not None
                and wall_s >= self.max_wall_s):
            return None

        # v2: 首趟用 init 组合(当前可工作参数的基线复测)
        if self._init_point is not None and n == 0:
            return {"params": dict(self._init_point), "phase": "init",
                    "reason": "init"}
        # v2: 穷举模式(全空间组合数小 -> 白盒全因子,不浪费 TPE 样本)
        if self._mode == "exhaust":
            if self._exhaust_idx >= len(self._exhaust_points):
                return None
            p = self._exhaust_points[self._exhaust_idx]
            self._exhaust_idx += 1
            return {"params": dict(p), "phase": "exhaust",
                    "reason": "exhaust %d/%d" % (self._exhaust_idx,
                                                self._total_exhaust)}

        d = len(self.params)
        def _observed_unit():
            return [self._to_unit([o["params"][p["name"]] for p in self.params])
                    for o in obs if o.get("params")]

        if n < self.warmup:
            # 空间填充:分层采样,挑距已观察点最远者(最大化最小距离)
            cand = _lhs_points(self._n_candidates, d, self.rng)
            best_u = max(cand, key=lambda u: self._min_dist_unit(u, _observed_unit()))
            return self._pack_proposal(best_u, "warmup")

        # ---- TPE 分组 ----
        unit_points = [self._to_unit([o["params"][p["name"]] for p in self.params])
                       for o in obs if o.get("params")]
        if not unit_points or len(unit_points) != len(obs):
            cand = _lhs_points(self._n_candidates, d, self.rng)
            return self._pack_proposal(cand[0], "warmup")

        vecs = [self._objective_vector(o.get("objectives", {})) for o in obs]
        layers, _unknown = _pareto_layers(vecs)
        good = set()
        acc = 0
        for layer in layers:
            for i in layer:
                good.add(i)
            acc += len(layer)
            if acc >= max(1, int(n * self._good_frac)):
                break
        if not good and layers:
            good = set(layers[0])
        bad = [i for i in range(n) if i not in good]

        if len(good) < 1 or len(bad) < 1:
            cand = _lhs_points(self._n_candidates, d, self.rng)
            best_u = max(cand, key=lambda u: self._min_dist_unit(u, unit_points))
            return self._pack_proposal(best_u, "tpe")

        # ---- 每维截断高斯核 KDE ----
        # 带宽用固定启发式(单位范围比例)。高维时样本稀疏,带宽必须显著大于
        # 组内 std,否则 KDE 退化成尖峰、密度比只在旧 good 点处爆表。
        def kde_params(idxs):
            n_g = max(len(idxs), 1)
            sigma = max(self._min_bandwidth_frac,
                        self._bw_base / math.sqrt(n_g)
                        * math.sqrt(1.0 + d / 4.0))
            mus, sigmas = [], []
            for di in range(d):
                vals = [unit_points[i][di] for i in idxs]
                mu = sum(vals) / len(vals)
                mus.append(mu)
                sigmas.append(sigma)
            return mus, sigmas

        g_mu, g_sig = kde_params(sorted(good))
        b_mu, b_sig = kde_params(bad)

        def density(u, mus, sigmas, idxs):
            # 连续维用截断高斯核;分类维用拉普拉斯平滑类别频率(v2)
            p = 1.0
            for di in range(d):
                if self.params[di]["type"] == "categorical":
                    nc = len(self.params[di]["choices"])
                    k = int(round(u[di] * (nc - 1)))
                    vals = [int(round(unit_points[i][di] * (nc - 1)))
                            for i in idxs]
                    cnt = sum(1 for v in vals if v == k)
                    p *= (cnt + 1.0) / (len(vals) + nc)
                else:
                    p *= _trunc_pdf(u[di], mus[di], sigmas[di], 0.0, 1.0)
            return p

        # ---- 候选 = LHS 点,取 good/bad 密度比 * 距离惩罚 最大 ----
        cand = _lhs_points(self._n_candidates, d, self.rng)
        # epsilon-greedy:部分轮次纯探索(均匀 LHS 随机取),防过早锁死
        if self.rng.random() < self._explore_prob:
            return self._pack_proposal(self.rng.choice(cand), "tpe")
        best_u, best_util = None, -1.0
        for u in cand:
            pg = density(u, g_mu, g_sig, sorted(good))
            pb = max(density(u, b_mu, b_sig, bad), 1e-12)
            dist_penalty = min(1.0,
                               self._min_dist_unit(u, unit_points)
                               / self._min_dist_frac)
            util = (pg / pb) * (0.5 + 0.5 * dist_penalty)
            if util > best_util:
                best_u, best_util = u, util
        if best_u is None:
            best_u = _lhs_points(1, d, self.rng)[0]
        return self._pack_proposal(best_u, "tpe")

    def _pack_proposal(self, u, phase):
        point = self._from_unit(u)
        params = {p["name"]: point[i] for i, p in enumerate(self.params)}
        return {"params": params, "phase": phase, "reason": phase}

    # -------------------------------------------------------------- summary
    def summary(self, observations, wall_s=None):
        """跑完汇总:best(首目标)/pareto_front/convergence/budget。"""
        obs = list(observations or [])
        best = None
        for o in obs:
            vec = self._objective_vector(o.get("objectives", {}))
            if vec is None:
                continue
            if best is None or vec[0] > best[0]:
                best = (vec[0], o)
        best_out = None
        if best is not None:
            o = best[1]
            best_out = {"params": o.get("params"),
                        "objectives": o.get("objectives", {})}
        layers, _ = _pareto_layers([self._objective_vector(o.get("objectives", {}))
                                    for o in obs])
        front = []
        if layers:
            for i in layers[0]:
                o = obs[i]
                front.append({"params": o.get("params"),
                              "objectives": o.get("objectives", {})})
        conv = []
        for o in obs:
            vec = self._objective_vector(o.get("objectives", {}))
            conv.append({"params": o.get("params"),
                         "objectives": o.get("objectives", {}),
                         "_rank": vec[0] if vec is not None else None})
        conv.sort(key=lambda c: (c["_rank"] is None, -(c["_rank"] or 0.0)))
        for c in conv:
            c.pop("_rank", None)
        out = {"n_runs": len(obs), "best": best_out, "pareto_front": front,
               "convergence": conv}
        if self.max_runs is not None:
            out["budget"] = {"max_runs": self.max_runs,
                             "runs_left": max(0, self.max_runs - len(obs))}
        if wall_s is not None:
            out["wall_s"] = round(wall_s, 1)
            if self.max_wall_s is not None:
                out.setdefault("budget", {})["max_wall_s"] = self.max_wall_s
                out["budget"]["wall_left"] = round(
                    max(0.0, self.max_wall_s - wall_s), 1)
        return out
