# 롯데웰푸드 비축재고 예측 (빼빼로 · 팥빙수)

브랜드 단위 판매(출고)량을 예측해 **이벤트/시즌 비축량**을 결정하는 파이프라인.
부족(결품)이 과잉보다 비용이 크다는 비즈니스 조건을 **분위수 예측 + 뉴스벤더**로 모델에 내재화했다.
CPU 전용(LightGBM), 데이터 2023년~, 외부데이터는 무료 API 만 사용(크롤링 없음).

```
lotte_stockpile_forecast/
├── README.md                     ← 이 문서
├── config.yaml                   브랜드별 윈도우/베이스라인 주차, 권역-기상청 코드, 분위수, 뉴스벤더 비용, LightGBM 설정
├── requirements.txt / .env.example
├── src/
│   ├── schema.py                 입력 데이터 계약(컬럼/타입) + 검증 로더
│   ├── common.py                 설정/.env/주 단위 헬퍼
│   ├── external/                 외부데이터 수집 (§3)
│   ├── features/                 주간 패널, 캘린더, 날씨, 외부지표, 데이터품질, 조립
│   ├── models/                   quantile_lgbm.py, m1_event_total.py, m2_weekly.py
│   ├── decision/newsvendor.py    τ → 비축량, 누적 곡선
│   ├── backtest.py               M2 롤링 오리진 / M1 leave-last-year-out
│   ├── jobs.py                   CLI·DAG 공용 작업 함수
│   └── pipeline.py               CLI
├── airflow/
│   ├── dags/                     Airflow 3.1.7 DAG 8개 (§4)
│   ├── setup_airflow_objects.sh  Connection/Variable 등록 스크립트
│   └── requirements-airflow.txt
├── examples/make_sample_data.py  합성 데이터 생성기 (스키마 예시 + 실행 테스트)
└── data/{raw,external,processed}, outputs/
```

---

## 1. 설계 전제

| 전제 | 설계 반영 |
|---|---|
| 이벤트 표본이 3~4개(23~25년)뿐 → 이벤트 배수를 ML 로 직접 학습 불가 | 베이스라인(ML 이 잘함) × 이벤트 uplift(연×채널×권역 **풀링**) 로 분해. uplift 의 연도 공통충격 분산은 판매데이터로 추정 불가하므로 **하한값 + 네이버 검색 10년치 변동성**으로 보완 |
| 부족 > 과잉 리스크 | 분위수 목적함수(0.5/0.7/0.8/0.9) + 뉴스벤더 τ = Cu/(Cu+Co). MAPE 는 참고 지표로만 |
| 비축 결정(2~3개월 전)과 주간 운영은 정보 집합이 다름 | **M1 전략**(누적 총량, 연 1~2회) / **M2 전술**(주간 1~4주) 분리. 28일 예보·유동인구는 M2 에만 |
| 데이터 품질 낮음, 마케팅 데이터 없음 | 주간 집계, 결품 검열 보정, 월말 밀어내기 플래그, 이상치 플래그(자동 캡핑 X). 마케팅 변수 미사용 |
| GPU 없음 | LightGBM / sklearn QuantileRegressor. 전체 학습·예측 수 초~수십 초 |

---

## 2. 모델 명세

### 2.1 문제 정의

| | 빼빼로 | 팥빙수 |
|---|---|---|
| 이벤트 | 11/11 (sell-in 피크는 10월 하순) | 여름 시즌 |
| M1 윈도우(ISO 주) | 40~46 (`window_iso_weeks`) | 23~35 |
| M1 베이스라인 구간 | 27~35, 행사 없는 주 중위값 | 10~17 |
| M1 예측 시점 | 8/31 (1차 생산계획), 9/30 (2차 사전발주 반영) | 4/15 |
| M2 | 10월~ 주간 1~4주 | 5~9월 주간 1~4주 |

* 예측 단위: **주간(월요일 시작)**, 시리즈 = 브랜드 × 채널(4) × 권역(5). 브랜드 값은 시리즈 합.
* 타깃은 출고(sell-in) 수량. 금액 사용 금지(가격 변동 왜곡).

### 2.2 입력 데이터 계약 (`src/schema.py`, `python -m src.pipeline schema`)

