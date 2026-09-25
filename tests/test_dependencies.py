"""依赖关系：环、缺失、版本/能力不兼容、稳定顺序与幂等注册。"""
import logging

import pytest

from app.errors import (
    CyclicDependencyError,
    DuplicateRegistrationError,
    IncompatibleDependencyError,
    MissingDependencyError,
)
from app.registry import ModuleRegistry
from app.spec import Dependency, Version
from tests.helpers import make_spec, recording_factory

log = logging.getLogger(__name__)


def test_stable_unique_load_order():
    events: list[str] = []
    reg = ModuleRegistry()
    reg.register(make_spec("c", deps=(Dependency("a"), Dependency("b"))),
                 recording_factory(events, "c"))
    reg.register(make_spec("b", deps=(Dependency("a"),)),
                 recording_factory(events, "b"))
    reg.register(make_spec("a"), recording_factory(events, "a"))
    order1 = reg.resolve_load_order()
    order2 = reg.resolve_load_order()
    log.info("输入: c->[a,b], b->[a], a->[]; 两次排序: %s / %s", order1, order2)
    assert order1 == order2 == ["a", "b", "c"], "拓扑序必须唯一且稳定"


def test_missing_dependency_rejected():
    reg = ModuleRegistry()
    reg.register(make_spec("x", deps=(Dependency("ghost"),)),
                 recording_factory([], "x"))
    with pytest.raises(MissingDependencyError) as exc:
        reg.resolve_load_order()
    log.info("输入: x 依赖未注册的 ghost; 拒绝原因: %s", exc.value)
    assert "ghost" in str(exc.value)


def test_incompatible_version_rejected():
    reg = ModuleRegistry()
    reg.register(make_spec("lib", version="1.2.0"), recording_factory([], "lib"))
    reg.register(make_spec("app", deps=(Dependency("lib", min_version=Version(2, 0, 0)),)),
                 recording_factory([], "app"))
    with pytest.raises(IncompatibleDependencyError) as exc:
        reg.resolve_load_order()
    log.info("输入: app 需要 lib>=2.0.0, 实际 1.2.0; 拒绝原因: %s", exc.value)


def test_missing_capability_rejected():
    reg = ModuleRegistry()
    reg.register(make_spec("store", provides=("kv",)), recording_factory([], "store"))
    reg.register(make_spec("cache", deps=(Dependency("store", capabilities=("kv", "ttl")),)),
                 recording_factory([], "cache"))
    with pytest.raises(IncompatibleDependencyError) as exc:
        reg.resolve_load_order()
    log.info("输入: cache 需要能力 ttl, store 仅提供 kv; 拒绝原因: %s", exc.value)
    assert "ttl" in str(exc.value)


def test_cyclic_dependency_rejected():
    reg = ModuleRegistry()
    reg.register(make_spec("m", deps=(Dependency("n"),)), recording_factory([], "m"))
    reg.register(make_spec("n", deps=(Dependency("m"),)), recording_factory([], "n"))
    with pytest.raises(CyclicDependencyError) as exc:
        reg.resolve_load_order()
    log.info("输入: m<->n 互相依赖; 拒绝原因: %s", exc.value)
    assert "m" in str(exc.value) and "n" in str(exc.value)


def test_duplicate_registration_idempotent_or_rejected():
    events: list[str] = []
    reg = ModuleRegistry()
    spec = make_spec("a")
    factory = recording_factory(events, "a")
    reg.register(spec, factory)
    reg.register(spec, factory)  # 完全一致 -> 幂等，无副作用
    log.info("重复注册同一声明: 幂等接受, 注册表=%s", reg.module_ids())
    assert reg.module_ids() == ["a"]
    with pytest.raises(DuplicateRegistrationError) as exc:
        reg.register(make_spec("a", version="2.0.0"), factory)
    log.info("重复注册不同声明: 拒绝原因: %s", exc.value)


def test_failure_kinds_are_distinguishable():
    """缺失/不兼容/循环三类失败原因必须可区分（不同异常类型）。"""
    kinds = set()
    reg = ModuleRegistry()
    reg.register(make_spec("x", deps=(Dependency("ghost"),)), recording_factory([], "x"))
    try:
        reg.resolve_load_order()
    except Exception as e:
        kinds.add(type(e))
    reg2 = ModuleRegistry()
    reg2.register(make_spec("m", deps=(Dependency("n"),)), recording_factory([], "m"))
    reg2.register(make_spec("n", deps=(Dependency("m"),)), recording_factory([], "n"))
    try:
        reg2.resolve_load_order()
    except Exception as e:
        kinds.add(type(e))
    log.info("失败类型集合: %s", {k.__name__ for k in kinds})
    assert kinds == {MissingDependencyError, CyclicDependencyError}
