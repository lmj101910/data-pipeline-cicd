"""개발계 결과 확인용 예측. 관측된 주간 출고 표본을 실행 기준일의 미래에 적용한다.

학습 모델을 실행하지 않으며, 오래된 실적·한 주 표본·빈 표본도 처리한다.
원본 실적 날짜는 바꾸지 않고 결과에 기준일, 표본 일자, 계산 방식을 기록한다.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from .decision.newsvendor import decide_stockpile
from .models.quantile_lgbm import qcol

SAMPLE_WEEKS = 13


def forecast_date(origin: str | None) -> pd.Timestamp:
    ts = pd.Timestamp(origin) if origin else pd.Timestamp.now(tz="Asia/Seoul")
    if ts.tzinfo is not None:
        ts = ts.tz_convert("Asia/Seoul").tz_localize(None)
    return ts.normalize()


def weekly_sample(shipments: pd.DataFrame | None, sku_master: pd.DataFrame | None,
                  brand: str, origin: str) -> tuple[pd.Series, dict]:
    """브랜드 합산 후 최근 최대 13개 관측 주를 사용. 없는 주를 실적 0으로 채우지 않는다."""
    empty = pd.Series(dtype=float)
    meta = {
        "sample_mode": True, "method": "zero_default", "data_as_of": None,
        "sample_rows": 0, "sample_weeks": 0, "sample_years": [], "n_series": 0,
        "reason": "no_usable_shipments",
    }
    if shipments is None or shipments.empty or sku_master is None or sku_master.empty:
        return empty, meta
    required_ship = {"date", "sku_code", "qty"}
    if not required_ship.issubset(shipments.columns) or not {"sku_code", "brand"}.issubset(sku_master.columns):
        raise ValueError("표본 예측에 date/sku_code/qty 및 SKU별 brand 컬럼이 필요합니다")

    codes = sku_master.loc[sku_master["brand"] == brand, "sku_code"].astype(str)
    selected = shipments[shipments["sku_code"].astype(str).isin(codes)].copy()
    selected["date"] = pd.to_datetime(selected["date"], errors="coerce")
    selected["qty"] = pd.to_numeric(selected["qty"], errors="coerce")
    selected = selected[selected["date"].notna() & np.isfinite(selected["qty"])]
    selected = selected[selected["date"] < forecast_date(origin) + pd.Timedelta(days=1)]
    if selected.empty:
        return empty, meta

    selected["week_start"] = selected["date"].dt.normalize() - pd.to_timedelta(selected["date"].dt.weekday, unit="D")
    weekly = selected.groupby("week_start")["qty"].sum().sort_index()
    weekly = weekly[np.isfinite(weekly)].clip(lower=0).tail(SAMPLE_WEEKS)
    if weekly.empty:
        return empty, meta
    used = selected[selected["week_start"].isin(weekly.index)]
    series_cols = [c for c in ("channel", "region") if c in used.columns]
    meta.update(
        method="sample_weekly_quantiles", data_as_of=str(used["date"].max().date()),
        sample_rows=int(len(used)), sample_weeks=int(len(weekly)),
        sample_years=sorted(int(y) for y in used["date"].dt.year.unique()),
        n_series=int(len(used[series_cols].drop_duplicates())) if series_cols else 1,
        reason="development_sample_mode",
    )
    return weekly, meta


def _quantiles(weekly: pd.Series, alphas: list[float], multiplier: int = 1) -> dict:
    return {qcol(a): float(weekly.quantile(a) * multiplier) if len(weekly) else 0.0 for a in alphas}


def predict_m2(shipments: pd.DataFrame | None, sku_master: pd.DataFrame | None,
               cfg: dict, brand: str, origin: str | None) -> dict:
    as_of = forecast_date(origin)
    origin_week = as_of - pd.Timedelta(days=as_of.weekday())
    weekly, meta = weekly_sample(shipments, sku_master, brand, str(as_of.date()))
    quantities = _quantiles(weekly, cfg["quantiles"])
    benchmark = float(weekly.median()) if len(weekly) else 0.0
    rows = [
        {
            "target_week": str((origin_week + pd.Timedelta(weeks=int(h))).date()),
            "h": int(h), **quantities, "benchmark": benchmark,
        }
        for h in cfg["brands"][brand]["m2"]["horizons"]
    ]
    return {
        "brand": brand, "origin": str(as_of.date()), "origin_week": str(origin_week.date()),
        **meta, "forecast": rows, "feature_importance": {},
    }


def target_window(cfg: dict, brand: str, origin: str, target_year: int | None = None) -> tuple[int, date, date]:
    """연도 미지정 시 기준일을 포함하거나 기준일 이후에 오는 가장 가까운 행사 연도."""
    as_of = forecast_date(origin).date()
    lo, hi = cfg["brands"][brand]["m1"]["window_iso_weeks"]
    year = int(target_year) if target_year is not None else as_of.year
    start, end = date.fromisocalendar(year, lo, 1), date.fromisocalendar(year, hi, 7)
    if target_year is None and end < as_of:
        year += 1
        start, end = date.fromisocalendar(year, lo, 1), date.fromisocalendar(year, hi, 7)
    return year, start, end


def predict_m1(shipments: pd.DataFrame | None, sku_master: pd.DataFrame | None,
               cfg: dict, brand: str, origin: str, target_year: int | None = None,
               cu: float | None = None, co: float | None = None, opening_inventory: float = 0.0,
               buffer: float = 0.0) -> dict:
    as_of = forecast_date(origin)
    weekly, meta = weekly_sample(shipments, sku_master, brand, str(as_of.date()))
    year, start, end = target_window(cfg, brand, origin, target_year)
    n_weeks = (end - start).days // 7 + 1
    total = {
        "brand": brand, "year": year, "n_series": meta["n_series"],
        "benchmark": float(weekly.median() * n_weeks) if len(weekly) else 0.0,
        "actual": None, **_quantiles(weekly, cfg["quantiles"], n_weeks),
    }
    nv = cfg["newsvendor"]
    decision = decide_stockpile(
        pd.DataFrame([total]), cfg["quantiles"],
        nv["cu"] if cu is None else cu, nv["co"] if co is None else co, opening_inventory, buffer,
    )
    curve = [
        {
            "iso_week": (start + pd.Timedelta(weeks=i)).isocalendar()[1],
            "target_week": str(start + pd.Timedelta(weeks=i)),
            "share": 1.0 / n_weeks, "cum_share": (i + 1) / n_weeks,
            "cum_demand_q_tau": decision["demand_q_tau"] * (i + 1) / n_weeks,
        }
        for i in range(n_weeks)
    ]
    return {
        "brand": brand, "origin": str(as_of.date()), "target_year": year,
        "window_start": str(start), "window_end": str(end), **meta,
        "total_quantiles": total, "decision": decision, "cum_curve": curve,
        "train_years": [], "sd_common_search": None, "backtest": [],
    }
