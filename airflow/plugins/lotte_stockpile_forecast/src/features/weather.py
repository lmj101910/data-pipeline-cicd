"""
날씨 변수 (권역 x 주).

- weekly_actual:    ASOS 일자료 -> 주간 집계 (학습용 실측)
- climatology:      ISO 주 x 권역 평년값 (예보 범위 밖 주의 대체값)
- weekly_forecast:  예보 아카이브에서 origin 시점에 알 수 있었던 예보만 사용해 대상 주 집계, 부족분은 평년값
                    -> 학습/추론 정보 일관성(train/serve skew 방지). 아카이브가 없으면 실측+플래그로 대체(낙관 편향 명시)

팥빙수 판매는 기온에 비선형(28~30°C 이상에서 가속)이므로 임계 초과 일수를 함께 사용.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..common import week_start

WEATHER_FEATURES = ["tmax_mean", "tmax_max", "tmin_mean", "days_tmax_ge_28", "days_tmax_ge_30", "days_tmax_ge_33",
                    "tropical_nights", "rain_days", "rain_mm_sum", "humidity_mean"]


def _agg(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "humidity" not in df.columns:
        df["humidity"] = np.nan
    if "rain_mm" not in df.columns:
        df["rain_mm"] = np.nan
    g = df.groupby(["region", "week_start"])
    out = g.agg(tmax_mean=("tmax", "mean"), tmax_max=("tmax", "max"), tmin_mean=("tmin", "mean"),
                days_tmax_ge_28=("tmax", lambda s: (s >= 28).sum()),
                days_tmax_ge_30=("tmax", lambda s: (s >= 30).sum()),
                days_tmax_ge_33=("tmax", lambda s: (s >= 33).sum()),
                tropical_nights=("tmin", lambda s: (s >= 25).sum()),
                rain_days=("rain_mm", lambda s: (s >= 1.0).sum()),
                rain_mm_sum=("rain_mm", "sum"), humidity_mean=("humidity", "mean"),
                n_days=("tmax", "count")).reset_index()
    # 7일 미만 관측 주는 일수 변수를 7일 기준으로 스케일
    for c in ["days_tmax_ge_28", "days_tmax_ge_30", "days_tmax_ge_33", "tropical_nights", "rain_days", "rain_mm_sum"]:
        out[c] = out[c] * 7.0 / out["n_days"].clip(lower=1)
    return out


def weekly_actual(weather_daily: pd.DataFrame) -> pd.DataFrame:
    w = weather_daily.copy()
    w["week_start"] = week_start(w["date"])
    return _agg(w)


def climatology(weekly: pd.DataFrame) -> pd.DataFrame:
    w = weekly.copy()
    w["iso_week"] = w["week_start"].dt.isocalendar().week.astype(int)
    return w.groupby(["region", "iso_week"])[WEATHER_FEATURES].mean().reset_index()


def weekly_forecast(forecast: pd.DataFrame | None, origin: pd.Timestamp, target_weeks: pd.DatetimeIndex,
                    regions: list[str], clim: pd.DataFrame) -> pd.DataFrame:
    """origin 이전에 발표된 최신 예보로 대상 주 집계. 예보 없는 일자는 평년값으로 채움 (coverage 변수로 비율 기록)."""
    rows = []
    for region in regions:
        for w in target_weeks:
            days = pd.date_range(w, w + pd.Timedelta(days=6))
            fc_week = None
            if forecast is not None and len(forecast):
                f = forecast[(forecast["region"] == region) & (forecast["issue_date"] <= origin)
                             & (forecast["target_date"].isin(days))]
                if len(f):
                    f = f.sort_values("issue_date").drop_duplicates("target_date", keep="last")
                    f = f.assign(week_start=w, rain_mm=f.get("rain_mm", pd.Series(np.nan, index=f.index)))
                    fc_week = _agg(f.rename(columns={"target_date": "date"}))
            iso = w.isocalendar()[1]
            c = clim[(clim["region"] == region) & (clim["iso_week"] == iso)]
            rec = {"region": region, "week_start": w, "forecast_coverage": 0.0}
            for feat in WEATHER_FEATURES:
                rec[feat] = float(c[feat].iloc[0]) if len(c) else np.nan
            if fc_week is not None and len(fc_week):
                cov = float(fc_week["n_days"].iloc[0]) / 7.0
                rec["forecast_coverage"] = cov
                for feat in WEATHER_FEATURES:
                    v = fc_week[feat].iloc[0]
                    if pd.notna(v):
                        # 예보 커버 일수와 평년값을 일수 비중으로 혼합
                        rec[feat] = cov * v + (1 - cov) * rec[feat] if pd.notna(rec[feat]) else v
            rows.append(rec)
    return pd.DataFrame(rows)


def add_weather_lags(panel: pd.DataFrame, weekly: pd.DataFrame, lags=(1, 2)) -> pd.DataFrame:
    """패널에 대상 주 실측 날씨 + 권역별 lag 날씨 결합 (sell-out -> sell-in 시차 반영)."""
    out = panel.merge(weekly[["region", "week_start"] + WEATHER_FEATURES], on=["region", "week_start"], how="left")
    for lag in lags:
        lagged = weekly[["region", "week_start", "tmax_mean", "days_tmax_ge_30", "rain_days"]].copy()
        lagged["week_start"] = lagged["week_start"] + pd.Timedelta(days=7 * lag)
        lagged = lagged.rename(columns={c: f"{c}_lag{lag}" for c in ["tmax_mean", "days_tmax_ge_30", "rain_days"]})
        out = out.merge(lagged, on=["region", "week_start"], how="left")
    return out