| 파일 | 필수 컬럼 | 비고 |
|---|---|---|
| `shipments.csv` **필수** | date, sku_code, channel, region, qty | returns_qty 선택 |
| `sku_master.csv` **필수** | sku_code, brand, sku_name, launch_date | discontinue_date, is_limited_edition(한정판) |
| `promotions.csv` 권장 | start_date, end_date, channel, brand, promo_type, status(actual/plan) | discount_rate, snapshot_date. channel='ALL' 허용 |
| `stockouts.csv` 권장 | date, sku_code, channel, region, stockout_flag | 없으면 결품 검열 보정 불가 → 과소예측 위험 |
| `distribution.csv` 선택 | week_start, brand, channel, region, active_stores | 취급점 수 |
| `preorders.csv` 선택 | snapshot_date, target_year, brand, channel, qty | 빼빼로 M1 2차의 핵심 변수 |
| `foot_traffic.csv` 선택 | week_start, region, index | 기존 수집분. is_forecast=1 로 차주 예측치 적재 |
| 외부: `weather_daily.csv`, `weather_forecast.csv`, `holidays.csv`, `school_schedule.csv`, `search_trend.csv`, `oni.csv`, `consumer_sentiment.csv` | §3 수집기가 생성 | 기존 28일 예보는 `weather_forecast` 스키마(issue_date, target_date, region, tmax, tmin, pop_max, rain_mm, source)로 적재 |

### 2.3 변수 명세

**내부 (주간 패널, `features/panel.py`)**

| 변수 | 정의 |
|---|---|
| `qty`, `returns_qty`, `n_sku_sold`, `imputed_zero` | 주간 합계. 미관측 주는 0 + 플래그 |
| `stockout_share`, `qty_adj` | 결품 SKU-일 / (활성 SKU 수 × 7). `qty_adj = qty / (1 − min(share, 0.6))` (검열 보정) |
| `qty_clean` | qty_adj 에 이상치 처리 적용(기본 플래그만, `cap_outliers` 로 캡핑 가능) |
| `promo_intensity`, `promo_is_plan` | 행사일 비율 × (1 + 할인율). actual 우선, 미래는 plan |
| `n_active_sku`, `n_limited_sku` | 출시/단종일 기준 활성 SKU 수, 한정판 수(미래 주도 계산 가능) |
| `active_stores` | 취급점 수(ffill) |
| `month_end_week` | 월말 포함 주(밀어내기) |

**캘린더 (`features/calendar.py`)**: `iso_week, month, week_of_month, n_holidays_wd(주중 공휴일 수), vacation_share(여름방학 비율), in_window, weeks_since_window_start`
+ 빼빼로 전용: `days_to_event, weeks_to_event, event_weekday(11/11 요일), chuseok_gap_days(추석~11/11), days_to_suneung`

**날씨 (`features/weather.py`, 권역×주)**: `tmax_mean, tmax_max, tmin_mean, days_tmax_ge_28/30/33, tropical_nights, rain_days, rain_mm_sum, humidity_mean` + lag1/lag2(sell-out→sell-in 시차). 예보 범위 밖은 ISO 주 평년값으로 채우고 커버 비율로 혼합.

**외부지표 (`features/external.py`)**: `search_ratio, search_ratio_4w, search_yoy`(네이버, 브랜드 그룹), `foot_traffic`, `csi`(1개월 lag)

### 2.4 M1 전략 모델 (`models/m1_event_total.py`)

```
행     : (연도 y, 채널, 권역)  →  3년 × 20 시리즈 = 60행 (연도가 쌓일수록 증가)
타깃   : log_uplift = log( 윈도우 누적 qty_clean / (baseline × 윈도우 주수) )
baseline: ref 구간 중 promo_intensity==0 인 주의 qty_clean 중위값 (4주 미만이면 전체), 
          연도별 origin 동일 시점(월-일 치환)까지 관측된 주만 사용 → 학습/예측 정보집합 동일
```

| 피처 | 설명 |
|---|---|
| `log_baseline`, `baseline_yoy` | 현재 런레이트, 전년 대비 |
| `log_uplift_lag1` | 전년 동일 시리즈 uplift |
| `promo_window`, `promo_ref` | 윈도우 행사 강도(예측연도는 plan), ref 구간 행사 강도 |
| `n_limited_window`, `n_active_window` | 한정판 수, 활성 SKU 수 |
| `active_stores_yoy` | 취급점 YoY |
| `event_weekday`, `chuseok_gap_days`, `suneung_gap_days` | 빼빼로 캘린더 |
| `search_yoy_ref` | ref 구간 검색량 YoY |
| `preorder_log_yoy` | 사전발주 YoY (origin 이전 스냅샷) |
| `outlook_p_above` | 팥빙수: 기상청 3개월 전망 '평년보다 높음' 확률 (config 수동 입력) |
| 채널/권역 one-hot | |

