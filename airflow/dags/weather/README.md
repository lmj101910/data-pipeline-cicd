# 날씨 예보 크롤러 DAG

`old_py_files/weather/`에 있던 기존 스크립트(`weather_crawler.py` + `common/crawler.py`)를
Airflow DAG로 변환한 것입니다. 원본은 로컬 PC에서 수동으로 실행하는 `.py` 스크립트였고,
이번 버전은 Airflow가 스케줄에 맞춰 자동으로 실행하고, 실패하면 재시도/알림까지 관리해줍니다.

## 1. 무슨 일을 하는 DAG인가

AccuWeather 웹사이트를 크롤링해서 국내 주요 도시의 **미래 날씨 예보**(최고/최저/평균기온, 강수량)를
가져온 다음, MSSQL DB의 `dbo.WEATHER` 테이블에 저장합니다.

```
crawl_weather (크롤링)  →  save_to_db (DB 저장)
```

두 단계(task)로 구성되어 있고, 순서대로 실행됩니다. `crawl_weather`가 실패하면 `save_to_db`는
실행되지 않습니다.

## 2. 파일 구조

```
airflow/
├── dags/weather/
│   ├── __init__.py       # 이 폴더를 파이썬 패키지로 인식시키기 위한 빈 파일
│   ├── weather_dag.py     # DAG 정의 (오케스트레이션만 담당)
│   └── README.md          # 이 문서
└── plugins/weather_common/
    ├── __init__.py
    └── crawler.py          # 실제 크롤링 로직 (WeatherCrawler 클래스)
```

`weather_dag.py`는 "언제, 어떤 순서로, 뭘 실행할지"만 정의하고, 실제로 웹페이지를 긁어오는
코드는 `plugins/weather_common/crawler.py`에 있습니다. `weather_dag.py` 안에서는
`from weather_common.crawler import WeatherCrawler`처럼 상대경로 없이 바로 불러옵니다 —
Airflow가 `plugins/` 폴더를 자동으로 파이썬 import 경로에 넣어주기 때문입니다.

## 3. 원본 스크립트와 달라진 점

| 항목 | 원본 (`old_py_files`) | 이번 DAG |
|------|----------------------|----------|
| DB 접속정보 | `common/config.py`에 서버 IP, 계정, **비밀번호를 코드에 하드코딩** | Airflow **Connection**(`weather_mssql`)으로 분리, 코드에 비밀번호 없음 |
| 대상 도시 / 조회 기간 | 코드 안에 딕셔너리로 하드코딩 | Airflow **Variable**로 분리, 코드 수정 없이 값만 바꿔서 조정 가능 |
| SQL 실행 | `DELETE ... WHERE IF_YYMMDD = '{if_date}'` (문자열 조합, SQL Injection 위험) | 파라미터 바인딩(`%s`)으로 수정 |
| INSERT 컬럼 타입 | `REF_VAL` 자리에 `%d` 사용 (소수점 있는 기온값이 정수로 잘릴 위험) | `%s`로 통일해서 소수점 유지 |
| 실행 방식 | 사람이 직접 `.py` 실행 | Airflow가 스케줄(매주 목요일 05:00)에 맞춰 자동 실행, 실패 시 로그가 UI에 남음 |

> ⚠️ `old_py_files/weather/common/config.py`에는 실제 사내 DB 비밀번호가 평문으로 들어있습니다.
> 이 파일은 새 DAG에 옮기지 않았고, git에도 올라가지 않는 게 좋습니다.

## 4. 실행 전 준비 (초보자를 위한 단계별 설명)

Airflow는 "코드"와 "코드가 참조하는 값"을 분리해서 관리합니다. DAG 코드는 이미 다 작성되어
있으니, 아래 두 가지만 Airflow 웹 UI에서 등록하면 됩니다.

### 4-1. Connection 등록 (DB 접속 정보)

Connection은 "이 DAG가 어느 DB에, 어떤 계정으로 접속할지"를 저장하는 곳입니다.
비밀번호가 코드에 노출되지 않도록 하기 위한 장치입니다.

**웹 UI로 등록:**
1. 브라우저에서 `http://localhost:8080` 접속 (계정: `airflow/.env`의 `AIRFLOW_WWW_USER_USERNAME`/`PASSWORD`)
2. 상단 메뉴 **Admin → Connections** 클릭
3. **+** (Add a new record) 클릭
4. 아래 값 입력:

