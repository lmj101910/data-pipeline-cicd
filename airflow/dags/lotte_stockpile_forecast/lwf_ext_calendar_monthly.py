"""
[외부데이터] 캘린더 - 월간.
  1) 한국천문연구원 특일정보: 공휴일 (올해 + 내년 + 후년). 대체공휴일/임시공휴일 지정을 반영하기 위해 매월 갱신
  2) NEIS 학사일정: 권역 표본 학교의 여름방학 시작/종료 (config.neis_sample_schools 가 비어 있으면 skip)

DB 저장: lwf_ext_holidays(PK: date,name), lwf_ext_school_schedule(PK: region,year)
CSV 저장은 WRITE_CSV=False 로 기본 비활성화 (코드는 유지).

스케줄: 매월 1일 03:00 KST
Connection: lwf_data_go_kr, lwf_neis (소스), sedp-dev-client-wfd (타겟 분석 DB)
"""
from __future__ import annotations

from datetime import date, timedelta

import pendulum
from airflow.sdk import Asset, dag, get_current_context, task

from lotte_stockpile_forecast.lwf_common import DEFAULT_ARGS, KST, export_keys, load_cfg, run_date

ASSET_HOLIDAYS = Asset(name="lwf_holidays", uri="lwf://external/holidays")
ASSET_SCHOOL = Asset(name="lwf_school_schedule", uri="lwf://external/school_schedule")

WRITE_CSV = False  # True 로 바꾸면 기존 CSV 저장 로직 재활성화 (코드는 항상 유지)


@dag(
    dag_id="lwf_ext_calendar_monthly",
    dag_display_name="[외부] 공휴일(특일정보) + 학사일정(NEIS) (월간)",
    schedule="0 3 1 * *",
    start_date=pendulum.datetime(2026, 1, 1, tz=KST),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["lwf", "external", "calendar"],
    doc_md=__doc__,
)
def lwf_ext_calendar_monthly():
    @task(outlets=[ASSET_HOLIDAYS], execution_timeout=timedelta(minutes=10))
    def fetch_holidays() -> dict:
        ctx = get_current_context()
        cfg = load_cfg()
        export_keys("data_go_kr_api_key")
        from src.external.holidays import derive_lunar_holiday, fetch_holidays

        y = run_date(ctx).year
        years = list(range(2023, y + 3))
        df = fetch_holidays(years)
        if len(df) == 0:
            raise RuntimeError("공휴일 수집 0건")

        if WRITE_CSV:
            df.to_csv(cfg["paths"]["external"] / "holidays.csv", index=False)

        export_keys("sedp-dev-client-wfd")
        from src.db import replace_all
        n_db = replace_all(df, "lwf_ext_holidays")   # 연도 범위가 매달 바뀌므로 전체 재적재

        chuseok = {int(k): str(v) for k, v in derive_lunar_holiday(df, "추석").items()}
        return {"rows": int(len(df)), "db_rows": int(n_db), "years": years, "chuseok": chuseok}

    @task(outlets=[ASSET_SCHOOL], execution_timeout=timedelta(minutes=10))
    def fetch_school_schedule() -> dict:
        ctx = get_current_context()
        cfg = load_cfg()
        schools = cfg.get("neis_sample_schools") or []
        if not schools:
            return {"skipped": True, "reason": "config.neis_sample_schools 비어 있음 -> default_summer_vacation 사용"}
        export_keys("neis_api_key")
        from src.external.neis import fetch_vacations_for_regions

        y = run_date(ctx).year
        df = fetch_vacations_for_regions(schools, date(2023, 1, 1), date(y, 12, 31))

        if WRITE_CSV:
            df.to_csv(cfg["paths"]["external"] / "school_schedule.csv", index=False)

        export_keys("sedp-dev-client-wfd")
        from src.db import replace_all
        n_db = replace_all(df, "lwf_ext_school_schedule")

        return {"rows": int(len(df)), "db_rows": int(n_db)}

    fetch_holidays()
    fetch_school_schedule()


lwf_ext_calendar_monthly()