* 절반 이상 결측인 피처는 자동 제외. 결측은 학습 중위값 대체.
* **모델**: `sklearn.QuantileRegressor(L1 α=0.1, solver=highs)` alpha 별 + 행 ≥40 이면 소형 LightGBM quantile(300 트리, num_leaves 4) 로그공간 평균. 분위수 정렬로 교차 방지.
* **잔차**: leave-one-year-out OOF 잔차(학습 연도 ≥2) → 연도평균(공통) / 나머지(고유) 분리.
* **브랜드 총량 집계** (`aggregation: simulate`):
  `total = Σ_s baseline_s·n_weeks·exp(med_s + common + idio_s)`, `common ~ N(0, sd_common)`,
  `sd_common = max(common_shock_sd_floor[0.12/0.15], OOF 연도평균잔차 sd, 검색트렌드 기반 추정치)`, idio 는 OOF 잔차 재표집, 20,000회.
  대안 `sum_quantiles`(시리즈 분위수 단순합 = 완전상관 상한).
* **검색트렌드 기반 sd**: 2016~ 연도별 log(윈도우 평균 검색지수 / ref 평균) 의 연차 변화 sd / √2.
* **벤치마크**: 전년 윈도우 총량 × (올해 baseline / 전년 baseline). 이걸 못 이기면 채택 금지.
* 산출: 시리즈별 분위수, 브랜드 총량 분위수, `sd_common_used`, 누적 곡선(과거 윈도우 주차별 비중 평균 × Q_τ).

### 2.5 M2 전술 모델 (`models/m2_weekly.py`)

```
행     : (시리즈, origin 주 t), horizon h ∈ {1,2,3,4} 별 별도 모델 (direct multi-horizon)
타깃   : log1p(qty_clean[t+h])  → 분위수 불변성으로 expm1 역변환
```

| 피처 그룹 | 변수 | 시점 |
|---|---|---|
| 판매 동학 | `lag0~3`, `roll_mean_4/8/13`, `roll_std_8`, `yoy_same_week`(t+h−52), `yoy_momentum`, `returns_share`, `stockout_share` | t |
| 외부(원점) | `search_ratio, search_ratio_4w, search_yoy, csi`, 실측 날씨 lag1/lag2 | t |
| 대상주 사전확정 | `promo_intensity(plan), n_active_sku, n_limited_sku, month_end_week, iso_week, month, week_of_month, n_holidays_wd, vacation_share, in_window, weeks_since_window_start, weeks_to_event, event_weekday, chuseok_gap_days, days_to_suneung` | t+h |
| 대상주 날씨(팥빙수) | 학습: 실측 / 추론: origin 이전 발표 예보 + 평년값 혼합 | t+h |
| 유동인구 | `foot_traffic` (h=1 만, 차주 예측치) | t+1 |
| 범주형 | `channel, region` | |

* **LightGBM** (`config.lgbm`): num_leaves 6, min_data_in_leaf 30, lr 0.03, n_estimators 800, feature/bagging fraction 0.7/0.8, λ₂ 5, n_jobs 4.
  최근 26주 검증으로 early stopping → best_iteration 으로 전체 재학습. `in_window_weight: 2.0` (시즌 주 가중).
* **두 모드** (`mode`): `quantile` = alpha 별 quantile objective (LightGBM 은 quantile 에 단조 제약 불가) /
  `conformal` = 단조 제약 L2 중심모델 + K-fold OOF 잔차의 그룹(in_window)별 경험 분위수(split-conformal).
  빼빼로 기본 quantile, 팥빙수 기본 conformal(기온 +, 강수 −, 행사 + 제약). 백테스트로 브랜드별 확정.
* 브랜드 합계 = 시리즈 분위수 합(보수적 상한). 벤치마크 = 전년 동주 × (최근 4주 / 전년 동기 4주).
* **train/serve skew 주의**: 학습은 대상주 실측 날씨. `weather_forecast.csv` 아카이브가 쌓이면 학습도 예보 기반으로 전환할 것.

