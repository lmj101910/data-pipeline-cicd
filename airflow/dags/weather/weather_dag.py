from __future__ import annotations

import pendulum

from airflow.models import Variable
from airflow.sdk import dag, task


@dag(
    dag_id='weather_forecast_crawler',
    schedule='0 5 * * 4',
    start_date=pendulum.datetime(2026, 1, 1, tz='Asia/Seoul'),
    catchup=False,
    tags=['weather', 'crawling'],
    doc_md="""
## 날씨 예보 크롤러

AccuWeather에서 주요 도시 날씨 예보를 크롤링하여 MSSQL DB에 적재합니다.

자세한 설명은 `dags/weather/README.md` 참고.

### Airflow Variables
| Key | 설명 | 기본값 |
|-----|------|--------|
| `weather_start_day` | 오늘 기준 시작 일수 | 1 |
| `weather_end_day` | 오늘 기준 종료 일수 | 37 |
| `weather_cities` | 도시 목록 (JSON) | - (필수) |

### Airflow Connections
| Conn ID | 설명 |
|---------|------|
| `weather_mssql` | MSSQL DB 연결 정보 |
""",
)
def weather_forecast_dag():

    @task()
    def crawl_weather(**context) -> list:
        from weather_common.crawler import WeatherCrawler

        start_day = int(Variable.get('weather_start_day', default_var=1))
        end_day = int(Variable.get('weather_end_day', default_var=37))
        cities = Variable.get('weather_cities', deserialize_json=True)

        if_date = context['logical_date'].in_timezone('Asia/Seoul').strftime('%Y%m%d')
        df = WeatherCrawler.weather_forecast(cities, if_date, start_day, end_day)
        return df.values.tolist()

    @task()
    def save_to_db(rows: list, **context) -> None:
        from airflow.providers.microsoft.mssql.hooks.mssql import MsSqlHook

        if_date = context['logical_date'].in_timezone('Asia/Seoul').strftime('%Y%m%d')
        hook = MsSqlHook(mssql_conn_id='selsndbdev-DMD_FCST')

        with hook.get_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM dbo.WEATHER WHERE IF_YYMMDD = %s",
                    (if_date,),
                )
                cursor.executemany(
                    "INSERT INTO dbo.WEATHER"
                    " (IF_YYMMDD, IDX_CD, IDX_DTL_CD, IDX_DTL_NM, YYMMDD, REF_VAL)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    [tuple(r) for r in rows],
                )
            conn.commit()

    rows = crawl_weather()
    save_to_db(rows)


weather_forecast_dag()
