"""模型层：LightGBM 分位数回归 + 经济先验（两阶段）+ 交叉修复 + purged walk-forward。

⚠️ 实测踩到的坑（写在这里免得再踩）：
   LightGBM 明确禁止在 quantile objective 上使用 monotone_constraints：
       [LightGBM] [Fatal] Cannot use ``monotone_constraints`` in quantile objective
   所以「把经验法则硬编码进模型」不能靠给分位数模型加约束来实现，
   改成两阶段：
       阶段 1  分位数模型（无约束）→ 输出 P10/P50/P90
       阶段 2  经济先验模型（objective=l2 + 单调约束）→ 输出 prior 分数
               单调约束在这里是合法且有效的，用来做融合排序与稳健性校验。
   prior 必须在报告里单独说明，不能假装分位数模型本身带单调性。
"""
from __future__ import annotations

import logging
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import lightgbm as lgb
import numpy as np
import pandas as pd

from .config import CFG, FEATURE_COLS, MONOTONE, Cfg

log = logging.getLogger("model")


def _mono(cols: list[str]) -> list[int]:
    return [MONOTONE.get(c, 0) for c in cols]


def _jobs(cfg: Cfg) -> int | None:
    """num_threads=0 交给 LightGBM 自己决定，否则显式限制核数"""
    return cfg.num_threads if cfg.num_threads and cfg.num_threads > 0 else None


# ---------------------------------------------------------------- 阶段 1：分位数
def train_one(
    X: pd.DataFrame,
    y: pd.Series,
    w: np.ndarray,
    tau: float,
    cfg: Cfg,
    seed: int = 42,
) -> lgb.LGBMRegressor:
    m = lgb.LGBMRegressor(
        objective="quantile",
        alpha=tau,                      # LightGBM 分位数的 tau 参数名是 alpha
        n_estimators=cfg.n_estimators,
        learning_rate=cfg.learning_rate,
        num_leaves=cfg.num_leaves,      # 样本少，树必须浅
        min_child_samples=cfg.min_child_samples,
        reg_lambda=cfg.reg_lambda,
        subsample=cfg.subsample,
        subsample_freq=1,
        colsample_bytree=cfg.colsample,
        random_state=seed,
        verbose=-1,
        n_jobs=_jobs(cfg),
    )
    m.fit(X, y, sample_weight=w)
    return m


# ---------------------------------------------------------------- 阶段 2：经济先验
def train_prior(
    X: pd.DataFrame,
    y: pd.Series,
    w: np.ndarray,
    cfg: Cfg,
    seed: int = 43,
) -> lgb.LGBMRegressor:
    """带单调约束的先验模型。

    这里才是 monotone_constraints 合法的位置（非 quantile objective）。
    它表达的是你那条判断：箱子越稀有 → 越好；存世增速越快 / 在售占比越高 → 越差。
    小样本下，这个约束能砍掉大量违反经济直觉的分裂，是防过拟合的主力。
    """
    m = lgb.LGBMRegressor(
        objective="regression",         # L2 均值回归，可用单调约束
        n_estimators=cfg.n_estimators,
        learning_rate=cfg.learning_rate,
        num_leaves=cfg.num_leaves,
        min_child_samples=cfg.min_child_samples,
        reg_lambda=cfg.reg_lambda,
        subsample=cfg.subsample,
        subsample_freq=1,
        colsample_bytree=cfg.colsample,
        monotone_constraints=_mono(list(X.columns)),
        monotone_constraints_method="advanced",
        random_state=seed,
        verbose=-1,
        n_jobs=_jobs(cfg),
    )
    m.fit(X, y, sample_weight=w)
    return m


def train_all(X: pd.DataFrame, y: pd.Series, w: np.ndarray, cfg: Cfg, with_prior: bool = True) -> "QuantileModel":
    models = {tau: train_one(X, y, w, tau, cfg) for tau in sorted(cfg.quantiles)}
    prior = train_prior(X, y, w, cfg) if with_prior else None
    return QuantileModel(models=models, cols=list(X.columns), version=cfg.model_version, prior=prior)


# ---------------------------------------------------------------- 推理
def fix_crossing(preds: np.ndarray) -> np.ndarray:
    """分位数交叉修复：逐行保序，保证 q10 <= q50 <= q90"""
    return np.sort(preds, axis=1)


@dataclass
class QuantileModel:
    models: dict[float, lgb.LGBMRegressor]
    cols: list[str]
    version: str
    prior: lgb.LGBMRegressor | None = field(default=None)

    def _X(self, X: pd.DataFrame) -> pd.DataFrame:
        return X.reindex(columns=self.cols, fill_value=0.0)

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        Xi = self._X(X)
        taus = sorted(self.models)
        raw = np.vstack([self.models[t].predict(Xi) for t in taus]).T
        return fix_crossing(raw)   # 列顺序 = 递增的 tau

    def predict_prior(self, X: pd.DataFrame) -> np.ndarray | None:
        if self.prior is None:
            return None
        return self.prior.predict(self._X(X))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(self, f)
        log.info("模型已保存：%s（%d 个分位数 + prior=%s）",
                 path, len(self.models), "yes" if self.prior else "no")

    @staticmethod
    def load(path: Path) -> "QuantileModel":
        with open(path, "rb") as f:
            obj = pickle.load(f)
        if not isinstance(obj, QuantileModel):
            raise TypeError("模型文件格式不符")
        return obj


# ---------------------------------------------------------------- 评估
def pinball(y: np.ndarray, q: np.ndarray, tau: float) -> float:
    e = y - q
    return float(np.maximum(tau * e, (tau - 1) * e).mean())


