from __future__ import annotations

import os
import shutil
from datetime import date, timedelta

import pandas as pd


class FootTrafficProcessor:

    @staticmethod
    def find_excel_file(directory: str, file_pattern: str) -> str | None:
        """directory 안에서 file_pattern이 포함된 .xlsx 파일 경로를 찾는다."""
        for filename in os.listdir(directory):
            if file_pattern in filename and filename.endswith('.xlsx'):
                return os.path.join(directory, filename)
        return None

    @staticmethod
    def move_file(source_file_path: str, destination_folder_path: str) -> None:
        """다운로드한 원본 엑셀 파일을 보관 폴더로 옮긴다 (동일 이름 파일이 있으면 덮어씀)."""
        os.makedirs(destination_folder_path, exist_ok=True)

        destination_file_path = os.path.join(
            destination_folder_path, os.path.basename(source_file_path)
        )
        if os.path.exists(destination_file_path):
            os.remove(destination_file_path)

        shutil.move(source_file_path, destination_file_path)

    @staticmethod
    def get_first_monday(year: int, month: int, week: int) -> str:
        """
        연/월/주차의 월요일 날짜(YYYYMMDD)를 반환한다.
        '주차'는 ISO 8601 기준, 그 달의 첫 번째 목요일이 속한 주를 1주차로 본다.
        """
        first_day_of_month = date(year, month, 1)
        first_day_weekday = first_day_of_month.weekday()  # 0=월요일 ... 6=일요일

        days_to_thursday = (3 - first_day_weekday) % 7
        first_thursday = first_day_of_month + timedelta(days=days_to_thursday)

        first_monday_of_month = first_thursday - timedelta(days=3)
        first_monday_of_week = first_monday_of_month + timedelta(days=7 * (week - 1))

        return first_monday_of_week.strftime('%Y%m%d')

    @classmethod
    def process(
        cls,
        excel_path: str,
        dist_file_path: str,
        calendar_df: pd.DataFrame,
    ) -> pd.DataFrame:
        """
        KOSIS에서 내려받은 유동인구 엑셀(3번째 시트)을 DB 적재용 형태로 가공한다.

        :param excel_path: 다운로드한 .xlsx 파일 경로
        :param dist_file_path: 법정동코드 참고자료(.txt, euc-kr, 탭 구분) 경로
        :param calendar_df: S&OP 캘린더 (컬럼: start_week_day, yw_yy, week) - 대소문자 무관
        :return: 컬럼 [YY, WEEK, DIST_CD, DIST_NM, POP] DataFrame
        """
        dist = pd.read_csv(dist_file_path, sep='\t', encoding='euc-kr')

        foot_traffic = pd.read_excel(excel_path, sheet_name=2, engine='openpyxl')
        last_row = foot_traffic.last_valid_index() + 1
        foot_traffic = pd.read_excel(
            excel_path, sheet_name=2, usecols=[0, 1, 2, 5], nrows=last_row, engine='openpyxl'
        )

        foot_traffic['DIST_NM'] = foot_traffic['시도'] + ' ' + foot_traffic['시군구']
        foot_traffic.loc[
            foot_traffic['DIST_NM'] == '세종특별자치시 세종특별자치시', 'DIST_NM'
        ] = '세종특별자치시'

        foot_traffic['YY'] = foot_traffic['주차구분'].str.split('.').str[0]
        foot_traffic['MM'] = foot_traffic['주차구분'].str.split('.').str[1].astype(int)
        foot_traffic['WK'] = foot_traffic['주차구분'].str.split('.').str[2].str[:1].astype(int)

        foot_traffic = pd.merge(
            foot_traffic, dist, left_on='DIST_NM', right_on='법정동명', how='left'
        )

        foot_traffic['법정동코드'] = foot_traffic['법정동코드'].apply(lambda x: str(int(float(x))))
        foot_traffic['합계'] = foot_traffic['합계'].fillna(0).apply(int)
        foot_traffic = foot_traffic.dropna(axis=0, how='any', subset=['합계'])

        foot_traffic['YYMMDD'] = foot_traffic.apply(
            lambda row: cls.get_first_monday(int(row['YY']), int(row['MM']), int(row['WK'])),
            axis=1,
        )

        calendar_df = calendar_df.copy()
        calendar_df.columns = calendar_df.columns.str.lower()
        foot_traffic = pd.merge(
            foot_traffic, calendar_df, left_on='YYMMDD', right_on='start_week_day', how='left'
        )

        foot_traffic = foot_traffic.rename(
            columns={'합계': 'POP', '법정동코드': 'DIST_CD', 'yw_yy': 'YY', 'week': 'WEEK'}
        )

        return foot_traffic[['YY', 'WEEK', 'DIST_CD', 'DIST_NM', 'POP']]
