"""分层配置：优先级、旧键迁移、严格校验。"""

from __future__ import annotations

import pytest

from orchestrator.config import ConfigKeySpec, ConfigSource, LayeredConfig
from orchestrator.errors import (
    AmbiguousConfigOverrideError,
    FailureReason,
    InvalidConfigValueError,
    UnknownConfigKeyError,
)

from .conftest import log_case


def _positive_int(value):
    iv = int(value)
    if iv <= 0:
        raise ValueError("必须为正整数")
    return iv


@pytest.fixture
def layered() -> LayeredConfig:
    return LayeredConfig(
        [
            ConfigKeySpec("workers", 2, _positive_int, legacy_keys=("worker_count",)),
            ConfigKeySpec("mode", "dev"),
        ]
    )


def test_layer_priority(layered):
    resolved = layered.resolve(
        service_config={"workers": 4, "mode": "prod"},
        env={"ORCH_WORKERS": "8"},
    )
    final = {k: (r.value, r.source.value) for k, r in resolved.items()}
    log_case(
        "层优先级",
        {"default": {"workers": 2}, "service": {"workers": 4}, "env": {"ORCH_WORKERS": "8"}},
        "env > service > default",
        final,
    )
    assert resolved["workers"].value == 8
    assert resolved["workers"].source is ConfigSource.ENV
    assert resolved["mode"].value == "prod"
    assert resolved["mode"].source is ConfigSource.SERVICE


def test_legacy_key_migration_service_layer(layered):
    resolved = layered.resolve(service_config={"worker_count": 3}, env={})
    log_case(
        "旧键迁移(service)",
        {"worker_count": 3},
        "旧键 worker_count 归一到 workers，来源记为 service",
        (resolved["workers"].value, resolved["workers"].source.value),
    )
    assert resolved["workers"].value == 3
    assert resolved["workers"].source is ConfigSource.SERVICE


def test_legacy_key_migration_env_layer(layered):
    resolved = layered.resolve(env={"ORCH_WORKER_COUNT": "5"})
    log_case(
        "旧键迁移(env)",
        {"ORCH_WORKER_COUNT": "5"},
        "旧键环境变量归一到 workers，来源记为 env",
        (resolved["workers"].value, resolved["workers"].source.value),
    )
    assert resolved["workers"].value == 5
    assert resolved["workers"].source is ConfigSource.ENV


def test_legacy_and_new_key_consistent_is_deterministic(layered):
    resolved = layered.resolve(
        service_config={"workers": 3, "worker_count": 3}, env={}
    )
    log_case(
        "新旧键一致",
        {"workers": 3, "worker_count": 3},
        "同层同值不歧义，取该值",
        resolved["workers"].value,
    )
    assert resolved["workers"].value == 3


def test_legacy_and_new_key_conflict_rejected(layered):
    with pytest.raises(AmbiguousConfigOverrideError) as exc:
        layered.resolve(service_config={"workers": 3, "worker_count": 4}, env={})
    log_case(
        "新旧键冲突",
        {"workers": 3, "worker_count": 4},
        "同层新旧键值不一致，拒绝静默选择",
        exc.value.reason,
    )
    assert exc.value.reason is FailureReason.AMBIGUOUS_CONFIG_OVERRIDE


def test_unknown_key_rejected(layered):
    with pytest.raises(UnknownConfigKeyError) as exc:
        layered.resolve(service_config={"surprise": 1}, env={})
    log_case("未知键", {"surprise": 1}, "未声明即拒绝", exc.value.reason)
    assert exc.value.reason is FailureReason.UNKNOWN_CONFIG_KEY


def test_invalid_value_rejected_with_source(layered):
    with pytest.raises(InvalidConfigValueError) as exc:
        layered.resolve(env={"ORCH_WORKERS": "-1"})
    log_case(
        "非法值",
        {"ORCH_WORKERS": "-1"},
        "校验器拒绝非正整数，并记录来源",
        exc.value.context,
    )
    assert exc.value.reason is FailureReason.INVALID_CONFIG_VALUE
    assert exc.value.context["source"] == "env"
