"""
Airflow DAG 공통 헬퍼 (Airflow 3.3.2 Task SDK 기준).

- 프로젝트 코드(src/) 는 Airflow 워커에서 import 가능해야 함:
    * Variable `lwf_project_root` (또는 env LWF_PROJECT_ROOT) 에 프로젝트 경로 지정 -> sys.path 에 추가
    * 또는 워커 이미지에 `pip install -e /opt/airflow/lotte_stockpile_forecast` 로 설치
- API 키는 Airflow Connection 에 보관하고, 태스크 실행 시 환경변수로 브리지해 src.external.* 가 그대로 동작하게 함.

Connection 규약 (Admin > Connections):
  conn_id                type       login          password        host
  data_go_kr_api_key     http       -              일반인증키(Decoding)  apis.data.go.kr
  lwf_naver_datalab      http       Client ID      Client Secret   openapi.naver.com
  neis_api_key           http       -              인증키          open.neis.go.kr
  ecos_api_key           http       -              인증키          ecos.bok.or.kr
  sedp-dev-client-wfd    postgres   DB 유저         DB 비밀번호      DB 호스트 (Port/Schema 도 입력)  <- ERP 조회/분석 결과 적재

Variable 규약 (Admin > Variables) - JSON 1개:
  lwf_settings = {
    "project_root": "/opt/airflow/lotte_stockpile_forecast",   (필수)
    "config_path": null,                                        (선택) 미지정 시 project_root/config.yaml
    "brands": ["빼빼로", "팥빙수"],                              (선택)
    "naver_shopping_enabled": false                             (선택) 쇼핑인사이트 수집 여부
  }
  (env LWF_PROJECT_ROOT 로도 project_root 지정 가능 - 로컬 테스트용)
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

KST = "Asia/Seoul"
DEFAULT_ARGS = {
    "owner": "scm-analytics",
    "retries": 2,
    "retry_delay": timedelta(minutes=10),
    "email_on_failure": False,
}
BRANDS_DEFAULT = ["빼빼로", "팥빙수"]

# Connection -> 환경변수 (src.common.get_env / src.db 가 읽는 이름)
# 바깥쪽 키: Airflow Connection ID (DAG 의 export_keys(...) 인자와 동일).
# 안쪽 키: 환경변수 이름, 안쪽 값: Connection 의 필드명 (실제 API 키 값은 Airflow 에 저장).
CONN_ENV_MAP = {
    "data_go_kr_api_key": {"DATA_GO_KR_KEY": "password"},
    "lwf_naver_datalab": {"NAVER_CLIENT_ID": "login", "NAVER_CLIENT_SECRET": "password"},
    "neis_api_key": {"NEIS_KEY": "password"},
    "ecos_api_key": {"ECOS_KEY": "password"},
    "sedp-dev-client-wfd": {
        "PGHOST": "host",
        "PGPORT": "port",
        "PGDATABASE": "schema",
        "PGUSER": "login",
        "PGPASSWORD": "password",
    },
}


_SETTINGS: dict | None = None


def settings() -> dict:
    """Variable lwf_settings(JSON) 1회 조회 + 기본값. (태스크 실행 시점에만 호출 - 파싱 시점 Variable 접근 금지)"""
    global _SETTINGS
    if _SETTINGS is None:
        from airflow.sdk import Variable

        s = dict(Variable.get("lwf_settings", default=None, deserialize_json=True) or {})
        s.setdefault("project_root", os.environ.get("LWF_PROJECT_ROOT", ""))
        s.setdefault("config_path", None)
        s.setdefault("brands", BRANDS_DEFAULT)
        s.setdefault("naver_shopping_enabled", False)
        if not s["project_root"]:
            raise RuntimeError("Variable lwf_settings.project_root (또는 env LWF_PROJECT_ROOT) 가 필요합니다")
        _SETTINGS = s
    return _SETTINGS


def project_root() -> Path:
    """프로젝트 경로를 sys.path 에 추가하고 반환."""
    p = Path(settings()["project_root"])
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
    return p


def load_cfg() -> dict:
    project_root()
    from src.common import load_config

    return load_config(settings()["config_path"])


def export_keys(*conn_ids: str) -> None:
    """Airflow Connection 의 자격증명을 환경변수로 내보냄 (src.external, src.db 모듈 호환)."""
    from airflow.sdk import Connection

    for cid in conn_ids:
        conn = Connection.get(cid)
        for env_name, attr in CONN_ENV_MAP[cid].items():
            val = getattr(conn, attr, None)
            val = "" if val is None else str(val)  # port 등 int 속성도 환경변수(str)로 안전하게 변환
            if not val:
                raise RuntimeError(f"Connection '{cid}' 의 {attr} 가 비어 있음 ({env_name})")
            os.environ[env_name] = val


def brands() -> list[str]:
    return list(settings()["brands"])


def run_date(context: dict) -> date:
    """
    실행 기준일 (KST).  Airflow 3: run_after(3.0+) > data_interval_end > logical_date > now.
    수동 트리거(logical_date=None) 에도 안전.
    """
    import pendulum

    dr = context.get("dag_run")
    cand = getattr(dr, "run_after", None) or context.get("data_interval_end") or context.get("logical_date")
    if cand is None:
        return pendulum.now(KST).date()
    if isinstance(cand, datetime):
        return pendulum.instance(cand).in_timezone(KST).date()
    return cand


def forecast_options(context: dict) -> dict:
    """한 DAG Run에서 고정할 예측 기준일. 입력 origin > 실제 실행 시작일(KST) > run_date."""
    import pendulum

    params = context.get("params", {})
    requested = params.get("origin")
    if requested:
        origin = date.fromisoformat(requested)
    else:
        started = getattr(context.get("dag_run"), "start_date", None)
        origin = pendulum.instance(started).in_timezone(KST).date() if started else run_date(context)
    return {"origin": origin.isoformat(), "sample_mode": params.get("sample_mode", True)}


def merge_csv(new, path: Path, keys: list[str], parse_dates: list[str]) -> int:
    """기존 CSV 와 병합 (keys 기준 최신 우선). 반환: 최종 행 수."""
    import pandas as pd

    if path.exists():
        old = pd.read_csv(path, parse_dates=parse_dates)
        allf = pd.concat([old, new], ignore_index=True)
    else:
        allf = new
    allf = allf.drop_duplicates(keys, keep="last").sort_values(keys)
    allf.to_csv(path, index=False)
    return int(len(allf))
