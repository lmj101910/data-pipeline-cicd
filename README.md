# Data Pipeline CI/CD

Apache Airflow + GitLab CE + MSSQL 기반 사내망 데이터 파이프라인 CI/CD 환경입니다.

---

## 전체 아키텍처

```
사내망
├── GitLab 서버
│   └── GitLab CE 18.11.5          # 소스 코드 관리 / CI 파이프라인 정의
│
├── Runner 서버
│   └── GitLab Runner v18.11.5     # CI 작업 실행 (GitLab 서버와 분리)
│
└── 데이터 서버
    ├── Airflow 3.1.7               # 데이터 파이프라인 오케스트레이션
    └── MSSQL 2022                  # 데이터 소스 (개발용 SQL Server)
```

---

## 프로젝트 구조

```
data-pipeline-cicd/
├── gitlab/
│   ├── docker-compose.yaml        # GitLab CE 18.11.5
│   ├── .env                       # 실제 값 (gitignore)
│   └── .env.example               # 환경변수 템플릿
│
├── runner/
│   ├── docker-compose.yaml        # GitLab Runner v18.11.5
│   ├── .env                       # 실제 값 (gitignore)
│   └── .env.example               # 환경변수 템플릿
│
├── airflow/
│   ├── docker-compose.yaml        # Airflow 3.1.7 + CeleryExecutor
│   ├── Dockerfile                 # 커스텀 이미지 (providers 포함)
│   ├── requirements.txt           # MSSQL, PostgreSQL provider 패키지
│   ├── nginx/
│   │   └── airflow.conf           # Nginx 리버스 프록시 설정
│   ├── dags/                      # DAG 파일
│   ├── plugins/                   # 커스텀 플러그인
│   └── config/                    # Airflow 설정 파일
│
├── mssql/
│   ├── docker-compose.yaml        # SQL Server 2022 (Developer Edition)
│   └── .env                       # 실제 값 (gitignore)
│
├── .env                           # Airflow 환경변수 (gitignore)
├── .env.example                   # Airflow 환경변수 템플릿
└── .gitignore
```

> 각 디렉토리는 독립된 `.env` 파일을 사용합니다. 모든 `.env` 파일은 `.gitignore`에 등록되어 git에 올라가지 않습니다.

---

## 사전 요구사항

| 항목 | 최소 사양 |
|------|----------|
| CPU | 4 코어 이상 |
| RAM | 16GB 이상 (GitLab 4GB + Airflow 8GB + MSSQL 2GB) |
| 디스크 | 40GB 이상 |
| Docker | 20.10 이상 |
| Docker Compose | v2.0 이상 |

---

## 환경변수 설정

각 서비스별로 `.env.example`을 복사하여 `.env`를 생성합니다.

### GitLab

```bash
cd gitlab
cp .env.example .env
# .env에 서버 IP 및 포트 입력
```

| 변수 | 설명 | 예시 |
|------|------|------|
| `GITLAB_HOSTNAME` | GitLab 서버 IP 또는 도메인 | `192.168.1.100` |
| `GITLAB_HTTP_PORT` | HTTP 접속 포트 | `8929` |
| `GITLAB_SSH_PORT` | SSH 접속 포트 | `2222` |

### GitLab Runner

```bash
cd runner
cp .env.example .env
# .env에 GitLab URL과 Runner 토큰 입력
```

| 변수 | 설명 | 예시 |
|------|------|------|
| `GITLAB_URL` | GitLab 서버 주소 (gitlab/.env와 일치) | `http://192.168.1.100:8929` |
| `RUNNER_TOKEN` | GitLab Admin > CI/CD > Runners에서 발급 | - |

### Airflow

```bash
cp .env.example .env
# .env에 실제 키 값 입력
```

| 변수 | 설명 |
|------|------|
| `AIRFLOW_UID` | Airflow 프로세스 UID (기본값: 50000) |
| `AIRFLOW_DOMAIN` | Airflow 접속 도메인 |
| `AIRFLOW__CORE__FERNET_KEY` | DB 저장 민감정보 암호화 키 |
| `AIRFLOW_WWW_USER_USERNAME` | 관리자 계정 아이디 |
| `AIRFLOW_WWW_USER_PASSWORD` | 관리자 계정 비밀번호 |
| `AIRFLOW__WEBSERVER__SECRET_KEY` | Flask 세션 서명 키 |
| `AIRFLOW__API_AUTH__JWT_SECRET` | Worker ↔ API 서버 내부 JWT 인증 키 |

키 생성 명령어:

```bash
# Fernet 키
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# SECRET_KEY / JWT Secret
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### MSSQL

```bash
cd mssql
# .env에 SA 비밀번호 입력 (대문자+소문자+숫자+특수문자, 8자 이상 필수)
```

| 변수 | 설명 |
|------|------|
| `MSSQL_SA_PASSWORD` | SQL Server SA 계정 비밀번호 |

---

## GitLab

### 실행

```bash
cd gitlab
docker compose up -d
```

### 초기화 확인

```bash
# Reconfigured! 메시지 확인 (약 2~3분 소요)
docker logs gitlab 2>&1 | grep "Reconfigured"
```

### 접속

```
http://<GITLAB_HOSTNAME>:<GITLAB_HTTP_PORT>
```

### 초기 계정

```bash
# root 초기 비밀번호 확인 (24시간 후 자동 삭제됨)
docker exec gitlab grep 'Password:' /etc/gitlab/initial_root_password
```

- **아이디:** `root`
- **비밀번호:** 위 명령으로 확인 후 즉시 변경 권장

### 포트 구성

| 용도 | 포트 |
|------|------|
| HTTP | `GITLAB_HTTP_PORT` (기본: 8929) |
| SSH | `GITLAB_SSH_PORT` (기본: 2222) |

---

## GitLab Runner

Runner는 **별도 서버**에서 운영합니다. 한 대의 Runner 서버가 여러 프로젝트의 CI 작업을 처리할 수 있습니다.

### 실행

```bash
cd runner
docker compose up -d
```

### Runner 등록

GitLab에서 먼저 Runner 토큰을 발급합니다:
`GitLab Admin > CI/CD > Runners > New instance runner`

발급된 토큰을 `runner/.env`의 `RUNNER_TOKEN`에 입력 후 등록합니다:

```bash
docker exec gitlab-runner gitlab-runner register \
  --non-interactive \
  --url "${GITLAB_URL}" \
  --token "${RUNNER_TOKEN}" \
  --executor "docker" \
  --docker-image "alpine:latest" \
  --description "shared-runner"
