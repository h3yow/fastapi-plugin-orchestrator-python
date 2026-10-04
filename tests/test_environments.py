"""多环境配置隔离：环境层生效、回落来源可解释、切换不串扰。"""

from __future__ import annotations

import pytest

from orchestrator.config import ConfigKeySpec, ConfigSource
from orchestrator.errors import FailureReason, UnknownEnvironmentError
from orchestrator.orchestrator import Orchestrator
from orchestrator.registry import ModuleRegistry

from .conftest import FakeModule, log_case


def _positive_int(value):
    iv = int(value)
    if iv <= 0:
        raise ValueError("必须为正整数")
    return iv


SPECS = [
    ConfigKeySpec("workers", 2, _positive_int),
    ConfigKeySpec("mode", "dev-default"),
]

ENV_CONFIGS = {
    "dev": {"a": {"workers": 2}},
    "prod": {"a": {"workers": 10, "mode": "prod-mode"}},
}


def _orchestrator(environment: str | None, **kwargs) -> tuple[FakeModule, Orchestrator]:
    a = FakeModule("a", config_specs=SPECS)
    registry = ModuleRegistry()
    registry.register(a)
    kwargs.setdefault("env", {})
    kwargs.setdefault("service_config", {"a": {"workers": 4, "mode": "svc-mode"}})
    orch = Orchestrator(
        registry,
        environment=environment,
        environment_configs=ENV_CONFIGS,
        **kwargs,
    )
    return a, orch


def _effective(orch: Orchestrator, mid: str) -> dict:
    return {k: (rv.value, rv.source.value) for k, rv in orch.effective_config(mid).items()}


def test_environment_layer_overrides_service_and_falls_back():
    a, orch = _orchestrator("prod")
    orch.start_all()
    final = _effective(orch, "a")
    log_case(
        "环境层生效与回落",
        {"environment": "prod", "env_config": ENV_CONFIGS["prod"], "service": {"workers": 4, "mode": "svc-mode"}},
        "prod 显式配置的键来自 environment 层；未配置的键回落到下层，来源可解释",
        final,
    )
    # prod 环境显式覆盖，优先于 service 层
    assert final["workers"] == (10, ConfigSource.ENVIRONMENT.value)
    assert final["mode"] == ("prod-mode", ConfigSource.ENVIRONMENT.value)
    assert a.received_config == {"workers": 10, "mode": "prod-mode"}


def test_environment_fallback_source_is_explainable():
    a, orch = _orchestrator("dev")
    orch.start_all()
    final = _effective(orch, "a")
    log_case(
        "回落来源可查询",
        {"environment": "dev", "env_config": ENV_CONFIGS["dev"]},
        "dev 未配置 mode -> 回落 service 层；workers 被 dev 覆盖",
        final,
    )
    assert final["workers"] == (2, ConfigSource.ENVIRONMENT.value)
    # dev 环境没有 mode，明确回落到 service 层且来源可查
    assert final["mode"] == ("svc-mode", ConfigSource.SERVICE.value)


def test_environments_are_isolated_and_switchable():
    a, orch = _orchestrator("dev")
    orch.start_all()
    dev_state = _effective(orch, "a")

    orch.switch_environment("prod")
    prod_state = _effective(orch, "a")
    log_case(
        "环境切换隔离",
        {"dev": ENV_CONFIGS["dev"], "prod": ENV_CONFIGS["prod"]},
        "切到 prod 后只生效 prod 的值，dev 的值不串入；切回 dev 还原",
        {"dev": dev_state, "prod": prod_state},
    )
    assert prod_state["workers"] == (10, ConfigSource.ENVIRONMENT.value)
    assert prod_state["mode"] == ("prod-mode", ConfigSource.ENVIRONMENT.value)
    assert a.received_config == {"workers": 10, "mode": "prod-mode"}

    orch.switch_environment("dev")
    back = _effective(orch, "a")
    # 切回 dev：prod 的 mode 不串扰，dev 未配置的项回落到 service
    assert back == dev_state
    assert back["mode"] == ("svc-mode", ConfigSource.SERVICE.value)
    assert a.received_config == {"workers": 2, "mode": "svc-mode"}


def test_switch_to_unknown_environment_rejected():
    a, orch = _orchestrator("dev")
    orch.start_all()
    before = _effective(orch, "a")

    with pytest.raises(UnknownEnvironmentError) as exc:
        orch.switch_environment("staging")
    log_case(
        "未知环境拒绝",
        {"target": "staging", "known": sorted(ENV_CONFIGS)},
        "来源不明的环境一律拒绝，当前环境与配置不受影响",
        {"environment": orch.environment, "config": before},
    )
    assert exc.value.reason is FailureReason.UNKNOWN_ENVIRONMENT
    assert orch.environment == "dev"
    assert _effective(orch, "a") == before
    assert a.received_config == {"workers": 2, "mode": "svc-mode"}


def test_unknown_environment_rejected_at_construction():
    with pytest.raises(UnknownEnvironmentError) as exc:
        _orchestrator("nowhere")
    log_case(
        "构造期未知环境",
        {"environment": "nowhere"},
        "启动前即拒绝，不进入半配置状态",
        exc.value.reason,
    )
    assert exc.value.reason is FailureReason.UNKNOWN_ENVIRONMENT


def test_switch_to_current_environment_is_idempotent():
    a, orch = _orchestrator("dev")
    orch.start_all()
    orch.switch_environment("dev")
    log_case(
        "切换幂等",
        {"current": "dev", "target": "dev"},
        "切到当前环境是空操作，不触发重载",
        {"reconfigure_count": a.reconfigure_count},
    )
    assert a.reconfigure_count == 0
    assert orch.environment == "dev"


def test_env_var_still_beats_environment_layer():
    a, orch = _orchestrator("prod", env={"ORCH_WORKERS": "16"})
    orch.start_all()
    final = _effective(orch, "a")
    log_case(
        "层优先级保持",
        {"environment": "prod", "env_var": {"ORCH_WORKERS": "16"}},
        "env(环境变量) > environment(部署环境) > service > default",
        final,
    )
    assert final["workers"] == (16, ConfigSource.ENV.value)
    assert final["mode"] == ("prod-mode", ConfigSource.ENVIRONMENT.value)
