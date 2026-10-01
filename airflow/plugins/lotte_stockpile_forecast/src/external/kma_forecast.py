"""
기상청 단기예보(~3일) + 중기예보(4~10일) -> weather_forecast 스키마로 아카이브.

[단기예보] 기상청_단기예보 ((구)_동네예보) 조회서비스
  GET http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst
  params: serviceKey, pageNo, numOfRows, dataType=JSON, base_date=YYYYMMDD, base_time=HHMM(0200 권장), nx, ny
  category: TMX(일최고), TMN(일최저), POP(강수확률), PCP(1시간 강수량 문자열), TMP, REH ...

[중기예보] 기상청_중기예보 조회서비스
  GET http://apis.data.go.kr/1360000/MidFcstInfoService/getMidTa        (중기기온: taMin4~taMin10, taMax4~taMax10)
  GET http://apis.data.go.kr/1360000/MidFcstInfoService/getMidLandFcst  (중기육상: rnSt4Am/Pm ~ rnSt10, 강수확률)
  params: serviceKey, pageNo, numOfRows, dataType=JSON, regId, tmFc=YYYYMMDD0600|1800
  * 발표 형식이 개정될 수 있어 필드는 정규식(ta(Min|Max)N, rnStN(Am|Pm)?)으로 파싱

기존에 수집 중인 28일 예보 피드도 동일 스키마(issue_date, target_date, region, tmax, tmin, pop_max, rain_mm, source)
로 적재하면 features/weather.py 가 그대로 사용.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

import pandas as pd

from ..common import get_env
from .base import Http, datago_items

log = logging.getLogger(__name__)
SHORT_URL = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
MID_TA_URL = "http://apis.data.go.kr/1360000/MidFcstInfoService/getMidTa"
MID_LAND_URL = "http://apis.data.go.kr/1360000/MidFcstInfoService/getMidLandFcst"


def _parse_pcp(v: str) -> float:
    """PCP 문자열 -> mm. '강수없음'->0, '1.0mm'->1.0, '30.0~50.0mm'->30, '50.0mm 이상'->50, '1mm 미만'->0.5"""
    if v is None:
        return 0.0
    s = str(v)
    if "없음" in s or s.strip() in ("", "-"):
        return 0.0
    if "미만" in s:
        return 0.5
    m = re.search(r"(\d+(\.\d+)?)", s)
    return float(m.group(1)) if m else 0.0


def fetch_short_term(nx: int, ny: int, base_date: date | None = None, key: str | None = None,
                     http: Http | None = None) -> pd.DataFrame:
    key = key or get_env("DATA_GO_KR_KEY")
    http = http or Http()
    base_date = base_date or date.today()
    params = dict(serviceKey=key, pageNo=1, numOfRows=1000, dataType="JSON",
                  base_date=base_date.strftime("%Y%m%d"), base_time="0200", nx=nx, ny=ny)
    items = datago_items(http.get_json(SHORT_URL, params=params))
    if not items:
        return pd.DataFrame()
    df = pd.DataFrame(items)
    df["target_date"] = pd.to_datetime(df["fcstDate"])
    rows = []
    for d, g in df.groupby("target_date"):
        def _val(cat):
            v = g.loc[g["category"] == cat, "fcstValue"]
            return pd.to_numeric(v.iloc[0], errors="coerce") if len(v) else float("nan")
        tmp = pd.to_numeric(g.loc[g["category"] == "TMP", "fcstValue"], errors="coerce")
        tmax, tmin = _val("TMX"), _val("TMN")
        if pd.isna(tmax) and len(tmp):
            tmax = tmp.max()
        if pd.isna(tmin) and len(tmp):
            tmin = tmp.min()
        pop = pd.to_numeric(g.loc[g["category"] == "POP", "fcstValue"], errors="coerce")
        pcp = g.loc[g["category"] == "PCP", "fcstValue"].map(_parse_pcp)
        rows.append(dict(issue_date=pd.Timestamp(base_date), target_date=d, tmax=tmax, tmin=tmin,
                         pop_max=pop.max() if len(pop) else float("nan"), rain_mm=pcp.sum(), source="kma_short"))
    out = pd.DataFrame(rows)
    return out[out["target_date"] > pd.Timestamp(base_date)]  # 발표일 당일 제외 (TMX/TMN 불완전)


def _mid_tmfc(now: datetime | None = None) -> str:
    now = now or datetime.now()
    if now.hour >= 18:
        return now.strftime("%Y%m%d") + "1800"
    if now.hour >= 6:
        return now.strftime("%Y%m%d") + "0600"
    return (now - timedelta(days=1)).strftime("%Y%m%d") + "1800"


def fetch_mid_term(reg_ta: str, reg_land: str, tm_fc: str | None = None, key: str | None = None,
                   http: Http | None = None) -> pd.DataFrame:
    key = key or get_env("DATA_GO_KR_KEY")
    http = http or Http()
    tm_fc = tm_fc or _mid_tmfc()
    issue = pd.Timestamp(datetime.strptime(tm_fc[:8], "%Y%m%d"))
    base = dict(serviceKey=key, pageNo=1, numOfRows=10, dataType="JSON", tmFc=tm_fc)

    ta_items = datago_items(http.get_json(MID_TA_URL, params={**base, "regId": reg_ta}))
    land_items = datago_items(http.get_json(MID_LAND_URL, params={**base, "regId": reg_land}))
    ta = ta_items[0] if ta_items else {}
    land = land_items[0] if land_items else {}

    rec: dict[int, dict] = {}
    for k, v in ta.items():
        m = re.fullmatch(r"ta(Min|Max)(\d+)", k)
        if m:
            d = int(m.group(2))
            rec.setdefault(d, {})["tmin" if m.group(1) == "Min" else "tmax"] = pd.to_numeric(v, errors="coerce")
    for k, v in land.items():
        m = re.fullmatch(r"rnSt(\d+)(Am|Pm)?", k)
        if m:
            d = int(m.group(1))
            cur = rec.setdefault(d, {}).get("pop_max", float("nan"))
            val = pd.to_numeric(v, errors="coerce")
            rec[d]["pop_max"] = val if pd.isna(cur) else max(cur, val)
    rows = [dict(issue_date=issue, target_date=issue + pd.Timedelta(days=d), tmax=r.get("tmax"), tmin=r.get("tmin"),
                 pop_max=r.get("pop_max", float("nan")), rain_mm=float("nan"), source="kma_mid")
            for d, r in sorted(rec.items()) if "tmax" in r or "tmin" in r]
    return pd.DataFrame(rows)


def fetch_forecast_for_regions(regions_cfg: dict, base_date: date | None = None) -> pd.DataFrame:
    """권역별 단기(D+1~3) + 중기(D+4~10) 예보 -> weather_forecast 스키마. 매일 호출해 아카이브에 append."""
    http = Http()
    frames = []
    for region, rc in regions_cfg.items():
        try:
            s = fetch_short_term(int(rc["nx"]), int(rc["ny"]), base_date=base_date, http=http)
            m = fetch_mid_term(rc["mid_ta_reg"], rc["mid_land_reg"], http=http)
            df = pd.concat([s, m], ignore_index=True)
            df["region"] = region
            frames.append(df)
        except Exception as e:  # noqa: BLE001
            log.error("예보 수집 실패 (%s): %s", region, e)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    # 단기/중기 중복 target_date 는 단기 우선
    out["prio"] = (out["source"] == "kma_short").astype(int)
    out = out.sort_values(["region", "target_date", "prio"], ascending=[True, True, False])
    out = out.drop_duplicates(["region", "target_date"]).drop(columns="prio")
    return out.reset_index(drop=True)


def append_forecast_archive(new: pd.DataFrame, path) -> pd.DataFrame:
    """예보 아카이브 파일에 append (issue_date, target_date, region 기준 중복 제거)."""
    import os
    if os.path.exists(path):
        old = pd.read_csv(path, parse_dates=["issue_date", "target_date"])
        allf = pd.concat([old, new], ignore_index=True)
    else:
        allf = new
    allf = allf.drop_duplicates(["issue_date", "target_date", "region"], keep="last")
    allf.to_csv(path, index=False)
    return allf
