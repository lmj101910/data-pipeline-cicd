"""
foot_traffic DAG/모듈 임포트 스모크 테스트.

크롤링 대상 사이트(KOSIS)가 페이지 구조(HTML)를 바꾸면 crawler.py의 XPath가 깨질 수 있고,
그건 이 테스트로는 잡을 수 없다 (실제로 그 페이지에 접속해서 다운로드까지 해봐야 알 수 있음).
대신 이 DAG가 의존하는 모듈들이 문법 오류, 누락된 패키지, 잘못된 참조 없이 정상적으로
import되는지만 확인한다 - 배포 전에 "적어도 이유로 안 죽는다"는 걸 보장하기 위한 최소 테스트.

실행 방법 (selenium/openpyxl/pandas 등이 설치된 Airflow 컨테이너 안에서):
    docker exec airflow-apiserver python /opt/airflow/dags/foot_traffic/test_foot_traffic_imports.py
"""
from __future__ import annotations

import os
import sys
import unittest

_PLUGINS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'plugins'))
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)


class TestFootTrafficImports(unittest.TestCase):

    def test_crawler_module_imports(self):
        from foot_traffic_common.crawler import FootTrafficCrawler

        self.assertTrue(hasattr(FootTrafficCrawler, 'download'))
        self.assertTrue(callable(FootTrafficCrawler.download))

    def test_processor_module_imports(self):
        from foot_traffic_common.processor import FootTrafficProcessor

        for name in ('find_excel_file', 'move_file', 'get_first_monday', 'process'):
            self.assertTrue(hasattr(FootTrafficProcessor, name), f'{name} 메서드가 없습니다')
            self.assertTrue(callable(getattr(FootTrafficProcessor, name)))

    def test_dist_code_data_file_shipped(self):
        dist_file_path = os.path.join(
            _PLUGINS_DIR, 'foot_traffic_common', 'data', '법정동코드 전체자료.txt'
        )
        self.assertTrue(
            os.path.exists(dist_file_path),
            f'법정동코드 참고자료 파일이 없습니다: {dist_file_path}',
        )

    def test_dag_file_has_no_import_or_syntax_errors(self):
        """
        DAG 정의 파일이 아무 예외 없이 import되는지 확인한다. Airflow 스케줄러가 DAG
        파일을 읽다가 예외가 나면 DAG 목록에서 빠지거나 'Broken DAG' 에러가 뜨는데,
        이 테스트를 미리 돌려보면 배포 전에 그 문제를 잡을 수 있다.
        Airflow가 설치되어 있지 않은 환경(예: 이 저장소를 clone만 한 로컬 PC)에서는
        건너뛴다.
        """
        try:
            import airflow.sdk  # noqa: F401
        except ImportError:
            self.skipTest('Airflow가 설치된 환경(컨테이너 안)에서만 실행되는 검사입니다.')

        dag_dir = os.path.dirname(__file__)
        if dag_dir not in sys.path:
            sys.path.insert(0, dag_dir)

        import foot_traffic_dag  # noqa: F401


if __name__ == '__main__':
    unittest.main()
