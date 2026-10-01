"""피처 테이블 조립: 내부 패널 + 캘린더 + 날씨 + 외부지표 -> data/processed/panel_weekly.csv (DB: lwf_panel_weekly)"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from ..external.holidays import derive_lunar_holiday
from ..schema import USE_DB, load_table
from .event_calendar import build_calendar  # calendar.py -> event_calendar.py (표준 라이브러리 calendar 모듈명 충돌 회피)
from .external import csi_weekly, search_weekly
from .panel import build_panel
from .quality import flag_outliers, quality_report
from .weather import add_weather_lags, weekly_actual

log = logging.getLogger(__name__)

_PANEL_PK = ["brand", "channel", "region", "week_start"]


def load_inputs(cfg: dict) -> dict:
    raw, ext = cfg["paths"]["raw"], cfg["paths"]["external"]
    # foot_traffic: 수집 DAG 미운영으로 제외. DB/CSV 조회도 하지 않는다.
    return dict(
        shipments=load_table("shipments", raw, required=True),
        sku_master=load_table("sku_master", raw, required=True),
        distribution=load_table("distribution", raw),
        preorders=load_table("preorders", raw),
        weather_daily=load_table("weather_daily", ext),
        weather_forecast=load_table("weather_forecast", ext),
        holidays=load_table("holidays", ext),
        school_schedule=load_table("school_schedule", ext),
        search_trend=load_table("search_trend", ext),
        consumer_sentiment=load_table("consumer_sentiment", ext),
    )


def build_feature_table(cfg: dict, extend_weeks: int = 12, save: bool = True) -> pd.DataFrame:
    d = load_inputs(cfg)
    dq = cfg["data_quality"]
    # promotions=None, stockouts=None: 둘 다 데이터 없음(확정) -> panel.py 가 이미 None 을 정상 처리
    # (행사강도=0, 검열보정 생략) 하므로 features/panel.py 코드 변경 없이 그대로 둠.
    panel = build_panel(d["shipments"], d["sku_master"], None, None, d["distribution"],
                        channels=cfg["channels"], max_stockout_share=dq["stockout_max_share"],
                        extend_weeks=extend_weeks)
    weeks = pd.DatetimeIndex(sorted(panel["week_start"].unique()))
    chuseok = derive_lunar_holiday(d["holidays"], "추석") if d["holidays"] is not None else {}
    if not chuseok:
        log.warning("holidays.csv 없음/추석 미도출 -> chuseok_gap_days 결측 처리")

    frames = []
    for brand in sorted(panel["brand"].unique()):
        if brand not in cfg["brands"]:
            log.warning("config 에 없는 브랜드 '%s' 제외", brand)
            continue
        pb = panel[panel["brand"] == brand]
        cals = []
        for region in sorted(pb["region"].unique()):
            cal = build_calendar(weeks, brand, cfg, d["holidays"], chuseok, d["school_schedule"], region)
            cal["region"] = region
            cals.append(cal)
        pb = pb.merge(pd.concat(cals, ignore_index=True), on=["region", "week_start"], how="left")
        pb = pb.merge(search_weekly(d["search_trend"], brand, weeks), on="week_start", how="left")
        frames.append(pb)
    panel = pd.concat(frames, ignore_index=True)

    if d["weather_daily"] is not None:
        panel = add_weather_lags(panel, weekly_actual(d["weather_daily"]))
    else:
        log.warning("weather_daily.csv 없음 -> 날씨 변수 미사용 (팥빙수 M2 성능 저하)")

    csi = csi_weekly(d["consumer_sentiment"], weeks)
    if csi is not None:
        panel = panel.merge(csi, on="week_start", how="left")

    panel = flag_outliers(panel, z=dq["outlier_z"], cap=dq.get("cap_outliers", False))
    panel = panel.sort_values(["brand", "channel", "region", "week_start"]).reset_index(drop=True)

    if USE_DB:
        from ..db import upsert

        n = upsert(panel, "lwf_panel_weekly", pk_cols=_PANEL_PK)
        qr = quality_report(panel)
        upsert(qr, "lwf_quality_report", pk_cols=["series_id"])
        log.info("피처 테이블 DB 적재: lwf_panel_weekly (%d rows, %d cols)", n, panel.shape[1])

    if save:
        # ---- 기존 CSV 저장 (USE_DB=true 인 기본 상태에서는 호출부에서 save=False 로 넘겨 비활성화.
        #      만일의 사태 대비용으로 코드는 그대로 유지) ----
        out = Path(cfg["paths"]["processed"]) / "panel_weekly.csv"
        panel.to_csv(out, index=False)
        quality_report(panel).to_csv(Path(cfg["paths"]["processed"]) / "quality_report.csv", index=False)
        log.info("피처 테이블 CSV 저장: %s (%d rows, %d cols)", out, len(panel), panel.shape[1])
    return panel


def load_feature_table(cfg: dict) -> pd.DataFrame:
    if USE_DB:
        from ..db import read_sql

        df = read_sql("lwf_panel_weekly")
        if df.empty:
            raise FileNotFoundError("lwf_panel_weekly 테이블에 데이터 없음. 먼저 build-features 실행")
        df["week_start"] = pd.to_datetime(df["week_start"])
        # 기존 적재분에 남은 인구이동 컬럼도 예측 입력에서 제외한다.
        return df.drop(columns=["foot_traffic"], errors="ignore")

    # ---- 기존 CSV 로딩 (USE_DB=false, 만일의 사태 대비) ----
    p = Path(cfg["paths"]["processed"]) / "panel_weekly.csv"
    if not p.exists():
        raise FileNotFoundError(f"{p} 없음. 먼저 build-features 실행")
    return pd.read_csv(p, parse_dates=["week_start"]).drop(columns=["foot_traffic"], errors="ignore")
