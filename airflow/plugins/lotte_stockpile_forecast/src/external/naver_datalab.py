"""
네이버 데이터랩 - 검색어 트렌드 / 쇼핑인사이트 (무료, 일 1,000회).

[검색어 트렌드] POST https://openapi.naver.com/v1/datalab/search
  headers: X-Naver-Client-Id, X-Naver-Client-Secret, Content-Type: application/json
  body: {startDate(>=2016-01-01), endDate, timeUnit(date|week|month),
         keywordGroups:[{groupName, keywords:[...<=20]}]  (<=5 그룹), device, ages, gender}
  응답 ratio 는 "요청 내" 상대지수 (요청 전체 기간·그룹 중 최대=100).
  -> 연도 간 비교가 필요하므로 전체 기간을 한 번의 요청으로 받아야 함 (기간을 나눠 호출하면 스케일이 달라짐).
  -> 2016년부터 제공되므로 판매 데이터(2023~)보다 긴 이벤트 형태(램프 시점/기울기) 프록시로 활용 가능.

[쇼핑인사이트 키워드] POST https://openapi.naver.com/v1/datalab/shopping/category/keywords
  body: {startDate(>=2017-08-01), endDate, timeUnit, category:"catId", keyword:[{name, param:[kw]}](<=5), device, gender, ages}
"""
from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from ..common import get_env
from .base import Http

log = logging.getLogger(__name__)
SEARCH_URL = "https://openapi.naver.com/v1/datalab/search"
SHOP_KW_URL = "https://openapi.naver.com/v1/datalab/shopping/category/keywords"


def _headers() -> dict:
    return {"X-Naver-Client-Id": get_env("NAVER_CLIENT_ID"),
            "X-Naver-Client-Secret": get_env("NAVER_CLIENT_SECRET"),
            "Content-Type": "application/json"}


def fetch_search_trend(keyword_groups: dict[str, list[str]], start: date, end: date, time_unit: str = "week",
                       http: Http | None = None) -> pd.DataFrame:
    """keyword_groups: {그룹명: [키워드...]} (<=5 그룹) -> search_trend 스키마 [period, group, ratio]."""
    if len(keyword_groups) > 5:
        raise ValueError("검색어 트렌드는 요청당 최대 5개 그룹")
    http = http or Http()
    body = {"startDate": max(start, date(2016, 1, 1)).isoformat(), "endDate": end.isoformat(), "timeUnit": time_unit,
            "keywordGroups": [{"groupName": g, "keywords": kws[:20]} for g, kws in keyword_groups.items()],
            "device": "", "ages": [], "gender": ""}
    res = http.post_json(SEARCH_URL, body, headers=_headers())
    rows = [dict(period=pd.to_datetime(d["period"]), group=r["title"], ratio=float(d["ratio"]))
            for r in res.get("results", []) for d in r.get("data", [])]
    log.info("데이터랩 검색어 트렌드: %d rows (%s)", len(rows), list(keyword_groups))
    return pd.DataFrame(rows)


def fetch_shopping_keyword_trend(category_id: str, keywords: list[str], start: date, end: date,
                                 time_unit: str = "week", http: Http | None = None) -> pd.DataFrame:
    http = http or Http()
    body = {"startDate": max(start, date(2017, 8, 1)).isoformat(), "endDate": end.isoformat(), "timeUnit": time_unit,
            "category": category_id, "keyword": [{"name": k, "param": [k]} for k in keywords[:5]],
            "device": "", "gender": "", "ages": []}
    res = http.post_json(SHOP_KW_URL, body, headers=_headers())
    rows = [dict(period=pd.to_datetime(d["period"]), group=f"shop:{r['title']}", ratio=float(d["ratio"]))
            for r in res.get("results", []) for d in r.get("data", [])]
    return pd.DataFrame(rows)
