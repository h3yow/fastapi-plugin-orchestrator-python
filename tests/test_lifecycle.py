"""生命周期：失败回滚、依赖连带、幂等启停与并发一致性。"""
import logging
import threading

import pytest

from app.config import ConfigResolver
from app.orchestrator import Orchestrator, ModuleState
from app.registry import ModuleRegistry
from app.spec import Dependency
from tests.helpers import make_spec, recording_factory

log = logging.getLogger(__name__)


def _orc(*registrations) -> Orchestrator:
    reg = ModuleRegistry()
    for spec, factory in registrations:
        reg.register(spec, factory)
    return Orchestrator(reg, ConfigResolver())


def test_failed_start_rolls_back_without_partial_state():
    events: list[str] = []
    orc = _orc(
        (make_spec("a"), recording_factory(events, "a")),
        (make_spec("b", deps=(Dependency("a"),)), recording_factory(events, "b")),
        (make_spec("c", deps=(Dependency("b"),)), recording_factory(events, "c", fail=True)),
    )
    with pytest.raises(RuntimeError):
        orc.start_all()
    log.info("输入: c 启动失败; 决策: 回滚本次已启动的 a,b; 事件: %s; 最终状态: %s",
             events, orc.snapshot())
    assert events == ["start:a", "start:b", "start:c", "stop:b", "stop:a"]
    assert orc.state_of("c") is ModuleState.FAILED
    assert orc.state_of("a") is ModuleState.STOPPED
    assert orc.state_of("b") is ModuleState.STOPPED
    assert "c 初始化失败" in (orc.failure_reason("c") or "")


def test_dependents_never_start_when_dependency_fails():
    events: list[str] = []
    orc = _orc(
        (make_spec("base"), recording_factory(events, "base", fail=True)),
        (make_spec("top", deps=(Dependency("base"),)), recording_factory(events, "top")),
    )
    with pytest.raises(RuntimeError):
        orc.start_all()
    log.info("输入: base 失败; 决策: top 不得启动; 事件: %s; 最终状态: %s",
             events, orc.snapshot())
    assert "start:top" not in events
    assert orc.state_of("top") is ModuleState.REGISTERED


def test_start_stop_idempotent_no_duplicate_side_effects():
    events: list[str] = []
    orc = _orc((make_spec("a"), recording_factory(events, "a")))
    orc.start_module("a")
    orc.start_module("a")
    orc.start_all()
    orc.stop_module("a")
    orc.stop_module("a")
    log.info("重复启停后事件序列: %s; 最终状态: %s", events, orc.snapshot())
    assert events == ["start:a", "stop:a"]


def test_stop_module_stops_dependents_first():
    events: list[str] = []
    orc = _orc(
        (make_spec("a"), recording_factory(events, "a")),
        (make_spec("b", deps=(Dependency("a"),)), recording_factory(events, "b")),
        (make_spec("c", deps=(Dependency("b"),)), recording_factory(events, "c")),
    )
    orc.start_all()
    events.clear()
    orc.stop_module("a")
    log.info("停止 a 的事件: %s; 最终状态: %s", events, orc.snapshot())
    assert events == ["stop:c", "stop:b", "stop:a"]
    assert orc.snapshot() == {"a": "stopped", "b": "stopped", "c": "stopped"}


def test_effective_config_and_source_queryable():
    from app.spec import ConfigField
    spec = make_spec("svc", config_schema={"n": ConfigField(int, 1)})
    orc = _orc((spec, recording_factory([], "svc")))
    orc._resolver = ConfigResolver(env={"APP_SVC__N": "42"})
    orc.start_module("svc")
    cfg = orc.effective_config("svc")
    log.info("生效配置: %s 来源: %s", cfg.values, cfg.sources)
    assert cfg.values["n"] == 42 and cfg.sources["n"] == "env"


def test_concurrent_start_stop_is_race_free_and_consistent():
    events: list[str] = []
    orc = _orc(
        (make_spec("a"), recording_factory(events, "a")),
        (make_spec("b", deps=(Dependency("a"),)), recording_factory(events, "b")),
    )
    log.info("输入: 3 线程并发 start_module(b) 与 stop_module(a) 各 50 次")

    def start_worker():
        for _ in range(50):
            try:
                orc.start_module("b")
            except Exception:
                pass

    def stop_worker():
        for _ in range(50):
            try:
                orc.stop_module("a")
            except Exception:
                pass

    threads = [threading.Thread(target=start_worker),
               threading.Thread(target=stop_worker),
               threading.Thread(target=start_worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snap = orc.snapshot()
    log.info("决策依据: 所有迁移串行化; 最终状态: %s", snap)
    # 一致性不变式：b 运行当且仅当 a 运行（b 依赖 a）
    assert (snap["a"] == "running") == (snap["b"] == "running"), snap
    # 无中间态泄漏
    assert set(snap.values()) <= {"running", "stopped", "registered"}


def test_concurrent_start_same_module_single_effect():
    events: list[str] = []
    orc = _orc((make_spec("a"), recording_factory(events, "a")))
    threads = [threading.Thread(target=orc.start_module, args=("a",))
               for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.info("10 线程并发启动同一模块, start 事件: %s; 最终状态: %s",
             [e for e in events if e == "start:a"], orc.snapshot())
    assert events.count("start:a") == 1
    assert orc.state_of("a") is ModuleState.RUNNING