### 2.6 의사결정 (`decision/newsvendor.py`)

```
τ = Cu / (Cu + Co)                     Cu: 결품 단위비용(마진+기회손실), Co: 과잉 단위비용(보관+할인/폐기)  ← 사업부 확정 필요
Q_τ = 분위수 선형보간 (학습 분위수 최대 초과 시 클립 + 경고 → quantiles 에 alpha 추가)
비축량 = max(0, Q_τ − 윈도우 시작 시점 예상재고) ;  판단적 버퍼(SNS 유행 등)는 비율로 '분리 기록'
누적 곡선: 과거 윈도우 주차 비중 평균 × Q_τ  → 생산/입고 계획이 곡선 위에 있어야 함
```

### 2.7 검증 (`backtest.py`)

| 대상 | 방법 | 지표 |
|---|---|---|
| M2 | 롤링 오리진(step 주), 매 origin 재학습, 브랜드 합계 | pinball(α), coverage(α)= P(실적 ≤ 예측), bias_q50, MAPE(참고), 벤치마크 대비 |
| M1 | leave-last-year-out: y−1 까지 학습 → y 예측 (연도 3개면 테스트 2개) | 총량 오차 %, 분위수별 커버 여부, 벤치마크 대비 |

합성 데이터(examples) 결과 요약 — 방향성 확인용이며 실데이터 성능이 아님:
* 빼빼로 M1: 2024(학습 1년) −21%, q90 도 미커버 → 첫 1~2년은 구간 신뢰 불가, 버퍼 필수. 2025 +13%(벤치마크 +23%).
* 팥빙수 M1: q50 오차 −1~−10%, q80 이 3개 연도 모두 커버.
* M2 h=1~4: 벤치마크 대비 pinball 개선, q80 coverage 0.6~1.0 (origin 5~6개라 잡음 큼).

### 2.8 알려진 한계

1. 연도 표본 부족: 공통충격 sd 는 판단(하한값)에 의존. 매년 실현 coverage 를 기록해 하한값을 보정.
2. 행사 '계획' 스냅샷이 없으면 과거 origin 백테스트는 실적을 계획 대용으로 써 낙관적.
3. 23~25년 여름이 모두 폭염 → 평년 여름 데이터 부재(과잉 방향 오차는 허용, 기록 경신은 과소 위험).
4. SNS 유행(두바이초콜릿류) 등은 무료 API 로 포착 불가 → 판단적 버퍼 항목.

---

## 3. Python 외부데이터 수집 프로그램 (`src/external/`)

공통: `base.Http`(재시도/백오프, data.go.kr XML 오류 감지), 키는 `.env` 또는 환경변수. CLI `python -m src.pipeline fetch-external [--sources weather,forecast,holidays,search,neis,oni,ecos]`.

