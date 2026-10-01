# 유동인구(인구이동량) 크롤러 DAG

`old_py_files/foot_traffic/`에 있던 기존 스크립트(`foot_traffic_crawling.py` +
`common/crawler.py` + `common/util.py` + `common/dao/*`)를 Airflow DAG로 변환한 것입니다.
원본은 Windows PC에서 `foot_traffic_activate.bat`으로 실행하던 `.py` 스크립트였고, 이번
버전은 Airflow가 스케줄에 맞춰 자동으로 실행하고, 실패하면 로그가 UI에 남습니다.

## 1. 무슨 일을 하는 DAG인가

통계청 KOSIS 빅데이터활용 페이지에서 **"통신 모바일 인구이동량 통계(시군구 관내외)"**
엑셀 파일을 Selenium으로 내려받은 다음, 법정동코드 참고자료와 S&OP 캘린더를 이용해
"연도 / 주차 / 지역코드 / 지역명 / 유동인구" 형태로 가공하고, MSSQL DB의
`DMD_FCST.dbo.FOOT_TRAFFIC` 테이블에 저장합니다.

```
crawl_and_process_foot_traffic (다운로드 + 가공)  →  save_to_db (DB 저장)
```

두 단계(task)로 구성되어 있고, 순서대로 실행됩니다. 원본 코드는 다운로드 → 파일 찾기 →
가공 → DB 저장 → 원본 이동을 여러 함수로 나눠서 순서대로 호출했는데, 이 DAG에서는 그
중 "다운로드 ~ 원본 이동"까지를 첫 번째 task 하나로 묶었습니다. (이유는 3번 항목 참고)

## 2. 파일 구조

```
airflow/
├── dags/foot_traffic/
│   ├── __init__.py                      # 이 폴더를 파이썬 패키지로 인식시키기 위한 빈 파일
│   ├── foot_traffic_dag.py              # DAG 정의 (오케스트레이션만 담당)
│   ├── test_foot_traffic_imports.py     # 모듈 import 스모크 테스트 (5번 항목 참고)
│   └── README.md                        # 이 문서
└── plugins/foot_traffic_common/
    ├── __init__.py
    ├── crawler.py                       # Selenium으로 엑셀 다운로드 (FootTrafficCrawler)
    ├── processor.py                     # 엑셀 가공 로직 (FootTrafficProcessor)
    └── data/
        └── 법정동코드 전체자료.txt        # 원본에서 그대로 가져온 참고자료 (시군구 코드 매핑용)
```

`foot_traffic_dag.py`는 "언제, 어떤 순서로, 뭘 실행할지"만 정의하고, 실제 크롤링/가공
로직은 `plugins/foot_traffic_common/`에 있습니다. DAG 파일 안에서는
`from foot_traffic_common.crawler import FootTrafficCrawler`처럼 상대경로 없이 바로
불러옵니다 — Airflow가 `plugins/` 폴더를 자동으로 파이썬 import 경로에 넣어주기 때문입니다
(weather DAG와 동일한 패턴).

## 3. 원본 스크립트와 달라진 점

