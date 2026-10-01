"""
[내부데이터] ERP/DW(YellowBrick, wfd_dm/wfd_dw) -> lwf_<table> (YellowBrick) 적재 - 일간.

실제 ERP 테이블로 확정된 부분:
  - shipments/distribution : wfd_dm.wfdm_f_sa_sa_dd (매출일별) 를
    wfd_dm.wfdm_d_st_matr_mst_intgr (자재마스터) 와 matr_no 로 JOIN 해서 idv_brnd_cd(개별브랜드코드)
    기준으로 브랜드 필터링. 자재코드를 나열할 필요 없이 BRAND_CODES 의 브랜드코드 2개만 유지하면 됨.
  - sku_master : wfd_dm.wfdm_d_st_matr_mst_intgr 에서 브랜드 필터로 직접 SELECT (더 이상 파이썬에서
    placeholder 로 구성하지 않음). matr_nm -> sku_name, matr_cre_dt -> launch_date 로 매핑.
    discontinue_date/is_limited_edition 은 마스터에 해당 컬럼이 없어 여전히 placeholder(NULL/False).
  - stockouts, promotions : 제외 확정 (wfd_dw/wfd_dm 에 결품 로그 없음 / 행사에 브랜드-자재 연결 없음).
                            src/schema.py, src/features/build.py 에서도 제거함.
  - preorders : 일시 제외 (item 6 - 사전발주/주문 테이블 아직 미확정, dw.fact_preorder 는 존재하지
    않는 placeholder였음). src/models/m1_event_total.py 의 build_m1_table() 은 preorders=None 을
    이미 안전하게 처리(if preorders is not None and len(preorders): ...)하므로 코드 변경 없이 제외 가능.
    실제 사전발주/주문 테이블 확인되면 SQL 딕셔너리에 다시 추가하면 됨.

BRAND_CODES 에 idv_brnd_cd -> 브랜드명(config.yaml 의 brands 키와 반드시 동일) 매핑만 넣으면 됨.
wfdm_f_sa_sa_dd 는 회사 전체 매출 테이블(용량 큼)이라 반드시 매출-자재마스터 JOIN + idv_brnd_cd
필터를 걸어서 가져와야 함 (필터 없이 전체를 긁으면 안 됨).

sku_master 는 이제 자재마스터에서 실제 자재명/생성일자를 가져오지만, discontinue_date(단종일)와
is_limited_edition(한정판 여부)은 마스터에 해당 컬럼이 없어 여전히 NULL/False 로 채워짐.
n_limited_sku 피처는 당분간 항상 0 - 나중에 한정판 판단 가능한 컬럼(예: matr_hier_*, brnd_se_cd 등)을
확인하면 이 부분만 CASE 문으로 교체하면 됨.

채널: dich_cd(유통경로코드) 원값 그대로 적재. 지역(region) 매핑 필드는 못 찾아 'ALL' 고정.
반품: sa_se_cd/rtrn_typ_cd 코드값을 아직 몰라 정상매출/반품을 분리하지 못함 -> 지금은 sa_qty 를
그대로 합산(순매출수량으로 간주)하고 returns_qty=0 으로 둠. 코드값 확인되면 분리 가능.

  - Connection sedp-dev-client-wfd 의 DB 드라이버로 SQL 실행 -> pandas -> 동일 DB 에 적재
  - 산출물은 src/schema.py 의 컬럼 계약을 지켜야 하며, 적재 직전 schema.validate 로 검증합니다.
  - shipments 는 lookback_days 만큼 재추출해 upsert (사후 정정/반품 반영).
  - sku_master/distribution 는 매일 전체 재추출해 TRUNCATE 후 재적재 (마스터성 테이블).
  - CSV 저장은 WRITE_CSV=False 로 기본 비활성화 (코드는 유지, 만일의 사태 대비. True 로 바꾸면 기존처럼 CSV 도 저장).

스케줄: 매일 06:00 KST (외부 날씨 07:00, 피처/M2 월 09:00 이전)
Connection: sedp-dev-client-wfd (소스 ERP/DW 조회 및 분석 결과 적재)
"""
from __future__ import annotations

from contextlib import closing
from datetime import timedelta

import pendulum
from airflow.sdk import Asset, Param, dag, get_current_context, task

from lotte_stockpile_forecast.lwf_common import DEFAULT_ARGS, KST, export_keys, load_cfg, merge_csv, run_date

ASSET_INTERNAL = Asset(name="lwf_internal_raw", uri="lwf://internal/raw")

WRITE_CSV = False  # True 로 바꾸면 기존 CSV 저장 로직 재활성화 (코드는 항상 유지)

# idv_brnd_cd(개별브랜드코드) -> 브랜드명. 브랜드명은 config.yaml 의 brands 키(빼빼로/팥빙수)와
# 반드시 동일해야 함. wfd_dm.wfdm_d_st_matr_mst_intgr.idv_brnd_cd 기준.
BRAND_CODES: dict[str, str] = {
    "20000033": "빼빼로",
    "20000183": "팥빙수",
}


def _sql_list(values: list[str]) -> str:
    return ", ".join(f"'{v}'" for v in values) or "'__NONE__'"  # 빈 리스트 -> 항상 매칭 0건


_BRAND_CODES_SQL = _sql_list(list(BRAND_CODES.keys()))
_BRAND_CASE = " ".join(
    f"WHEN idv_brnd_cd = '{code}' THEN '{name}'" for code, name in BRAND_CODES.items()
) or "WHEN FALSE THEN NULL"

