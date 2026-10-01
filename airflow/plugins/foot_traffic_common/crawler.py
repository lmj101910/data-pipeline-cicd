from __future__ import annotations

import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By


class FootTrafficCrawler:

    _URL = (
        'https://data.kostat.go.kr/nowcast/bigDataDwnOrgDt.do'
        '?menuId=6&subMenuId=1&searchDvsn=1'
    )
    # 원자료 다운로드 표의 2번째 행, 4번째 열(엑셀 다운로드) 링크.
    # KOSIS가 표 구성을 바꾸면 이 XPath도 같이 깨질 수 있다.
    _DOWNLOAD_LINK_XPATH = '//*[@id="datatable"]/tbody/tr[2]/td[4]/a'

    # Dockerfile에서 apt로 설치한 경로 (Debian bookworm 기준)
    _CHROME_BINARY = '/usr/bin/chromium'
    _CHROMEDRIVER_BINARY = '/usr/bin/chromedriver'

    @classmethod
    def download(cls, download_dir: str, wait_seconds: int = 20) -> None:
        """
        KOSIS 빅데이터활용 페이지에서 유동인구(모바일 인구이동) 엑셀 파일을
        download_dir에 내려받는다. 파일이 실제로 도착했는지는 호출하는 쪽에서 확인한다.
        """
        options = Options()
        options.binary_location = cls._CHROME_BINARY
        options.add_argument('--headless=new')
        options.add_argument('--disable-gpu')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')

        options.add_experimental_option(
            'prefs',
            {
                'download.default_directory': download_dir,
                'download.prompt_for_download': False,
                'download.directory_upgrade': True,
                'safebrowsing.enabled': True,
            },
        )

        service = Service(executable_path=cls._CHROMEDRIVER_BINARY)
        driver = webdriver.Chrome(service=service, options=options)

        try:
            print('크롤링 실행')
            driver.get(cls._URL)
            time.sleep(3)  # 페이지 로딩 대기

            download_button = driver.find_element(By.XPATH, cls._DOWNLOAD_LINK_XPATH)
            ActionChains(driver).move_to_element(download_button).click().perform()

            time.sleep(wait_seconds)  # 다운로드 완료 대기
            print('크롤링 종료')
        finally:
            driver.quit()