| 항목 | 원본 (`old_py_files`) | 이번 DAG |
|------|----------------------|----------|
| DB 접속정보 | `common/dao/config.py`에 서버 IP, 계정, **비밀번호를 코드에 하드코딩** | Airflow **Connection**(`foot_traffic_mssql`)으로 분리, 코드에 비밀번호 없음 |
| DB 접근 방식 | `SqlSession`이 SQLAlchemy 엔진 2개(`SNOP`, `DMD_FCST`)를 직접 관리하는 커스텀 DAO 클래스 | Airflow `MsSqlHook` 하나로 대체 (SQL 안에서 `SNOP.dbo.X`, `DMD_FCST.dbo.Y`처럼 DB명을 직접 명시하는 cross-database 쿼리는 원본과 동일하게 유지) |
| 다운로드 폴더 / 파일 이동 경로 | `C:\Users\ai_computer\Downloads`, `D:\foot_traffic\...` (Windows 경로 하드코딩) | 매 실행마다 임시 폴더(`tempfile.mkdtemp`)에 받고, 처리 끝나면 Airflow **Variable**(`foot_traffic_archive_dir`)이 가리키는 폴더로 원본을 옮긴 뒤 임시 폴더는 삭제 |
| 크롬 드라이버 | 로컬 PC에 설치된 Chrome을 그대로 사용 | 컨테이너 안에 **Chromium + chromedriver**를 새로 설치 (`Dockerfile`에 `apt-get install chromium chromium-driver` 추가) |
| 실행 방식 | 사람이 직접 `.bat` 실행 (Windows 작업 스케줄러로 예약) | Airflow가 스케줄에 맞춰 자동 실행, 실패 시 로그가 UI에 남음 |
| 실패 시 대체 로직 | 크롤링/가공이 실패하면 **전년 동기 4주 평균**으로 누락 데이터를 채우는 `foot_traffic_process_average()`가 같은 프로세스 안에서 실행됨 | **포팅하지 않음.** 아래 4번 항목 참고 |
| Windows 전용 코드 | `common/util.py`의 `excel_pivot_filtering`(win32com), `foot_traffic_process_old`(xlwings) | **포팅하지 않음** — 실제 실행 경로(`foot_traffic_crawling.py`의 `__main__`)에서 호출되지 않는 죽은 코드였고, 리눅스 컨테이너에서 애초에 동작하지 않는 라이브러리(win32com, xlwings)에 의존 |
| `dist_cd_process` 함수 | `common/util.py`에 있지만 정의되지 않은 `np`를 참조해서 애초에 호출하면 에러가 나는 함수 (실행 경로에서도 호출 안 됨) | **포팅하지 않음** |

## 4. 의도적으로 빼놓은 기능: 전년 평균 채우기

원본 `foot_traffic_crawling.py`는 크롤링이나 가공 중 예외가 발생하면 `except` 블록에서
`util.foot_traffic_process_average()`를 실행해서, 지역×주차별로 누락된 데이터를
**전년(또는 전전년) 동기 4주 평균**으로 채워 넣습니다 (`common/util.py` 약 250줄 분량의
로직).

이번 버전에는 이 대체 로직을 옮기지 않았습니다. 이유:
- 별도의 대규모 로직이라 크롤링 DAG 하나의 스코프를 벗어난다고 판단했습니다.
- Airflow에서는 이걸 "같은 task 안 except"가 아니라 `trigger_rule='one_failed'`을 쓰는
  별도 task(또는 별도 DAG)로 만드는 게 더 자연스러운데, 그러려면 설계를 따로 논의하는 게
  좋습니다.

원본 로직은 `dags/old_py_files/foot_traffic/common/util.py`의
`foot_traffic_process_average`에 그대로 남아있으니, 필요하면 이걸 기반으로 후속 작업으로
추가할 수 있습니다.

## 5. 테스트: 왜 "import만" 확인하는가

`plugins/foot_traffic_common/crawler.py`는 KOSIS 페이지의 특정 HTML 구조
(`//*[@id="datatable"]/tbody/tr[2]/td[4]/a`라는 XPath)에 의존합니다. 이 사이트가 페이지
구성을 바꾸면 이 XPath가 깨지는데, 이건 실제로 그 페이지에 접속해서 다운로드까지 해봐야
알 수 있는 문제라 자동화된 테스트로 미리 잡을 수 없습니다 (weather DAG의 AccuWeather
크롤러도 같은 이유로 CSS class 이름에 의존하는 게 README에 명시되어 있습니다).

그래서 `test_foot_traffic_imports.py`는 실제 크롤링/DB 접속 없이 **모듈들이 문법 오류나
누락된 패키지 없이 정상적으로 import되는지**만 확인합니다:

- `foot_traffic_common.crawler`, `foot_traffic_common.processor` 모듈이 import되고
  예상하는 메서드가 있는지
- 법정동코드 참고자료 파일이 제대로 들어있는지
- `foot_traffic_dag.py` 자체가 Airflow 환경에서 예외 없이 import되는지 (Airflow
  스케줄러가 DAG를 못 읽으면 "Broken DAG" 에러가 나는데, 배포 전에 미리 잡기 위함)

**실행 방법** (selenium/openpyxl 등 의존성이 설치된 컨테이너 안에서):
```bash
docker exec airflow-apiserver python /opt/airflow/dags/foot_traffic/test_foot_traffic_imports.py -v
```