| 필드 | 값 |
|------|-----|
| Connection Id | `weather_mssql` |
| Connection Type | `Microsoft SQL Server` |
| Host | `host.docker.internal` |
| Database | (아래 4-3에서 만들 DB 이름, 예: `DMD_FCST`) |
| Login | `sa` |
| Password | `mssql/.env`의 `MSSQL_SA_PASSWORD` 값 |
| Port | `1433` |

5. **Save**

> `host.docker.internal`을 쓰는 이유: Airflow 컨테이너와 `mssql` 컨테이너는 서로 다른
> `docker compose` 프로젝트라 완전히 분리된 네트워크에 있습니다. Docker Desktop이 컨테이너
> 안에서 "내가 실행되고 있는 실제 컴퓨터(맥)"를 가리키도록 제공하는 특수 주소가
> `host.docker.internal`이고, `mssql/docker-compose.yaml`이 `1433` 포트를 호스트에
> 열어놨기 때문에 이 주소로 접근할 수 있습니다.

**CLI로 등록하고 싶다면 (터미널에서 실행):**
```bash
docker exec airflow-apiserver airflow connections add 'weather_mssql' \
  --conn-type mssql \
  --conn-host host.docker.internal \
  --conn-schema DMD_FCST \
  --conn-login sa \
  --conn-password '여기에_mssql/.env의_MSSQL_SA_PASSWORD_값' \
  --conn-port 1433
```

### 4-2. Variable 등록 (도시 목록 / 조회 기간)

Variable은 "DAG가 실행될 때 참고하는 설정값"입니다. 여기서는 크롤링할 도시 목록과
며칠 치 예보를 가져올지를 저장합니다.

**Admin → Variables → +** 에서 아래 3개를 각각 등록:

| Key | Value | 설명 |
|-----|-------|------|
| `weather_cities` | 아래 JSON 참고 | 크롤링할 도시 목록 |
| `weather_start_day` | `1` | 오늘(0) 기준 며칠 후부터 (1=내일) |
| `weather_end_day` | `37` | 며칠 후까지 (원본 기준 최대 37일) |

`weather_cities`에 넣을 JSON (원본 코드에 있던 30개 도시 그대로):
```json
{
  "강릉": ["gangneung", "223558", "105"],
  "원주": ["wonju", "223559", "114"],
  "춘천": ["chuncheon", "223554", "101"],
  "동두천": ["dongducheon", "223651", "98"],
  "수원": ["suwon", "223670", "119"],
  "파주": ["paju", "223659", "99"],
  "김해시": ["gimhae", "223799", "253"],
  "양산시": ["yangsan", "223801", "257"],
  "진주": ["jinju", "223798", "192"],
  "창원": ["changwon", "223791", "155"],
  "통영": ["tongyeong", "223796", "162"],
  "구미": ["gumi", "223680", "279"],
  "안동": ["andong", "223679", "136"],
  "포항": ["pohang", "223682", "138"],
  "광주": ["gwangju", "223627", "156"],
  "대구": ["daegu", "223347", "143"],
  "대전": ["daejeon", "223352", "133"],
  "부산": ["busan", "222888", "159"],
  "서울": ["seoul", "226081", "108"],
  "울산": ["ulsan", "226451", "152"],
  "인천": ["incheon", "224032", "112"],
  "목포": ["mokpo", "224257", "165"],
  "순천": ["suncheon", "224258", "174"],
  "군산": ["gunsan", "223083", "140"],
  "전주": ["jeonju", "223078", "146"],
  "제주": ["jeju", "224209", "184"],
  "부여": ["buyeo-gun", "223154", "236"],
  "천안": ["cheonan", "223148", "232"],
  "홍성": ["hongseong-gun", "223151", "177"],
  "청주": ["cheongju", "223115", "131"],
  "충주": ["chungju", "223117", "127"]
}
```

각 도시 값은 `[영문 슬러그, AccuWeather 도시코드, 지역코드]` 순서입니다 (크롤링 URL과
DB의 `IDX_DTL_CD` 컬럼에 쓰임).

> **처음 테스트할 땐 도시를 1~2개, `weather_end_day`를 `3` 정도로 줄여서 등록하는 걸
> 강력히 추천합니다.** 이유는 5번 항목 참고.

