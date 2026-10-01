"""
CLI 진입점.

  python -m src.pipeline schema                                  # 입력 스키마 출력
  python -m src.pipeline fetch-external --start 2023-01-01       # 외부 API 수집 (.env 필요)
  python -m src.pipeline build-features                          # 주간 패널 + 피처 테이블
  python -m src.pipeline predict-m1 --brand 빼빼로 --origin 2026-08-31 --target-year 2026
  python -m src.pipeline predict-m2 --brand 팥빙수 --origin 2026-06-01
  python -m src.pipeline backtest-m1 --brand 빼빼로 --origin-mmdd 08-31
  python -m src.pipeline backtest-m2 --brand 팥빙수 --start 2025-05-05 --end 2025-08-25
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date
from pathlib import Path

import pandas as pd

from .common import load_config, setup_logging
from .features.build import build_feature_table, load_feature_table
from .schema import describe_schemas, load_table

log = logging.getLogger("pipeline")


def cmd_schema(_a, _cfg):
    print(describe_schemas())


def cmd_fetch(a, cfg):
    from .external.collect import collect_all
    collect_all(cfg, pd.Timestamp(a.start).date(), pd.Timestamp(a.end).date() if a.end else date.today(),
                a.sources.split(",") if a.sources else None)


def cmd_build(a, cfg):
    panel = build_feature_table(cfg, extend_weeks=a.extend_weeks)
    print(panel.groupby("brand")["week_start"].agg(["min", "max", "count"]))


def cmd_predict_m1(a, cfg):
    from .jobs import job_predict_m1
    res = job_predict_m1(cfg, a.brand, a.origin, a.target_year, a.cu, a.co, a.opening_inventory, a.buffer,
                         with_backtest=not a.no_backtest)
    print(f"\n[M1 {a.brand} {a.target_year}] origin={res['origin']}  학습연도={res['train_years']}  "
          f"검색기반 공통충격 sd={res['sd_common_search']}")
    print("\n[브랜드 총량 분위수]")
    for k, v in res["total_quantiles"].items():
        print(f"  {k:<22} {v:,.0f}" if isinstance(v, (int, float)) and v is not None and abs(v) > 10 else f"  {k:<22} {v}")
    print("\n[비축 결정]")
    for k, v in res["decision"].items():
        print(f"  {k:<22} {v:,.0f}" if isinstance(v, float) and abs(v) > 10 else f"  {k:<22} {v}")
    print("\n[누적 곡선 (윈도우 주차별 비중)]")
    print(pd.DataFrame(res["cum_curve"]).round(3).to_string(index=False))
    if "backtest" in res and res["backtest"]:
        pd.set_option("display.width", 250)
        print("\n[M1 백테스트 leave-last-year-out]")
        print(pd.DataFrame(res["backtest"]).round(3).to_string(index=False))


def cmd_predict_m2(a, cfg):
    from .jobs import job_predict_m2
    res = job_predict_m2(cfg, a.brand, a.origin)
    pd.set_option("display.width", 200)
    print(f"\n[M2 {a.brand}] origin={res['origin']} 브랜드 합계 (시리즈 분위수 합 = 보수적)")
    print(pd.DataFrame(res["forecast"]).round(0).to_string(index=False))
    for h, imp in res["feature_importance"].items():
        print(f"\n[h={h}] feature importance (gain, top 10)")
        print(pd.Series(imp).to_string())


def cmd_backtest_m1(a, cfg):
    from .backtest import backtest_m1
    panel = load_feature_table(cfg)
    preorders = load_table("preorders", cfg["paths"]["raw"])
    search = load_table("search_trend", cfg["paths"]["external"])
    res = backtest_m1(panel, a.brand, cfg, a.origin_mmdd, preorders, search)
    res.to_csv(Path(cfg["paths"]["outputs"]) / f"backtest_m1_{a.brand}_{a.origin_mmdd}.csv", index=False)
    pd.set_option("display.width", 250)
    print(f"\n[M1 백테스트 {a.brand}, origin {a.origin_mmdd}] leave-last-year-out")
    print(res.round(3).to_string(index=False))


def cmd_backtest_m2(a, cfg):
    from .backtest import backtest_m2
    panel = load_feature_table(cfg)
    ext = cfg["paths"]["external"]
    weather_daily, forecast = load_table("weather_daily", ext), load_table("weather_forecast", ext)
    allp, summary = backtest_m2(panel, a.brand, cfg, pd.Timestamp(a.start), pd.Timestamp(a.end), a.step,
                                weather_daily, forecast)
    out = Path(cfg["paths"]["outputs"])
    allp.to_csv(out / f"backtest_m2_{a.brand}_{a.start}_{a.end}.csv", index=False)
    summary.to_csv(out / f"backtest_m2_summary_{a.brand}_{a.start}_{a.end}.csv", index=False)
    pd.set_option("display.width", 250)
    print(f"\n[M2 백테스트 {a.brand}] {a.start}~{a.end}, step={a.step}주, 브랜드 합계 기준")
    print(summary.round(3).to_string(index=False))


def main():
    setup_logging()
    p = argparse.ArgumentParser(description="롯데웰푸드 비축재고 예측 파이프라인")
    p.add_argument("--config", default=None)
    sp = p.add_subparsers(dest="cmd", required=True)

    sp.add_parser("schema").set_defaults(fn=cmd_schema)

    f = sp.add_parser("fetch-external")
    f.add_argument("--start", default="2023-01-01")
    f.add_argument("--end", default=None)
    f.add_argument("--sources", default=None, help="weather,forecast,holidays,search,neis,oni,ecos 중 콤마 구분")
    f.set_defaults(fn=cmd_fetch)

    b = sp.add_parser("build-features")
    b.add_argument("--extend-weeks", type=int, default=12, help="미래 주 격자 확장 (예측 대상)")
    b.set_defaults(fn=cmd_build)

    m1 = sp.add_parser("predict-m1")
    m1.add_argument("--brand", required=True)
    m1.add_argument("--origin", required=True, help="예측 시점 (예: 2026-08-31)")
    m1.add_argument("--target-year", type=int, required=True)
    m1.add_argument("--cu", type=float, default=None)
    m1.add_argument("--co", type=float, default=None)
    m1.add_argument("--opening-inventory", type=float, default=0.0)
    m1.add_argument("--buffer", type=float, default=0.0, help="판단적 버퍼 비율 (모델 외, 분리 기록)")
    m1.add_argument("--no-backtest", action="store_true")
    m1.set_defaults(fn=cmd_predict_m1)

    m2 = sp.add_parser("predict-m2")
    m2.add_argument("--brand", required=True)
    m2.add_argument("--origin", default=None, help="마지막 실적 주(월요일). 미지정 시 최신")
    m2.set_defaults(fn=cmd_predict_m2)

    b1 = sp.add_parser("backtest-m1")
    b1.add_argument("--brand", required=True)
    b1.add_argument("--origin-mmdd", default="08-31")
    b1.set_defaults(fn=cmd_backtest_m1)

    b2 = sp.add_parser("backtest-m2")
    b2.add_argument("--brand", required=True)
    b2.add_argument("--start", required=True)
    b2.add_argument("--end", required=True)
    b2.add_argument("--step", type=int, default=4)
    b2.set_defaults(fn=cmd_backtest_m2)

    a = p.parse_args()
    cfg = load_config(a.config)
    a.fn(a, cfg)


if __name__ == "__main__":
    main()
