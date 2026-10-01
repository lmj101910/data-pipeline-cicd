"""
M1 전략 모델: 이벤트 윈도우 누적 출고량 (비축 총량 결정용).

행 = (연도 x 채널 x 권역) 풀링.  target = log( window_total / (baseline x n_window_weeks) ) = log uplift
  baseline = ref 구간 중 '행사 없는 주' 의 qty_clean 중위값. origin 시점(연도 치환)까지 관측된 ref 주만 사용
             -> 학습 연도와 예측 연도의 정보 집합을 동일하게 맞춤.
모델 = L1 분위수 선형회귀(sklearn QuantileRegressor) + 소형 LightGBM(행 수 충분할 때) 로그공간 평균 앙상블.
벤치마크 = 전년 윈도우 총량 x (올해 baseline / 전년 baseline)

표본이 3~4년이므로 통계적 확신은 제한적. 이 모델의 가치는 (1) 채널·권역 풀링, (2) 행사/한정판/취급점/사전발주의 체계적 반영,
(3) 분위수 출력. 최종 비축량에는 판단적 버퍼(SNS 유행 등)를 '분리 기록' 해서 얹을 것.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from sklearn.linear_model import QuantileRegressor
from sklearn.preprocessing import StandardScaler

from .quantile_lgbm import QuantileLGBM, qcol

log = logging.getLogger(__name__)

NUMERIC_FEATURES = ["log_baseline", "baseline_yoy", "log_uplift_lag1", "promo_window", "promo_ref", "n_limited_window",
                    "n_active_window", "active_stores_yoy", "event_weekday", "chuseok_gap_days", "suneung_gap_days",
                    "search_yoy_ref", "preorder_log_yoy", "outlook_p_above"]


def as_of(panel: pd.DataFrame, origin: pd.Timestamp) -> pd.DataFrame:
    """
    origin 시점 정보 집합으로 패널 절단: origin 이후 주의 실적(qty*)을 NaN 처리.
    행사/SKU 수/캘린더는 유지 (사전 확정 정보로 간주). 과거 origin 에 대해서는 행사 '실적' 이 '계획' 대용으로 쓰이므로
    백테스트 결과는 그만큼 낙관적일 수 있음 -> promotions.csv 에 snapshot_date 별 계획을 남기면 이 편향을 제거 가능.
    """
    p = panel.copy()
    fut = p["week_start"] > origin
    p.loc[fut, ["qty", "qty_adj", "qty_clean", "returns_qty"]] = np.nan
    return p


def window_totals(panel: pd.DataFrame, brand: str, cfg: dict) -> pd.DataFrame:
    """실적 기준 (year, channel, region) 윈도우 총량 (백테스트 정답)."""
    lo, hi = cfg["brands"][brand]["m1"]["window_iso_weeks"]
    p = panel[(panel["brand"] == brand) & (panel["iso_week"] >= lo) & (panel["iso_week"] <= hi)].copy()
    p["year"] = p["week_start"].dt.year
    g = p.groupby(["year", "channel", "region"]).agg(total=("qty_clean", "sum"), n=("qty_clean", "count")).reset_index()
    g.loc[g["n"] < (hi - lo + 1), "total"] = np.nan
    return g.drop(columns="n")


def attach_actuals(pred: pd.DataFrame, actual: pd.DataFrame) -> pd.DataFrame:
    """예측 테이블에 실적 총량 결합 (merge 는 attrs 를 잃으므로 복원)."""
    attrs = dict(pred.attrs)
    out = pred.drop(columns="total").merge(actual, on=["year", "channel", "region"], how="left")
    out.attrs.update(attrs)
    return out


def _origin_equiv(origin: pd.Timestamp, year: int) -> pd.Timestamp:
    try:
        return origin.replace(year=year)
    except ValueError:  # 2/29
        return origin.replace(year=year, day=28)


def build_m1_table(pb: pd.DataFrame, brand: str, cfg: dict, origin: pd.Timestamp,
                   preorders: pd.DataFrame | None = None) -> pd.DataFrame:
    bc = cfg["brands"][brand]
    lo, hi = bc["m1"]["window_iso_weeks"]
    rlo, rhi = bc["m1"]["ref_iso_weeks"]
    n_weeks = hi - lo + 1
    pb = pb[pb["brand"] == brand].copy()
    pb["year"] = pb["week_start"].dt.year
    years = sorted(pb["year"].unique())
    outlook = cfg.get("seasonal_outlook", {}) or {}

    rows = []
    for (ch, rg), s in pb.groupby(["channel", "region"]):
        s = s.sort_values("week_start")
        prev: dict = {}
        for y in years:
            oy = _origin_equiv(origin, y)
            sy = s[s["year"] == y]
            ref = sy[(sy["iso_week"] >= rlo) & (sy["iso_week"] <= rhi) & (sy["week_start"] + pd.Timedelta(days=6) <= oy)
                     & sy["qty_clean"].notna()]
            if len(ref) < 3:
                prev = {}
                continue
            ref_np = ref[ref["promo_intensity"] == 0]
            baseline = float((ref_np if len(ref_np) >= 4 else ref)["qty_clean"].median())
            baseline = max(baseline, 1.0)
            win = sy[(sy["iso_week"] >= lo) & (sy["iso_week"] <= hi)]
            complete = len(win) == n_weeks and win["qty_clean"].notna().all()
            total = float(win["qty_clean"].sum()) if complete else np.nan
            log_uplift = np.log(total / (baseline * n_weeks)) if complete and total > 0 else np.nan

            # 사전발주 (origin 시점까지 스냅샷), 채널 단위
            pre = np.nan
            if preorders is not None and len(preorders):
                p = preorders[(preorders["brand"] == brand) & (preorders["target_year"] == y) & (preorders["channel"] == ch)
                              & (preorders["snapshot_date"] <= oy)]
                if "region" in p.columns and p["region"].notna().any():
                    p = p[p["region"] == rg]
                pre = float(p["qty"].sum()) if len(p) else np.nan

            oy_prob = np.nan
            if bc["event_type"] == "season" and y in outlook:
                vals = [v[0] for v in outlook[y].values() if v]
                oy_prob = float(np.mean(vals)) if vals else np.nan

            sn = cfg["calendar"].get("suneung_dates", {})
            ev = pd.Timestamp(year=y, month=int(bc["event_date"].split("-")[0]), day=int(bc["event_date"].split("-")[1])) \
                if bc["event_type"] == "fixed_date" else None
            rec = dict(
                brand=brand, channel=ch, region=rg, year=y, origin=oy, n_weeks=n_weeks,
                baseline=baseline, total=total, log_uplift=log_uplift,
                log_baseline=np.log(baseline),
                baseline_yoy=np.log(baseline / prev["baseline"]) if prev else np.nan,
                log_uplift_lag1=prev.get("log_uplift", np.nan) if prev else np.nan,
                total_lag1=prev.get("total", np.nan) if prev else np.nan,
                promo_window=float(win["promo_intensity"].mean()) if len(win) else np.nan,
                promo_ref=float(ref["promo_intensity"].mean()),
                n_limited_window=float(win["n_limited_sku"].max()) if len(win) else np.nan,
                n_active_window=float(win["n_active_sku"].mean()) if len(win) else np.nan,
                active_stores_yoy=(np.log(ref["active_stores"].mean() / prev["active_stores"])
                                   if prev and prev.get("active_stores") and ref["active_stores"].notna().any() else np.nan),
                event_weekday=float(ev.weekday()) if ev is not None else np.nan,
                chuseok_gap_days=float(win["chuseok_gap_days"].iloc[0]) if len(win) and "chuseok_gap_days" in win else np.nan,
                suneung_gap_days=float((pd.Timestamp(sn[y]) - ev).days) if ev is not None and y in sn else np.nan,
                search_yoy_ref=float(np.log(ref["search_yoy"].mean())) if "search_yoy" in ref and ref["search_yoy"].notna().any() and ref["search_yoy"].mean() > 0 else np.nan,
                preorder=pre,
                preorder_log_yoy=(np.log(pre / prev["preorder"]) if prev and pre and prev.get("preorder") and prev["preorder"] > 0 else np.nan),
                outlook_p_above=oy_prob,
            )
            rows.append(rec)
            prev = dict(baseline=baseline, log_uplift=log_uplift, total=total, preorder=pre,
                        active_stores=ref["active_stores"].mean() if ref["active_stores"].notna().any() else None)
    df = pd.DataFrame(rows)
    df["benchmark"] = df["total_lag1"] * (df["baseline"] / df.groupby(["channel", "region"])["baseline"].shift(1))
    return df


def _design(df: pd.DataFrame, feats: list[str]) -> pd.DataFrame:
    X = df[feats].copy()
    X = pd.concat([X, pd.get_dummies(df["channel"], prefix="ch", dtype=float),
                   pd.get_dummies(df["region"], prefix="rg", dtype=float)], axis=1)
    return X


def fit_predict_m1(table: pd.DataFrame, target_year: int, cfg: dict, min_rows_lgbm: int = 40) -> pd.DataFrame:
    """
    시리즈별 log uplift 분위수 예측. 학습 잔차는 out-of-fold(연도 단위 leave-one-year-out) 로 계산해 attrs 에 저장
    -> 브랜드 총량 시뮬레이션에서 공통충격/고유잔차 분산으로 사용.
    """
    alphas = cfg["quantiles"]
    brand = table["brand"].iloc[0]
    l1_alpha = float(cfg["brands"][brand]["m1"].get("l1_alpha", 0.1))
    train = table[(table["year"] < target_year) & table["log_uplift"].notna()].copy()
    test = table[table["year"] == target_year].copy()
    if len(train) == 0 or len(test) == 0:
        raise ValueError(f"M1 학습/예측 행 부족: train={len(train)}, test={len(test)} (target_year={target_year})")
    feats = [c for c in NUMERIC_FEATURES if train[c].notna().mean() >= 0.5]  # 절반 이상 결측인 변수 제외
    log.info("M1 features(%d): %s", len(feats), feats)

    def _fit_predict(tr: pd.DataFrame, te: pd.DataFrame) -> np.ndarray:
        """행렬 (len(te) x len(alphas)) 로그 uplift 예측: L1 분위수 선형 (+ 소형 GBM 앙상블)."""
        Xtr, Xte = _design(tr, feats), _design(te, feats)
        Xte = Xte.reindex(columns=Xtr.columns, fill_value=0.0)
        med = Xtr.median()
        Xtr_l, Xte_l = Xtr.fillna(med).fillna(0.0), Xte.fillna(med).fillna(0.0)  # fold 내 전부 결측인 열은 0
        scaler = StandardScaler().fit(Xtr_l)
        Ztr, Zte = scaler.transform(Xtr_l), scaler.transform(Xte_l)
        lin = np.column_stack([QuantileRegressor(quantile=a, alpha=l1_alpha, solver="highs")
                               .fit(Ztr, tr["log_uplift"]).predict(Zte) for a in alphas])
        if len(tr) >= min_rows_lgbm:
            p = {**cfg["lgbm"], "mode": "quantile", "n_estimators": 300, "num_leaves": 4,
                 "min_data_in_leaf": max(5, len(tr) // 10)}
            gq = QuantileLGBM(alphas, p, categorical=[]).fit(Xtr, tr["log_uplift"]).predict(Xte)
            lin = 0.5 * (lin + gq.values)
        return np.sort(lin, axis=1)  # 분위수 교차 방지

    pred_log = _fit_predict(train, test)
    out = test[["brand", "channel", "region", "year", "baseline", "n_weeks", "total", "benchmark", "preorder"]].copy()
    for j, a in enumerate(alphas):
        out[f"log_uplift_{qcol(a)}"] = pred_log[:, j]
        out[qcol(a)] = out["baseline"] * out["n_weeks"] * np.exp(pred_log[:, j])

    # out-of-fold 잔차 (연도 단위 LOYO). 학습 연도가 2개 이상일 때만 가능; 아니면 in-sample 잔차 (낙관적) 사용
    years = sorted(train["year"].unique())
    j50 = alphas.index(0.5) if 0.5 in alphas else 0
    resid = np.full(len(train), np.nan)
    if len(years) >= 2:
        for y in years:
            m = (train["year"] == y).values
            resid[m] = train.loc[m, "log_uplift"].values - _fit_predict(train[~m], train[m])[:, j50]
        resid_kind = "oof_loyo"
    else:
        resid = train["log_uplift"].values - _fit_predict(train, train)[:, j50]
        resid_kind = "in_sample"
    out.attrs.update(train_resid=resid, train_years=train["year"].values, features=feats, resid_kind=resid_kind)
    return out


def event_shock_sd_from_search(search: pd.DataFrame | None, brand: str, cfg: dict) -> float | None:
    """
    네이버 검색 트렌드(2016~) 로 '이벤트 강도의 연도 간 변동성' 프록시 추정.
    연도별 log(윈도우 평균 지수 / ref 평균 지수) 의 연차 변화 표준편차 / sqrt(2) = 공통 충격 sd 추정치.
    판매 데이터 3년으로는 추정 불가한 값을 10년 검색 데이터로 보완하는 용도.
    """
    if search is None or len(search) == 0 or brand not in set(search["group"]):
        return None
    bc = cfg["brands"][brand]["m1"]
    s = search[search["group"] == brand].copy()
    s["year"] = s["period"].dt.year
    s["iso_week"] = s["period"].dt.isocalendar().week.astype(int)
    lo, hi = bc["window_iso_weeks"]
    rlo, rhi = bc["ref_iso_weeks"]
    rows = []
    for y, g in s.groupby("year"):
        w = g[(g["iso_week"] >= lo) & (g["iso_week"] <= hi)]["ratio"]
        r = g[(g["iso_week"] >= rlo) & (g["iso_week"] <= rhi)]["ratio"]
        if len(w) >= (hi - lo) and len(r) >= 3 and r.mean() > 0:
            rows.append(np.log(w.mean() / r.mean()))
    if len(rows) < 4:
        return None
    return float(np.std(np.diff(rows), ddof=1) / np.sqrt(2))


def aggregate_m1_to_brand(pred: pd.DataFrame, alphas: list[float], method: str = "simulate",
                          common_shock_sd: float | None = None, common_sd_floor: float = 0.10,
                          n_sims: int = 20000, seed: int = 42) -> pd.DataFrame:
    """
    시리즈 예측 -> 브랜드 총량 분위수.
    sum_quantiles : 시리즈 분위수 단순 합 (완전 양의 상관 가정 = 보수적 상한, 분포 정보 없음)
    simulate      : total = sum_s scale_s * exp(med_s + common + idio_s)
                    common ~ N(0, sd_common), sd_common = max(floor, OOF 연도평균잔차 sd, 검색기반 추정치)
                    idio_s ~ OOF 잔차(연도평균 제거) 재표집
                    -> 연도 공통 충격(유행·경기·날씨)이 총량 리스크의 핵심이며 3년 데이터로는 추정 불가하므로 floor 로 하한을 둠.
    """
    rows = {"brand": pred["brand"].iloc[0], "year": int(pred["year"].iloc[0]), "n_series": len(pred),
            "benchmark": float(pred["benchmark"].sum()) if pred["benchmark"].notna().any() else np.nan,
            "actual": float(pred["total"].sum()) if pred["total"].notna().all() else np.nan,
            "point_sum_q50": float(pred[qcol(0.5)].sum())}
    if method == "sum_quantiles":
        for a in alphas:
            rows[qcol(a)] = float(pred[qcol(a)].sum())
        return pd.DataFrame([rows])

    rng = np.random.default_rng(seed)
    resid, years = pred.attrs["train_resid"], pred.attrs["train_years"]
    ok = np.isfinite(resid)
    resid, years = resid[ok], years[ok]
    ym = pd.Series(resid).groupby(years).mean()
    sd_year = float(ym.std(ddof=1)) if len(ym) >= 2 else 0.0
    idio = resid - ym.reindex(years).values
    sd_common = max(common_sd_floor, sd_year, common_shock_sd or 0.0)
    scale = (pred["baseline"] * pred["n_weeks"]).values
    med = pred[f"log_uplift_{qcol(0.5)}"].values
    common = rng.normal(0.0, sd_common, n_sims)
    e = rng.choice(idio, size=(n_sims, len(pred)), replace=True) if len(idio) else np.zeros((n_sims, len(pred)))
    tot = (scale * np.exp(med + e + common[:, None])).sum(axis=1)
    for a in alphas:
        rows[qcol(a)] = float(np.quantile(tot, a))
    rows.update(sd_common_used=sd_common, sd_common_oof_years=sd_year, sd_idio=float(np.std(idio)) if len(idio) else np.nan,
                resid_kind=pred.attrs.get("resid_kind"))
    return pd.DataFrame([rows])
