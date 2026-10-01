"""기존 src.build import 경로 호환. 피처 처리는 src.features.build 한 곳에서 관리한다."""

from .features.build import build_feature_table, load_feature_table, load_inputs

__all__ = ["build_feature_table", "load_feature_table", "load_inputs"]
