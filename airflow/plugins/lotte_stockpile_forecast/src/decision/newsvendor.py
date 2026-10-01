"""
뉴스벤더 기반 비축량 결정.

tau = Cu / (Cu + Co)   (Cu: 결품 단위비용, Co: 과잉 단위비용)  ->  비축량 = Q_tau(윈도우 누적수요) - 윈도우 시작 시점 예상 재고
분위수 모델은 [0.5, 0.7, 0.8, 0.9] 에서 학습되므로 tau 는 선형 보간. 학습 범위를 넘으면 최대 분위수로 클립 + 경고.
누적 곡선: 과거 연도 윈도우 내 주차별 비중 평균 x 총량 분위수 -> 생산 phasing 점검용.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ..models.quantile_lgbm import qcol

log = logging.getLogger(__name__)


def critical_ratio(cu: float, co: float) -> float:
    if cu <= 0 or co <= 0:
        raise ValueError("cu, co 는 양수여야 함")
    return cu / (cu + co)


def quantile_at(row: pd.Series, alphas: list[float], tau: float) -> float:
    a = np.array(sorted(alphas))
    v = np.array([row[qcol(x)] for x in a], dtype=float)
    if tau > a.max():
        log.warning("tau=%.2f 가 학습 분위수 최대 %.2f 초과 -> 클립. 더 높은 서비스레벨은 quantiles 설정에 alpha 추가 필요", tau, a.max())
        return float(v[-1])
    if tau < a.min():
        return float(v[0])
    return float(np.interp(tau, a, v))


def decide_stockpile(brand_total: pd.DataFrame, alphas: list[float], cu: float, co: float,
                     opening_inventory: float = 0.0, judgmental_buffer_ratio: float = 0.0) -> dict:
    """brand_total: aggregate_m1_to_brand 결과 1행. judgmental_buffer 는 모델 외 판단(SNS 유행 등) - 분리 기록."""
    tau = critical_ratio(cu, co)
    row = brand_total.iloc[0]
    q_tau = quantile_at(row, alphas, tau)
    model_stock = max(0.0, q_tau - opening_inventory)
    buffer = model_stock * judgmental_buffer_ratio
    return dict(brand=row["brand"], year=int(row["year"]), tau=round(tau, 3),
                demand_q50=float(row[qcol(0.5)]), demand_q_tau=q_tau, benchmark=float(row.get("benchmark", np.nan)),
                opening_inventory=opening_inventory, stockpile_model=model_stock,
                judgmental_buffer=buffer, stockpile_total=model_stock + buffer)


def cumulative_curve(panel_brand: pd.DataFrame, brand: str, cfg: dict, total_q: float, target_year: int) -> pd.DataFrame:
    """과거 연도 윈도우 주차별 출고 비중 평균 -> 누적 곡선 (target_year 주차 라벨). 생산·입고 계획이 이 곡선 위에 있어야 함."""
    lo, hi = cfg["brands"][brand]["m1"]["window_iso_weeks"]
    p = panel_brand[(panel_brand["brand"] == brand) & (panel_brand["iso_week"] >= lo) & (panel_brand["iso_week"] <= hi)
                    & panel_brand["qty_clean"].notna()].copy()
    p["year"] = p["week_start"].dt.year
    p = p[p["year"] < target_year]
    wk = p.groupby(["year", "iso_week"])["qty_clean"].sum().reset_index()
    wk["share"] = wk["qty_clean"] / wk.groupby("year")["qty_clean"].transform("sum")
    shape = wk.groupby("iso_week")["share"].mean()
    shape = shape / shape.sum()
    out = pd.DataFrame({"iso_week": shape.index, "share": shape.values})
    out["cum_share"] = out["share"].cumsum()
    out["cum_demand_q_tau"] = out["cum_share"] * total_q
    return out
