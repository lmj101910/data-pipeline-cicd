# Data Pipeline CI/CD

Airflow + dbt + GitLab CE 기반 데이터 파이프라인 CI/CD 프로젝트입니다.

---

## Quick Start

### 1. 환경변수 설정

```bash
cp .env.example .env
# .env 파일 열어 실제 값 입력
```

### 2. hosts 파일 등록 (최초 1회)

```bash
# macOS / Linux
echo "127.0.0.1 gitlab.example.com" | sudo tee -a /etc/hosts
echo "127.0.0.1 airflow.local" | sudo tee -a /etc/hosts

# Windows (관리자 권한 PowerShell)
# Add-Content -Path "C:\Windows\System32\drivers\etc\hosts" -Value "127.0.0.1 gitlab.example.com"
# Add-Content -Path "C:\Windows\System32\drivers\etc\hosts" -Value "127.0.0.1 airflow.local"
```

### 3. GitLab 시작

```bash
cd gitlab
docker compose up -d

# 초기화 완료 확인 (Reconfigured! 뜰 때까지 대기, 약 2~3분 소요)
docker logs gitlab 2>&1 | grep "Reconfigured"
```

접속: `http://gitlab.example.com:8929`

### 4. Airflow 시작

```bash
cd airflow
docker compose --env-file ../.env up -d

# 초기화 완료 확인 (airflow-init이 Exited (0) 될 때까지 대기)
docker compose logs airflow-init
```

접속: `http://airflow.local` 또는 `http://localhost:8080`

---

## 프로젝트 구조

```
data-pipeline-cicd/
├── gitlab/
│   └── docker-compose.yaml      # GitLab CE 온프레미스
├── airflow/
│   ├── docker-compose.yaml      # Airflow 3.1.7 + CeleryExecutor
│   ├── nginx/
│   │   └── airflow.conf         # Nginx 리버스 프록시 설정
│   ├── dags/                    # DAG 파일
│   ├── plugins/                 # 커스텀 플러그인
│   ├── config/                  # Airflow 설정 파일
│   └── logs/                    # 로그 (gitignore)
├── .env                         # 실제 값 (gitignore)
├── .env.example                 # 환경변수 템플릿
└── .gitignore
```

---

## 사전 요구사항

| 항목 | 최소 사양 |
|------|----------|
| CPU | 4 코어 이상 |
| RAM | 8GB 이상 (GitLab 4GB + Airflow 4GB) |
| 디스크 | 20GB 이상 |
| Docker | 20.10 이상 |
| Docker Compose | v2.0 이상 |

---

## 환경변수 설정

`.env.example`을 복사하여 `.env` 파일을 생성합니다:

```bash
cp .env.example .env
```

`.env` 파일에 실제 값을 입력합니다:

```env
# GitLab SMTP (선택사항)
GITLAB_SMTP_USER=your_email@gmail.com
GITLAB_SMTP_PASSWORD=your_app_password

# Airflow
AIRFLOW_UID=50000
AIRFLOW_DOMAIN=airflow.local
AIRFLOW__CORE__FERNET_KEY=<fernet 키 생성 후 입력>
AIRFLOW_WWW_USER_USERNAME=admin
AIRFLOW_WWW_USER_PASSWORD=<원하는 비밀번호>
AIRFLOW__WEBSERVER__SECRET_KEY=<secret key 생성 후 입력>
AIRFLOW__API_AUTH__JWT_SECRET=<jwt secret 생성 후 입력>
```

키 생성 명령어:
```bash
# Fernet 키
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# SECRET_KEY
python3 -c "import secrets; print(secrets.token_hex(32))"

# JWT Secret
python3 -c "import secrets; from base64 import urlsafe_b64encode; print(urlsafe_b64encode(secrets.token_bytes(16)).decode().rstrip('='))"
```

> `.env` 파일은 `.gitignore`에 등록되어 git에 커밋되지 않습니다.

### 환경변수 역할