def purged_wf_splits(
    dates: pd.Series, n_splits: int, horizon: int, embargo: int
) -> Iterable[tuple[np.ndarray, np.ndarray]]:
    """purged walk-forward：训练段必须在测试段开始前 (horizon + embargo) 天结束。

    随机 K-fold 在这个问题上等于自欺 —— 同一段行情会同时出现在训练和测试里，
    你会得到 AUC 0.85 的幻觉。
    """
    d = pd.to_datetime(dates).dt.normalize()
    uniq = np.sort(d.unique())
    if len(uniq) < n_splits * 3:
        return
    edges = np.array_split(np.arange(len(uniq)), n_splits + 1)
    for i in range(1, len(edges) - 1):
        test_days = uniq[edges[i]]
        cut = test_days[0] - np.timedelta64(horizon + embargo, "D")
        tr = np.flatnonzero(d <= cut)
        te = np.flatnonzero(d.isin(test_days))
        if len(tr) < 300 or len(te) < 30:
            continue
        yield tr, te


def walk_forward(df: pd.DataFrame, cfg: Cfg) -> tuple[pd.DataFrame, dict[str, float]]:
    """返回 (逐折指标, 无条件基线指标)。打不过基线就说明特征没有信息量。"""
    from .labels import sample_weights

    d = df.dropna(subset=["y"]).sort_values("date").reset_index(drop=True)
    if "is_warm" in d.columns:
        d = d[d["is_warm"]].reset_index(drop=True)
    if d.empty:
        return pd.DataFrame(), {}

    rows: list[dict[str, Any]] = []
    for fold, (tr, te) in enumerate(purged_wf_splits(d["date"], cfg.n_splits, cfg.horizon, cfg.embargo)):
        trd, ted = d.iloc[tr], d.iloc[te]
        if len(trd) < 300 or len(ted) < 30:
            continue
        w = sample_weights(trd, cfg)
        mdl = train_all(trd[FEATURE_COLS], trd["y"], w, cfg)
        P = mdl.predict(ted[FEATURE_COLS])
        rec: dict[str, Any] = {"fold": fold, "n_train": len(trd), "n_test": len(ted)}
        ya = ted["y"].to_numpy()
        for k, tau in enumerate(sorted(cfg.quantiles)):
            rec[f"pinball_{tau}"] = pinball(ya, P[:, k], tau)
            rec[f"cover_{tau}"] = float((ya <= P[:, k]).mean())
        rows.append(rec)

    y_all = d["y"].to_numpy()
    base = {
        f"pinball_{tau}": pinball(y_all, np.full_like(y_all, np.quantile(y_all, tau)), tau)
        for tau in cfg.quantiles
    }
    return pd.DataFrame(rows), base


def acceptance(metrics: pd.DataFrame, base: dict[str, float]) -> tuple[bool, list[str]]:
    """验收标准：跑不过这一关就不要上实盘"""
    notes: list[str] = []
    ok = True
    if metrics.empty:
        return False, ["没有产生任何折，样本量不足"]

    for tau in sorted(CFG.quantiles):
        col = f"pinball_{tau}"
        if col not in metrics:
            continue
        got, ref = float(metrics[col].mean()), base.get(col, float("nan"))
        improve = (ref - got) / abs(ref) if ref else 0.0
        cover = float(metrics.get(f"cover_{tau}", pd.Series([np.nan])).mean())
        notes.append(f"tau={tau}: pinball {got:.4f} vs 基线 {ref:.4f}（改善 {improve:+.1%}），覆盖率 {cover:.1%}")
        if improve < 0.08:
            ok = False
            notes.append(f"  ✗ tau={tau} 相对基线改善不足 8%")
        if not (0.7 * tau <= cover <= 1.35 * tau + 0.03):
            ok = False
            notes.append(f"  ✗ tau={tau} 覆盖率偏离标称值（{cover:.1%} vs {tau:.0%}）")
    return ok, notes


def ablation(df: pd.DataFrame, cfg: Cfg, drop: list[str]) -> dict[str, float]:
    """消融：移除指定特征后看 pinball 恶化多少。

    这是检验「箱子稀有度到底值不值钱」的唯一诚实办法 —— 别用直觉，用数字。
    """
    from .labels import sample_weights

    d = df.dropna(subset=["y"]).sort_values("date").reset_index(drop=True)
    if "is_warm" in d.columns:
        d = d[d["is_warm"]].reset_index(drop=True)
    cols = [c for c in FEATURE_COLS if c not in drop]
    if not cols or d.empty:
        return {}

    out: dict[str, list[float]] = {}
    for tr, te in purged_wf_splits(d["date"], cfg.n_splits, cfg.horizon, cfg.embargo):
        trd, ted = d.iloc[tr], d.iloc[te]
        if len(trd) < 300 or len(ted) < 30:
            continue
        w = sample_weights(trd, cfg)
        # 手动构造只含子集列的模型
        models = {t: train_one(trd[cols], trd["y"], w, t, cfg) for t in sorted(cfg.quantiles)}
        P = fix_crossing(np.vstack([models[t].predict(ted[cols]) for t in sorted(cfg.quantiles)]).T)
        ya = ted["y"].to_numpy()
        for k, tau in enumerate(sorted(cfg.quantiles)):
            out.setdefault(f"pinball_{tau}", []).append(pinball(ya, P[:, k], tau))

    return {k: float(np.mean(v)) for k, v in out.items()}
