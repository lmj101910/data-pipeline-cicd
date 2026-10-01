# Airflow 데이터 파이프라인 아키텍처

> PPT 제작용 소스 문서. DAG 개별 로직은 상세히 다루지 않고, 전체 구성/아키텍처 위주로 정리.

---

## 1. 배경 및 목적

- 기존: Windows 작업 스케줄러로 돌리던 독립 Python 스크립트(크롤링 + DB 적재)
- 전환 목표: **Airflow로 스케줄링을 중앙화**하고, 실패 감지·재시도·이력 관리·자격증명 분리를 표준화

---

## 2. 전체 구성 개요

Docker Compose 기반으로 하나의 스택 안에 여러 서비스를 분리 실행 (CeleryExecutor 구조).

```
                        ┌─────────────┐
        외부 접속  ───▶ │    Nginx    │  (리버스 프록시, 80 포트)
                        └──────┬──────┘
                               │
                        ┌──────▼──────────┐
                        │ airflow-apiserver│  (웹 UI / REST API)
                        └──────┬──────────┘
                               │
        ┌──────────────────────┼───────────────────────┐
        │                      │                        │
┌───────▼────────┐   ┌─────────▼─────────┐   ┌──────────▼─────────┐
│ airflow-scheduler│   │ airflow-dag-processor│  │ airflow-triggerer │
│ (스케줄 판단)     │   │ (DAG 파일 파싱)       │  │ (비동기 대기 처리)  │
└───────┬────────┘   └────────────────────┘   └────────────────────┘
        │
   ┌────▼────┐        ┌──────────────┐
   │  Redis  │◀──────▶│ airflow-worker│  (실제 task 실행 - Celery)
   │ (큐/브로커)│        └──────┬───────┘
   └─────────┘                │
                        ┌──────▼──────┐
                        │  Postgres   │  (Airflow 자체 메타DB: 실행이력/Connection/Variable)
                        └─────────────┘
```

- 모든 `airflow-*` 서비스는 **동일한 커스텀 이미지**(`custom-airflow:3.1.7`)를 공유
- Postgres(메타DB)는 **Airflow 운영 정보 전용**이며, 파이프라인이 다루는 실제 비즈니스 데이터를 저장하는 곳이 아님 (5번 항목 참고)

---

## 3. 이미지 빌드 구조

- Base: `apache/airflow:3.1.7` (공식 이미지)
- `Dockerfile`에서 두 단계로 커스터마이징
  1. `USER root` 상태에서 시스템 패키지 추가 (예: Selenium 크롤링에 필요한 Chromium/chromedriver)
  2. `USER airflow`로 전환 후 `requirements.txt` 기준 Python 패키지 설치 (DB 드라이버, selenium, openpyxl 등)
- **하나의 이미지를 모든 서비스가 공유** — apiserver든 worker든 별도 이미지를 안 만들고 동일한 런타임 환경 보장

---

## 4. 코드 구조 — 관심사 분리

```
airflow/
├── dags/           오케스트레이션 정의만 (스케줄, task 순서, 의존관계)
│   └── <pipeline>/
│       ├── <pipeline>_dag.py
│       └── README.md
└── plugins/        실제 비즈니스 로직 (크롤링, 데이터 가공)
    └── <pipeline>_common/
        ├── crawler.py
        └── processor.py
```

- Airflow는 시작 시점에 `plugins/` 폴더를 파이썬 `sys.path`에 자동으로 등록 (`airflow.plugins_manager`가 처리)
- 그 덕분에 DAG 파일에서 상대경로 없이 `from <pipeline>_common.crawler import ...` 형태로 바로 import 가능
- **DAG 파일 = "언제/어떤 순서로"만 정의, plugins = "무엇을 어떻게"** 를 정의 → DAG는 짧고 가독성 유지, 로직은 독립적으로 관리

---

## 5. 자격증명 / 설정 분리

