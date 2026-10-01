#!/usr/bin/env bash
# Airflow 3.1.x 에 본 프로젝트가 필요로 하는 Connection / Variable 을 등록하는 예시 스크립트.
# 값(<...>)을 채운 뒤 Airflow 가 설치된 환경에서 실행.  (UI: Admin > Connections / Variables 에서 동일하게 입력 가능)
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/opt/airflow/lotte_stockpile_forecast}"

# ---------- Variables ----------
airflow variables set --json lwf_settings "{
  \"project_root\": \"${PROJECT_ROOT}\",
  \"config_path\": null,
  \"brands\": [\"빼빼로\", \"팥빙수\"],
  \"naver_shopping_enabled\": false
}"

# ---------- Connections (API 키는 password 필드) ----------
# 공공데이터포털: '일반 인증키(Decoding)' 값. 기상청 ASOS/단기예보/중기예보 + 천문연 특일정보 4개 서비스 모두 '활용신청' 필요
airflow connections add lwf_data_go_kr --conn-type http --conn-host apis.data.go.kr \
  --conn-password '<DATA_GO_KR_DECODING_KEY>'

# 네이버 개발자센터 애플리케이션 (데이터랩 검색어트렌드 + 쇼핑인사이트 API 사용 설정)
airflow connections add lwf_naver_datalab --conn-type http --conn-host openapi.naver.com \
  --conn-login '<NAVER_CLIENT_ID>' --conn-password '<NAVER_CLIENT_SECRET>'

# 나이스 교육정보 개방포털
airflow connections add lwf_neis --conn-type http --conn-host open.neis.go.kr \
  --conn-password '<NEIS_KEY>'

# 한국은행 ECOS (선택)
airflow connections add lwf_ecos --conn-type http --conn-host ecos.bok.or.kr \
  --conn-password '<ECOS_KEY>'

# 내부 ERP/DW (내부데이터 적재 템플릿 DAG). 예: MSSQL. provider 패키지 설치 필요 (apache-airflow-providers-microsoft-mssql 등)
# airflow connections add lwf_erp_db --conn-type mssql --conn-host <host> --conn-port 1433 \
#   --conn-schema <db> --conn-login <user> --conn-password '<pw>'

echo "done. 확인: airflow connections list | grep lwf_ ; airflow variables get lwf_settings"
