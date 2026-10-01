"""
[외부데이터] 거시/기후 보조지표 - 월간 (우선순위 낮음, 없어도 파이프라인 동작).
  1) NOAA ONI (엘니뇨 지수, 정적 텍스트 파일, 인증 불필요) - 매월 둘째 주 갱신
  2) 한국은행 ECOS 소비자심리지수 - 매월 4주차 발표

DB 저장: lwf_ext_oni(PK: year,month), lwf_ext_consumer_sentiment(PK: month)
CSV 저장은 WRITE_CSV=False 로 기본 비활성화 (코드는 유지).

스케줄: 매월 28일 09:00 KST
Connection: lwf_ecos (ONI 는 불필요) (소스), sedp-dev-client-wfd (타겟 분석 DB)
"""
from __future__ import annotations

from datetime import date, timedelta

import pendulum
from airflow.sdk import Asset, dag, get_current_context, task

from lotte_stockpile_forecast.lwf_common import DEFAULT_ARGS, KST, export_keys, load_cfg, run_date

ASSET_ONI = Asset(name="lwf_oni", uri="lwf://external/oni")
ASSET_CSI = Asset(name="lwf_consumer_sentiment", uri="lwf://external/consumer_sentiment")

WRITE_CSV = False  # True 로 바꾸면 기존 CSV 저장 로직 재활성화 (코드는 항상 유지)


@dag(
    dag_id="lwf_ext_macro_monthly",
    dag_display_name="[외부] NOAA ONI + ECOS 소비자심리 (월간)",
    schedule="0 9 28 * *",
    start_date=pendulum.datetime(2026, 1, 1, tz=KST),
    catchup=False,
    max_active_runs=1,
    default_args={**DEFAULT_ARGS, "retries": 1},
    tags=["lwf", "external", "macro"],
    doc_md=__doc__,
)
def lwf_ext_macro_monthly():
    @task(outlets=[ASSET_ONI], execution_timeout=timedelta(minutes=5))
    def fetch_oni() -> dict:
        cfg = load_cfg()
        from src.external.macro import fetch_oni

        df = fetch_oni()
        if len(df) == 0:
            raise RuntimeError("ONI 파싱 0건 (파일 포맷 변경 여부 확인)")

        if WRITE_CSV:
            df.to_csv(cfg["paths"]["external"] / "oni.csv", index=False)

        export_keys("sedp-dev-client-wfd")
        from src.db import replace_all
        n_db = replace_all(df, "lwf_ext_oni")

        return {"rows": int(len(df)), "db_rows": int(n_db), "last": f"{int(df.year.iloc[-1])}-{int(df.month.iloc[-1]):02d}"}

    @task(outlets=[ASSET_CSI], execution_timeout=timedelta(minutes=5))
    def fetch_consumer_sentiment() -> dict:
        ctx = get_current_context()
        cfg = load_cfg()
        export_keys("ecos_api_key")
        from src.external.macro import fetch_consumer_sentiment

        ec = cfg.get("ecos", {})
        df = fetch_consumer_sentiment(date(2023, 1, 1), run_date(ctx), ec.get("stat_code", "511Y002"), ec.get("item_code", "FME"))
        if len(df) == 0:
            raise RuntimeError("ECOS 0건 (stat_code/item_code 확인)")

        if WRITE_CSV:
            df.to_csv(cfg["paths"]["external"] / "consumer_sentiment.csv", index=False)

        export_keys("sedp-dev-client-wfd")
        from src.db import replace_all
        n_db = replace_all(df, "lwf_ext_consumer_sentiment")

        return {"rows": int(len(df)), "db_rows": int(n_db), "last_month": str(df.month.iloc[-1])}

    fetch_oni()
    fetch_consumer_sentiment()


lwf_ext_macro_monthly()