| 항목 | 기존 방식 | Airflow 전환 후 |
|---|---|---|
| DB 접속정보 (호스트/계정/비밀번호) | 코드에 하드코딩된 `config.py` | **Connection** (암호화되어 메타DB에 저장, 코드에는 Connection ID만 존재) |
| 실행 시 조정값 (대상 목록, 기간, 경로 등) | 코드 안에 상수로 박혀있음 | **Variable** (코드 수정 없이 값만 교체 가능) |
| SQL Injection 취약점 (문자열 조합 쿼리) | `f"WHERE ID = '{value}'"` | 파라미터 바인딩(`%s`)으로 대체 |

→ 코드와 "코드가 참조하는 값"을 완전히 분리. 배포된 코드를 건드리지 않고 운영 중 설정 변경 가능.

---

## 6. 외부 데이터 저장소와의 관계

- **Airflow 자체 Postgres**: Airflow 운영 메타데이터 전용 (DAG 실행 이력, Connection, Variable, 사용자 계정 등)
- **실제 파이프라인 데이터**: 별도로 구성된 외부 DB에 저장 (예: MSSQL, 필요 시 별도 Postgres 인스턴스)
- 각 외부 DB는 **독립된 Docker Compose 프로젝트**로 분리 운영 (Airflow 스택과 생명주기 분리)
- Airflow는 어디까지나 **오케스트레이션 레이어**이고, 데이터 저장은 별도 시스템의 책임 — 역할 분리

---

## 7. 배포 환경 (dev / prod)

- 개발: RHEL 8.4 / 운영: RHEL 7.9 — OS 버전 차이 존재
- **원칙: `docker-compose.yaml`, `Dockerfile`은 환경 구분 없이 하나로 유지**
- 환경별 차이는 `.env` 파일로만 분리 (도메인, 계정 UID, 비밀번호 등)
- 오래된 OS/커널 호환성 이슈가 발견되면, 특정 서비스에 한해 안전한 옵션을 추가하는 방식으로 대응 (예: 구형 커널 대응 옵션 1건 추가) — 이 역시 파일은 그대로 유지하고 새 환경에서도 부작용 없이 동작하도록 처리
- `.env` 변경은 컨테이너 재생성(`docker compose up -d`)이 필요하며, 단순 재시작(`restart`)으로는 반영되지 않음 (환경변수는 컨테이너 생성 시점에 고정되는 구조적 특성)

---

## 8. 배포 전 검증 절차

1. **모듈 import 스모크 테스트**: DB 접속/네트워크 없이, 코드가 문법 오류나 누락된 패키지 없이 로드되는지만 확인
2. **`airflow dags list-import-errors`**: 스케줄러가 DAG 파일을 정상적으로 인식하는지 확인 (Broken DAG 사전 방지)
3. 크롤링 대상 사이트의 HTML 구조 변경까지는 자동화 테스트로 보증하지 않음 — 실제 크롤링 정합성은 수동 트리거(`airflow tasks test`)로 확인하는 것을 원칙으로 함

---

## 9. 현재 구현된 파이프라인 (참고)

동일한 아키텍처 패턴(**크롤링 → 가공 → DB 적재**, 2단계 task 구성)을 따르는 예시 구현 2건 운영 중. 개별 로직 상세는 각 DAG 폴더의 README 참고.

- `weather_forecast_crawler`
- `foot_traffic_crawler`

---

## 10. 운영 시 유의사항 (공통 규칙)

- **타임존**: Airflow는 내부적으로 모든 시각을 UTC로 저장. 스케줄 자체는 지정한 타임존(KST) 기준으로 정확히 동작하지만, 실행 결과의 날짜를 비즈니스 로직에 쓸 경우 명시적으로 타임존 변환 필요
- **알림**: SMTP Connection 등록만으로 task 실패 시 이메일 알림 가능 (`apache-airflow-providers-smtp`, 코드 변경 최소화)
- **크롤링 안정성**: 외부 사이트 구조 변경에 대한 근본적 해결책은 없음 — 실패 시 로그 기반으로 빠르게 원인 파악할 수 있도록 각 단계별 로그를 남기는 것을 코드 컨벤션으로 유지
