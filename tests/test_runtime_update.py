"""运行期配置调整：热更新、校验拒绝、级联回滚、幂等与并发一致性。"""

from __future__ import annotations

import threading

import pytest

from orchestrator.config import ConfigKeySpec, ConfigSource
from orchestrator.errors import (
    AmbiguousConfigOverrideError,
    ConfigUpdateError,
    FailureReason,
    InvalidConfigValueError,
    ModuleNotRunningError,
    UnknownConfigKeyError,
)
from orchestrator.orchestrator import ModuleState, Orchestrator
from orchestrator.registry import ModuleRegistry
from orchestrator.spec import Dependency

from .conftest import FakeModule, log_case


def _positive_int(value):
    iv = int(value)
    if iv <= 0:
        raise ValueError("必须为正整数")
    return iv


WORKERS_SPEC = [ConfigKeySpec("workers", 2, _positive_int, legacy_keys=("worker_count",))]


def _orchestrator(*modules: FakeModule, **kwargs) -> Orchestrator:
    kwargs.setdefault("env", {})
    registry = ModuleRegistry()
    for m in modules:
        registry.register(m)
    return Orchestrator(registry, **kwargs)


def _effective(orch: Orchestrator, mid: str) -> dict:
    return {k: (rv.value, rv.source.value) for k, rv in orch.effective_config(mid).items()}


def test_hot_update_success_and_source_visible():
    a = FakeModule("a", config_specs=WORKERS_SPEC)
    orch = _orchestrator(
        a, service_config={"a": {"workers": 3}}, env={"ORCH_WORKERS": "8"}
    )
    orch.start_all()
    assert a.received_config == {"workers": 8}  # env 层生效

    record = orch.update_config("a", {"workers": 5}, request_id="req-hot-1")
    final = _effective(orch, "a")
    log_case(
        "热更新成功",
        {"overrides": {"workers": 5}, "env": {"ORCH_WORKERS": "8"}},
        "runtime 层优先级最高，更新后来源可查询",
        final,
    )
    assert record.success and record.failed_step is None
    assert a.received_config == {"workers": 5}
    assert a.reconfigure_count == 1
    assert final["workers"] == (5, ConfigSource.RUNTIME.value)
    last = orch.last_update("a")
    assert last is not None and last.success and last.request_id == "req-hot-1"


def test_update_with_legacy_key_migrates():
    a = FakeModule("a", config_specs=WORKERS_SPEC)
    orch = _orchestrator(a)
    orch.start_all()

    orch.update_config("a", {"worker_count": 6})
    final = _effective(orch, "a")
    log_case(
        "热更新旧键迁移",
        {"overrides": {"worker_count": 6}},
        "旧键归一到 workers，来源记为 runtime",
        final,
    )
    assert final["workers"] == (6, ConfigSource.RUNTIME.value)


@pytest.mark.parametrize(
    "overrides,exc_type,reason",
    [
        ({"workers": -1}, InvalidConfigValueError, FailureReason.INVALID_CONFIG_VALUE),
        ({"surprise": 1}, UnknownConfigKeyError, FailureReason.UNKNOWN_CONFIG_KEY),
        (
            {"workers": 4, "worker_count": 5},
            AmbiguousConfigOverrideError,
            FailureReason.AMBIGUOUS_CONFIG_OVERRIDE,
        ),
    ],
    ids=["非法值", "未知键", "新旧键冲突"],
)
def test_invalid_update_rejected_and_old_config_intact(overrides, exc_type, reason):
    a = FakeModule("a", config_specs=WORKERS_SPEC)
    orch = _orchestrator(a, service_config={"a": {"workers": 3}})
    orch.start_all()

    with pytest.raises(exc_type) as exc:
        orch.update_config("a", overrides, request_id=f"req-bad-{reason.value}")
    final = _effective(orch, "a")
    last = orch.last_update("a")
    log_case(
        "校验失败拒绝",
        {"overrides": overrides},
        f"reason={reason.value}，旧配置完整生效，reconfigure 未被调用",
        {"final": final, "failed_step": last.failed_step if last else None},
    )
    assert exc.value.reason is reason
    # 旧配置分毫未动：值、来源、模块读到的配置都保持原样
    assert final["workers"] == (3, ConfigSource.SERVICE.value)
    assert a.received_config == {"workers": 3}
    assert a.reconfigure_count == 0
    # 失败结果可查询：卡在哪一步
    assert last is not None
    assert not last.success
    assert last.failed_step == "validate:a"
    assert last.detail


def test_update_rejected_when_module_not_running():
    a = FakeModule("a", config_specs=WORKERS_SPEC)
    orch = _orchestrator(a)

    with pytest.raises(ModuleNotRunningError) as exc:
        orch.update_config("a", {"workers": 5})
    log_case(
        "未运行拒绝调整",
        {"state": orch.status("a").state.value},
        "运行期调整只针对运行中模块",
        exc.value.reason,
    )
    assert exc.value.reason is FailureReason.MODULE_NOT_RUNNING


def test_cascade_updates_dependents_in_dependency_order():
    a = FakeModule("a", config_specs=WORKERS_SPEC)
    b = FakeModule("b", deps=(Dependency("a"),))
    c = FakeModule("c", deps=(Dependency("b"),))
    d = FakeModule("d")  # 与 a 无关，不应被级联
    orch = _orchestrator(a, b, c, d)
    orch.start_all()

    record = orch.update_config("a", {"workers": 9})
    log_case(
        "依赖级联更新",
        {"target": "a", "overrides": {"workers": 9}},
        "a 的运行中传递依赖方 b、c 按依赖序一起重载，d 不受影响",
        {"affected": record.affected, "reconfigure": {"b": b.reconfigure_count, "c": c.reconfigure_count, "d": d.reconfigure_count}},
    )
    assert record.success
    assert record.affected == ("a", "b", "c")
    assert b.reconfigure_count == 1 and c.reconfigure_count == 1
    assert d.reconfigure_count == 0
    # 级联批次内每个模块都能查到同一条调整记录
    assert orch.last_update("b") is record
    assert orch.last_update("c") is record


def test_cascade_rollback_on_apply_failure():
    a = FakeModule("a", config_specs=WORKERS_SPEC)
    b = FakeModule("b", deps=(Dependency("a"),), reconfigure_failures_remaining=1)
    c = FakeModule("c", deps=(Dependency("b"),))
    orch = _orchestrator(a, b, c, service_config={"a": {"workers": 3}})
    orch.start_all()
    before = {m.spec.module_id: dict(m.received_config) for m in (a, b, c)}

    with pytest.raises(ConfigUpdateError) as exc:
        orch.update_config("a", {"workers": 9})

    after = {m.spec.module_id: dict(m.received_config) for m in (a, b, c)}
    last = orch.last_update("a")
    log_case(
        "级联回滚",
        {"target": "a", "overrides": {"workers": 9}, "b": "重载注入失败一次"},
        "b 应用失败 -> a、b 逆序回滚到旧配置，c 未被触及；整批无半新半旧",
        {"before": before, "after": after, "failed_step": last.failed_step},
    )
    assert exc.value.reason is FailureReason.CONFIG_UPDATE_FAILED
    # 整批回到改动前的一致状态
    assert after == before
    assert all(
        orch.status(mid).state is ModuleState.RUNNING for mid in ("a", "b", "c")
    )
    # a 应用后回滚、b 失败后被恢复、c 从未被应用
    assert a.reconfigure_count == 2
    assert b.reconfigure_count == 2
    assert c.reconfigure_count == 0
    # 生效配置仍是旧值，来源不变
    assert _effective(orch, "a")["workers"] == (3, ConfigSource.SERVICE.value)
    assert last is not None and not last.success
    assert last.failed_step == "apply:b"


def test_duplicate_request_id_is_idempotent():
    a = FakeModule("a", config_specs=WORKERS_SPEC)
    orch = _orchestrator(a)
    orch.start_all()

    first = orch.update_config("a", {"workers": 7}, request_id="req-dup")
    second = orch.update_config("a", {"workers": 7}, request_id="req-dup")
    log_case(
        "重复提交幂等",
        {"request_id": "req-dup", "提交次数": 2},
        "同一 request_id 直接返回首次结果，不重复生效",
        {"reconfigure_count": a.reconfigure_count},
    )
    assert first is second
    assert a.reconfigure_count == 1
    assert _effective(orch, "a")["workers"] == (7, ConfigSource.RUNTIME.value)


def test_concurrent_updates_settle_to_serializable_result():
    a = FakeModule("a", config_specs=WORKERS_SPEC)
    orch = _orchestrator(a)
    orch.start_all()

    submitted = {f"req-{i}": 100 + i for i in range(8)}
    errors: list[BaseException] = []

    def submit(req_id: str, value: int) -> None:
        try:
            orch.update_config("a", {"workers": value}, request_id=req_id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=submit, args=(req_id, value))
        for req_id, value in submitted.items()
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final_value = orch.effective_config("a")["workers"].value
    last = orch.last_update("a")
    log_case(
        "并发调整一致",
        {"并发请求": submitted},
        "互斥锁串行化：最终态等价于某串行顺序，最后一次提交的值生效",
        {"final": final_value, "last_request": last.request_id if last else None},
    )
    assert not errors
    # 最终值必为某一次提交的值（无混杂、无丢失更新导致的中间态）
    assert final_value in submitted.values()
    # 最后记录的调整与最终生效值一致：状态唯一且可解释
    assert last is not None and last.success
    assert submitted[last.request_id] == final_value
    # 每次提交都完整生效过一次（串行执行，无重复无副作用）
    assert a.reconfigure_count == len(submitted)


def test_concurrent_duplicate_request_applied_once():
    a = FakeModule("a", config_specs=WORKERS_SPEC)
    orch = _orchestrator(a)
    orch.start_all()

    errors: list[BaseException] = []

    def submit() -> None:
        try:
            orch.update_config("a", {"workers": 42}, request_id="req-shared")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=submit) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log_case(
        "并发重复提交",
        {"request_id": "req-shared", "线程数": 6},
        "同一 request_id 并发提交只生效一次且不报错",
        {"reconfigure_count": a.reconfigure_count, "errors": len(errors)},
    )
    assert not errors
    assert a.reconfigure_count == 1
    assert orch.effective_config("a")["workers"].value == 42