| 변수 | 역할 |
|------|------|
| `AIRFLOW__CORE__FERNET_KEY` | DB에 저장되는 민감정보 암호화 키 |
| `AIRFLOW__WEBSERVER__SECRET_KEY` | Flask 세션 서명 키 |
| `AIRFLOW__API_AUTH__JWT_SECRET` | Worker ↔ api-server 내부 JWT 인증 키 (미설정 시 컨테이너마다 랜덤 생성되어 인증 실패 발생) |

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
http://gitlab.example.com:8929
```

### 초기 계정

```bash
# root 초기 비밀번호 확인 (24시간 후 자동 삭제됨)
docker exec gitlab grep 'Password:' /etc/gitlab/initial_root_password
```

- **아이디:** `root`
- **비밀번호:** 위 명령으로 확인 후 즉시 변경 권장

### 포트 구성

| 용도 | 호스트 포트 | 컨테이너 포트 |
|------|------------|--------------|
| HTTP | 8929 | 8929 |
| HTTPS | 8443 | 443 |
| SSH | 2222 | 22 |

> GitLab Omnibus는 내장 Nginx를 사용합니다. `external_url`에 포트를 명시하면 해당 포트로 리슨하며, `ports` 매핑 양쪽을 반드시 일치시켜야 합니다.

---

## Airflow

Apache Airflow **3.1.7** 기반으로, **CeleryExecutor**를 사용한 분산 태스크 처리 구성입니다.

### 아키텍처 변경 사항 (Airflow 2.x → 3.x)

| 항목 | Airflow 2.x | Airflow 3.x |
|------|-------------|-------------|
| 웹 서버 컴포넌트 | `webserver` | `api-server` |
| 인증 방식 | 내장 FAB | `apache-airflow-providers-fab` 패키지 |
| 헬스체크 엔드포인트 | `/health` | `/api/v2/version` |
| DAG 처리 | scheduler 내장 | `dag-processor` 별도 컴포넌트 |
| 관리자 계정 생성 | `airflow users create` CLI | `_AIRFLOW_WWW_USER_CREATE` 환경변수 |
| 실행 API URL | 불필요 | `AIRFLOW__CORE__EXECUTION_API_SERVER_URL` 필수 |

### 서비스 구성

| 서비스 | 역할 | 포트 |
|--------|------|------|
| `postgres` | Airflow 메타데이터 DB (PostgreSQL 17) | - |
| `redis` | Celery 브로커 | - |
| `airflow-init` | DB 마이그레이션 및 관리자 계정 생성 (1회 후 종료) | - |
| `airflow-apiserver` | Web UI 및 REST API | 8080 |
| `airflow-scheduler` | DAG 스케줄링 | - |
| `airflow-dag-processor` | DAG 파일 파싱 및 처리 | - |
| `airflow-worker` | Celery 태스크 실행 | - |
| `airflow-triggerer` | Deferrable Operator 처리 | - |
| `nginx` | 리버스 프록시 (airflow.local → 8080) | 80 |

### 실행

```bash
cd airflow
docker compose --env-file ../.env up -d
```

`airflow-init` → `postgres`/`redis` 준비 완료 → 나머지 서비스 순서로 자동 기동됩니다.

> `--env-file ../.env`를 반드시 명시해야 합니다. 누락 시 일부 환경변수가 빈 값으로 설정되어 JWT 인증 실패가 발생합니다.

### 초기화 확인

```bash
# airflow-init이 Exited (0)으로 종료되면 정상
docker compose ps airflow-init
docker compose logs airflow-init
```

### 접속

| 방법 | URL |
|------|-----|
| Nginx (도메인) | `http://airflow.local` |
| 직접 접속 | `http://localhost:8080` |

> `http://airflow.local` 사용 시 `/etc/hosts`에 `127.0.0.1 airflow.local` 등록 필요

### 초기 계정

- **아이디:** `.env`의 `AIRFLOW_WWW_USER_USERNAME` (기본값: `admin`)
- **비밀번호:** `.env`의 `AIRFLOW_WWW_USER_PASSWORD` (기본값: `admin`)

