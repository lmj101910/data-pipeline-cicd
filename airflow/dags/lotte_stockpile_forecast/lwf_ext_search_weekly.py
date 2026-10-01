"""
[외부데이터] 네이버 데이터랩 검색어 트렌드 - 주간.

* ratio 는 '요청 내' 상대지수(최대=100) 이므로 매번 2016-01-01 ~ 전일 전체 기간을 한 번의 요청으로 다시 받아
  DB 를 덮어씀 (증분 수집하면 연도 간 스케일이 깨짐).  호출 1회/주 -> 일 1,000회 한도와 무관.
* 그룹명 = 브랜드명 (config.brands.<brand>.search_keywords) -> features/external.search_weekly 가 그룹명으로 매칭.
* 쇼핑인사이트(카테고리 키워드 클릭 추이)는 선택: Variable lwf_settings.naver_shopping_enabled = true 일 때만.

DB 저장: lwf_ext_search_trend (PK: period, group_name — 'group' 은 Postgres 예약어라 group_name 으로 저장).
CSV 저장은 WRITE_CSV=False 로 기본 비활성화 (코드는 유지).

스케줄: 매주 월요일 08:00 KST (전주 데이터 확정 후, 피처/M2 DAG 09:00 이전)
Connection: lwf_naver_datalab (소스), sedp-dev-client-wfd (타겟 분석 DB)
"""
from __future__ import annotations

from datetime import date, timedelta

import pendulum
from airflow.sdk import Asset, dag, get_current_context, task

from lotte_stockpile_forecast.lwf_common import DEFAULT_ARGS, KST, export_keys, load_cfg, run_date, settings

ASSET_SEARCH = Asset(name="lwf_search_trend", uri="lwf://external/search_trend")

WRITE_CSV = False  # True 로 바꾸면 기존 CSV 저장 로직 재활성화 (코드는 항상 유지)


@dag(
    dag_id="lwf_ext_search_weekly",
    dag_display_name="[외부] 네이버 데이터랩 검색어트렌드 (주간)",
    schedule="0 8 * * 1",
    start_date=pendulum.datetime(2026, 1, 1, tz=KST),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["lwf", "external", "search"],
    doc_md=__doc__,
)
def lwf_ext_search_weekly():
    @task(outlets=[ASSET_SEARCH], execution_timeout=timedelta(minutes=10))
    def fetch_search_trend() -> dict:
        ctx = get_current_context()
        cfg = load_cfg()
        export_keys("lwf_naver_datalab")
        from src.external.naver_datalab import fetch_search_trend

        groups = {b: bc["search_keywords"] for b, bc in cfg["brands"].items()}
        end = run_date(ctx) - timedelta(days=1)
        df = fetch_search_trend(groups, date(2016, 1, 1), end, time_unit="week")
        if len(df) == 0:
            raise RuntimeError("검색어 트렌드 수집 0건")

        if WRITE_CSV:
            df.to_csv(cfg["paths"]["external"] / "search_trend.csv", index=False)

        export_keys("sedp-dev-client-wfd")
        from src.db import replace_all
        db_df = df.rename(columns={"group": "group_name"})   # 'group' 은 Postgres 예약어
        n_db = replace_all(db_df, "lwf_ext_search_trend")     # 전체 기간 재수집이므로 매번 전체 재적재

        return {"rows": int(len(df)), "db_rows": int(n_db), "groups": list(groups), "end": str(end)}

    @task(execution_timeout=timedelta(minutes=10))
    def fetch_shopping_insight() -> dict:
        ctx = get_current_context()
        if not settings().get("naver_shopping_enabled"):
            return {"skipped": True, "reason": "lwf_settings.naver_shopping_enabled = false"}
        cfg = load_cfg()
        export_keys("lwf_naver_datalab")
        from src.external.naver_datalab import fetch_shopping_keyword_trend

        cat = cfg.get("naver", {}).get("shopping_category_id", "50000006")
        kws = [kw for bc in cfg["brands"].values() for kw in bc["search_keywords"]][:5]
        df = fetch_shopping_keyword_trend(cat, kws, date(2017, 8, 1), run_date(ctx) - timedelta(days=1))
        if WRITE_CSV:
            df.to_csv(cfg["paths"]["external"] / "shopping_trend.csv", index=False)
        return {"rows": int(len(df)), "category": cat, "keywords": kws}

    fetch_search_trend()
    fetch_shopping_insight()


lwf_ext_search_weekly()
