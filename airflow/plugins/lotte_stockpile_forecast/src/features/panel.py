"""
주간 패널 구축: 일별 SKU 출고 -> (brand, channel, region, week_start) 주간 시계열 + 데이터품질/행사/SKU 변수.

모델 기본 단위를 '주간'으로 두는 이유: 일 단위 노이즈·월말 밀어내기·요일 효과를 흡수하고, 결품/반품 보정이 단순해짐.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ..common import week_start

log = logging.getLogger(__name__)
KEYS = ["brand", "channel", "region", "week_start"]

# discount_rate 가 없을 때 행사 유형별 기본 할인 강도
_PROMO_DEFAULT_DISCOUNT = {"1+1": 0.5, "2+1": 0.33, "할인": 0.2, "덤": 0.15}


def _attach_brand(shipments: pd.DataFrame, sku_master: pd.DataFrame) -> pd.DataFrame:
    m = sku_master[["sku_code", "brand"]].drop_duplicates("sku_code")
    df = shipments.merge(m, on="sku_code", how="left")
    n_unmapped = df["brand"].isna().sum()
    if n_unmapped:
        log.warning("브랜드 매핑 안 된 출고 %d행 제거 (sku_master 갱신 필요)", n_unmapped)
        df = df.dropna(subset=["brand"])
    return df


def aggregate_weekly(shipments: pd.DataFrame, sku_master: pd.DataFrame) -> pd.DataFrame:
    df = _attach_brand(shipments, sku_master)
    df["week_start"] = week_start(df["date"])
    if "returns_qty" not in df.columns:
        df["returns_qty"] = 0.0
    df["returns_qty"] = df["returns_qty"].fillna(0.0)
    agg = df.groupby(KEYS, as_index=False).agg(qty=("qty", "sum"), returns_qty=("returns_qty", "sum"),
                                                n_sku_sold=("sku_code", "nunique"))
    # 완전 격자 (관측된 시리즈 x 전체 주). 미관측 = 0 출고로 채우고 플래그.
    weeks = pd.date_range(agg["week_start"].min(), agg["week_start"].max(), freq="7D")
    series = agg[["brand", "channel", "region"]].drop_duplicates()
    grid = series.merge(pd.DataFrame({"week_start": weeks}), how="cross")
    out = grid.merge(agg, on=KEYS, how="left")
    out["imputed_zero"] = out["qty"].isna().astype(int)
    out[["qty", "returns_qty", "n_sku_sold"]] = out[["qty", "returns_qty", "n_sku_sold"]].fillna(0)

    # 마지막 주가 미완결(일요일 전)이면 제거
    last_date = df["date"].max()
    last_week_end = out["week_start"].max() + pd.Timedelta(days=6)
    if last_date < last_week_end:
        log.info("미완결 마지막 주 제거: %s (데이터 마지막 일자 %s)", out["week_start"].max().date(), last_date.date())
        out = out[out["week_start"] < out["week_start"].max()]
    return out.sort_values(KEYS).reset_index(drop=True)


def sku_counts(sku_master: pd.DataFrame, weeks: pd.DatetimeIndex) -> pd.DataFrame:
    """(brand, week_start) 별 활성 SKU 수 / 한정판 SKU 수. 출시·단종일은 사전 확정되므로 미래 주에도 계산 가능."""
    sm = sku_master.copy()
    if "is_limited_edition" not in sm.columns:
        sm["is_limited_edition"] = False
    if "discontinue_date" not in sm.columns:
        sm["discontinue_date"] = pd.NaT
    rows = []
    for w in weeks:
        w_end = w + pd.Timedelta(days=6)
        active = sm[(sm["launch_date"] <= w_end) & (sm["discontinue_date"].isna() | (sm["discontinue_date"] >= w))]
        g = active.groupby("brand").agg(n_active_sku=("sku_code", "nunique"),
                                        n_limited_sku=("is_limited_edition", "sum")).reset_index()
        g["week_start"] = w
        rows.append(g)
    out = pd.concat(rows, ignore_index=True)
    out["n_limited_sku"] = out["n_limited_sku"].astype(int)
    return out


def promo_intensity(promotions: pd.DataFrame | None, brands: list[str], channels: list[str],
                    weeks: pd.DatetimeIndex) -> pd.DataFrame:
    """
    (brand, channel, week_start) 행사 강도 = 행사 진행일 비율 x (1 + 할인율).  범위 대략 [0, 1.5].
    같은 날짜에 actual/plan 이 모두 있으면 actual 우선. 미래 주는 plan 만 존재.
    """
    idx = pd.MultiIndex.from_product([brands, channels, weeks], names=["brand", "channel", "week_start"])
    base = pd.DataFrame(index=idx).reset_index()
    base["promo_intensity"] = 0.0
    base["promo_is_plan"] = 0
    if promotions is None or len(promotions) == 0:
        return base
    p = promotions.copy()
    if "discount_rate" not in p.columns:
        p["discount_rate"] = np.nan
    p["discount_rate"] = p["discount_rate"].fillna(p["promo_type"].map(_PROMO_DEFAULT_DISCOUNT)).fillna(0.1)
    p["channel"] = p["channel"].where(p["channel"] != "ALL", None)
    rows = []
    for r in p.itertuples(index=False):
        chs = channels if r.channel is None else [r.channel]
        for d in pd.date_range(r.start_date, r.end_date, freq="D"):
            for ch in chs:
                rows.append((r.brand, ch, d, float(r.discount_rate), 1 if r.status == "plan" else 0))
    daily = pd.DataFrame(rows, columns=["brand", "channel", "date", "discount_rate", "is_plan"])
    daily = daily.sort_values("is_plan").drop_duplicates(["brand", "channel", "date"], keep="first")  # actual 우선
    daily["week_start"] = week_start(daily["date"])
    wk = daily.groupby(["brand", "channel", "week_start"], as_index=False).agg(
        promo_days=("date", "nunique"), discount_rate=("discount_rate", "mean"), promo_is_plan=("is_plan", "max"))
    wk["promo_intensity"] = (wk["promo_days"] / 7.0) * (1.0 + wk["discount_rate"])
    out = base.drop(columns=["promo_intensity", "promo_is_plan"]).merge(
        wk[["brand", "channel", "week_start", "promo_intensity", "promo_is_plan"]],
        on=["brand", "channel", "week_start"], how="left")
    out["promo_intensity"] = out["promo_intensity"].fillna(0.0)
    out["promo_is_plan"] = out["promo_is_plan"].fillna(0).astype(int)
    return out


def stockout_share(stockouts: pd.DataFrame | None, sku_master: pd.DataFrame, counts: pd.DataFrame) -> pd.DataFrame | None:
    """(brand, channel, region, week_start) 결품 SKU-일 비율 = 결품 SKU-일 / (활성 SKU 수 x 7)."""
    if stockouts is None or len(stockouts) == 0:
        return None
    s = stockouts[stockouts["stockout_flag"]].merge(sku_master[["sku_code", "brand"]], on="sku_code", how="inner")
    s["week_start"] = week_start(s["date"])
    g = s.groupby(KEYS, as_index=False).agg(stockout_sku_days=("sku_code", "size"))
    g = g.merge(counts[["brand", "week_start", "n_active_sku"]], on=["brand", "week_start"], how="left")
    g["stockout_share"] = g["stockout_sku_days"] / (g["n_active_sku"].clip(lower=1) * 7.0)
    return g[KEYS + ["stockout_sku_days", "stockout_share"]]


def build_panel(shipments, sku_master, promotions=None, stockouts=None, distribution=None,
                channels: list[str] | None = None, max_stockout_share: float = 0.6,
                extend_weeks: int = 0) -> pd.DataFrame:
    """
    주간 패널 생성. extend_weeks>0 이면 미래 주(예측 대상)를 격자에 추가 (qty NaN, 행사계획/SKU수/캘린더만 채움).
    """
    panel = aggregate_weekly(shipments, sku_master)
    weeks = pd.DatetimeIndex(sorted(panel["week_start"].unique()))
    if extend_weeks > 0:
        future = pd.date_range(weeks.max() + pd.Timedelta(days=7), periods=extend_weeks, freq="7D")
        series = panel[["brand", "channel", "region"]].drop_duplicates()
        fut = series.merge(pd.DataFrame({"week_start": future}), how="cross")
        fut["imputed_zero"] = 0
        panel = pd.concat([panel, fut], ignore_index=True)
        weeks = weeks.append(future)

    brands = sorted(panel["brand"].unique())
    channels = channels or sorted(panel["channel"].unique())

    counts = sku_counts(sku_master, weeks)
    panel = panel.merge(counts, on=["brand", "week_start"], how="left")
    panel[["n_active_sku", "n_limited_sku"]] = panel[["n_active_sku", "n_limited_sku"]].fillna(0)

    promo = promo_intensity(promotions, brands, channels, weeks)
    panel = panel.merge(promo, on=["brand", "channel", "week_start"], how="left")
    panel["promo_intensity"] = panel["promo_intensity"].fillna(0.0)
    panel["promo_is_plan"] = panel["promo_is_plan"].fillna(0).astype(int)

    so = stockout_share(stockouts, sku_master, counts)
    if so is not None:
        panel = panel.merge(so, on=KEYS, how="left")
    else:
        panel["stockout_sku_days"] = 0.0
        panel["stockout_share"] = 0.0
        log.warning("stockouts.csv 없음 -> 결품 검열 보정 불가. 과거 결품이 있었다면 체계적 과소예측 위험.")
    panel[["stockout_sku_days", "stockout_share"]] = panel[["stockout_sku_days", "stockout_share"]].fillna(0.0)
    share = panel["stockout_share"].clip(upper=max_stockout_share)
    panel["qty_adj"] = panel["qty"] / (1.0 - share)  # 검열 보정 (단순 비례). 결품 없으면 qty 와 동일

    if distribution is not None and len(distribution):
        d = distribution.copy()
        d["week_start"] = week_start(d["week_start"])
        panel = panel.merge(d[KEYS + ["active_stores"]], on=KEYS, how="left")
        panel = panel.sort_values(KEYS)
        panel["active_stores"] = panel.groupby(["brand", "channel", "region"])["active_stores"].ffill()
    else:
        panel["active_stores"] = np.nan

    w_end = panel["week_start"] + pd.Timedelta(days=6)
    panel["month_end_week"] = (panel["week_start"].dt.month != w_end.dt.month).astype(int)
    panel["series_id"] = panel["brand"] + "|" + panel["channel"] + "|" + panel["region"]
    return panel.sort_values(KEYS).reset_index(drop=True)
