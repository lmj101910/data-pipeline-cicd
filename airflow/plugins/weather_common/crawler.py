from __future__ import annotations

import datetime
import random
import time
import warnings

import pandas as pd
import requests
import urllib3
from bs4 import BeautifulSoup


class WeatherCrawler:

    _BASE_URL = 'https://www.accuweather.com/ko/kr'
    _HEADERS = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/42.0.2311.135 Safari/537.36 Edge/12.246'
        )
    }

    @staticmethod
    def _build_url(citynm: str, citycd: str, day: int) -> str:
        base = f'{WeatherCrawler._BASE_URL}/{citynm}/{citycd}'
        if day == 1:
            return f'{base}/weather-today/{citycd}'
        if day == 2:
            return f'{base}/weather-tomorrow/{citycd}'
        return f'{base}/daily-weather-forecast/{citycd}?day={day}'

    @staticmethod
    def _parse_temp(soup: BeautifulSoup) -> tuple[float, float, float]:
        for div in soup.find_all('div', class_='row first'):
            if '예보' in div.get_text():
                parts = div.get_text().split('\n')
                temp_max = float(parts[2][:-1])
                temp_min = float(parts[3][:-1])
                return temp_max, temp_min, (temp_max + temp_min) / 2
        return 0.0, 0.0, 0.0

    @staticmethod
    def _parse_rain(soup: BeautifulSoup) -> float:
        rain = 0.0
        for p in soup.find_all('p', class_='panel-item'):
            text = p.get_text()
            if text[:2] == '강수' and text[-2:] == 'mm':
                rain += float(text[2:-2])
        return rain

    @staticmethod
    def weather_forecast(
        cities: dict,
        if_date: str,
        start_day: int,
        end_day: int,
    ) -> pd.DataFrame:
        # AccuWeather가 너무 빠른 요청을 차단하기 때문에 요청 사이 랜덤 대기가 필수입니다.
        # (도시당 0~10초, 도시 전환 시 20~100초 — 임의로 줄이면 IP가 차단될 수 있습니다)
        warnings.filterwarnings('ignore', category=urllib3.exceptions.InsecureRequestWarning)

        today = datetime.date.today()
        records: list[list] = []

        for city, (citynm, citycd, idx_dtl_cd) in cities.items():
            for i in range(start_day, end_day):
                dt = (today + datetime.timedelta(days=i - 1)).strftime('%Y%m%d')
                url = WeatherCrawler._build_url(citynm, citycd, i)

                time.sleep(random.randrange(0, 10))
                resp = requests.get(url=url, headers=WeatherCrawler._HEADERS, verify=False)
                soup = BeautifulSoup(resp.content, 'html.parser')

                temp_max, temp_min, temp_avg = WeatherCrawler._parse_temp(soup)
                rain = WeatherCrawler._parse_rain(soup)

                records.extend([
                    [if_date, 'TEMP_AVG', idx_dtl_cd, city, dt, temp_avg],
                    [if_date, 'TEMP_MIN', idx_dtl_cd, city, dt, temp_min],
                    [if_date, 'TEMP_MAX', idx_dtl_cd, city, dt, temp_max],
                    [if_date, 'RAIN_SUM', idx_dtl_cd, city, dt, rain],
                ])

            time.sleep(random.randrange(20, 100))

        return pd.DataFrame(
            records,
            columns=['IF_YYMMDD', 'IDX_CD', 'IDX_DTL_CD', 'IDX_DTL_NM', 'YYMMDD', 'REF_VAL'],
        )
