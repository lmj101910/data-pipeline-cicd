"""
거시/기후 보조 지표 (우선순위 낮음 - 3년 데이터에서는 추세와 구분이 어려움. ablation 후 유지 여부 결정).

[NOAA ONI] https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt  (정적 텍스트, 인증 불필요)
  컬럼: SEAS YR TOTAL ANOM  (SEAS 는 3개월 이동평균 라벨 e.g. JJA -> 중심월 7월)

[한국은행 ECOS] https://ecos.bok.or.kr/api/StatisticSearch/{KEY}/json/kr/1/1000/{STAT}/M/{YYYYMM}/{YYYYMM}/{ITEM}
  소비자심리지수: STAT=511Y002, ITEM=FME (코드는 ECOS 통계코드검색에서 확인 후 config 수정)
"""
from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from ..common import get_env
from .base import Http

log = logging.getLogger(__name__)
ONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"
_SEAS = {"DJF": 1, "JFM": 2, "FMA": 3, "MAM": 4, "AMJ": 5, "MJJ": 6, "JJA": 7, "JAS": 8, "ASO": 9,
         "SON": 10, "OND": 11, "NDJ": 12}


def fetch_oni(http: Http | None = None) -> pd.DataFrame:
    http = http or Http()
    rows = []
    for line in http.get_text(ONI_URL).splitlines()[1:]:
        p = line.split()
        if len(p) >= 4 and p[0] in _SEAS:
            rows.append(dict(year=int(p[1]), month=_SEAS[p[0]], oni=float(p[3])))
    return pd.DataFrame(rows)


def fetch_consumer_sentiment(start: date, end: date, stat_code: str = "511Y002", item_code: str = "FME",
                             http: Http | None = None) -> pd.DataFrame:
    http = http or Http()
    key = get_env("ECOS_KEY")
    url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/1000/{stat_code}/M/"
           f"{start.strftime('%Y%m')}/{end.strftime('%Y%m')}/{item_code}")
    res = http.get_json(url)
    if "StatisticSearch" not in res:
        raise RuntimeError(f"ECOS 응답 오류: {res}")
    rows = [dict(month=r["TIME"], csi=float(r["DATA_VALUE"])) for r in res["StatisticSearch"].get("row", [])]
    return pd.DataFrame(rows)