```

### Runner 태그 활용

파이프라인에서 특정 Runner만 선택하려면 태그를 사용합니다:

```yaml
# .gitlab-ci.yml
build-job:
  tags:
    - python
  script:
    - python build.py
```

---

## Airflow

Apache Airflow **3.1.7** + **CeleryExecutor** 기반 분산 처리 구성입니다.

### 실행

```bash
cd airflow
docker compose --env-file ../.env up -d
```

> `--env-file ../.env`를 반드시 명시해야 합니다. 누락 시 JWT 인증 실패가 발생합니다.

### 초기화 확인

```bash
# airflow-init이 Exited (0)으로 종료되면 정상
docker compose ps airflow-init
```

### 접속

| 방법 | URL |
|------|-----|
| Nginx (도메인) | `http://airflow.local` |
| 직접 접속 | `http://localhost:8080` |

> `http://airflow.local` 사용 시 `/etc/hosts`에 `127.0.0.1 airflow.local` 등록 필요

### 서비스 구성

| 서비스 | 역할 |
|--------|------|
| `postgres` | Airflow 메타데이터 DB |
| `redis` | Celery 브로커 |
| `airflow-init` | DB 마이그레이션 및 관리자 계정 생성 (1회 후 종료) |
| `airflow-apiserver` | Web UI 및 REST API (포트 8080) |
| `airflow-scheduler` | DAG 스케줄링 |
| `airflow-dag-processor` | DAG 파일 파싱 |
| `airflow-worker` | Celery 태스크 실행 |
| `airflow-triggerer` | Deferrable Operator 처리 |
| `nginx` | 리버스 프록시 (airflow.local → 8080) |

### 주요 명령어

```bash
# 서비스 상태 확인
docker compose ps

# 로그 실시간 확인
docker compose logs -f airflow-apiserver

# 중지 (데이터 유지)
docker compose down

# 데이터까지 삭제 (완전 초기화)
docker compose down -v
```

### DAG 추가

`airflow/dags/` 디렉토리에 Python 파일을 추가하면 `dag-processor`가 자동으로 인식합니다.

---

## MSSQL

개발용 SQL Server 2022 (Developer Edition, 무료) 환경입니다.

### 실행

```bash
cd mssql
docker compose up -d
```

### 접속 정보

| 항목 | 값 |
|------|-----|
| 호스트 | `localhost` |
| 포트 | `1433` |
| 계정 | `sa` |
| 비밀번호 | `mssql/.env`의 `MSSQL_SA_PASSWORD` |

---

## 네트워크 구성

각 서비스는 독립된 Docker 네트워크를 사용합니다:

| 네트워크 | 포함 서비스 |
|---------|-----------|
| `gitlab_network` | GitLab |
| `runner_network` | GitLab Runner |
| `airflow_network` | Airflow 전체 스택 |

---

## Dev Containers (VSCode)

DAG 개발 시 자동완성 및 타입 힌트를 사용하려면 Dev Containers를 활용합니다.

**사전 조건:** VSCode `Dev Containers` 확장 설치, Airflow 실행 중

1. `data-pipeline-cicd/` 폴더를 VSCode로 열기
2. `Cmd+Shift+P` → `Dev Containers: Reopen in Container`
3. `airflow-apiserver` 컨테이너 내부로 진입

나올 때: `Cmd+Shift+P` → `Dev Containers: Reopen Folder Locally`

---

## 트러블슈팅

### JWT 인증 실패 (Signature verification failed)

**증상:** 태스크가 `queued` 상태에서 멈추고 Worker 로그에 `Invalid auth token` 출력

**원인:** `AIRFLOW__API_AUTH__JWT_SECRET` 미설정 시 컨테이너마다 랜덤 키 생성 → Worker와 api-server 서명 불일치

**해결:**
```bash
# .env에 JWT Secret 추가 후 재시작
docker compose down && docker compose --env-file ../.env up -d
```

### 502 Bad Gateway (Airflow Nginx)

**원인:** Nginx가 캐시한 api-server IP가 컨테이너 재시작 후 변경됨

**해결:** `airflow/nginx/airflow.conf`에 `resolver 127.0.0.11` 설정으로 자동 해결됨

### GitLab 접속 불가

**확인 사항:**
- `gitlab/.env`의 `GITLAB_HOSTNAME`이 실제 서버 IP와 일치하는지 확인
- 초기화 완료 여부 확인: `docker logs gitlab 2>&1 | grep "Reconfigured"`

---

## 참고 링크

- [Apache Airflow 공식 문서](https://airflow.apache.org/docs/)
- [GitLab Docker 공식 문서](https://docs.gitlab.com/ee/install/docker.html)
- [GitLab Runner 공식 문서](https://docs.gitlab.com/runner/)
