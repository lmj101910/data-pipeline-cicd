from __future__ import annotations

import pendulum

from airflow.models import Variable
from airflow.sdk import dag, task


@dag(
    dag_id='foot_traffic_crawler',
    schedule='0 6 * * 2',
    start_date=pendulum.datetime(2026, 1, 1, tz='Asia/Seoul'),
    catchup=False,
    tags=['foot_traffic', 'crawling'],
    doc_md="""
## 유동인구(인구이동량) 크롤러

통계청 KOSIS 빅데이터활용 페이지에서 '통신 모바일 인구이동량 통계(시군구 관내외)' 엑셀을
Selenium으로 내려받아 가공한 뒤 MSSQL DB의 `DMD_FCST.dbo.FOOT_TRAFFIC` 테이블에 적재합니다.

자세한 설명은 `dags/foot_traffic/README.md` 참고.

### Airflow Variables
| Key | 설명 | 기본값 |
|-----|------|--------|
| `foot_traffic_file_pattern` | 다운로드 엑셀 파일명에 포함되어야 하는 패턴 | `통신+모바일+인구이동량+통계+시군구+관내외+자료` |
| `foot_traffic_archive_dir` | 처리 완료 후 원본 엑셀을 옮길 폴더 | `/opt/airflow/dags/foot_traffic/archive` |

### Airflow Connections
| Conn ID | 설명 |
|---------|------|
| `foot_traffic_mssql` | MSSQL DB 연결 정보 (SNOP, DMD_FCST 두 DB 모두 접근 권한 필요) |
""",
)
def foot_traffic_dag():

    @task()
    def crawl_and_process_foot_traffic() -> list:
        import os
        import shutil
        import tempfile

        import foot_traffic_common
        from airflow.providers.microsoft.mssql.hooks.mssql import MsSqlHook
        from foot_traffic_common.crawler import FootTrafficCrawler
        from foot_traffic_common.processor import FootTrafficProcessor

        file_pattern = Variable.get(
            'foot_traffic_file_pattern',
            default_var='통신+모바일+인구이동량+통계+시군구+관내외+자료',
        )
        archive_dir = Variable.get(
            'foot_traffic_archive_dir',
            default_var='/opt/airflow/dags/foot_traffic/archive',
        )
        dist_file_path = os.path.join(
            os.path.dirname(foot_traffic_common.__file__), 'data', '법정동코드 전체자료.txt'
        )

        download_dir = tempfile.mkdtemp(prefix='foot_traffic_')
        try:
            FootTrafficCrawler.download(download_dir)

            excel_path = FootTrafficProcessor.find_excel_file(download_dir, file_pattern)
            if excel_path is None:
                raise FileNotFoundError(
                    f"'{file_pattern}' 패턴에 맞는 엑셀 파일을 다운로드 폴더에서 찾지 못했습니다."
                )

            hook = MsSqlHook(mssql_conn_id='foot_traffic_mssql')
            calendar_df = hook.get_pandas_df(
                'SELECT DISTINCT START_WEEK_DAY, YW_YY, WEEK FROM SNOP.dbo.M4S_I002030'
            )

            foot_traffic_df = FootTrafficProcessor.process(excel_path, dist_file_path, calendar_df)
            foot_traffic_df = foot_traffic_df[foot_traffic_df['YY'].astype(int) > 2018]
            foot_traffic_df = foot_traffic_df.astype({'YY': 'int'})

            FootTrafficProcessor.move_file(excel_path, archive_dir)
        finally:
            shutil.rmtree(download_dir, ignore_errors=True)

        return foot_traffic_df.values.tolist()

    @task()
    def save_to_db(rows: list) -> None:
        from airflow.providers.microsoft.mssql.hooks.mssql import MsSqlHook

        hook = MsSqlHook(mssql_conn_id='foot_traffic_mssql')

        with hook.get_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM DMD_FCST.dbo.FOOT_TRAFFIC WHERE YY > %s",
                    ('2018',),
                )
                cursor.executemany(
                    "INSERT INTO DMD_FCST.dbo.FOOT_TRAFFIC"
                    " (YY, WEEK, DIST_CD, DIST_NM, POP)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    [tuple(r) for r in rows],
                )
            conn.commit()

    rows = crawl_and_process_foot_traffic()
    save_to_db(rows)


foot_traffic_dag()
