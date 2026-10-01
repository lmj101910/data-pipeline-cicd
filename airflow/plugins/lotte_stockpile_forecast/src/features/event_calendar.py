"""
캘린더 변수 (브랜드별).

빼빼로(fixed_date): 11/11 까지 주수, 11/11 요일, 추석~11/11 간격, 수능까지 일수, 이벤트 윈도우 플래그
팥빙수(season):     시즌 시작 후 경과 주, 여름방학 비율, 윈도우 플래그
공통:               ISO 주, 월, 월중 주차, 주중 공휴일 수

파일명 변경 이력: 원래 calendar.py 였으나, plugins/ 폴더 전체가 Airflow PluginsManager 스캔 대상이라
파이썬 표준 라이브러리 calendar 모듈과 이름이 충돌 -> Celery(calendar.monthrange 사용)가 이 파일을
잘못 import 하면서 스케줄러가 기동 시점에 계속 죽는 문제가 있었음. event_calendar.py 로 개명해 충돌 제거.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from ..common import week_start


def _event_date(year: int, mmdd: str) -> pd.Timestamp:
    m, d = mmdd.split("-")
    return pd.Timestamp(year=year, month=int(m), day=int(d))


def weekday_holidays(holidays: pd.DataFrame | None, weeks: pd.DatetimeIndex) -> pd.Series:
    """주(월~일) 내 평일 공휴일 수."""
    if holidays is None or len(holidays) == 0:
        return pd.Series(0, index=weeks, dtype=int)
    h = holidays[holidays["is_holiday"]].copy()
    h = h[h["date"].dt.weekday < 5]
    h["week_start"] = week_start(h["date"])
    cnt = h.groupby("week_start")["date"].nunique()
    return cnt.reindex(weeks, fill_value=0).astype(int)


def vacation_share(weeks: pd.DatetimeIndex, region: str | None, vacations: pd.DataFrame | None,
                   default_mmdd: dict) -> pd.Series:
    """주 내 여름방학 일수 비율. 권역별 NEIS 값 없으면 config 기본값."""
    out = pd.Series(0.0, index=weeks)
    years = sorted(set(weeks.year) | {weeks.year.max() + 1})
    for y in years:
        s = e = None
        if vacations is not None and len(vacations):
            v = vacations[(vacations["year"] == y) & ((vacations["region"] == region) if region else True)]
            if len(v):
                s, e = v["summer_start"].iloc[0], v["summer_end"].iloc[0]
        if s is None:
            s, e = _event_date(y, default_mmdd["start"]), _event_date(y, default_mmdd["end"])
        for w in weeks[(weeks >= s - pd.Timedelta(days=6)) & (weeks <= e)]:
            days = pd.date_range(w, w + pd.Timedelta(days=6))
            out[w] = ((days >= s) & (days <= e)).mean()
    return out


def build_calendar(weeks: pd.DatetimeIndex, brand: str, cfg: dict, holidays: pd.DataFrame | None = None,
                   chuseok: dict[int, date] | None = None, vacations: pd.DataFrame | None = None,
                   region: str | None = None) -> pd.DataFrame:
    bc = cfg["brands"][brand]
    weeks = pd.DatetimeIndex(sorted(set(weeks)))
    iso = weeks.isocalendar()
    cal = pd.DataFrame({"week_start": weeks,
                        "year": weeks.year, "iso_week": iso["week"].astype(int).values,
                        "month": weeks.month, "week_of_month": ((weeks.day - 1) // 7 + 1)})
    cal["n_holidays_wd"] = weekday_holidays(holidays, weeks).values
    cal["vacation_share"] = vacation_share(weeks, region, vacations, cfg["calendar"]["default_summer_vacation"]).values

    lo, hi = bc["m1"]["window_iso_weeks"]
    cal["in_window"] = ((cal["iso_week"] >= lo) & (cal["iso_week"] <= hi)).astype(int)
    cal["weeks_since_window_start"] = cal["iso_week"] - lo  # 시즌 진행도 (음수 = 시즌 전)

    if bc["event_type"] == "fixed_date":
        ev = pd.Series([_event_date(y, bc["event_date"]) for y in cal["year"]], index=cal.index)
        cal["days_to_event"] = (ev - cal["week_start"]).dt.days
        cal["weeks_to_event"] = np.floor(cal["days_to_event"] / 7).astype(int)
        cal["event_weekday"] = ev.dt.weekday  # 0=월 ... 6=일
        chuseok = chuseok or {}
        cal["chuseok_gap_days"] = [(e - pd.Timestamp(chuseok[y])).days if y in chuseok else np.nan
                                   for y, e in zip(cal["year"], ev)]
        sn = {int(k): pd.Timestamp(v) for k, v in cfg["calendar"].get("suneung_dates", {}).items()}
        cal["days_to_suneung"] = [(sn[y] - w).days if y in sn else np.nan for y, w in zip(cal["year"], cal["week_start"])]
    return cal
