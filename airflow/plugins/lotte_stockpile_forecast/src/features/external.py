"""외부 지표 주간 정렬: 네이버 검색 트렌드(브랜드별), 유동인구(권역별), 소비자심리(월->주)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..common import week_start


def search_weekly(search: pd.DataFrame | None, brand: str, weeks: pd.DatetimeIndex) -> pd.DataFrame:
    """[week_start, search_ratio, search_ratio_4w, search_yoy]. 그룹명 = 브랜드명으로 수집되어 있어야 함."""
    out = pd.DataFrame({"week_start": weeks})
    if search is None or len(search) == 0 or brand not in set(search["group"]):
        out["search_ratio"] = np.nan
        out["search_ratio_4w"] = np.nan
        out["search_yoy"] = np.nan
        return out
    s = search[search["group"] == brand].copy()
    s["week_start"] = week_start(s["period"])
    s = s.groupby("week_start", as_index=False)["ratio"].mean().sort_values("week_start")
    full = pd.DataFrame({"week_start": pd.date_range(s["week_start"].min(), max(s["week_start"].max(), weeks.max()),
                                                     freq="7D")})
    s = full.merge(s, on="week_start", how="left")
    s["ratio"] = s["ratio"].ffill()
    s["search_ratio_4w"] = s["ratio"].rolling(4, min_periods=1).mean()
    s["search_yoy"] = s["search_ratio_4w"] / s["search_ratio_4w"].shift(52).replace(0, np.nan)
    s = s.rename(columns={"ratio": "search_ratio"})
    return out.merge(s, on="week_start", how="left")


def foot_traffic_weekly(ft: pd.DataFrame | None) -> pd.DataFrame | None:
    if ft is None or len(ft) == 0:
        return None
    f = ft.copy()
    f["week_start"] = week_start(f["week_start"])
    if "is_forecast" not in f.columns:
        f["is_forecast"] = False
    f = f.sort_values("is_forecast").drop_duplicates(["region", "week_start"], keep="first")  # 실측 우선
    return f[["region", "week_start", "index"]].rename(columns={"index": "foot_traffic"})


def csi_weekly(csi: pd.DataFrame | None, weeks: pd.DatetimeIndex) -> pd.DataFrame | None:
    """소비자심리지수(월) -> 주. 발표 시차(익월 말) 고려해 1개월 lag 적용."""
    if csi is None or len(csi) == 0:
        return None
    c = csi.copy()
    c["month_start"] = pd.to_datetime(c["month"].astype(str), format="%Y%m") + pd.DateOffset(months=1)
    c = c.sort_values("month_start")
    out = pd.DataFrame({"week_start": weeks}).sort_values("week_start")
    return pd.merge_asof(out, c[["month_start", "csi"]], left_on="week_start", right_on="month_start",
                         direction="backward")[["week_start", "csi"]]
