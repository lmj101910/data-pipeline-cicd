FROM apache/airflow:3.3.2

# foot_traffic DAG의 Selenium 크롤링(headless Chromium)에 필요한 시스템 패키지.
# root 권한이 필요해서 pip 설치(USER airflow)보다 먼저 실행합니다.
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium chromium-driver libgomp1\
    && rm -rf /var/lib/apt/lists/*

USER airflow

COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt
