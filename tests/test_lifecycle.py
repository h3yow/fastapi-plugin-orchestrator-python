"""生命周期：失败回滚、依赖保护、幂等与并发一致性。"""

from __future__ import annotations

import threading

import pytest

from orchestrator.errors import (
    DependencyUnavailableError,
    FailureReason,
    ModuleInitError,
)
from orchestrator.orchestrator import ModuleState, Orchestrator
from orchestrator.registry import ModuleRegistry
from orchestrator.spec import Dependency

from .conftest import FakeModule, log_case


def _orchestrator(*modules: FakeModule) -> Orchestrator:
    registry = ModuleRegistry()
    for m in modules:
        registry.register(m)
    return Orchestrator(registry, env={})


def _states(orch: Orchestrator) -> dict[str, str]:
    return {mid: s.state.value for mid, s in orch.statuses().items()}


def test_init_failure_rolls_back_and_blocks_dependents():
    a = FakeModule("a")
    b = FakeModule("b", deps=(Dependency("a"),), fail_on_init=True)
    c = FakeModule("c", deps=(Dependency("b"),))
    orch = _orchestrator(a, b, c)

    with pytest.raises(ModuleInitError) as exc:
        orch.start_all()

    final = _states(orch)
    log_case(
        "初始化失败回滚",
        "a 正常, b 注入失败, c 依赖 b",
        "b 失败 -> 回滚已启动的 a，c 不启动",
        final,
    )
    assert exc.value.reason is FailureReason.MODULE_INIT_FAILED
    # a 被回滚停止，无半生效状态
    assert final["a"] == ModuleState.STOPPED.value
    assert a.stop_count == 1
    # b 标记失败且原因可查询
    assert final["b"] == ModuleState.FAILED.value
    assert orch.status("b").failure_reason is FailureReason.MODULE_INIT_FAILED
    # 依赖 b 的 c 从未启动
    assert final["c"] == ModuleState.REGISTERED.value
    assert c.init_count == 0


def test_duplicate_start_and_stop_are_idempotent():
    a = FakeModule("a")
    b = FakeModule("b", deps=(Dependency("a"),))
    orch = _orchestrator(a, b)

    orch.start_all()
    orch.start_all()
    orch.start_module("a")
    log_case(
        "重复启动幂等",
        "start_all x2 + start_module(a)",
        "已运行模块跳过启动",
        {"a.init": a.init_count, "b.init": b.init_count},
    )
    assert a.init_count == 1 and b.init_count == 1

    orch.stop_all()
    orch.stop_all()
    orch.stop_module("a")
    log_case(
        "重复停止幂等",
        "stop_all x2 + stop_module(a)",
        "非运行模块的停止是空操作",
        {"a.stop": a.stop_count, "b.stop": b.stop_count},
    )
    assert a.stop_count == 1 and b.stop_count == 1
    assert _states(orch) == {"a": "stopped", "b": "stopped"}


def test_stop_rejected_while_dependent_running():
    a = FakeModule("a")
    b = FakeModule("b", deps=(Dependency("a"),))
    orch = _orchestrator(a, b)
    orch.start_all()

    with pytest.raises(DependencyUnavailableError) as exc:
        orch.stop_module("a")
    log_case(
        "停止被依赖模块",
        "b 运行中时 stop_module(a)",
        "存在运行中的依赖方，拒绝停止",
        _states(orch),
    )
    assert exc.value.reason is FailureReason.DEPENDENCY_UNAVAILABLE
    assert orch.status("a").state is ModuleState.RUNNING

    orch.stop_module("b")
    orch.stop_module("a")
    assert _states(orch) == {"a": "stopped", "b": "stopped"}


def test_concurrent_start_stop_is_race_free_and_consistent():
    modules = [FakeModule(f"m{i}") for i in range(6)]
    # 加一条依赖链，放大并发下的状态迁移窗口
    modules.append(FakeModule("tail", deps=(Dependency("m0"),)))
    orch = _orchestrator(*modules)

    errors: list[BaseException] = []

    def run(fn):
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - 收集线程内所有失败
            errors.append(exc)

    starters = [threading.Thread(target=run, args=(orch.start_all,)) for _ in range(6)]
    for t in starters:
        t.start()
    for t in starters:
        t.join()

    log_case(
        "并发启动",
        "6 线程同时 start_all",
        "互斥锁串行化，已运行模块幂等跳过",
        _states(orch),
    )
    assert not errors
    assert all(s.state is ModuleState.RUNNING for s in orch.statuses().values())
    # 并发下每个模块恰好初始化一次：无重复副作用
    assert all(m.init_count == 1 for m in modules)

    stoppers = [threading.Thread(target=run, args=(orch.stop_all,)) for _ in range(6)]
    for t in stoppers:
        t.start()
    for t in stoppers:
        t.join()

    log_case(
        "并发停止",
        "6 线程同时 stop_all",
        "互斥锁串行化，非运行模块停止为空操作",
        _states(orch),
    )
    assert not errors
    assert all(s.state is ModuleState.STOPPED for s in orch.statuses().values())
    assert all(m.stop_count == 1 for m in modules)


def test_concurrent_start_and_stop_settles_to_serializable_result():
    """start 与 stop 并发交错时，最终状态必为某一串行顺序的结果。"""
    a = FakeModule("a")
    b = FakeModule("b", deps=(Dependency("a"),))
    orch = _orchestrator(a, b)

    errors: list[BaseException] = []
    threads = [
        threading.Thread(target=lambda: orch.start_all()),
        threading.Thread(target=lambda: orch.stop_all()),
        threading.Thread(target=lambda: orch.start_all()),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = _states(orch)
    log_case(
        "启停交错",
        "start/stop/start 三线程并发",
        "锁内串行化 -> 最终态等价于某串行执行",
        final,
    )
    # 最终要么全部运行（stop 先于最后一次 start），要么全部停止（stop 最后）
    assert final in (
        {"a": "running", "b": "running"},
        {"a": "stopped", "b": "stopped"},
    )
    # 无论哪种结果，init/stop 次数都保持成对，无半生效状态
    assert a.init_count == a.stop_count + (1 if final["a"] == "running" else 0)
    assert b.init_count == b.stop_count + (1 if final["b"] == "running" else 0)


def test_config_sources_visible_in_status():
    from orchestrator.config import ConfigKeySpec

    class ConfiguredModule(FakeModule):
        def config_specs(self):
            return [ConfigKeySpec("workers", 2, lambda v: int(v))]

    a = ConfiguredModule("a")
    registry = ModuleRegistry()
    registry.register(a)
    orch = Orchestrator(
        registry, service_config={"a": {"workers": 5}}, env={}
    )
    orch.start_all()

    status = orch.status("a")
    log_case(
        "配置来源可查询",
        {"service": {"a": {"workers": 5}}},
        "生效配置来源记录到状态",
        {k: v.value for k, v in status.config_sources.items()},
    )
    assert a.received_config == {"workers": 5}
    assert status.config_sources["workers"].value == "service"