| 모듈 / 함수 | 데이터 | 엔드포인트 | 필요한 키 (발급처) | 제약·주의사항 | 출력 |
|---|---|---|---|---|---|
| `kma_asos.fetch_weather_daily_for_regions` | 기상청 지상(종관, ASOS) **일자료** — 권역 대표 관측소(서울108·대전133·광주156·부산159·강릉105) 최고/최저/평균기온, 강수, 습도, 일조 | `GET http://apis.data.go.kr/1360000/AsosDalyInfoService/getWthrDataList` | `DATA_GO_KR_KEY` — 공공데이터포털 https://www.data.go.kr → "기상청_지상(종관, ASOS) 일자료 조회서비스" 활용신청. **일반 인증키(Decoding)** 사용 | numOfRows ≤ 999 → 연 단위 청크. 무강수일 공백 → 0 처리. 전일 자료는 익일 제공. 개발계정 일 트래픽 한도(기본 10,000) | `weather_daily.csv` |
| `kma_forecast.fetch_short_term` | 단기예보(D+1~3) TMX/TMN/POP/PCP, 격자 nx/ny | `GET .../1360000/VilageFcstInfoService_2.0/getVilageFcst` (base_time 0200) | 동일 키, "기상청_단기예보 조회서비스" 활용신청 | PCP 는 문자열("강수없음", "30.0~50.0mm") → 파서 처리. 발표일 당일 제외 | `weather_forecast.csv` (아카이브 append) |
| `kma_forecast.fetch_mid_term` | 중기기온(D+4~10 taMin/taMax) + 중기육상(강수확률 rnSt) | `GET .../1360000/MidFcstInfoService/getMidTa`, `getMidLandFcst` (tmFc = YYYYMMDD0600/1800) | 동일 키, "기상청_중기예보 조회서비스" 활용신청 | 발표 형식이 개정될 수 있어 `ta(Min|Max)N`, `rnStN(Am|Pm)?` 정규식 파싱. 권역 regId 는 config | 동일 |
| `holidays.fetch_holidays` / `derive_lunar_holiday` | 공휴일, 추석/설날 당일 도출 | `GET http://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo` | 동일 키, "한국천문연구원_특일 정보제공 서비스" 활용신청 | 대체·임시공휴일 지정을 반영하려면 월 1회 재수집. 연휴 3일 중 가운데 날 = 당일 | `holidays.csv` |
| `naver_datalab.fetch_search_trend` | 검색어 트렌드(주간, 2016~) 그룹 = 브랜드명 | `POST https://openapi.naver.com/v1/datalab/search` | `NAVER_CLIENT_ID/SECRET` — 네이버 개발자센터 https://developers.naver.com 애플리케이션 등록 → "데이터랩(검색어트렌드)" 사용 API 추가 | **일 1,000회**. 요청당 그룹 ≤5, 그룹당 키워드 ≤20. ratio 는 요청 내 상대지수(최대=100) → **전체 기간을 1회 요청**(증분 수집 금지). startDate ≥ 2016-01-01 | `search_trend.csv` (덮어쓰기) |
| `naver_datalab.fetch_shopping_keyword_trend` | 쇼핑인사이트 카테고리 내 키워드 클릭 추이 (선택) | `POST https://openapi.naver.com/v1/datalab/shopping/category/keywords` | 동일 (쇼핑인사이트 API 추가) | 키워드 ≤5, 2017-08-01~. 카테고리 ID(catId)는 네이버쇼핑 URL 에서 확인 → `config.naver.shopping_category_id` | `shopping_trend.csv` |
| `neis.fetch_vacations_for_regions` | 학사일정 → 권역 표본학교 여름방학 시작/종료 | `GET https://open.neis.go.kr/hub/SchoolSchedule`, `schoolInfo` | `NEIS_KEY` — 나이스 교육정보 개방포털 https://open.neis.go.kr 인증키 신청 | 학교 단위 조회 → `config.neis_sample_schools` 에 (권역, 교육청코드, 학교코드) 입력 필요(`find_school_code` 로 조회). pSize ≤ 1000. 비어 있으면 `default_summer_vacation` 사용 | `school_schedule.csv` |
| `macro.fetch_oni` | NOAA ONI(엘니뇨 지수) | `GET https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt` | 없음 | 정적 텍스트, 월 1회 갱신. 3개월 라벨(JJA 등) → 중심월 변환 | `oni.csv` |
| `macro.fetch_consumer_sentiment` | 한국은행 소비자심리지수 (선택) | `GET https://ecos.bok.or.kr/api/StatisticSearch/{KEY}/json/kr/1/1000/511Y002/M/{YYYYMM}/{YYYYMM}/FME` | `ECOS_KEY` — https://ecos.bok.or.kr 인증키 신청 | 통계표(511Y002)/항목(FME) 코드는 ECOS 통계코드검색에서 확인 후 `config.ecos` 수정. 3년 데이터에서는 추세와 구분 어려움(우선순위 하) | `consumer_sentiment.csv` |
| (수동) 기상청 3개월 전망 | 6/7/8월 평년 대비 높음/비슷/낮음 확률 | 기상청 날씨누리 기후전망 (API 없음) | 없음 | 매년 4월 발표값 3개를 `config.seasonal_outlook` 에 입력 | config |
| `collect.collect_all` | 위 전체 일괄 실행 | | | 소스별 실패 격리(로그 후 계속) | |

제외한 것: 구글 트렌드(공식 API 없음, pytrends 는 스크래핑), Open-Meteo(상업 이용 유료), SNS/리뷰(크롤링).

---

## 4. Airflow DAG 기반 수집·예측 (`airflow/dags/`, Airflow **3.1.7** 검증)

