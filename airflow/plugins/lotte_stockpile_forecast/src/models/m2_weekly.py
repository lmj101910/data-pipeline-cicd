"""
M2 전술 모델: 주간 롤링, horizon h=1..4 직접 예측(direct multi-horizon), 브랜드별 별도 학습.

target = log1p(qty_clean[t+h]).  분위수는 단조변환에 불변이므로 expm1 로 역변환.
피처 시점 규칙 (leak 방지):
  - origin(t) 시점 정보: 판매 lag/롤링, 검색 트렌드, 결품, 유통재고, 실측 날씨(lag)
  - target(t+h) 시점 정보 중 '사전 확정' 되는 것만: 행사 계획, SKU 수, 캘린더, 날씨(예보/평년)
  - foot_traffic(인구이동)은 수집 DAG 미운영으로 제외. 기존 패널에 컬럼이 남아 있어도 사용하지 않음.
벤치마크: seasonal naive = 전년 동주 x (최근 4주 / 전년 동기 4주)
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ..features.weather import WEATHER_FEATURES, climatology, weekly_actual, weekly_forecast
from .quantile_lgbm import QuantileLGBM, qcol

log = logging.getLogger(__name__)

ORIGIN_FEATURES = ["stockout_share", "search_ratio", "search_ratio_4w", "search_yoy", "csi",
                   "tmax_mean_lag1", "days_tmax_ge_30_lag1", "rain_days_lag1", "tmax_mean_lag2", "days_tmax_ge_30_lag2",
                   "rain_days_lag2"]
TARGET_KNOWN_FEATURES = ["promo_intensity", "n_active_sku", "n_limited_sku", "month_end_week", "iso_week", "month",
                         "week_of_month", "n_holidays_wd", "vacation_share", "in_window", "weeks_since_window_start",
                         "weeks_to_event", "event_weekday", "chuseok_gap_days", "days_to_suneung"]
TARGET_WEATHER = WEATHER_FEATURES
CATEGORICAL = ["channel", "region"]


def make_supervised(pb: pd.DataFrame, h: int, use_weather: bool, use_search: bool) -> pd.DataFrame:
    """단일 브랜드 패널 -> horizon h 학습/예측 테이블. 각 행 = (series, origin_week t)."""
    pb = pb.sort_values(["series_id", "week_start"]).copy()
    g = pb.groupby("series_id", sort=False)
    x = np.log1p(pb["qty_clean"])
    pb["_x"] = x
    gx = pb.groupby("series_id", sort=False)["_x"]

    f = pb[["brand", "series_id", "channel", "region", "week_start"]].copy().rename(columns={"week_start": "origin_week"})
    f["h"] = h
    f["target_week"] = f["origin_week"] + pd.Timedelta(days=7 * h)
    for lag in range(4):
        f[f"lag{lag}"] = gx.shift(lag)
    for w in (4, 8, 13):
        f[f"roll_mean_{w}"] = gx.transform(lambda s: s.rolling(w, min_periods=2).mean())
    f["roll_std_8"] = gx.transform(lambda s: s.rolling(8, min_periods=3).std())
    f["yoy_same_week"] = gx.shift(52 - h)                                  # x[t+h-52]
    f["yoy_momentum"] = f["roll_mean_4"] - g["_x"].transform(lambda s: s.rolling(4, min_periods=2).mean().shift(52))
    f["returns_share"] = (pb["returns_qty"] / (pb["qty"].abs() + 1.0)).values

    for c in ORIGIN_FEATURES:
        if c not in pb.columns:
            continue
        if c.startswith("search") and not use_search:
            continue
        if c.endswith(("_lag1", "_lag2")) and not use_weather:
            continue
        f[c] = pb[c].values
    for c in TARGET_KNOWN_FEATURES:
        if c in pb.columns:
            f[c] = g[c].shift(-h)
    if use_weather:
        for c in TARGET_WEATHER:
            if c in pb.columns:
                f[c] = g[c].shift(-h)
    f["y"] = gx.shift(-h)
    f["actual"] = g["qty_clean"].shift(-h)
    f["benchmark"] = np.expm1(f["yoy_same_week"] + f["yoy_momentum"].fillna(0.0))
    return f.reset_index(drop=True)


def feature_columns(df: pd.DataFrame) -> list[str]:
    meta = {"brand", "series_id", "origin_week", "target_week", "h", "y", "actual", "benchmark"}
    return [c for c in df.columns if c not in meta]


def _fill_target_weather(pred_rows: pd.DataFrame, origin: pd.Timestamp, weather_daily: pd.DataFrame | None,
                         forecast: pd.DataFrame | None) -> pd.DataFrame:
    """예측 행의 대상 주 날씨를 origin 시점 예보(+평년값)로 대체. 실측은 미래라 없음."""
    if weather_daily is None or len(pred_rows) == 0:
        return pred_rows
    wk = weekly_actual(weather_daily)
    clim = climatology(wk)
    regions = sorted(pred_rows["region"].unique())
    weeks = pd.DatetimeIndex(sorted(pred_rows["target_week"].unique()))
    fc = weekly_forecast(forecast, origin, weeks, regions, clim)
    fc = fc.rename(columns={"week_start": "target_week"})
    out = pred_rows.drop(columns=[c for c in TARGET_WEATHER if c in pred_rows.columns])
    out = out.merge(fc[["region", "target_week", "forecast_coverage"] + TARGET_WEATHER], on=["region", "target_week"], how="left")
    log.info("대상 주 날씨: 예보 커버리지 평균 %.2f (나머지 평년값)", out["forecast_coverage"].mean())
    return out.drop(columns=["forecast_coverage"])


def train_predict_m2(panel: pd.DataFrame, brand: str, cfg: dict, origin: pd.Timestamp,
                     weather_daily: pd.DataFrame | None = None, forecast: pd.DataFrame | None = None,
                     val_weeks: int = 26) -> tuple[pd.DataFrame, dict]:
    """origin(마지막 실적 주) 기준 h=1..H 예측. 반환: (예측 테이블, {h: 모델})"""
    bc = cfg["brands"][brand]["m2"]
    alphas = cfg["quantiles"]
    pb = panel[(panel["brand"] == brand)].copy()
    preds, models = [], {}
    for h in bc["horizons"]:
        sup = make_supervised(pb, h, bc.get("use_weather", False), bc.get("use_search", True))
        feats = feature_columns(sup)
        train = sup[(sup["y"].notna()) & (sup["target_week"] <= origin) & sup["lag3"].notna()]
        pred_rows = sup[sup["origin_week"] == origin].copy()
        if bc.get("use_weather", False):
            pred_rows = _fill_target_weather(pred_rows, origin, weather_daily, forecast)
        if len(train) < 100:
            log.warning("[%s h=%d] 학습 행 %d개 - 너무 적음", brand, h, len(train))
        w = pd.Series(1.0, index=train.index)
        if "in_window" in train.columns:
            w = w.where(train["in_window"].fillna(0) == 0, float(bc.get("in_window_weight", 1.0)))

        cut = origin - pd.Timedelta(days=7 * val_weeks)
        tr, va = train[train["target_week"] <= cut], train[train["target_week"] > cut]
        mode = bc.get("mode", cfg["lgbm"].get("mode", "quantile"))
        mk = lambda params: QuantileLGBM(alphas, params, monotone=bc.get("monotone", {}), categorical=CATEGORICAL, mode=mode)  # noqa: E731
        n_est = cfg["lgbm"]["n_estimators"]
        if len(tr) >= 30 and len(va) >= 30:
            probe = mk(cfg["lgbm"]).fit(tr[feats], tr["y"], va[feats], va["y"], sample_weight=w.loc[tr.index])
            n_est = max(50, int(np.median(list(probe.best_iters.values()))))  # 검증 기반 반복 수 -> 전체 재학습
        model = mk({**cfg["lgbm"], "n_estimators": n_est}).fit(train[feats], train["y"], sample_weight=w)
        models[h] = model

        q = model.predict(pred_rows[feats])
        out = pred_rows[["brand", "series_id", "channel", "region", "origin_week", "target_week", "h", "benchmark"]].copy()
        for a in alphas:
            out[qcol(a)] = np.expm1(q[qcol(a)].values).clip(min=0)
        preds.append(out)
    res = pd.concat(preds, ignore_index=True)
    return res, models


def aggregate_to_brand(pred: pd.DataFrame, alphas: list[float]) -> pd.DataFrame:
    """시리즈 예측 -> 브랜드 합계. 분위수 합은 상한(보수적)임을 유의: 서비스레벨 tau 의 실제 커버리지는 이보다 높음."""
    cols = [qcol(a) for a in alphas] + ["benchmark"]
    return pred.groupby(["brand", "origin_week", "target_week", "h"], as_index=False)[cols].sum()
