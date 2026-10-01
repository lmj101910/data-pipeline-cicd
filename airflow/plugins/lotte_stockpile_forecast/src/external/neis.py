"""
나이스(NEIS) 교육정보 개방포털 - 학사일정 -> 권역별 여름방학 시작/종료.

  GET https://open.neis.go.kr/hub/schoolInfo      (학교 기본정보: 학교코드 조회)
  GET https://open.neis.go.kr/hub/SchoolSchedule  (학사일정)
  params: KEY, Type=json, pIndex, pSize, ATPT_OFCDC_SC_CODE(교육청코드), SD_SCHUL_CODE(학교코드), AA_FROM_YMD, AA_TO_YMD
  교육청코드: B10서울 C10부산 D10대구 E10인천 F10광주 G10대전 H10울산 I10세종 J10경기 K10강원 M10충북 N10충남 P10전북 Q10전남 R10경북 S10경남 T10제주

방학은 학교별로 다르므로 권역별 대표 학교 1~2개(config.neis_sample_schools)를 표본으로 사용.
"""
from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from ..common import get_env
from .base import Http

log = logging.getLogger(__name__)
SCHEDULE_URL = "https://open.neis.go.kr/hub/SchoolSchedule"
SCHOOL_URL = "https://open.neis.go.kr/hub/schoolInfo"


def _rows(payload: dict, key: str) -> list[dict]:
    if key not in payload:  # {"RESULT": {"CODE": "INFO-200", "MESSAGE": "해당하는 데이터가 없습니다."}}
        msg = payload.get("RESULT", {})
        if str(msg.get("CODE", "")).startswith("INFO-200"):
            return []
        raise RuntimeError(f"NEIS 응답 오류: {msg}")
    for part in payload[key]:
        if "row" in part:
            return part["row"]
    return []


def find_school_code(office_code: str, school_name: str, http: Http | None = None) -> pd.DataFrame:
    http = http or Http()
    params = dict(KEY=get_env("NEIS_KEY"), Type="json", pIndex=1, pSize=50,
                  ATPT_OFCDC_SC_CODE=office_code, SCHUL_NM=school_name)
    rows = _rows(http.get_json(SCHOOL_URL, params=params), "schoolInfo")
    return pd.DataFrame(rows)[["ATPT_OFCDC_SC_CODE", "SD_SCHUL_CODE", "SCHUL_NM", "SCHUL_KND_SC_NM", "ORG_RDNMA"]] \
        if rows else pd.DataFrame()


def fetch_school_schedule(office_code: str, school_code: str, start: date, end: date,
                          http: Http | None = None) -> pd.DataFrame:
    http = http or Http()
    out = []
    page = 1
    while True:
        params = dict(KEY=get_env("NEIS_KEY"), Type="json", pIndex=page, pSize=1000,
                      ATPT_OFCDC_SC_CODE=office_code, SD_SCHUL_CODE=school_code,
                      AA_FROM_YMD=start.strftime("%Y%m%d"), AA_TO_YMD=end.strftime("%Y%m%d"))
        rows = _rows(http.get_json(SCHEDULE_URL, params=params), "SchoolSchedule")
        out.extend(rows)
        if len(rows) < 1000:
            break
        page += 1
    if not out:
        return pd.DataFrame(columns=["date", "event_name"])
    df = pd.DataFrame(out)
    return pd.DataFrame({"date": pd.to_datetime(df["AA_YMD"], format="%Y%m%d"), "event_name": df["EVENT_NM"]})


def derive_summer_vacation(schedule: pd.DataFrame) -> pd.DataFrame:
    """학사일정 -> 연도별 여름방학 시작/종료 (휴리스틱: 7~8월 '방학' 최초일 ~ 8~9월 '개학' 최초일)."""
    rows = []
    for y, g in schedule.groupby(schedule["date"].dt.year):
        s = g[(g["date"].dt.month.isin([7, 8])) & g["event_name"].str.contains("방학")]
        e = g[(g["date"].dt.month.isin([8, 9])) & g["event_name"].str.contains("개학")]
        if len(s):
            start = s["date"].min()
            end = e["date"].min() if len(e) else start + pd.Timedelta(days=30)
            rows.append(dict(year=int(y), summer_start=start, summer_end=end))
    return pd.DataFrame(rows)


def fetch_vacations_for_regions(sample_schools: list[dict], start: date, end: date) -> pd.DataFrame:
    """config.neis_sample_schools -> school_schedule 스키마 [region, year, summer_start, summer_end] (권역 내 중위값)."""
    http = Http()
    frames = []
    for s in sample_schools:
        try:
            sched = fetch_school_schedule(s["office_code"], str(s["school_code"]), start, end, http=http)
            v = derive_summer_vacation(sched)
            v["region"] = s["region"]
            frames.append(v)
        except Exception as e:  # noqa: BLE001
            log.error("NEIS 수집 실패 %s: %s", s, e)
    if not frames:
        return pd.DataFrame(columns=["region", "year", "summer_start", "summer_end"])
    allv = pd.concat(frames, ignore_index=True)
    return (allv.groupby(["region", "year"], as_index=False)
                .agg(summer_start=("summer_start", "median"), summer_end=("summer_end", "median")))