### 4.1 DAG 목록

| dag_id | 주기 (KST) | 태스크 | Connection | 산출물 / Asset(outlet) |
|---|---|---|---|---|
| `lwf_internal_ingest_daily` (템플릿) | 매일 06:00 `0 6 * * *` | `extract`(테이블별 동적 매핑: shipments, sku_master, promotions, stockouts, distribution, preorders) → `summarize` | `lwf_erp_db` | `data/raw/*.csv` / `lwf_internal_raw` |
| `lwf_ext_weather_daily` | 매일 07:00 `0 7 * * *` | `fetch_asos`(전일까지 lookback 7일 재수집 병합) · `fetch_forecast`(단기+중기 아카이브 append) | `lwf_data_go_kr` | `weather_daily.csv` / `lwf_weather_daily`, `weather_forecast.csv` / `lwf_weather_forecast` |
| `lwf_ext_calendar_monthly` | 매월 1일 03:00 `0 3 1 * *` | `fetch_holidays`(올해~후년) · `fetch_school_schedule` | `lwf_data_go_kr`, `lwf_neis` | `holidays.csv` / `lwf_holidays`, `school_schedule.csv` / `lwf_school_schedule` |
| `lwf_ext_search_weekly` | 매주 월 08:00 `0 8 * * 1` | `fetch_search_trend`(2016~전일 전체 1회 요청, 덮어쓰기) · `fetch_shopping_insight`(선택) | `lwf_naver_datalab` | `search_trend.csv` / `lwf_search_trend` |
| `lwf_ext_macro_monthly` | 매월 28일 09:00 `0 9 28 * *` | `fetch_oni` · `fetch_consumer_sentiment` | `lwf_ecos` | `oni.csv` / `lwf_oni`, `consumer_sentiment.csv` / `lwf_consumer_sentiment` |
| `lwf_features_m2_weekly` | 매주 월 09:00 `0 9 * * 1` | `check_inputs`(실적 최신성 ≤10일 게이트) → `build_features` → `list_brands` → `predict_m2`(브랜드 동적 매핑) → `publish` | — (Variable 만) | `panel_weekly.csv` / `lwf_panel_weekly`, `m2_pred_*.csv`, `m2_latest.json` / `lwf_m2_forecast` |
| `lwf_m1_pepero` | 8/31 09:00 + 9/30 09:00 (`MultipleCronTriggerTimetable`) | `build_features` → `predict_m1`(origin=실행일, LOYO 백테스트 포함) → `publish` | — | `m1_decision_*.json`, `m1_pred_total_*.csv`, `m1_cum_curve_*.csv`, `backtest_m1_*.csv`, `m1_latest_빼빼로.json` / `lwf_m1_decision` |
| `lwf_m1_patbingsu` | 4/15 09:00 | 동일 | — | `m1_latest_팥빙수.json` |

시간 순서: 내부 적재 06:00 → 날씨 07:00 → 검색 08:00(월) → 피처/M2 09:00(월). 모든 DAG `catchup=False`, `max_active_runs=1`, retries 2(10분), 태스크별 `execution_timeout`. Asset(outlet) 은 계보 표시용이며, 필요하면 `schedule=[Asset...]` 로 자산 기반 트리거로 전환 가능.

### 4.2 Connection / Variable 설정

| 종류 | 이름 | 값 | 사용 DAG |
|---|---|---|---|
| Connection | `lwf_data_go_kr` | type `http`, host `apis.data.go.kr`, **password** = 공공데이터포털 일반 인증키(Decoding) | 날씨, 캘린더 |
| Connection | `lwf_naver_datalab` | type `http`, host `openapi.naver.com`, **login** = Client ID, **password** = Client Secret | 검색 |
| Connection | `lwf_neis` | type `http`, host `open.neis.go.kr`, password = 인증키 | 캘린더 |
| Connection | `lwf_ecos` | type `http`, host `ecos.bok.or.kr`, password = 인증키 | 거시 |
| Connection | `lwf_erp_db` | 사내 DB(mssql/oracle/postgres…) 접속정보. 해당 provider 설치 | 내부 적재 |
| Variable (JSON) | `lwf_settings` | `{"project_root": "/opt/airflow/lotte_stockpile_forecast", "config_path": null, "brands": ["빼빼로","팥빙수"], "naver_shopping_enabled": false}` | 전체 |

