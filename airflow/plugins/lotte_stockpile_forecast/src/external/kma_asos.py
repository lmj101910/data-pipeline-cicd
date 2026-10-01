"""
기상청 지상(종관, ASOS) 일자료 - 과거 실제 날씨 (학습용).

공공데이터포털: 기상청_지상(종관, ASOS) 일자료 조회서비스
  GET http://apis.data.go.kr/1360000/AsosDalyInfoService/getWthrDataList
  params: serviceKey, pageNo, numOfRows(<=999), dataType=JSON, dataCd=ASOS, dateCd=DAY,
          startDt=YYYYMMDD, endDt=YYYYMMDD, stnIds=관측소ID
  응답 필드(일부): tm(일자), stnId, avgTa, maxTa, minTa, sumRn(강수량mm), avgRhm(습도%), sumSsHr(일조시간)

주요 관측소: 서울108 인천112 수원119 강릉105 대전133 청주131 광주156 전주146 대구143 부산159 울산152 창원155 제주184
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd

from ..common import get_env
from .base import Http, datago_items

log = logging.getLogger(__name__)
URL = "http://apis.data.go.kr/1360000/AsosDalyInfoService/getWthrDataList"

_COLS = {"tm": "date", "stnId": "station_id", "avgTa": "tavg", "maxTa": "tmax", "minTa": "tmin",
         "sumRn": "rain_mm", "avgRhm": "humidity", "sumSsHr": "sunshine_hr"}


def fetch_asos_daily(station_id: int, start: date, end: date, key: str | None = None,
                     http: Http | None = None) -> pd.DataFrame:
    key = key or get_env("DATA_GO_KR_KEY")
    http = http or Http()
    frames = []
    cur = start
    while cur <= end:
        chunk_end = min(end, date(cur.year, 12, 31))  # 연 단위 청크 (<=366행, numOfRows 999 이내)
        params = dict(serviceKey=key, pageNo=1, numOfRows=999, dataType="JSON", dataCd="ASOS", dateCd="DAY",
                      startDt=cur.strftime("%Y%m%d"), endDt=chunk_end.strftime("%Y%m%d"), stnIds=station_id)
        items = datago_items(http.get_json(URL, params=params))
        if items:
            df = pd.DataFrame(items)[[c for c in _COLS if c in pd.DataFrame(items).columns]].rename(columns=_COLS)
            frames.append(df)
        log.info("ASOS %s %s~%s: %d rows", station_id, cur, chunk_end, len(items))
        cur = chunk_end + timedelta(days=1)
    if not frames:
        return pd.DataFrame(columns=list(_COLS.values()))
    out = pd.concat(frames, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"])
    for c in ["tavg", "tmax", "tmin", "rain_mm", "humidity", "sunshine_hr"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    if "rain_mm" in out.columns:
        out["rain_mm"] = out["rain_mm"].fillna(0.0)  # ASOS 는 무강수일을 공백으로 반환
    out["station_id"] = pd.to_numeric(out["station_id"], errors="coerce").astype("Int64")
    return out


def fetch_weather_daily_for_regions(regions_cfg: dict, start: date, end: date) -> pd.DataFrame:
    """config.regions 의 권역별 대표 관측소로 수집 -> weather_daily 스키마."""
    http = Http()
    frames = []
    for region, rc in regions_cfg.items():
        df = fetch_asos_daily(int(rc["asos_station"]), start, end, http=http)
        df["region"] = region
        frames.append(df)
    return pd.concat(frames, ignore_index=True)