## 6. 실행 전 준비 (초보자를 위한 단계별 설명)

### 6-1. Connection 등록 (DB 접속 정보)

**웹 UI로 등록:**
1. 브라우저에서 `http://localhost:8080` 접속 (계정: `airflow/.env`의
   `AIRFLOW_WWW_USER_USERNAME`/`PASSWORD`)
2. 상단 메뉴 **Admin → Connections** 클릭
3. **+** (Add a new record) 클릭
4. 아래 값 입력:

| 필드 | 값 |
|------|-----|
| Connection Id | `foot_traffic_mssql` |
| Connection Type | `Microsoft SQL Server` |
| Host | `host.docker.internal` |
| Database | `DMD_FCST` |
| Login | `sa` |
| Password | `mssql/.env`의 `MSSQL_SA_PASSWORD` 값 |
| Port | `1433` |

5. **Save**

> ⚠️ 이 Connection의 로그인 계정은 `DMD_FCST`뿐 아니라 `SNOP` 데이터베이스에도 접근
> 권한이 있어야 합니다. DAG가 캘린더 정보를 읽을 때 `SNOP.dbo.M4S_I002030`처럼 DB명을
> 직접 명시한 cross-database 쿼리를 날리기 때문입니다 (원본 스크립트도 동일하게 두 DB
> 모두에 접근 가능한 계정을 썼습니다).

**CLI로 등록하고 싶다면:**
```bash
docker exec airflow-apiserver airflow connections add 'foot_traffic_mssql' \
  --conn-type mssql \
  --conn-host host.docker.internal \
  --conn-schema DMD_FCST \
  --conn-login sa \
  --conn-password '여기에_mssql/.env의_MSSQL_SA_PASSWORD_값' \
  --conn-port 1433
```

### 6-2. Variable 등록 (선택)

기본값이 이미 원본 스크립트와 동일하게 설정되어 있어서 등록하지 않아도 동작합니다.
경로를 바꾸고 싶을 때만 **Admin → Variables → +** 에서 등록하세요.

| Key | 기본값 | 설명 |
|-----|--------|------|
| `foot_traffic_file_pattern` | `통신+모바일+인구이동량+통계+시군구+관내외+자료` | 다운로드된 엑셀 파일명에 포함되어야 하는 패턴 |
| `foot_traffic_archive_dir` | `/opt/airflow/dags/foot_traffic/archive` | 처리 완료 후 원본 엑셀을 옮길 폴더 (컨테이너 안 경로. `./dags`가 호스트와 바인드 마운트되어 있어서 맥에서도 `airflow/dags/foot_traffic/archive`로 보임) |

### 6-3. 대상 DB / 테이블 준비

로컬 `mssql` 컨테이너는 빈 SQL Server라 DB와 테이블이 없습니다. DBeaver로 `mssql`
(호스트: `localhost`, 포트: `1433`, 계정: `sa`)에 접속해서 아래를 실행하세요.

```sql
-- DMD_FCST: 유동인구 결과가 쌓이는 DB
CREATE DATABASE DMD_FCST;
GO
USE DMD_FCST;
GO

CREATE TABLE dbo.FOOT_TRAFFIC (
    YY       VARCHAR(4)    NOT NULL,  -- 연도 (예: '2025')
    WEEK     VARCHAR(5)    NOT NULL,  -- 주차 (예: 'W34')
    DIST_CD  VARCHAR(20)   NOT NULL,  -- 법정동코드 (시군구 단위)
    DIST_NM  NVARCHAR(50)  NOT NULL,  -- 지역명 (한글, 예: '서울특별시 강남구')
    POP      INT           NOT NULL  -- 유동인구 합계
);
GO

-- SNOP: 캘린더 마스터가 있는 DB. 원본 운영 환경에는 이미 존재하지만,
-- 로컬 테스트 환경에는 없으므로 최소한의 테이블/데이터를 직접 만들어야 합니다.
CREATE DATABASE SNOP;
GO
USE SNOP;
GO

CREATE TABLE dbo.M4S_I002030 (
    START_WEEK_DAY  VARCHAR(8)   NOT NULL,  -- 그 주의 월요일 (YYYYMMDD)
    YW_YY            VARCHAR(4)   NOT NULL,  -- 연도
    WEEK             VARCHAR(5)   NOT NULL   -- 주차 (예: 'W34')
);
GO
-- 예시 데이터 (실제로는 연중 모든 주차가 다 들어있어야 정상적으로 매핑됩니다)
INSERT INTO dbo.M4S_I002030 (START_WEEK_DAY, YW_YY, WEEK) VALUES
    ('20250630', '2025', 'W27'),
    ('20250707', '2025', 'W28');
GO
```

