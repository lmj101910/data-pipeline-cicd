"""
[모델] 피처 테이블 재구축 + M2 전술 주간 예측 (1~4주) - 주간.

  Airflow 3.3.2 / Python 3.13. foot_traffic(인구이동) 데이터는 사용하지 않음.
  prepare_run -> check_inputs -> build_features -> predict_m2[브랜드별 매핑] -> publish
  * origin: 입력한 예측 기준일(YYYY-MM-DD), 미지정 시 DAG 실행일(KST). 한 Run 안에서는 고정.
  * 예측 대상: 기준일이 속한 주의 다음 월요일부터 1~4주.
  * sample_mode=true(기본): 오래된/부족한 출고 표본으로도 결과 생성. 표본이 없으면 0.
    최신성 검사와 피처 구축/모델 학습을 생략하며 원본 실적 날짜는 변경하지 않음.
  * sample_mode=false: 기존 피처 구축/학습 모델과 최신성 검사 사용.
  * DB 저장이 기본: predict_m2 -> lwf_m2_forecast (src/jobs.py 내부에서 처리).
    CSV 저장 코드는 schema.USE_DB 플래그로 감싸 유지 (LWF_USE_DB=false 로 언제든 복귀 가능).
  * 선행 DAG: lwf_internal_ingest_daily(06:00), lwf_ext_weather_daily(07:00), lwf_ext_search_weekly(월 08:00)

스케줄: 매주 월요일 09:00 KST
Connection: sedp-dev-client-wfd (분석 DB, Postgres)
"""
from __future__ import annotations

import json
from datetime import timedelta

import pendulum
from airflow.sdk import Asset, Param, dag, get_current_context, task

from lotte_stockpile_forecast.lwf_common import DEFAULT_ARGS, KST, brands, export_keys, forecast_options, load_cfg

ASSET_M2 = Asset(name="lwf_m2_forecast", uri="lwf://outputs/m2_forecast")


@dag(
    dag_id="lwf_features_m2_weekly",
    dag_display_name="[모델] 피처 구축 + M2 주간 예측",
    schedule="0 9 * * 1",
    start_date=pendulum.datetime(2026, 1, 1, tz=KST),
    catchup=False,
    max_active_runs=1,
    default_args={**DEFAULT_ARGS, "retries": 1},
    dagrun_timeout=timedelta(hours=2),
    params={
        "origin": Param(None, type=["null", "string"], format="date",
                        description="예측 기준일 YYYY-MM-DD. 미입력 시 실행일(KST), 다음 주부터 1~4주 예측"),
        "sample_mode": Param(True, type="boolean",
                             description="true: 표본으로 결과 생성(빈 표본은 0), false: 기존 학습 모델"),
    },
    tags=["lwf", "model", "m2"],
    doc_md=__doc__,
)
def lwf_features_m2_weekly():
    @task
    def prepare_run() -> dict:
        return forecast_options(get_current_context())

    @task(execution_timeout=timedelta(minutes=5))
    def check_inputs(run: dict) -> dict:
        cfg = load_cfg()
        export_keys("sedp-dev-client-wfd")
        from src.jobs import job_check_inputs

        return job_check_inputs(cfg, max_staleness_days=10, origin=run["origin"], sample_mode=run["sample_mode"])

    @task(execution_timeout=timedelta(minutes=30))
    def build_features(run: dict) -> dict:
        cfg = load_cfg()
        export_keys("sedp-dev-client-wfd")
        from src.jobs import job_build_features

        return job_build_features(cfg, extend_weeks=12, sample_mode=run["sample_mode"])

    @task
    def list_brands() -> list[str]:
        return brands()  # Variable lwf_brands (기본 ["빼빼로", "팥빙수"]) - 파싱 시점이 아닌 실행 시점에 조회

    @task(execution_timeout=timedelta(minutes=40))
    def predict_m2(brand: str, run: dict) -> dict:
        cfg = load_cfg()
        export_keys("sedp-dev-client-wfd")
        from src.jobs import job_predict_m2

        res = job_predict_m2(cfg, brand, origin=run["origin"], sample_mode=run["sample_mode"])
        res.pop("feature_importance", None)  # XCom 크기 절약
        return res

    @task(outlets=[ASSET_M2])
    def publish(results) -> dict:
        results = list(results)  # 매핑 태스크 결과는 LazyXComSequence -> list 로 실체화
        cfg = load_cfg()
        # DB 저장은 job_predict_m2()가 이미 lwf_m2_forecast 에 적재 완료. 여기서는 최신 결과 요약만 파일로 유지.
        path = cfg["paths"]["outputs"] / "m2_latest.json"
        path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"published": str(path), "brands": [r["brand"] for r in results]}

    run = prepare_run()
    chk = check_inputs(run)
    feats = build_features(run)
    preds = predict_m2.partial(run=run).expand(brand=list_brands())
    chk >> feats >> preds
    publish(preds)


lwf_features_m2_weekly()
