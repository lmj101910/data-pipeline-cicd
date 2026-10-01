"""공통 유틸: 설정 로드, .env 로드, 주 단위 캘린더 헬퍼."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")


def load_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else ROOT / "config.yaml"
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for k, v in cfg["paths"].items():
        p = ROOT / v
        p.mkdir(parents=True, exist_ok=True)
        cfg["paths"][k] = p
    return cfg


def load_env(path: str | Path | None = None) -> None:
    """.env 의 KEY=VALUE 를 os.environ 에 주입 (이미 설정된 값은 유지)."""
    path = Path(path) if path else ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_env(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        raise RuntimeError(f"환경변수 {name} 가 비어 있습니다. .env.example 참고하여 .env 작성 필요.")
    return v


def week_start(dates: pd.Series | pd.DatetimeIndex) -> pd.Series | pd.DatetimeIndex:
    """월요일 시작 주 (week_start)."""
    d = pd.to_datetime(dates)
    if isinstance(d, pd.DatetimeIndex):
        return (d - pd.to_timedelta(d.weekday, unit="D")).normalize()
    return (d - pd.to_timedelta(d.dt.weekday, unit="D")).dt.normalize()


def iso_week(dates: pd.Series) -> pd.Series:
    return pd.to_datetime(dates).dt.isocalendar().week.astype(int)


def iso_year(dates: pd.Series) -> pd.Series:
    return pd.to_datetime(dates).dt.isocalendar().year.astype(int)