> 운영 환경에서는 반드시 강력한 비밀번호로 변경하세요.

### DAG 추가

`airflow/dags/` 디렉토리에 Python 파일을 추가하면 `dag-processor`가 자동으로 인식합니다.

### 주요 명령어

```bash
# 서비스 상태 확인
docker compose ps

# 전체 로그 실시간 확인
docker compose logs -f

# 특정 서비스 로그 확인
docker compose logs -f airflow-apiserver
docker compose logs -f airflow-scheduler
docker compose logs -f airflow-worker

# 중지 (데이터 유지)
docker compose down

# 데이터까지 삭제 (완전 초기화)
docker compose down -v
```

### Nginx 리버스 프록시

`airflow/nginx/airflow.conf`에서 도메인 및 프록시 설정을 관리합니다.

```nginx
server {
    listen 80;
    server_name airflow.local;       # hosts 파일의 도메인과 일치
    resolver 127.0.0.11 valid=30s;   # Docker 내장 DNS (컨테이너 재시작 시 IP 변경 대응)

    location / {
        set $upstream http://airflow-apiserver:8080;
        proxy_pass $upstream;        # 변수 사용 시 매 요청마다 DNS 재조회
    }
}
```

> `resolver 127.0.0.11`은 Docker 내장 DNS 서버입니다. 컨테이너가 재시작되면 IP가 바뀌는데, 이를 주기적으로 재조회하여 502 Bad Gateway를 방지합니다.

---

## 네트워크 구성

각 서비스는 독립된 Docker 네트워크를 사용합니다:

| 네트워크 | 포함 서비스 |
|---------|-----------|
| `airflow_network` | Airflow 전체 스택 (postgres, redis, nginx, apiserver, ...) |
| `gitlab_network` | GitLab |

> GitLab과 Airflow는 현재 별도 네트워크로 분리되어 있으며, 향후 연동 시 공통 네트워크 설정이 필요합니다.

---

## Dev Containers (VSCode)

DAG 개발 시 빨간줄 없이 자동완성을 사용하려면 Dev Containers를 사용합니다.

### 사전 조건
- VSCode `Dev Containers` 확장 설치 (Microsoft 공식)
- Airflow가 실행 중이어야 함

### 사용 방법

1. `data-pipeline-cicd/` 폴더를 VSCode로 열기
2. Airflow 실행: `cd airflow && docker compose --env-file ../.env up -d`
3. `Cmd+Shift+P` → `Dev Containers: Reopen in Container`
4. `airflow-apiserver` 컨테이너 안으로 VSCode 진입 (최초 1회 extensions 자동 설치)

### 나올 때

`Cmd+Shift+P` → `Dev Containers: Reopen Folder Locally`

---

## 트러블슈팅

### JWT 인증 실패 (Signature verification failed)

**증상:** 태스크가 `queued` 상태에서 멈추고 Worker 로그에 `Invalid auth token: Signature verification failed` 출력

**원인:** `AIRFLOW__API_AUTH__JWT_SECRET` 미설정 시 컨테이너마다 랜덤 키 생성 → Worker와 api-server 서명 불일치

**해결:**
1. `.env`에 `AIRFLOW__API_AUTH__JWT_SECRET` 추가
2. `docker compose down && docker compose --env-file ../.env up -d`

### 502 Bad Gateway

**원인:** Nginx가 시작 시 캐시한 api-server IP가 컨테이너 재시작 후 변경됨

**해결:** `docker compose restart nginx` 또는 `airflow/nginx/airflow.conf`에 `resolver 127.0.0.11` 설정 (이미 적용됨)

---

## 참고 링크

- [Apache Airflow 공식 문서](https://airflow.apache.org/docs/)
- [Airflow 3.0 마이그레이션 가이드](https://airflow.apache.org/docs/apache-airflow/stable/migration-guide.html)
- [GitLab Docker 공식 문서](https://docs.gitlab.com/ee/install/docker.html)
