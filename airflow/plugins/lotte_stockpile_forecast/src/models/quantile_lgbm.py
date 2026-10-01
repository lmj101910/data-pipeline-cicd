"""
LightGBM 분위수 예측 래퍼 - 두 가지 모드.

mode="quantile"   : alpha 별 LightGBM quantile objective. 핀볼 손실을 직접 최적화.
                    * LightGBM 제약: quantile objective 에는 monotone_constraints 를 쓸 수 없음 -> 단조 제약 무시(경고).
mode="conformal"  : 단조 제약을 건 L2 중심 모델(로그 공간) 1개 + out-of-fold 잔차의 경험적 분위수 (split-conformal).
                    * 단조 제약 사용 가능, 소표본에서 상단 분위수가 더 안정적. 잔차 분위수는 그룹(in_window 등)별로 계산 가능.
                    * 단점: 구간 폭이 그룹 내에서 동일(등분산 가정, 로그 변환으로 완화).

두 모드는 backtest 로 비교해 브랜드별로 선택 (config.lgbm.mode).
공통: 작은 트리, 큰 min_data_in_leaf, 강한 L2, bagging -> 소표본 과적합 억제. 예측 분위수는 정렬로 교차 방지.
"""
from __future__ import annotations

import inspect
import logging
from typing import Sequence

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)
_WARNED_MONO = set()


def qcol(alpha: float) -> str:
    return f"q{int(round(alpha * 100)):02d}"