> `host.docker.internal`을 쓰는 이유는 `weather` DAG README의 4-1 항목과 동일합니다
> (Airflow 컨테이너와 `mssql` 컨테이너가 서로 다른 `docker compose` 프로젝트라 분리된
> 네트워크에 있기 때문).

## 7. 크롤링이 느린 이유 / Selenium 관련 참고

`plugins/foot_traffic_common/crawler.py`는 페이지 로딩 대기 3초 + 다운로드 대기 20초,
총 20초 이상 걸립니다. 인터넷 속도나 KOSIS 서버 상태에 따라 다운로드가 20초 안에 안 끝날
수도 있는데, 이 경우 `crawler.py`의 `FootTrafficCrawler.download()`에 있는
`wait_seconds` 기본값(20)을 늘려야 합니다.

컨테이너 안에는 화면이 없기 때문에 Chromium을 `--headless=new` 옵션으로 실행하고,
Docker 환경에서 흔히 필요한 `--no-sandbox`, `--disable-dev-shm-usage` 옵션도 추가했습니다
(원본 코드에는 주석 처리되어 있었지만, 컨테이너에서는 이 옵션이 없으면 Chromium이 아예
뜨지 않을 수 있습니다).

## 8. 실행 방법

### 웹 UI
1. `http://localhost:8080` 접속
2. DAG 목록에서 `foot_traffic_crawler` 찾기
3. 왼쪽 토글을 켜서 활성화 (기본적으로 꺼진 상태로 배포됨)
4. 오른쪽 ▶(Trigger DAG) 버튼 클릭
5. DAG 이름 클릭 → Graph 뷰에서 `crawl_and_process_foot_traffic`, `save_to_db` 박스
   색으로 진행상황 확인
6. 박스 클릭 → **Logs** 탭에서 실시간 로그 확인

### CLI
```bash
docker exec airflow-apiserver airflow dags trigger foot_traffic_crawler
```

## 9. 실패했을 때 확인할 것

- **Connection 관련 에러** (`Connection refused`, `Login failed` 등): 6-1에서 등록한
  `foot_traffic_mssql` Connection의 host/port/계정/비밀번호 확인. `mssql` 컨테이너가
  떠있는지도 `docker compose ps` (mssql 폴더에서)로 확인.
- **`'...' 패턴에 맞는 엑셀 파일을 다운로드 폴더에서 찾지 못했습니다`**: KOSIS 페이지
  구조가 바뀌어서 다운로드 버튼 XPath가 깨졌거나, 다운로드가 `wait_seconds` 안에 끝나지
  않은 경우입니다. `crawler.py`의 `_DOWNLOAD_LINK_XPATH`가 실제 페이지 구조와 맞는지
  확인하세요.
- **엑셀 가공 중 에러 (`KeyError`, `ValueError` 등)**: KOSIS가 다운로드 엑셀의 시트 구성/
  컬럼 순서를 바꿨을 가능성이 있습니다. `processor.py`의 `FootTrafficProcessor.process`가
  가정하는 시트(3번째 시트) / 컬럼 위치(0, 1, 2, 5번)를 실제 파일과 비교해보세요.
- **`Invalid object name 'SNOP.dbo.M4S_I002030'` 또는 그 비슷한 에러**: 6-3에서 SNOP DB /
  테이블을 안 만들었거나, Connection 계정에 SNOP 접근 권한이 없는 경우입니다.
- **Selenium/Chromium 관련 에러** (`session not created`, `chrome not reachable` 등):
  이미지가 새로 빌드됐는지 확인하세요 (`docker compose build` 후 `docker compose up -d`
  로 컨테이너를 재생성해야 `Dockerfile`에 추가한 Chromium이 반영됩니다).
