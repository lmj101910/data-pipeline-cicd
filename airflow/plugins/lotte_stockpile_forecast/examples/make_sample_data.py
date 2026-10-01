"""
합성 샘플 데이터 생성기 - 스키마 예시 + 파이프라인 end-to-end 실행용.

실제 패턴을 흉내냄: 빼빼로 10월 sell-in 램프(채널별 배수 상이) / 연도별 공통 충격 / 한정판 / 행사 / 2024년 편의점 결품(검열),
팥빙수 기온 비선형 반응 + 장마 / 방학, 월말 밀어내기, 요일 효과, 성장률.  실데이터가 준비되면 이 파일은 필요 없음.

python -m examples.make_sample_data  [--end 2026-09-13]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW, EXT = ROOT / "data/raw", ROOT / "data/external"

REGIONS = {"수도권": 0.45, "영남": 0.22, "호남": 0.13, "충청": 0.12, "강원": 0.08}
CHANNELS = {"편의점": 0.40, "대형마트": 0.25, "도매대리점": 0.20, "온라인": 0.15}
REGION_TEMP = {"수도권": 17.0, "충청": 17.5, "호남": 18.5, "영남": 19.0, "강원": 15.5}
YEAR_ANOM = {2023: 0.5, 2024: 1.8, 2025: 1.2, 2026: 0.8}
PEPERO_EVENT_SHOCK = {2023: 1.00, 2024: 1.15, 2025: 0.95, 2026: 1.05}  # 연도 공통 충격 (모델은 2026 값을 모름)
PEPERO_CH_PEAK = {"편의점": 9.0, "대형마트": 5.0, "온라인": 6.0, "도매대리점": 4.0}
WEEKDAY = np.array([1.10, 1.05, 1.00, 1.00, 1.10, 0.50, 0.25])

LUNAR = {  # (name, dates)
    "설날": {2023: ["01-21", "01-22", "01-23", "01-24"], 2024: ["02-09", "02-10", "02-11", "02-12"],
           2025: ["01-28", "01-29", "01-30"], 2026: ["02-16", "02-17", "02-18"], 2027: ["02-06", "02-07", "02-08"]},
    "추석": {2023: ["09-28", "09-29", "09-30"], 2024: ["09-16", "09-17", "09-18"],
           2025: ["10-05", "10-06", "10-07"], 2026: ["09-24", "09-25", "09-26"], 2027: ["09-14", "09-15", "09-16"]},
    "부처님오신날": {2023: ["05-27"], 2024: ["05-15"], 2025: ["05-05"], 2026: ["05-24"], 2027: ["05-13"]},
}
FIXED = {"01-01": "1월1일", "03-01": "삼일절", "05-05": "어린이날", "06-06": "현충일", "08-15": "광복절",
         "10-03": "개천절", "10-09": "한글날", "12-25": "기독탄신일"}


def make_holidays(years) -> pd.DataFrame:
    rows = []
    for y in years:
        for mmdd, n in FIXED.items():
            rows.append((f"{y}-{mmdd}", n, 1))
        for n, d in LUNAR.items():
            for mmdd in d.get(y, []):
                rows.append((f"{y}-{mmdd}", n, 1))
    return pd.DataFrame(rows, columns=["date", "name", "is_holiday"]).sort_values("date")


def make_weather(dates: pd.DatetimeIndex, rng) -> pd.DataFrame:
    frames = []
    doy = dates.dayofyear.values
    for r, base in REGION_TEMP.items():
        season = base + 13.0 * np.sin(2 * np.pi * (doy - 110) / 365.25)
        noise = np.zeros(len(dates))
        for i in range(1, len(dates)):
            noise[i] = 0.7 * noise[i - 1] + rng.normal(0, 2.2)
        anom = np.array([YEAR_ANOM.get(y, 0.5) for y in dates.year])
        tmax = season + noise + anom
        monsoon = ((dates.month == 6) & (dates.day >= 25)) | ((dates.month == 7) & (dates.day <= 25))
        p_rain = np.where(monsoon, 0.55, np.where(dates.month.isin([6, 7, 8, 9]), 0.28, 0.15))
        rain = np.where(rng.random(len(dates)) < p_rain, rng.exponential(12, len(dates)), 0.0)
        tmax = tmax - 2.5 * (rain > 1)
        tmin = tmax - 8 - rng.normal(0, 1, len(dates))
        hum = 60 + 20 * monsoon + rng.normal(0, 6, len(dates))
        frames.append(pd.DataFrame({"date": dates, "region": r, "tavg": (tmax + tmin) / 2, "tmax": tmax, "tmin": tmin,
                                    "rain_mm": rain.round(1), "humidity": hum.clip(20, 100)}))
    return pd.concat(frames, ignore_index=True)


def pepero_multiplier(dates: pd.DatetimeIndex, channel: str, n_limited: np.ndarray) -> np.ndarray:
    d1111 = pd.to_datetime([f"{y}-11-11" for y in dates.year])
    dte = (d1111 - dates).days.values  # days to event
    shock = np.array([PEPERO_EVENT_SHOCK.get(y, 1.0) for y in dates.year])
    peak = PEPERO_CH_PEAK[channel]
    ramp = 1 + (peak - 1) * np.exp(-0.5 * ((dte - 14) / 11.0) ** 2) * (dte > -3)   # sell-in 피크 ~ 10/28
    ramp *= np.where(dte >= 0, shock, 1.0) ** (ramp > 1.3)
    ramp *= 1 + 0.05 * n_limited * (ramp > 1.3)
    # 발렌타인/화이트데이 소형 범프
    for mmdd in ("02-10", "03-10"):
        dd = (pd.to_datetime([f"{y}-{mmdd}" for y in dates.year]) - dates).days.values
        ramp *= 1 + 0.5 * np.exp(-0.5 * (dd / 4.0) ** 2)
    return ramp


def main(end: str) -> None:
    rng = np.random.default_rng(7)
    RAW.mkdir(parents=True, exist_ok=True)
    EXT.mkdir(parents=True, exist_ok=True)
    dates = pd.date_range("2023-01-01", end, freq="D")
    years = sorted(set(dates.year))
    weather = make_weather(dates, rng)
    weather.to_csv(EXT / "weather_daily.csv", index=False)
    make_holidays(years + [years[-1] + 1]).to_csv(EXT / "holidays.csv", index=False)

    # ---- SKU 마스터 ----
    sku = [("PP001", "빼빼로", "빼빼로 오리지널", "2010-01-01", None, 0), ("PP002", "빼빼로", "빼빼로 아몬드", "2010-01-01", None, 0),
           ("PP003", "빼빼로", "빼빼로 초코필드", "2015-01-01", None, 0), ("PP004", "빼빼로", "빼빼로 화이트쿠키", "2016-01-01", None, 0),
           ("PP005", "빼빼로", "빼빼로 누드", "2012-01-01", None, 0), ("PP006", "빼빼로", "빼빼로 크런키", "2020-01-01", None, 0),
           ("PB001", "팥빙수", "팥빙수 컵", "2005-01-01", None, 0), ("PB002", "팥빙수", "팥빙수 미니", "2018-01-01", None, 0),
           ("PB003", "팥빙수", "인절미빙수", "2021-01-01", None, 0)]
    n_lim = {2023: 2, 2024: 3, 2025: 2, 2026: 3}
    for y in years:
        for i in range(n_lim[y]):
            sku.append((f"PPL{y}{i}", "빼빼로", f"빼빼로 {y} 한정판 {i + 1}", f"{y}-09-01", f"{y}-12-15", 1))
        sku.append((f"PBL{y}", "팥빙수", f"망고빙수 {y}", f"{y}-05-01", f"{y}-09-30", 1))
    sku_master = pd.DataFrame(sku, columns=["sku_code", "brand", "sku_name", "launch_date", "discontinue_date", "is_limited_edition"])
    sku_master.to_csv(RAW / "sku_master.csv", index=False)

    # ---- 행사 (actual: 과거, plan: 미래) ----
    promo = []
    last = dates[-1]
    for y in years + [years[-1] + 1]:
        for s, e, ch, b, t, dr in [(f"{y}-10-20", f"{y}-11-11", "편의점", "빼빼로", "2+1", 0.33),
                                   (f"{y}-11-01", f"{y}-11-11", "대형마트", "빼빼로", "할인", 0.2),
                                   (f"{y}-02-05", f"{y}-02-14", "편의점", "빼빼로", "1+1", 0.5),
                                   (f"{y}-07-07", f"{y}-07-20", "편의점", "팥빙수", "1+1", 0.5),
                                   (f"{y}-06-10", f"{y}-06-23", "대형마트", "팥빙수", "할인", 0.2)]:
            if pd.Timestamp(s) > last + pd.Timedelta(days=120):
                continue
            promo.append((s, e, ch, b, t, "actual" if pd.Timestamp(e) <= last else "plan", dr, f"{y}-01-15"))
    promotions = pd.DataFrame(promo, columns=["start_date", "end_date", "channel", "brand", "promo_type", "status", "discount_rate", "snapshot_date"])
    promotions.to_csv(RAW / "promotions.csv", index=False)

    # 일별 (brand, channel) 행사 강도
    pi = {}
    for b in ("빼빼로", "팥빙수"):
        for ch in CHANNELS:
            v = np.zeros(len(dates))
            for r in promotions[(promotions.brand == b) & (promotions.channel == ch)].itertuples():
                m = (dates >= pd.Timestamp(r.start_date)) & (dates <= pd.Timestamp(r.end_date))
                v[m] = 1 + r.discount_rate
            pi[(b, ch)] = v

    # ---- 브랜드 일별 수요 -> 시리즈 -> SKU ----
    doy_growth = ((dates - dates[0]).days.values / 365.25)
    me = np.where(dates.day >= 28, 1.3, np.where(dates.day <= 3, 0.8, 1.0))  # 월말 밀어내기
    wd = WEEKDAY[dates.weekday.values]
    n_limited = np.array([n_lim.get(y, 0) if (m >= 9 and not (m == 12 and d > 15)) else 0
                          for y, m, d in zip(dates.year, dates.month, dates.day)])
    vac = ((dates.month == 7) & (dates.day >= 20)) | ((dates.month == 8) & (dates.day <= 20))
    seoul = weather[weather.region == "수도권"].set_index("date")
    tmax_fwd = seoul["tmax"].rolling(7, min_periods=1).mean().shift(-6).bfill().ffill().values  # 채널 선행 발주
    rain_pen = np.where(seoul["rain_mm"].values > 1, 0.85, 1.0)

    ship_rows, stockout_rows, dist_rows = [], [], []
    for b, base in (("빼빼로", 20000.0), ("팥빙수", 3000.0)):
        skus = sku_master[sku_master.brand == b]
        launch = pd.to_datetime(skus.launch_date).values
        disc = pd.to_datetime(skus.discontinue_date).fillna(pd.Timestamp("2099-12-31")).values
        active_by_day = []
        for d in dates:
            m = (launch <= d.to_datetime64()) & (disc >= d.to_datetime64())
            codes = skus.sku_code.values[m]
            w = np.where(skus.is_limited_edition.values[m] == 1, 0.6, 1.0)
            active_by_day.append((codes, w / w.sum()))
        for ch, cw in CHANNELS.items():
            for rg, rw in REGIONS.items():
                if b == "빼빼로":
                    mult = pepero_multiplier(dates, ch, n_limited) * (1.06 ** doy_growth)
                else:
                    mult = np.exp(0.085 * np.clip(tmax_fwd - 18, 0, None)) * rain_pen * (1 + 0.15 * vac) * (1.04 ** doy_growth)
                promo_eff = 1 + 0.4 * pi[(b, ch)]
                series_noise = np.exp(rng.normal(0, 0.12, len(dates)))
                demand = base * cw * rw * mult * promo_eff * wd * me * series_noise
                shipped = demand.copy()
                # 2024년 편의점 결품 (10/24~11/01): 출고 캡 -> 검열
                cap = (dates.year == 2024) & (dates >= "2024-10-24") & (dates <= "2024-11-01") & (ch == "편의점") & (b == "빼빼로")
                shipped[cap] = demand[cap] * 0.62
                for d_i, d in enumerate(dates):
                    codes, w = active_by_day[d_i]
                    q = np.round(shipped[d_i] * w * np.exp(rng.normal(0, 0.05, len(w))))
                    dd = d.date()
                    for code, qq in zip(codes, q):
                        if qq > 0:
                            ship_rows.append((dd, code, ch, rg, qq, np.round(qq * 0.01 * rng.random())))
                    if cap[d_i]:
                        for code in codes[: len(codes) // 2]:
                            stockout_rows.append((dd, code, ch, rg, 1))
                # 취급점
                for w0 in pd.date_range(dates[0] - pd.Timedelta(days=dates[0].weekday()), dates[-1], freq="7D"):
                    n = 1000 * cw * rw * (1.03 ** ((w0 - dates[0]).days / 365.25))
                    if b == "빼빼로" and w0.month in (10, 11):
                        n *= 1.25
                    dist_rows.append((w0.date(), b, ch, rg, round(n)))

    shipments = pd.DataFrame(ship_rows, columns=["date", "sku_code", "channel", "region", "qty", "returns_qty"])
    shipments.to_csv(RAW / "shipments.csv", index=False)
    pd.DataFrame(stockout_rows, columns=["date", "sku_code", "channel", "region", "stockout_flag"]).to_csv(RAW / "stockouts.csv", index=False)
    pd.DataFrame(dist_rows, columns=["week_start", "brand", "channel", "region", "active_stores"]).to_csv(RAW / "distribution.csv", index=False)

    # ---- 사전발주 (빼빼로): 윈도우 실적과 상관, 스냅샷 누적 ----
    ship = shipments.merge(sku_master[["sku_code", "brand"]], on="sku_code")
    ship["date"] = pd.to_datetime(ship["date"])
    pre = []
    for y in years:
        win = ship[(ship.brand == "빼빼로") & (ship.date >= f"{y}-10-01") & (ship.date <= f"{y}-11-15")]
        tot_ch = win.groupby("channel")["qty"].sum()
        for snap, share in ((f"{y}-09-05", 0.3), (f"{y}-09-20", 0.6), (f"{y}-10-05", 0.85)):
            if pd.Timestamp(snap) > last:
                continue
            for ch in ("편의점", "대형마트"):
                base_tot = tot_ch.get(ch, 0) if len(win) else 0
                if y == years[-1] and len(win) < 20:   # 마지막 연도는 실적 미확정 -> 전년 x 성장 x 충격 프록시
                    prev = ship[(ship.brand == "빼빼로") & (ship.date >= f"{y - 1}-10-01") & (ship.date <= f"{y - 1}-11-15") & (ship.channel == ch)]["qty"].sum()
                    base_tot = prev * 1.06 * PEPERO_EVENT_SHOCK.get(y, 1.0) / PEPERO_EVENT_SHOCK.get(y - 1, 1.0)
                pre.append((snap, y, "빼빼로", ch, round(base_tot * share * np.exp(rng.normal(0, 0.05)))))
    pd.DataFrame(pre, columns=["snapshot_date", "target_year", "brand", "channel", "qty"]).to_csv(RAW / "preorders.csv", index=False)

    # ---- 유동인구 (주간, 권역) ----
    wk = pd.date_range("2023-01-02", last + pd.Timedelta(days=7), freq="7D")
    ft = [(w.date(), r, round(100 + 8 * np.sin(2 * np.pi * (w.dayofyear - 100) / 365.25) + rng.normal(0, 3), 1), int(w > last))
          for w in wk for r in REGIONS]
    pd.DataFrame(ft, columns=["week_start", "region", "index", "is_forecast"]).to_csv(RAW / "foot_traffic.csv", index=False)

    # ---- 검색 트렌드 (2016~, 주간, 그룹=브랜드명) ----
    wk16 = pd.date_range("2016-01-04", last, freq="7D")
    d1111 = pd.to_datetime([f"{y}-11-11" for y in wk16.year])
    dte = (d1111 - wk16).days.values
    pp = 4 + 96 * np.exp(-0.5 * ((dte - 4) / 9.0) ** 2) * np.array([PEPERO_EVENT_SHOCK.get(y, 1.0) for y in wk16.year])
    pb = 8 + 70 * np.clip(np.sin(2 * np.pi * (wk16.dayofyear - 110) / 365.25), 0, None) ** 2
    pp, pb = pp * np.exp(rng.normal(0, 0.08, len(wk16))), pb * np.exp(rng.normal(0, 0.1, len(wk16)))
    scale = max(pp.max(), pb.max()) / 100
    st = pd.concat([pd.DataFrame({"period": wk16, "group": "빼빼로", "ratio": (pp / scale).round(2)}),
                    pd.DataFrame({"period": wk16, "group": "팥빙수", "ratio": (pb / scale).round(2)})])
    st.to_csv(EXT / "search_trend.csv", index=False)

    print(f"shipments {len(shipments):,} rows, stockouts {len(stockout_rows):,}, promotions {len(promotions)}, "
          f"preorders {len(pre)}, weather {len(weather):,}  ->  {RAW}, {EXT}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", default="2026-09-13", help="마지막 출고 일자 (일요일 권장)")
    main(ap.parse_args().end)