class QuantileLGBM:
    def __init__(self, alphas: Sequence[float], params: dict, monotone: dict | None = None,
                 categorical: Sequence[str] = (), mode: str | None = None, conformal_group: str | None = "in_window"):
        self.alphas = list(alphas)
        p = dict(params)
        self.mode = mode or p.pop("mode", "quantile")
        p.pop("mode", None)
        self.es_rounds = p.pop("early_stopping_rounds", 50)
        self.params = p
        self.monotone = monotone or {}
        self.categorical = list(categorical)
        self.conformal_group = conformal_group
        self.models: dict = {}
        self.features: list[str] = []
        self.best_iters: dict = {}
        self.resid_q: dict = {}   # conformal: {group_value: {alpha: q}}
        self.cat_levels: dict = {}

    # ------------------------------------------------------------------ utils
    def _prep(self, X: pd.DataFrame) -> pd.DataFrame:
        """범주형은 학습 시 고정한 레벨로 pandas category 변환 (학습/검증/예측 간 코드 일치)."""
        X = X.copy()
        for c in self.categorical:
            if c in X.columns:
                if c not in self.cat_levels:
                    self.cat_levels[c] = sorted(X[c].dropna().astype(str).unique())
                X[c] = pd.Categorical(X[c].astype(str), categories=self.cat_levels[c])
        return X

    def _cats(self, X: pd.DataFrame):
        return "auto"  # pandas category dtype 자동 인식

    def _mono(self) -> list[int]:
        return [int(self.monotone.get(c, 0)) for c in self.features]

    def _fit_one(self, objective: str, X, y, X_val, y_val, w, mono, alpha=None):
        import lightgbm as lgb

        kw = dict(objective=objective, verbose=-1, random_state=42, **self.params)
        if alpha is not None:
            kw["alpha"] = alpha
        if mono is not None and any(mono):
            kw["monotone_constraints"] = mono
        m = lgb.LGBMRegressor(**kw)
        fit_kw = {}
        if X_val is not None and len(X_val) > 0:
            fit_kw = dict(callbacks=[lgb.early_stopping(self.es_rounds, verbose=False)])
            if "eval_X" in inspect.signature(m.fit).parameters:      # lightgbm >= 4.7
                fit_kw.update(eval_X=self._prep(X_val), eval_y=np.asarray(y_val, dtype=float))
            else:
                fit_kw.update(eval_set=[(self._prep(X_val), y_val)])
        m.fit(self._prep(X), y, sample_weight=w, categorical_feature=self._cats(X), **fit_kw)
        best = int(m.best_iteration_) if (X_val is not None and m.best_iteration_) else int(self.params.get("n_estimators", 100))
        return m, best

    # ------------------------------------------------------------------ fit
    def fit(self, X: pd.DataFrame, y: pd.Series, X_val=None, y_val=None, sample_weight=None,
            groups: pd.Series | None = None) -> "QuantileLGBM":
        self.features = list(X.columns)
        y = pd.Series(np.asarray(y, dtype=float), index=X.index)
        if self.mode == "quantile":
            if any(self._mono()) and "q" not in _WARNED_MONO:
                _WARNED_MONO.add("q")
                log.warning("quantile objective 는 monotone_constraints 미지원 -> 단조 제약 무시. 필요하면 mode=conformal 사용")
            for a in self.alphas:
                self.models[a], self.best_iters[a] = self._fit_one("quantile", X, y, X_val, y_val, sample_weight, None, alpha=a)
        elif self.mode == "conformal":
            from sklearn.model_selection import KFold

            mono = self._mono()
            self.models["center"], self.best_iters["center"] = self._fit_one("regression", X, y, X_val, y_val, sample_weight, mono)
            # out-of-fold 잔차 (학습 데이터 재사용으로 인한 낙관 편향 방지)
            oof = np.full(len(X), np.nan)
            n_splits = 5 if len(X) >= 200 else 3
            for tr_idx, te_idx in KFold(n_splits, shuffle=True, random_state=42).split(X):
                m, _ = self._fit_one("regression", X.iloc[tr_idx], y.iloc[tr_idx], None, None,
                                     None if sample_weight is None else np.asarray(sample_weight)[tr_idx], mono)
                m.set_params(n_estimators=self.best_iters["center"])
                oof[te_idx] = m.predict(self._prep(X.iloc[te_idx]))
            resid = y.values - oof
            g = self._group_values(X, groups)
            for gv in np.unique(g):
                r = resid[g == gv]
                self.resid_q[gv] = {a: float(np.quantile(r, a)) for a in self.alphas}
            self.resid_q["__all__"] = {a: float(np.quantile(resid, a)) for a in self.alphas}
        else:
            raise ValueError(f"unknown mode {self.mode}")
        log.info("QuantileLGBM[%s] fit: n=%d, features=%d, best_iters=%s", self.mode, len(X), len(self.features), self.best_iters)
        return self

    def _group_values(self, X: pd.DataFrame, groups: pd.Series | None) -> np.ndarray:
        if groups is not None:
            return np.asarray(groups).astype(str)
        if self.conformal_group and self.conformal_group in X.columns:
            return X[self.conformal_group].fillna(0).astype(int).astype(str).values
        return np.array(["__all__"] * len(X))

    # ------------------------------------------------------------------ predict
    def predict(self, X: pd.DataFrame, groups: pd.Series | None = None) -> pd.DataFrame:
        Xp = X[self.features]
        if self.mode == "quantile":
            preds = np.column_stack([self.models[a].predict(self._prep(Xp)) for a in self.alphas])
        else:
            center = self.models["center"].predict(self._prep(Xp))
            g = self._group_values(Xp, groups)
            preds = np.column_stack([center + np.array([self.resid_q.get(gv, self.resid_q["__all__"])[a] for gv in g])
                                     for a in self.alphas])
        preds = np.sort(preds, axis=1)  # 분위수 교차 방지
        return pd.DataFrame(preds, columns=[qcol(a) for a in self.alphas], index=X.index)

    def feature_importance(self, alpha: float | None = None) -> pd.Series:
        key = "center" if self.mode == "conformal" else (alpha or self.alphas[len(self.alphas) // 2])
        imp = pd.Series(self.models[key].booster_.feature_importance("gain"), index=self.features)
        return imp.sort_values(ascending=False)


def pinball_loss(y: np.ndarray, q: np.ndarray, alpha: float) -> float:
    diff = y - q
    return float(np.mean(np.maximum(alpha * diff, (alpha - 1) * diff)))
