"""외부 데이터 일괄 수집. 소스별 실패는 로그만 남기고 계속 진행 (선택 데이터는 없어도 파이프라인 동작)."""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pandas as pd

from ..common import load_env
from . import holidays, kma_asos, kma_forecast, macro, naver_datalab, neis

log = logging.getLogger(__name__)


def _save(df: pd.DataFrame, path: Path, what: str) -> None:
    if df is None or len(df) == 0:
        log.warning("%s: 수집 결과 0건 -> 저장 생략", what)
        return
    df.to_csv(path, index=False)
    log.info("%s -> %s (%d rows)", what, path, len(df))


def collect_all(cfg: dict, start: date, end: date, sources: list[str] | None = None) -> None:
    load_env()
    ext = Path(cfg["paths"]["external"])
    sources = sources or ["weather", "forecast", "holidays", "search", "neis", "oni", "ecos"]
    years = list(range(start.year, end.year + 2))  # 다음 연도 공휴일까지 (예측 대상 기간)

    if "weather" in sources:
        try:
            _save(kma_asos.fetch_weather_daily_for_regions(cfg["regions"], start, end), ext / "weather_daily.csv", "ASOS 일자료")
        except Exception as e:  # noqa: BLE001
            log.error("ASOS 실패: %s", e)

    if "forecast" in sources:
        try:
            fc = kma_forecast.fetch_forecast_for_regions(cfg["regions"])
            if len(fc):
                kma_forecast.append_forecast_archive(fc, ext / "weather_forecast.csv")
                log.info("예보 아카이브 append: %d rows", len(fc))
        except Exception as e:  # noqa: BLE001
            log.error("단기/중기예보 실패: %s", e)

    if "holidays" in sources:
        try:
            _save(holidays.fetch_holidays(years), ext / "holidays.csv", "공휴일")
        except Exception as e:  # noqa: BLE001
            log.error("공휴일 실패: %s", e)

    if "search" in sources:
        try:
            groups = {b: bc["search_keywords"] for b, bc in cfg["brands"].items()}
            # 전체 기간 1회 요청 (연도 간 비교 가능하도록). 2016년부터 받아 장기 이벤트 형태 프록시로도 사용.
            df = naver_datalab.fetch_search_trend(groups, date(2016, 1, 1), end, time_unit="week")
            _save(df, ext / "search_trend.csv", "데이터랩 검색어트렌드")
        except Exception as e:  # noqa: BLE001
            log.error("데이터랩 실패: %s", e)

    if "neis" in sources and cfg.get("neis_sample_schools"):
        try:
            _save(neis.fetch_vacations_for_regions(cfg["neis_sample_schools"], start, end),
                  ext / "school_schedule.csv", "NEIS 여름방학")
        except Exception as e:  # noqa: BLE001
            log.error("NEIS 실패: %s", e)

    if "oni" in sources:
        try:
            _save(macro.fetch_oni(), ext / "oni.csv", "NOAA ONI")
        except Exception as e:  # noqa: BLE001
            log.error("ONI 실패: %s", e)

    if "ecos" in sources:
        try:
            ec = cfg.get("ecos", {})
            _save(macro.fetch_consumer_sentiment(start, end, ec.get("stat_code", "511Y002"), ec.get("item_code", "FME")),
                  ext / "consumer_sentiment.csv", "ECOS 소비자심리지수")
        except Exception as e:  # noqa: BLE001
            log.error("ECOS 실패: %s", e)
