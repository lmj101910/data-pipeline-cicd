"""
데이터 품질 처리.

- 결품 검열 보정: panel.py 에서 qty_adj 로 처리 (결품 SKU-일 비율 기반)
- 이상치: 이벤트 윈도우 밖에서만 robust z 로 플래그. 기본은 '플래그만' (자동 캡핑은 과소예측 방향이므로 옵션).
- 월말 밀어내기: month_end_week 플래그 (panel.py) -> 모델 변수로 흡수
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def flag_outliers(panel: pd.DataFrame, z: float = 4.0, cap: bool = False) -> pd.DataFrame:
    out = panel.sort_values(["series_id", "week_start"]).copy()
    out["outlier_flag"] = 0
    out["qty_clean"] = out["qty_adj"]
    for sid, idx in out.groupby("series_id").groups.items():
        g = out.loc[idx]
        x = np.log1p(g["qty_adj"].astype(float))
        med = x.rolling(9, center=True, min_periods=3).median()
        resid = x - med
        mad = (resid.abs().median() or 0.0) * 1.4826
        if not np.isfinite(mad) or mad == 0:
            continue
        rz = resid / mad
        flag = (rz.abs() > z) & (g["in_window"] == 0) & g["qty_adj"].notna()
        out.loc[idx, "outlier_flag"] = flag.astype(int).values
        if cap:
            capped = np.expm1(med + np.sign(resid) * z * mad)
            out.loc[idx[flag.values], "qty_clean"] = capped[flag].values
    return out


def quality_report(panel: pd.DataFrame) -> pd.DataFrame:
    """시리즈별 품질 요약: 결측(0 대체) 주 비율, 결품 주 수, 이상치 수, 마지막 관측."""
    hist = panel[panel["qty"].notna()]
    rep = hist.groupby("series_id").agg(
        weeks=("week_start", "count"),
        imputed_zero_share=("imputed_zero", "mean"),
        stockout_weeks=("stockout_share", lambda s: int((s > 0).sum())),
        outliers=("outlier_flag", "sum"),
        mean_qty=("qty", "mean"),
        last_week=("week_start", "max"),
    ).reset_index()
    return rep.sort_values("imputed_zero_share", ascending=False)