### 4-3. 대상 DB / 테이블 만들기

로컬 `mssql` 컨테이너는 방금 띄운 빈 SQL Server라 DB와 테이블이 없습니다. DBeaver로
`mssql`(호스트: `localhost`, 포트: `1433`, 계정: `sa`)에 접속해서 아래를 실행하세요.

```sql
CREATE DATABASE DMD_FCST;
GO

USE DMD_FCST;
GO

CREATE TABLE dbo.WEATHER (
    IF_YYMMDD    VARCHAR(8)    NOT NULL,  -- 크롤링을 실행한 기준일 (yyyymmdd)
    IDX_CD       VARCHAR(20)   NOT NULL,  -- TEMP_AVG / TEMP_MIN / TEMP_MAX / RAIN_SUM
    IDX_DTL_CD   VARCHAR(20)   NOT NULL,  -- 지역 코드
    IDX_DTL_NM   NVARCHAR(50)  NOT NULL,  -- 도시명 (한글)
    YYMMDD       VARCHAR(8)    NOT NULL,  -- 예보 대상일 (yyyymmdd)
    REF_VAL      FLOAT         NOT NULL   -- 기온(℃) 또는 강수량(mm)
);
GO
```

## 5. 왜 오래 걸리고, 왜 천천히 해야 하는가

`plugins/weather_common/crawler.py`에는 요청 사이마다 일부러 넣은 대기 시간이 있습니다:

```python
time.sleep(random.randrange(0, 10))     # 요청 한 번마다 0~10초 대기
...
time.sleep(random.randrange(20, 100))   # 도시가 바뀔 때마다 20~100초 대기
```

이건 버그가 아니라 **의도된 동작**입니다. AccuWeather는 짧은 시간에 요청이 몰리면
접속을 차단합니다. 이 대기시간을 줄이거나 지우면 크롤링 도중 막힐 가능성이 높습니다.

전체 30개 도시 × 최대 36일 × (도시당 최소 20~100초 대기) 기준으로 계산하면 **총 실행 시간이
수 시간 단위**가 됩니다. 그래서:

- **처음 동작 확인은 반드시 `weather_cities`를 1~2개 도시, `weather_end_day`를 2~3 정도로
  줄여서** 실행하세요. (4-2에서 만든 Variable 값만 바꾸면 됨, 코드 수정 불필요)
- 전체 30개 도시로 돌리는 건 이 DAG가 정상 동작하는 걸 확인한 뒤, 실제 스케줄(매주 목요일
  새벽 5시)에 맡기는 걸 권장합니다.

## 6. 실행 방법

### 웹 UI
1. `http://localhost:8080` 접속
2. DAG 목록에서 `weather_forecast_crawler` 찾기
3. 왼쪽 토글을 켜서 활성화 (기본적으로 꺼진 상태로 배포됨)
4. 오른쪽 ▶(Trigger DAG) 버튼 클릭
5. DAG 이름 클릭 → Graph 뷰에서 `crawl_weather`, `save_to_db` 박스 색으로 진행상황 확인
   (연한 초록: 실행 중, 진한 초록: 성공, 빨강: 실패)
6. 박스 클릭 → **Logs** 탭에서 실시간 로그 확인 가능 (몇 번째 도시/며칠째 크롤링 중인지 출력됨)

### CLI (터미널)
```bash
docker exec airflow-apiserver airflow dags trigger weather_forecast_crawler
```

## 7. 실패했을 때 확인할 것

- **Connection 관련 에러** (`Connection refused`, `Login failed` 등): 4-1에서 등록한
  `weather_mssql` Connection의 host/port/계정/비밀번호 확인. `mssql` 컨테이너가 떠있는지도
  `docker compose ps` (mssql 폴더에서)로 확인.
- **크롤링 결과가 이상함 / 필드 파싱 에러**: AccuWeather가 웹페이지의 HTML 구조를 바꿨을
  가능성이 있습니다 (원본 코드도 특정 CSS class 이름에 의존하고 있어서 사이트 개편에 취약합니다).
  이 경우 `crawler.py`의 `_parse_temp`, `_parse_rain`이 찾는 HTML 태그/클래스명을 실제
  페이지 구조에 맞게 다시 확인해야 합니다.
- **`Variable weather_cities does not exist`**: 4-2의 Variable 등록을 안 했을 때 나는 에러.
