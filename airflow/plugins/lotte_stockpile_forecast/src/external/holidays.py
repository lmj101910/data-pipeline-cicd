"""
한국천문연구원 특일 정보 - 공휴일 (+ 추석/설날 날짜 도출).

공공데이터포털: 한국천문연구원_특일 정보제공 서비스 (SpcdeInfoService)
  GET http://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo   (공휴일)
  params: serviceKey, solYear=YYYY, [solMonth=MM], numOfRows, pageNo, _type=json
  응답: locdate(YYYYMMDD int), dateName, isHoliday(Y/N), dateKind
"""
from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from ..common import get_env
from .base import Http, datago_items

log = logging.getLogger(__name__)
URL = "http://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"


def fetch_holidays(years: list[int], key: str | None = None, http: Http | None = None) -> pd.DataFrame:
    key = key or get_env("DATA_GO_KR_KEY")
    http = http or Http()
    rows = []
    for y in years:
        params = dict(serviceKey=key, solYear=y, numOfRows=100, pageNo=1, _type="json")
        for it in datago_items(http.get_json(URL, params=params)):
            rows.append(dict(date=pd.to_datetime(str(it["locdate"]), format="%Y%m%d"),
                             name=str(it.get("dateName", "")).strip(),
                             is_holiday=str(it.get("isHoliday", "Y")).upper() == "Y"))
        log.info("공휴일 %d: %d건", y, sum(1 for r in rows if r["date"].year == y))
    return pd.DataFrame(rows).drop_duplicates(["date", "name"]).sort_values("date").reset_index(drop=True)


def derive_lunar_holiday(holidays: pd.DataFrame, keyword: str) -> dict[int, date]:
    """'추석'/'설날' 이름을 가진 연휴(3일)의 가운데 날 = 당일. 대체공휴일 제외."""
    out: dict[int, date] = {}
    h = holidays[holidays["name"].str.contains(keyword) & ~holidays["name"].str.contains("대체")]
    for y, g in h.groupby(h["date"].dt.year):
        ds = sorted(g["date"].dt.date.unique())
        if ds:
            out[int(y)] = ds[len(ds) // 2]
    return out
