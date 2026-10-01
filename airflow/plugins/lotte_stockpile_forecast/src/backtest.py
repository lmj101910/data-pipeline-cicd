"""
백테스트.

M2: 롤링 오리진 (origin 을 step 주 간격으로 이동, 매 origin 재학습) -> h 별 pinball loss / coverage / bias, 벤치마크 대비.
M1: leave-last-year-out (연도 y 를 y-1 까지로 학습해 예측) -> 연도별 총량 오차, coverage.

지표 해석:
  pinball(tau)  : 비대칭 손실. tau 분위수 예측의 직접 목표.
  coverage(q)   : 실적 <= 예측 비율. 잘 보정됐으면 q 에 근접 (부족 회피가 목표이므로 q 이상이면 허용, 크게 낮으면 위험).
  bias          : (q50 - 실적)/실적 평균. 음수 = 과소예측 경향 (이 문제에서 가장 피해야 할 방향).
MAPE 는 참고용. 과소예측에 유리한 지표라 목표로 삼지 말 것.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .models.m1_event_total import (aggregate_m1_to_brand, as_of, attach_actuals, build_m1_table,
                                    event_shock_sd_from_search, fit_predict_m1, window_totals)
from .models.m2_weekly import make_supervised, train_predict_m2
from .models.quantile_lgbm import pinball_loss, qcol

log = logging.getLogger(__name__)


def _metrics(df: pd.DataFrame, alphas: list[float]) -> dict:
    y = df["actual"].values.astype(float)
    m = {"n": len(df)}
    for a in alphas:
        q = df[qcol(a)].values.astype(float)
        m[f"pinball_{qcol(a)}"] = pinball_loss(y, q, a)
        m[f"coverage_{qcol(a)}"] = float(np.mean(y <= q))
    q50 = df[qcol(0.5)].values
    mask = y > 0
    m["bias_q50"] = float(np.mean((q50[mask] - y[mask]) / y[mask])) if mask.any() else np.nan
    m["mape_q50"] = float(np.mean(np.abs(q50[mask] - y[mask]) / y[mask])) if mask.any() else np.nan
    b = df["benchmark"].values.astype(float)
    ok = np.isfinite(b)
    m["pinball_q50_benchmark"] = pinball_loss(y[ok], b[ok], 0.5) if ok.any() else np.nan
    m["mape_benchmark"] = float(np.mean(np.abs(b[ok & mask] - y[ok & mask]) / y[ok & mask])) if (ok & mask).any() else np.nan
    return m


def backtest_m2(panel: pd.DataFrame, brand: str, cfg: dict, start: pd.Timestamp, end: pd.Timestamp,
                step_weeks: int = 4, weather_daily=None, forecast=None, brand_level: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    """origin 을 start~end 사이 step 주 간격으로 이동. 반환: (origin별 예측+실적, h별 지표 요약)."""
    alphas = cfg["quantiles"]
    pb = panel[panel["brand"] == brand]
    actual_map = {}
    for h in cfg["brands"][brand]["m2"]["horizons"]:
        sup = make_supervised(pb, h, False, False)
        actual_map[h] = sup.set_index(["series_id", "origin_week"])["actual"]
    origins = pd.date_range(start, end, freq=f"{7 * step_weeks}D")
    frames = []
    for o in origins:
        try:
            pred, _ = train_predict_m2(panel, brand, cfg, o, weather_daily, forecast)
        except Exception as e:  # noqa: BLE001
            log.warning("backtest origin %s 실패: %s", o.date(), e)
            continue
        pred["actual"] = [actual_map[h].get((s, o), np.nan) for s, h in zip(pred["series_id"], pred["h"])]
        frames.append(pred)
        log.info("backtest origin %s 완료", o.date())
    allp = pd.concat(frames, ignore_index=True)
    allp = allp[allp["actual"].notna()]
    if brand_level:
        cols = [qcol(a) for a in alphas] + ["benchmark", "actual"]
        agg = allp.groupby(["brand", "origin_week", "target_week", "h"], as_index=False)[cols].sum()
    else:
        agg = allp
    summary = pd.DataFrame([{"h": h, **_metrics(g, alphas)} for h, g in agg.groupby("h")])
    return agg, summary


def backtest_m1(panel: pd.DataFrame, brand: str, cfg: dict, origin_mmdd: str, preorders=None, search=None) -> pd.DataFrame:
    """각 연도 y 에 대해 y-1 까지 학습 -> y 총량 예측. 연도가 3개면 테스트 포인트는 최대 2개."""
    alphas = cfg["quantiles"]
    sd_search = event_shock_sd_from_search(search, brand, cfg)
    floor = cfg["brands"][brand]["m1"].get("common_shock_sd_floor", 0.1)
    pb = panel[panel["brand"] == brand]
    years = sorted(pb["week_start"].dt.year.unique())
    actual_all = window_totals(panel, brand, cfg)
    rows = []
    for y in years[1:]:
        origin = pd.Timestamp(f"{y}-{origin_mmdd}")
        act = actual_all[actual_all["year"] == y]
        if len(act) == 0 or act["total"].isna().any():
            continue  # 실적 미완결 연도
        tbl = build_m1_table(as_of(pb, origin), brand, cfg, origin, preorders)
        tbl = tbl[tbl["year"] <= y]
        if len(tbl[(tbl["year"] < y) & tbl["log_uplift"].notna()]) == 0:
            continue
        pred = fit_predict_m1(tbl, y, cfg)
        pred = attach_actuals(pred, act)
        tot = aggregate_m1_to_brand(pred, alphas, cfg["newsvendor"].get("aggregation", "simulate"),
                                    common_shock_sd=sd_search, common_sd_floor=floor)
        rec = {"year": y, "actual": float(act["total"].sum()), "benchmark": float(tot["benchmark"].iloc[0])}
        for a in alphas:
            rec[qcol(a)] = float(tot[qcol(a)].iloc[0])
            rec[f"covered_{qcol(a)}"] = bool(rec["actual"] <= rec[qcol(a)])
        rec["err_q50_pct"] = (rec[qcol(0.5)] - rec["actual"]) / rec["actual"]
        rec["err_benchmark_pct"] = (rec["benchmark"] - rec["actual"]) / rec["actual"] if np.isfinite(rec["benchmark"]) else np.nan
        rows.append(rec)
    return pd.DataFrame(rows)
