"""
[모델] M1 전략 예측 - 이벤트 윈도우 누적수요 분위수 -> 비축량 (뉴스벤더).

Airflow 3.3.2 / Python 3.13. foot_traffic(인구이동) 데이터는 사용하지 않음.
브랜드별 DAG 를 팩토리로 생성:
  lwf_m1_pepero     : 8/31 09:00 (1차, 생산계획용) + 9/30 09:00 (2차, 사전발주 반영)  <- MultipleCronTriggerTimetable
  lwf_m1_patbingsu  : 4/15 09:00 (시즌 전 비축)
origin = 입력 기준일 또는 실행일(KST). prepare_run -> build_features -> predict_m1 -> publish
sample_mode=true(기본): 출고 표본의 주간 분위수 x 행사 주 수로 결과 생성. 표본이 없으면 0.
피처 구축/모델 학습/백테스트 생략. 대상 연도 미지정 시 기준일 이후 가장 가까운 행사/시즌 사용.
sample_mode=false: 기존 학습 모델. 대상 연도 기본값은 기준일의 연도.
수동 트리거 시 origin/target_year/sample_mode/cu/co/opening_inventory/buffer 조정 가능.

DB 저장이 기본: build_features -> lwf_panel_weekly, predict_m1 -> lwf_m1_decision / lwf_m1_decision_raw
(src/jobs.py 내부에서 처리). CSV 저장 코드는 schema.USE_DB 플래그로 감싸 유지.

Connection: sedp-dev-client-wfd (분석 DB, Postgres)
"""
from __future__ import annotations

import json
from datetime import timedelta

import pendulum
from airflow.sdk import Asset, Param, dag, get_current_context, task
from airflow.timetables.trigger import MultipleCronTriggerTimetable

from lotte_stockpile_forecast.lwf_common import DEFAULT_ARGS, KST, export_keys, forecast_options, load_cfg

ASSET_M1 = Asset(name="lwf_m1_decision", uri="lwf://outputs/m1_decision")

M1_SPECS = {
    "lwf_m1_pepero": {"brand": "빼빼로", "crons": ["0 9 31 8 *", "0 9 30 9 *"], "display": "[모델] M1 빼빼로데이 비축량 (8/31, 9/30)"},
    "lwf_m1_patbingsu": {"brand": "팥빙수", "crons": ["0 9 15 4 *"], "display": "[모델] M1 팥빙수 여름 비축량 (4/15)"},
}


def make_m1_dag(dag_id: str, brand: str, crons: list[str], display: str):
    @dag(
        dag_id=dag_id,
        dag_display_name=display,
        schedule=MultipleCronTriggerTimetable(*crons, timezone=KST),
        start_date=pendulum.datetime(2026, 1, 1, tz=KST),
        catchup=False,
        max_active_runs=1,
        default_args={**DEFAULT_ARGS, "retries": 1},
        dagrun_timeout=timedelta(hours=2),
        params={
            "cu": Param(None, type=["null", "number"], exclusiveMinimum=0,
                        description="결품 단위비용 (미지정 시 config.newsvendor.cu)"),
            "co": Param(None, type=["null", "number"], exclusiveMinimum=0,
                        description="과잉 단위비용 (미지정 시 config.newsvendor.co)"),
            "opening_inventory": Param(0, type="number", minimum=0, description="윈도우 시작 시점 예상 재고"),
            "buffer": Param(0.0, type="number", minimum=0, maximum=1, description="판단적 버퍼 비율 (모델 외, 분리 기록)"),
            "origin": Param(None, type=["null", "string"], format="date",
                            description="예측 기준일 YYYY-MM-DD. 미입력 시 실행일(KST)"),
            "target_year": Param(None, type=["null", "integer"], minimum=2000, maximum=2100,
                                 description="대상 행사 연도. 표본 모드에서 미입력 시 가장 가까운 행사/시즌"),
            "sample_mode": Param(True, type="boolean",
                                 description="true: 표본으로 결과 생성(빈 표본은 0), false: 기존 학습 모델"),
        },
        tags=["lwf", "model", "m1", brand],
        doc_md=__doc__,
    )
    def _m1_dag():
        @task
        def prepare_run() -> dict:
            return forecast_options(get_current_context())

        @task(execution_timeout=timedelta(minutes=30))
        def build_features(run: dict) -> dict:
            cfg = load_cfg()
            export_keys("sedp-dev-client-wfd")
            from src.jobs import job_build_features

            return job_build_features(cfg, extend_weeks=16, sample_mode=run["sample_mode"])

        @task(execution_timeout=timedelta(minutes=30))
        def predict_m1(run: dict) -> dict:
            ctx = get_current_context()
            p = ctx["params"]
            cfg = load_cfg()
            export_keys("sedp-dev-client-wfd")
            from src.jobs import job_predict_m1

            res = job_predict_m1(cfg, brand, run["origin"], p.get("target_year"), p.get("cu"), p.get("co"),
                                 float(p.get("opening_inventory") or 0.0), float(p.get("buffer") or 0.0),
                                 with_backtest=not run["sample_mode"], sample_mode=run["sample_mode"])
            res.pop("cum_curve", None)
            return res

        @task(outlets=[ASSET_M1])
        def publish(res: dict) -> dict:
            cfg = load_cfg()
            # DB 저장은 job_predict_m1()이 이미 lwf_m1_decision/lwf_m1_decision_raw 에 적재 완료.
            # 여기서는 최신 결과 요약만 파일로 유지 (다른 시스템 연동용).
            d = res["decision"]
            summary = {"brand": res["brand"], "target_year": res["target_year"], "origin": res["origin"], "tau": d["tau"],
                       "demand_q50": d["demand_q50"], "demand_q_tau": d["demand_q_tau"], "benchmark": d["benchmark"],
                       "stockpile_total": d["stockpile_total"], "train_years": res["train_years"]}
            summary.update({key: res.get(key) for key in
                            ("sample_mode", "method", "data_as_of", "sample_rows", "sample_weeks",
                             "sample_years", "window_start", "window_end", "reason")})
            path = cfg["paths"]["outputs"] / f"m1_latest_{res['brand']}.json"
            path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            return summary

        run = prepare_run()
        feats = build_features(run)
        res = predict_m1(run)
        feats >> res
        publish(res)

    return _m1_dag()


for _dag_id, _spec in M1_SPECS.items():
    globals()[_dag_id] = make_m1_dag(_dag_id, _spec["brand"], _spec["crons"], _spec["display"])
