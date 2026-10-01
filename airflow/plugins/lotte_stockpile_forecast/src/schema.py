"""
입력 데이터 스키마 정의 + 검증 로더.

내부 데이터 (data/raw/) - ERP/영업 시스템에서 추출
-------------------------------------------------------------------------
shipments.csv      일별 SKU 출고 실적                                  [필수]
sku_master.csv     SKU 마스터 (브랜드 매핑, 출시/단종, 한정판 여부)      [필수]
(promotions 는 제외 확정 - wfdw_s4_sdpromotreginfo 에 브랜드/자재 연결 없음. build_panel(promotions=None) 으로 처리)
(stockouts 는 제외 확정 - wfd_dw/wfd_dm 에 결품 로그 없음. build_panel(stockouts=None) 으로 처리)
distribution.csv   취급 거래처(점포) 수, 주간                          [선택]
preorders.csv      키어카운트 사전발주 (빼빼로)                         [선택] 빼빼로 M1 2차 예측의 핵심 변수
channel_inventory.csv 유통재고 스냅샷                                   [선택]
(foot_traffic 는 제외 확정 - 유동인구 데이터 소스 미확정, 수집 DAG 없음. foot_traffic_weekly(None) 으로 처리)

외부 데이터 (data/external/) - src/external/* 로 수집
-------------------------------------------------------------------------
weather_daily.csv     ASOS 일자료 (권역별 대표 관측소)
weather_forecast.csv  예보 아카이브 (issue_date 별로 누적). 기존 28일 예보도 이 스키마로 적재
holidays.csv          공휴일
school_schedule.csv   여름방학 시작/종료 (권역별)
search_trend.csv      네이버 데이터랩 검색어 트렌드 (주간)
oni.csv               NOAA ONI (엘니뇨 지수)
consumer_sentiment.csv 소비자심리지수 (선택)

컬럼 타입: date=YYYY-MM-DD 문자열, str, float, int, bool(0/1 또는 true/false)

--------------------------------------------------------------------------
DB 연동 (신규)
--------------------------------------------------------------------------
USE_DB=true(기본) 이면 load_table() 은 CSV 대신 Postgres 테이블(lwf_<name>, 매핑은
DB_TABLE_MAP)에서 SELECT 한다. CSV 로딩 코드는 그대로 남겨뒀고 USE_DB=false 일 때만
실행된다 (만일의 사태 대비 fallback). 환경변수 LWF_USE_DB=false 로 끌 수 있음.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

log = logging.getLogger(__name__)

USE_DB = os.environ.get("LWF_USE_DB", "true").strip().lower() not in ("false", "0", "no")


@dataclass
class TableSchema:
    name: str
    required: Dict[str, str]
    optional: Dict[str, str] = field(default_factory=dict)
    description: str = ""

    @property
    def all_columns(self) -> Dict[str, str]:
        return {**self.required, **self.optional}


SCHEMAS: Dict[str, TableSchema] = {
    # ---------------- 내부 ----------------
    "shipments": TableSchema(
        name="shipments",
        description="일별 SKU 출고 실적. qty 단위는 브랜드 내 통일(낱개 or 박스). 금액 대신 수량 사용.",
        required={"date": "date", "sku_code": "str", "channel": "str", "region": "str", "qty": "float"},
        optional={"returns_qty": "float", "net_sales_krw": "float", "order_type": "str"},
    ),
    "sku_master": TableSchema(
        name="sku_master",
        description="SKU -> 브랜드 매핑. 한정판/시즌한정 플래그는 빼빼로 uplift 의 핵심 변수.",
        required={"sku_code": "str", "brand": "str", "sku_name": "str", "launch_date": "date"},
        optional={"discontinue_date": "date", "is_limited_edition": "bool", "pack_size": "int",
                  "unit_weight_g": "float", "list_price": "float"},
    ),
    # "promotions": 제외 - wfdw_s4_sdpromotreginfo 에 브랜드/자재 연결 없어 데이터 없음으로 확정.
    # build_panel(promotions=None) 은 원래부터 정상 처리되므로(features/panel.py) 코드 변경 없이 안전하게 제거.
    # "stockouts": 제외 - 결품/품절 로그를 wfd_dw/wfd_dm 에서 못 찾아 데이터 없음으로 확정.
    # build_panel(stockouts=None) 은 원래부터 정상 처리되므로(features/panel.py) 코드 변경 없이 안전하게 제거.
    "distribution": TableSchema(
        name="distribution",
        description="주간 취급 거래처(점포) 수.",
        required={"week_start": "date", "brand": "str", "channel": "str", "region": "str", "active_stores": "float"},
    ),
    "preorders": TableSchema(
        name="preorders",
        description="키어카운트 사전발주 (빼빼로데이). snapshot_date 기준 누적 수량.",
        required={"snapshot_date": "date", "target_year": "int", "brand": "str", "channel": "str", "qty": "float"},
        optional={"account": "str", "region": "str"},
    ),
    "channel_inventory": TableSchema(
        name="channel_inventory",
        description="유통재고 스냅샷 (있으면 sell-in 예측 정확도 크게 개선).",
        required={"date": "date", "brand": "str", "channel": "str", "region": "str", "on_hand_qty": "float"},
    ),
    # "foot_traffic": 제외 - 유동인구 데이터 소스(ERP/외부 API 무엇이든) 미확정, 수집 DAG 없음.
    # build.py 의 foot_traffic_weekly(None) 은 원래부터 None 을 정상 처리(features/external.py)하므로
    # 코드 변경 없이 안전하게 제거. 소스 확보되면 SCHEMAS/DB_TABLE_MAP 에 다시 추가하면 됨.
    # ---------------- 외부 ----------------
    "weather_daily": TableSchema(
        name="weather_daily",
        required={"date": "date", "region": "str", "tavg": "float", "tmax": "float", "tmin": "float",
                  "rain_mm": "float"},
        optional={"humidity": "float", "sunshine_hr": "float", "station_id": "int"},
    ),
    "weather_forecast": TableSchema(
        name="weather_forecast",
        description="예보 아카이브. 학습 시 실제 예보를 쓰려면 매일 적재해서 누적해야 함 (train/serve skew 방지).",
        required={"issue_date": "date", "target_date": "date", "region": "str", "tmax": "float", "tmin": "float"},
        optional={"pop_max": "float", "rain_mm": "float", "source": "str"},
    ),
    "holidays": TableSchema(
        name="holidays",
        required={"date": "date", "name": "str", "is_holiday": "bool"},
    ),
    "school_schedule": TableSchema(
        name="school_schedule",
        required={"region": "str", "year": "int", "summer_start": "date", "summer_end": "date"},
    ),
    "search_trend": TableSchema(
        name="search_trend",
        description="네이버 데이터랩 검색어트렌드 (주간). ratio 는 요청 내 상대지수(최대=100).",
        required={"period": "date", "group": "str", "ratio": "float"},
    ),
    "oni": TableSchema(name="oni", required={"year": "int", "month": "int", "oni": "float"}),
    "consumer_sentiment": TableSchema(name="consumer_sentiment", required={"month": "str", "csi": "float"}),
}

# schema.py 상의 테이블명 -> 실제 DB 테이블명. search_trend 는 'group' 이 Postgres 예약어라
# DB 컬럼명은 group_name 으로 저장하고, 읽어올 때 여기서 다시 'group' 으로 되돌림.
DB_TABLE_MAP: Dict[str, str] = {
    "shipments": "lwf_shipments",
    "sku_master": "lwf_sku_master",
    "distribution": "lwf_distribution",
    "preorders": "lwf_preorders",
    "channel_inventory": "lwf_channel_inventory",
    "weather_daily": "lwf_ext_weather_daily",
    "weather_forecast": "lwf_ext_weather_forecast",
    "holidays": "lwf_ext_holidays",
    "school_schedule": "lwf_ext_school_schedule",
    "search_trend": "lwf_ext_search_trend",
    "oni": "lwf_ext_oni",
    "consumer_sentiment": "lwf_ext_consumer_sentiment",
}
DB_COLUMN_RENAME_ON_READ: Dict[str, Dict[str, str]] = {
    "search_trend": {"group_name": "group"},
}


def _cast(df: pd.DataFrame, col: str, typ: str) -> pd.Series:
    s = df[col]
    if typ == "date":
        return pd.to_datetime(s, errors="coerce").dt.normalize()
    if typ == "float":
        return pd.to_numeric(s, errors="coerce").astype("float64")
    if typ == "int":
        return pd.to_numeric(s, errors="coerce").astype("Int64")
    if typ == "bool":
        if s.dtype == bool:
            return s
        return s.astype(str).str.strip().str.lower().isin(["1", "true", "y", "yes", "t"])
    return s.astype(str).str.strip()


def validate(df: pd.DataFrame, schema: TableSchema) -> pd.DataFrame:
    missing = [c for c in schema.required if c not in df.columns]
    if missing:
        raise ValueError(f"[{schema.name}] 필수 컬럼 누락: {missing}. 필요 컬럼: {list(schema.required)}")
    out = df.copy()
    for col, typ in schema.all_columns.items():
        if col in out.columns:
            out[col] = _cast(out, col, typ)
    for col, typ in schema.required.items():
        n_null = out[col].isna().sum()
        if n_null:
            log.warning("[%s] 필수 컬럼 '%s' 결측 %d행 -> 제거", schema.name, col, n_null)
    out = out.dropna(subset=list(schema.required))
    return out.reset_index(drop=True)


def _load_table_db(name: str, schema: TableSchema, required: bool) -> Optional[pd.DataFrame]:
    from .db import read_sql

    table = DB_TABLE_MAP.get(name, f"lwf_{name}")
    df = read_sql(table)
    if df.empty:
        if required:
            raise FileNotFoundError(f"DB 테이블 '{table}' 에 데이터 없음\n{schema.description}")
        log.info("[%s] DB 테이블 '%s' 비어있음 (선택) -> 건너뜀", name, table)
        return None
    rename = DB_COLUMN_RENAME_ON_READ.get(name)
    if rename:
        df = df.rename(columns=rename)
    df = validate(df, schema)
    log.info("[%s] %d rows loaded (DB: %s)", name, len(df), table)
    return df


def _load_table_csv(name: str, schema: TableSchema, directory: str | Path, required: bool) -> Optional[pd.DataFrame]:
    """기존 CSV 로딩. USE_DB=false 일 때만 실행 (만일의 사태 대비, 코드는 그대로 유지)."""
    path = Path(directory) / f"{name}.csv"
    if not path.exists():
        if required:
            raise FileNotFoundError(f"필수 파일 없음: {path}\n{schema.description}")
        log.info("[%s] 파일 없음 (선택) -> 건너뜀: %s", name, path)
        return None
    df = pd.read_csv(path, dtype=str, keep_default_na=True)
    df = validate(df, schema)
    log.info("[%s] %d rows loaded (CSV: %s)", name, len(df), path)
    return df


def load_table(name: str, directory: str | Path, required: bool = False) -> Optional[pd.DataFrame]:
    """USE_DB=true(기본): Postgres(lwf_<name>)에서 SELECT.
    USE_DB=false: 기존처럼 directory/{name}.csv 를 스키마 검증 후 로드.
    둘 다 없으면 None (required=True 면 예외)."""
    schema = SCHEMAS[name]
    if USE_DB:
        return _load_table_db(name, schema, required)
    return _load_table_csv(name, schema, directory, required)


def describe_schemas() -> str:
    lines = []
    for s in SCHEMAS.values():
        lines.append(f"## {s.name}.csv  {s.description}")
        for c, t in s.required.items():
            lines.append(f"   - {c:<18} {t:<6} [필수]")
        for c, t in s.optional.items():
            lines.append(f"   - {c:<18} {t:<6} [선택]")
    return "\n".join(lines)