등록 예시: `airflow/setup_airflow_objects.sh` (또는 UI Admin > Connections/Variables). 로컬 테스트는 환경변수 `AIRFLOW_CONN_LWF_DATA_GO_KR='http://:<key>@apis.data.go.kr'`, `AIRFLOW_VAR_LWF_SETTINGS='{...}'` 로 대체 가능.

### 4.3 동작 방식·배포

* `dags/lwf_common.py`: Connection → 환경변수 브리지(`export_keys`) 로 `src.external.*` 를 수정 없이 재사용. `settings()` 가 Variable 을 태스크 실행 시점에 1회 조회(파싱 시점 Variable 접근 없음). `run_date()` 는 Airflow 3 의 `dag_run.run_after` → `data_interval_end` → `logical_date` → now 순으로 실행 기준일(KST) 결정(수동 트리거 안전).
* 프로젝트 코드는 워커에서 import 되어야 함: `lwf_settings.project_root` 경로를 `sys.path` 에 추가(또는 `pip install -e`). 무거운 import(pandas/lightgbm/src)는 태스크 함수 내부에서만 수행.
* 워커 패키지: `airflow/requirements-airflow.txt` (pandas, lightgbm, scikit-learn, requests, pyyaml + DB provider). Airflow 3.1.x 는 Python 3.10~3.13.
* XCom 은 JSON 직렬화 가능한 dict/list 만 반환. 매핑 태스크 결과는 `LazyXComSequence` → `list()` 로 실체화 후 사용.
* 수동 실행 파라미터: 날씨 `lookback_days`(최초 적재 400), 내부 적재 `lookback_days`(최초 2000), M1 `cu/co/opening_inventory/buffer/origin`.
* 검증: `DagBag` import 오류 0, `airflow dags test lwf_features_m2_weekly`, `airflow dags test lwf_m1_pepero` 가 Airflow 3.1.7 Task SDK 런타임에서 전 태스크 성공(합성 데이터). 외부 API 태스크는 키 발급 후 `airflow tasks test lwf_ext_weather_daily fetch_asos` 로 확인.

---

## 5. 실행 방법 (CLI)

```bash
cd ~/lotte_stockpile_forecast && source .venv/bin/activate
python -m src.pipeline schema                                   # 입력 스키마
python -m examples.make_sample_data                             # 실데이터 전 합성 데이터
cp .env.example .env  # 키 입력 후
python -m src.pipeline fetch-external --start 2023-01-01        # 외부 수집 (소스 선택: --sources weather,holidays)
python -m src.pipeline build-features --extend-weeks 12
python -m src.pipeline predict-m1 --brand 빼빼로 --origin 2026-09-30 --target-year 2026 --cu 1 --co 0.25 [--buffer 0.05]
python -m src.pipeline predict-m1 --brand 팥빙수 --origin 2026-04-15 --target-year 2026
python -m src.pipeline predict-m2 --brand 팥빙수 [--origin 2026-06-01]
python -m src.pipeline backtest-m1 --brand 빼빼로 --origin-mmdd 09-30
python -m src.pipeline backtest-m2 --brand 팥빙수 --start 2025-05-05 --end 2025-08-25 --step 4
```

---

## 6. 실데이터 연결 체크리스트

1. `shipments` 수량 단위 통일(낱개 vs 박스), 브랜드 매핑 누락 SKU 0 확인(`quality_report.csv`).
2. `stockouts.csv` 확보 — 없으면 결품 기간이 학습에 그대로 들어가 **체계적 과소예측**.
3. `promotions.csv` 에 `status(plan/actual)` 와 `snapshot_date` 를 지금부터 적재(백테스트 편향 제거).
4. `window_iso_weeks` 를 "주간출고/베이스라인 > 1.3" 구간으로 데이터 확인 후 조정(`m1_table_*.csv` 참고).
5. `Cu, Co` 를 사업부와 확정 → τ. 실현 coverage 를 매년 기록해 `common_shock_sd_floor` 보정.
6. `weather_forecast.csv` 아카이브가 1시즌 이상 쌓이면 M2 학습을 예보 기반으로 전환.
7. `config.regions` 의 관측소/격자/구역코드, `neis_sample_schools`, `seasonal_outlook`, 수능일(`suneung_dates`) 갱신.
