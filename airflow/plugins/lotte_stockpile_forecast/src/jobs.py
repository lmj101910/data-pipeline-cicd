"""
실행 단위 작업 함수 (CLI 와 Airflow DAG 가 공유). DB(Postgres) 저장이 기본, CSV 저장 코드는
schema.USE_DB 플래그로 감싸 유지 (만일의 사태 대비 - LWF_USE_DB=false 로 언제든 CSV 모드로 복귀 가능).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from .decision.newsvendor import cumulative_curve, decide_stockpile
from .models.quantile_lgbm import qcol
from .schema import USE_DB, load_table

log = logging.getLogger(__name__)


def _jsonable(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, (np.floating, np.integer)):
            v = v.item()
        elif isinstance(v, (pd.Timestamp,)):
            v = str(v.date())
        elif isinstance(v, float) and not np.isfinite(v):
            v = None
        out[k] = v
    return out


def job_build_features(cfg: dict, extend_weeks: int = 12, *, sample_mode: bool = False) -> dict:
    if sample_mode:
        log.info("표본 결과 모드: 예측 태스크에서 출고 표본을 직접 집계합니다. 피처 테이블 구축 생략")
        return {"sample_mode": True, "feature_table_rebuilt": False}
    from .features.build import build_feature_table

    # USE_DB=true(기본) 이면 CSV 저장은 build_feature_table 내부에서 건너뜀 (save=False).
    # LWF_USE_DB=false 로 끄면 자동으로 CSV 저장 재개.
    panel = build_feature_table(cfg, extend_weeks=extend_weeks, save=not USE_DB)
    hist = panel[panel["qty"].notna()]
    return {"rows": int(len(panel)), "cols": int(panel.shape[1]), "last_actual_week": str(hist["week_start"].max().date()),
            "brands": sorted(panel["brand"].unique().tolist())}


def job_check_inputs(cfg: dict, max_staleness_days: int = 10, *,
                     origin: str | None = None, sample_mode: bool = False) -> dict:
    """주간 예측 전 게이트: 필수 파일 존재 + 출고 실적 최신성."""
    from .sample_forecast import forecast_date

    raw = cfg["paths"]["raw"]
    ship = load_table("shipments", raw, required=not sample_mode)
    load_table("sku_master", raw, required=not sample_mode)
    as_of = forecast_date(origin)
    eligible = ship[ship["date"] < as_of + pd.Timedelta(days=1)] if ship is not None else pd.DataFrame()
    last = eligible["date"].max() if not eligible.empty else pd.NaT
    stale = int((as_of - last).days) if pd.notna(last) else None
    if not sample_mode and stale is None:
        raise RuntimeError(f"예측 기준일 {as_of.date()} 이전의 출고 실적이 없습니다")
    if not sample_mode and stale > max_staleness_days:
        raise RuntimeError(f"shipments 최신 일자 {last.date()} 가 {stale}일 전 -> 적재 지연. 예측 중단")
    if sample_mode:
        log.info("표본 결과 모드: 최신성 검사 생략 (기준일=%s, 마지막 실적=%s, 경과일=%s)",
                 as_of.date(), last, stale)
    return {
        "shipments_rows": int(len(eligible)), "last_date": str(last.date()) if pd.notna(last) else None,
        "staleness_days": stale, "origin": str(as_of.date()), "sample_mode": sample_mode,
    }


def _save_m2_result(cfg: dict, result: dict) -> None:
    brand, origin = result["brand"], result["origin"]
    if USE_DB:
        from .db import upsert
        db_rows = pd.DataFrame([{
            "brand": brand, "origin": pd.Timestamp(origin).date(),
            "target_week": pd.Timestamp(r["target_week"]).date(), "h": r["h"],
            "q50": r.get("q50"), "q70": r.get("q70"), "q80": r.get("q80"),
            "q90": r.get("q90"), "benchmark": r.get("benchmark"),
        } for r in result["forecast"]])
        upsert(db_rows, "lwf_m2_forecast", pk_cols=["brand", "origin", "target_week"])
    elif result.get("sample_mode"):
        out = Path(cfg["paths"]["outputs"])
        out.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(result["forecast"]).to_csv(out / f"m2_pred_brand_{brand}_{origin}.csv", index=False)
        (out / f"m2_result_{brand}_{origin}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8",
        )


def job_predict_m2(cfg: dict, brand: str, origin: str | None = None, *, sample_mode: bool = False) -> dict:
    if sample_mode:
        from .sample_forecast import predict_m2

        raw = cfg["paths"]["raw"]
        result = predict_m2(load_table("shipments", raw), load_table("sku_master", raw), cfg, brand, origin)
        _save_m2_result(cfg, result)
        log.info("[M2 %s] 기준일=%s, 첫 대상 주=%s, 표본 최신일=%s, 계산=%s",
                 brand, result["origin"], result["forecast"][0]["target_week"],
                 result["data_as_of"], result["method"])
        return result

    from .features.build import load_feature_table
    from .models.m2_weekly import aggregate_to_brand, train_predict_m2
    from .sample_forecast import forecast_date

    panel = load_feature_table(cfg)
    requested_date = forecast_date(origin) if origin else panel.loc[panel["qty"].notna(), "week_start"].max()
    origin_ts = requested_date - pd.Timedelta(days=requested_date.weekday())
    ext = cfg["paths"]["external"]
    weather_daily, forecast = load_table("weather_daily", ext), load_table("weather_forecast", ext)
    pred, models = train_predict_m2(panel, brand, cfg, origin_ts, weather_daily, forecast)
    brand_df = aggregate_to_brand(pred, cfg["quantiles"])

    if not USE_DB:
        # ---- 기존 CSV 저장 (USE_DB=false 일 때만 실행, 만일의 사태 대비용으로 코드 유지) ----
        out = Path(cfg["paths"]["outputs"])
        pred.to_csv(out / f"m2_pred_series_{brand}_{origin_ts.date()}.csv", index=False)
        brand_df.to_csv(out / f"m2_pred_brand_{brand}_{origin_ts.date()}.csv", index=False)

    imp = {h: m.feature_importance().head(10).round(0).to_dict() for h, m in models.items()}
    rows = [_jsonable({"target_week": r.target_week, "h": int(r.h), **{qcol(a): float(getattr(r, qcol(a))) for a in cfg["quantiles"]},
                       "benchmark": float(r.benchmark) if np.isfinite(r.benchmark) else None})
            for r in brand_df.itertuples()]
    result = {"brand": brand, "origin": str(requested_date.date()), "origin_week": str(origin_ts.date()),
              "sample_mode": False, "method": "model", "forecast": rows,
              "feature_importance": {str(k): v for k, v in imp.items()}}

    _save_m2_result(cfg, result)
    return result


def job_predict_m1(cfg: dict, brand: str, origin: str, target_year: int | None = None,
                   cu: float | None = None, co: float | None = None, opening_inventory: float = 0.0,
                   buffer: float = 0.0, with_backtest: bool = True, *, sample_mode: bool = False) -> dict:
    if sample_mode:
        from .sample_forecast import predict_m1

        raw = cfg["paths"]["raw"]
        result = predict_m1(
            load_table("shipments", raw), load_table("sku_master", raw),
            cfg, brand, origin, target_year, cu, co, opening_inventory, buffer,
        )
        _save_m1_result(cfg, result)
        log.info("[M1 %s] 기준일=%s, 대상 기간=%s~%s, 표본 최신일=%s, 계산=%s",
                 brand, result["origin"], result["window_start"], result["window_end"],
                 result["data_as_of"], result["method"])
        return result

    from .features.build import load_feature_table
    from .models.m1_event_total import (aggregate_m1_to_brand, as_of, attach_actuals, build_m1_table,
                                        event_shock_sd_from_search, fit_predict_m1, window_totals)

    panel = load_feature_table(cfg)
    origin_ts = pd.Timestamp(origin)
    target_year = origin_ts.year if target_year is None else int(target_year)
    raw, ext, out = cfg["paths"]["raw"], cfg["paths"]["external"], Path(cfg["paths"]["outputs"])
    preorders = load_table("preorders", raw)
    search = load_table("search_trend", ext)
    nv, bm1 = cfg["newsvendor"], cfg["brands"][brand]["m1"]

    table = build_m1_table(as_of(panel, origin_ts), brand, cfg, origin_ts, preorders)
    pred = attach_actuals(fit_predict_m1(table, target_year, cfg), window_totals(panel, brand, cfg))
    sd_search = event_shock_sd_from_search(search, brand, cfg)
    total = aggregate_m1_to_brand(pred, cfg["quantiles"], nv.get("aggregation", "simulate"), common_shock_sd=sd_search,
                                  common_sd_floor=bm1.get("common_shock_sd_floor", 0.1))
    decision = decide_stockpile(total, cfg["quantiles"], cu or nv["cu"], co or nv["co"], opening_inventory, buffer)
    curve = cumulative_curve(panel, brand, cfg, decision["demand_q_tau"], target_year)

    tag = f"{brand}_{target_year}_{origin_ts.date()}"

    if not USE_DB:
        # ---- 기존 CSV 저장 (USE_DB=false 일 때만 실행, 만일의 사태 대비용으로 코드 유지) ----
        table.to_csv(out / f"m1_table_{tag}.csv", index=False)
        pred.to_csv(out / f"m1_pred_series_{tag}.csv", index=False)
        total.to_csv(out / f"m1_pred_total_{tag}.csv", index=False)
        curve.to_csv(out / f"m1_cum_curve_{tag}.csv", index=False)

    result = {"brand": brand, "target_year": target_year, "origin": str(origin_ts.date()),
              "sample_mode": False, "method": "model",
              "total_quantiles": _jsonable(total.iloc[0].to_dict()), "decision": _jsonable(decision),
              "sd_common_search": sd_search, "cum_curve": curve.round(4).to_dict(orient="records"),
              "train_years": sorted(int(y) for y in table.loc[table["log_uplift"].notna(), "year"].unique())}
    if with_backtest:
        from .backtest import backtest_m1

        bt = backtest_m1(panel, brand, cfg, origin_ts.strftime("%m-%d"), preorders, search)
        if not USE_DB:
            bt.to_csv(out / f"backtest_m1_{brand}_{origin_ts.strftime('%m-%d')}.csv", index=False)
        result["backtest"] = [_jsonable(r) for r in bt.to_dict(orient="records")]

    _save_m1_result(cfg, result)
    return result


def _save_m1_result(cfg: dict, result: dict) -> None:
    brand, target_year = result["brand"], result["target_year"]
    origin_ts = pd.Timestamp(result["origin"])
    if not USE_DB:
        out = Path(cfg["paths"]["outputs"])
        out.mkdir(parents=True, exist_ok=True)
        tag = f"{brand}_{target_year}_{origin_ts.date()}"
        (out / f"m1_decision_{tag}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
        )
        if result.get("sample_mode"):
            pd.DataFrame([result["total_quantiles"]]).to_csv(out / f"m1_pred_total_{tag}.csv", index=False)
            pd.DataFrame(result["cum_curve"]).to_csv(out / f"m1_cum_curve_{tag}.csv", index=False)
    if USE_DB:
        from .db import upsert
        d = result["decision"]
        core = pd.DataFrame([{
            "brand": brand, "target_year": target_year, "origin": origin_ts.date(),
            "tau": d["tau"], "demand_q50": d["demand_q50"], "demand_q_tau": d["demand_q_tau"],
            "benchmark": d.get("benchmark"), "opening_inventory": d["opening_inventory"],
            "stockpile_model": d["stockpile_model"], "judgmental_buffer": d["judgmental_buffer"],
            "stockpile_total": d["stockpile_total"], "train_years": json.dumps(result["train_years"]),  # VARCHAR (YellowBrick 배열타입 미지원)
        }])
        upsert(core, "lwf_m1_decision", pk_cols=["brand", "target_year", "origin"])

        raw_df = pd.DataFrame([{
            "brand": brand, "target_year": target_year, "origin": origin_ts.date(),
            "payload": json.dumps(result, ensure_ascii=False, default=str),
        }])
        upsert(raw_df, "lwf_m1_decision_raw", pk_cols=["brand", "target_year", "origin"])