SQL = {
    "shipments": f"""
        SELECT TO_DATE(s.std_dt, 'YYYYMMDD')  AS date,
               s.matr_no                       AS sku_code,
               COALESCE(s.dich_cd, 'ALL')      AS channel,
               'ALL'                           AS region,
               SUM(s.sa_qty)                   AS qty,
               0                                AS returns_qty  -- TODO: sa_se_cd/rtrn_typ_cd 코드값 확인되면 분리
        FROM   wfd_dm.wfdm_f_sa_sa_dd s
        JOIN   wfd_dm.wfdm_d_st_matr_mst_intgr m ON m.matr_no = s.matr_no
        WHERE  s.std_dt BETWEEN '{{start_ymd}}' AND '{{end_ymd}}'
          AND  m.idv_brnd_cd IN ({_BRAND_CODES_SQL})   -- 반드시 브랜드 필터 (전체 매출 테이블이라 용량 큼)
        GROUP  BY s.std_dt, s.matr_no, COALESCE(s.dich_cd, 'ALL')
    """,
    "sku_master": f"""
        SELECT matr_no                                     AS sku_code,
               CASE {_BRAND_CASE} END                       AS brand,
               matr_nm                                      AS sku_name,
               TO_DATE(NULLIF(matr_cre_dt, ''), 'YYYYMMDD')  AS launch_date,
               NULL                                          AS discontinue_date,   -- 마스터에 단종일 컬럼 없음
               FALSE                                         AS is_limited_edition  -- 마스터에 한정판 플래그 없음
        FROM   wfd_dm.wfdm_d_st_matr_mst_intgr
        WHERE  idv_brnd_cd IN ({_BRAND_CODES_SQL})
    """,
    "distribution": f"""
        SELECT DATE_TRUNC('week', TO_DATE(s.std_dt, 'YYYYMMDD'))::date AS week_start,
               CASE {_BRAND_CASE} END          AS brand,
               'ALL' AS channel,
               'ALL' AS region,
               COUNT(DISTINCT s.cust_dscm_id)   AS active_stores
        FROM   wfd_dm.wfdm_f_sa_sa_dd s
        JOIN   wfd_dm.wfdm_d_st_matr_mst_intgr m ON m.matr_no = s.matr_no
        WHERE  m.idv_brnd_cd IN ({_BRAND_CODES_SQL})
          AND  s.std_dt BETWEEN '{{start_ymd}}' AND '{{end_ymd}}'
        GROUP  BY 1, 2
    """,
    # "preorders": 일시 제외 (item 6 - 사전발주/주문 테이블 아직 미확정, dw.fact_preorder 는 존재하지
    # 않는 placeholder였음). src/models/m1_event_total.py 의 build_m1_table() 은 preorders=None 을
    # 이미 안전하게 처리(if preorders is not None and len(preorders): ...)하므로 코드 변경 없이 제외 가능.
    # 실제 사전발주/주문 테이블 확인되면 여기에 SQL 추가하면 됨.
}
INCREMENTAL = {"shipments": (["date", "sku_code", "channel", "region"], ["date"])}
DB_TABLE = {t: f"lwf_{t}" for t in SQL}  # shipments -> lwf_shipments 등


@dag(
    dag_id="lwf_internal_ingest_daily",
    dag_display_name="[내부] ERP/DW -> DB 적재 (일간, 템플릿)",
    schedule="0 6 * * *",
    start_date=pendulum.datetime(2026, 1, 1, tz=KST),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    params={"lookback_days": Param(35, type="integer", minimum=1, maximum=2000,
                                   description="shipments 재추출 일수 (최초 적재 시 2000)")},
    tags=["lwf", "internal"],
    doc_md=__doc__,
)
def lwf_internal_ingest_daily():
    @task(execution_timeout=timedelta(minutes=30))
    def extract_and_load(table: str) -> dict:
        from airflow.sdk import Connection

        ctx = get_current_context()
        cfg = load_cfg()
        from src.db import read_query, replace_all, upsert
        from src.schema import SCHEMAS, validate

        end = run_date(ctx) - timedelta(days=1)
        start = end - timedelta(days=int(ctx["params"]["lookback_days"]))

        # std_dt 가 'YYYYMMDD' 문자열(VARCHAR)이라 날짜가 아닌 문자열로 비교해야 함.
        # sku_master 는 날짜 필터가 필요 없는 마스터 조회라 start_ymd/end_ymd 를 안 씀 (format 무시됨).
        sql = SQL[table].format(start_ymd=start.strftime("%Y%m%d"), end_ymd=end.strftime("%Y%m%d"))
        hook = Connection.get("sedp-dev-client-wfd").get_hook()
        # get_pandas_df/get_df 는 SQLAlchemy 의 PostgreSQL 버전 판별을 거쳐 YellowBrick 에서 실패함.
        with closing(hook.get_conn()) as conn:
            df = read_query(conn, sql)
        df = validate(df, SCHEMAS[table])                 # 컬럼 계약 검증 (누락 시 실패)

        if WRITE_CSV:
            path = cfg["paths"]["raw"] / f"{table}.csv"
            if table in INCREMENTAL:
                keys, dates = INCREMENTAL[table]
                merge_csv(df, path, keys, dates)
            else:
                df.to_csv(path, index=False)

        export_keys("sedp-dev-client-wfd")

        if table in INCREMENTAL:
            keys, _dates = INCREMENTAL[table]
            n = upsert(df, DB_TABLE[table], pk_cols=keys)
        else:
            n = replace_all(df, DB_TABLE[table])           # 마스터성 테이블: 매일 전체 재적재

        return {"table": table, "extracted": int(len(df)), "db_rows": int(n)}

    @task(outlets=[ASSET_INTERNAL])
    def summarize(results) -> dict:
        return {r["table"]: r["db_rows"] for r in list(results)}  # LazyXComSequence -> list

    summarize(extract_and_load.expand(table=list(SQL)))


lwf_internal_ingest_daily()
