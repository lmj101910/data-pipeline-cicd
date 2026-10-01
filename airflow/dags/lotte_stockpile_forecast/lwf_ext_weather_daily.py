"""
[외부데이터] 기상청 날씨 - 일간.
  1) ASOS 일자료 (실측, 학습용): 전일까지 lookback_days 만큼 재수집해 upsert (기상청 사후 보정 반영)
  2) 단기(~3일) + 중기(4~10일) 예보: 발표일 기준 아카이브 append (train/serve 일관성용 예보 이력)

DB 저장: lwf_ext_weather_daily(PK: date,region), lwf_ext_weather_forecast(PK: issue_date,target_date,region)
CSV 저장은 WRITE_CSV=False 로 기본 비활성화 (코드는 유지).

스케줄: 매일 07:00 KST (ASOS 전일 자료는 익일 새벽 확정, 중기예보 06:00 발표 이후)
Connection: lwf_data_go_kr (소스), sedp-dev-client-wfd (타겟 분석 DB)
"""
from __future__ import annotations

from datetime import timedelta

import pendulum
from airflow.sdk import Asset, Param, dag, get_current_context, task

from lotte_stockpile_forecast.lwf_common import DEFAULT_ARGS, KST, export_keys, load_cfg, merge_csv, run_date

ASSET_WEATHER_DAILY = Asset(name="lwf_weather_daily", uri="lwf://external/weather_daily")
ASSET_WEATHER_FORECAST = Asset(name="lwf_weather_forecast", uri="lwf://external/weather_forecast")

WRITE_CSV = False  # True 로 바꾸면 기존 CSV 저장 로직 재활성화 (코드는 항상 유지)


@dag(
    dag_id="lwf_ext_weather_daily",
    dag_display_name="[외부] 기상청 ASOS 실측 + 단기/중기 예보 (일간)",
    schedule="0 7 * * *",
    start_date=pendulum.datetime(2026, 1, 1, tz=KST),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    params={"lookback_days": Param(7, type="integer", minimum=1, maximum=400,
                                   description="ASOS 재수집 일수. 최초 적재 시 400 으로 수동 트리거")},
    tags=["lwf", "external", "weather"],
    doc_md=__doc__,
)
def lwf_ext_weather_daily():
    @task(outlets=[ASSET_WEATHER_DAILY], execution_timeout=timedelta(minutes=30))
    def fetch_asos() -> dict:
        ctx = get_current_context()
        cfg = load_cfg()
        export_keys("data_go_kr_api_key")
        from src.external.kma_asos import fetch_weather_daily_for_regions

        end = run_date(ctx) - timedelta(days=1)
        start = end - timedelta(days=int(ctx["params"]["lookback_days"]))
        df = fetch_weather_daily_for_regions(cfg["regions"], start, end)
        if len(df) == 0:
            raise RuntimeError(f"ASOS 수집 0건 ({start}~{end})")

        n_total = len(df)
        if WRITE_CSV:
            n_total = merge_csv(df, cfg["paths"]["external"] / "weather_daily.csv", ["region", "date"], ["date"])

        export_keys("sedp-dev-client-wfd")
        from src.db import upsert
        n_db = upsert(df, "lwf_ext_weather_daily", pk_cols=["date", "region"])

        return {"start": str(start), "end": str(end), "fetched": int(len(df)), "db_rows": int(n_db), "total_rows": int(n_total)}

    @task(outlets=[ASSET_WEATHER_FORECAST], execution_timeout=timedelta(minutes=20))
    def fetch_forecast() -> dict:
        ctx = get_current_context()
        cfg = load_cfg()
        export_keys("data_go_kr_api_key")
        from src.external.kma_forecast import append_forecast_archive, fetch_forecast_for_regions

        fc = fetch_forecast_for_regions(cfg["regions"], base_date=run_date(ctx))
        if len(fc) == 0:
            raise RuntimeError("예보 수집 0건")

        n_total = len(fc)
        if WRITE_CSV:
            allf = append_forecast_archive(fc, cfg["paths"]["external"] / "weather_forecast.csv")
            n_total = int(len(allf))

        export_keys("sedp-dev-client-wfd")
        from src.db import upsert
        n_db = upsert(fc, "lwf_ext_weather_forecast", pk_cols=["issue_date", "target_date", "region"])

        return {"fetched": int(len(fc)), "db_rows": int(n_db), "archive_rows": n_total}

    fetch_asos()
    fetch_forecast()


lwf_ext_weather_daily()
