"""외부 API 공통 HTTP 클라이언트 (재시도/백오프, JSON 파싱, data.go.kr 에러 처리)."""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)


class Http:
    def __init__(self, timeout: int = 30, retries: int = 3, backoff: float = 1.5):
        self.s = requests.Session()
        self.timeout, self.retries, self.backoff = timeout, retries, backoff

    def _request(self, method: str, url: str, **kw) -> requests.Response:
        last_exc: Optional[Exception] = None
        for i in range(self.retries):
            try:
                r = self.s.request(method, url, timeout=self.timeout, **kw)
                if r.status_code >= 500 or r.status_code == 429:
                    raise requests.HTTPError(f"{r.status_code} {r.text[:200]}")
                r.raise_for_status()
                return r
            except Exception as e:  # noqa: BLE001
                last_exc = e
                wait = self.backoff ** i
                log.warning("요청 실패(%d/%d) %s -> %.1fs 후 재시도: %s", i + 1, self.retries, url, wait, e)
                time.sleep(wait)
        raise RuntimeError(f"요청 최종 실패: {url}") from last_exc

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None) -> Any:
        r = self._request("GET", url, params=params, headers=headers)
        return _parse_json(r)

    def post_json(self, url: str, body: dict, headers: dict | None = None) -> Any:
        r = self._request("POST", url, json=body, headers=headers)
        return _parse_json(r)

    def get_text(self, url: str, params: dict | None = None) -> str:
        return self._request("GET", url, params=params).text


def _parse_json(r: requests.Response) -> Any:
    text = r.text.strip()
    if text.startswith("<"):
        # data.go.kr 은 인증 오류 등을 XML 로 반환 (SERVICE_KEY_IS_NOT_REGISTERED_ERROR 등)
        raise RuntimeError(f"JSON 대신 XML/HTML 응답 (인증키/활용신청 확인): {text[:300]}")
    return r.json()


def datago_items(payload: dict) -> list[dict]:
    """공공데이터포털 표준 응답 -> items 리스트 (0건/단건 처리 포함)."""
    resp = payload.get("response", {})
    header = resp.get("header", {})
    code = str(header.get("resultCode", ""))
    if code not in ("00", "0", ""):
        raise RuntimeError(f"data.go.kr 오류 {code}: {header.get('resultMsg')}")
    body = resp.get("body", {}) or {}
    items = body.get("items", {}) or {}
    if isinstance(items, str):
        return []
    item = items.get("item", [])
    if isinstance(item, dict):
        return [item]
    return item or []
