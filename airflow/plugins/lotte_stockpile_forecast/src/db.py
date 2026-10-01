"""분석 DB(YellowBrick, Postgres 호환) 연결. 환경변수 PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD 사용
(psycopg2 표준 libpq 환경변수 이름과 동일하게 맞춤).

SQLAlchemy 의 PostgreSQL dialect 는 YellowBrick 버전 문자열을 해석하지 못하므로,
조회/적재 모두 DB-API 커서로 실행한다. 대상 테이블은 미리 생성되어 있어야 한다.

Airflow 에서는 dags/lwf_common.py 의 export_keys("sedp-dev-client-wfd") 가
Connection 'sedp-dev-client-wfd' 값을 이 환경변수들로 세팅해줌.
로컬 CLI(`python -m src.pipeline ...`) 실행 시에는 .env 에 직접 넣어도 동일하게 동작
(src/ 는 Airflow 를 import 하지 않음 - CLI/DAG 공용 유지).

DB_SCHEMA: 실제 테이블이 있는 스키마. YellowBrick 에서 `SELECT * FROM WFD_DW.lwf_xxx` 형태로 조회해야
하므로, 이 모듈의 모든 함수는 bare 테이블명("lwf_xxx")만 받고 내부에서 자동으로 스키마를 붙인다.
호출부(schema.py/build.py/jobs.py/DAG)는 스키마를 몰라도 되고, 수정할 필요도 없다.
환경변수 LWF_DB_SCHEMA 로 다른 값으로 덮어쓸 수 있음(기본 WFD_DW).
"""
from __future__ import annotations

import logging
import os
from contextlib import closing

import pandas as pd

log = logging.getLogger(__name__)

DB_SCHEMA = os.environ.get("LWF_DB_SCHEMA", "WFD_DW")


def get_connection():
    """호출자가 close 할 DB-API 연결. 접속값을 URL 조립 없이 드라이버에 전달한다."""
    import psycopg2

    return psycopg2.connect(
        host=os.environ["PGHOST"],
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
    )


def _qualified(table: str) -> str:
    """bare 테이블명 -> 'WFD_DW.table' (이미 스키마가 포함돼 있으면 그대로 둠)."""
    return table if "." in table else f"{DB_SCHEMA}.{table}"


def read_query(connection, query: str, parameters=None) -> pd.DataFrame:
    """이미 열린 DB-API 연결로 SELECT 실행. 연결의 종료는 호출자가 담당한다."""
    with connection.cursor() as cursor:
        cursor.execute(query, parameters)
        columns = [column[0] for column in cursor.description]
        return pd.DataFrame.from_records(cursor.fetchall(), columns=columns, coerce_float=True)


def read_sql(table: str, where: str | None = None) -> pd.DataFrame:
    """SELECT * FROM {schema}.{table} [WHERE ...] -> DataFrame. 테이블 없거나 비어있으면 빈 DataFrame."""
    sql = f"SELECT * FROM {_qualified(table)}"
    if where:
        sql += f" WHERE {where}"
    try:
        with closing(get_connection()) as conn:
            return read_query(conn, sql)
    except Exception as e:
        log.warning("[db] %s 조회 실패: %s", _qualified(table), e)
        return pd.DataFrame()


def _db_value(value):
    """pandas/NumPy 스칼라를 psycopg2 가 처리할 수 있는 값으로 변환."""
    if pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if isinstance(value, pd.Timedelta):
        return value.to_pytimedelta()
    return value.item() if hasattr(value, "item") else value


def _db_rows(df: pd.DataFrame):
    for row in df.itertuples(index=False, name=None):
        yield tuple(_db_value(value) for value in row)


def _quote_column(column: str) -> str:
    return '"' + column.replace('"', '""') + '"'


def _insert(cursor, df: pd.DataFrame, table: str) -> None:
    from psycopg2.extras import execute_values

    columns = ", ".join(_quote_column(column) for column in df.columns)
    query = f"INSERT INTO {_qualified(table)} ({columns}) VALUES %s"
    execute_values(cursor, query, _db_rows(df), page_size=1000)


def replace_all(df: pd.DataFrame, table: str) -> int:
    """테이블 전체 TRUNCATE 후 INSERT (마스터성 테이블의 '매번 전체 덮어쓰기' 방식과 동일)."""
    with closing(get_connection()) as conn:
        # psycopg2 연결 context: 성공 시 COMMIT, 예외 시 TRUNCATE/INSERT 전체 ROLLBACK.
        with conn, conn.cursor() as cursor:
            cursor.execute(f"TRUNCATE TABLE {_qualified(table)}")
            if df is not None and not df.empty:
                _insert(cursor, df, table)
    return 0 if df is None else len(df)


def upsert(df: pd.DataFrame, table: str, pk_cols: list[str]) -> int:
    """단순 upsert: PK 겹치는 기존 행 삭제 후 INSERT (배치 일/주간 적재용).
    대량 실시간 스트리밍에는 부적합, 이 프로젝트 규모(일/주 배치)에는 충분."""
    if df is None or df.empty:
        return 0
    qtable = _qualified(table)
    keys = df[pk_cols].drop_duplicates()
    cond = " AND ".join(f"{_quote_column(column)} = %s" for column in pk_cols)
    with closing(get_connection()) as conn:
        with conn, conn.cursor() as cursor:
            for key in _db_rows(keys):
                cursor.execute(f"DELETE FROM {qtable} WHERE {cond}", key)
            _insert(cursor, df, table)
    return len(df)
