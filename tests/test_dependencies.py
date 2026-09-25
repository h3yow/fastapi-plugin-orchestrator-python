"""依赖解析：环、缺失、版本/能力不兼容、稳定顺序。"""

from __future__ import annotations

import pytest

from orchestrator.dependencies import resolve_load_order
from orchestrator.errors import (
    CapabilityIncompatibleError,
    DependencyCycleError,
    FailureReason,
    MissingDependencyError,
    VersionIncompatibleError,
)
from orchestrator.spec import Dependency, Version

from .conftest import FakeModule, log_case


def test_stable_unique_load_order():
    modules = {
        "c": FakeModule("c", deps=(Dependency("a"),)),
        "b": FakeModule("b", deps=(Dependency("a"),)),
        "a": FakeModule("a"),
    }
    order1 = resolve_load_order(modules)
    order2 = resolve_load_order(dict(reversed(list(modules.items()))))
    log_case("稳定加载顺序", list(modules), "拓扑排序+字典序打破平局", order1)
    assert order1 == ["a", "b", "c"]
    assert order1 == order2  # 与注册顺序无关，结果唯一


def test_missing_dependency_rejected():
    modules = {"a": FakeModule("a", deps=(Dependency("ghost"),))}
    with pytest.raises(MissingDependencyError) as exc:
        resolve_load_order(modules)
    log_case("缺失依赖", "a -> ghost", "依赖未注册即拒绝", exc.value.reason)
    assert exc.value.reason is FailureReason.MISSING_DEPENDENCY
    assert exc.value.context["dependency"] == "ghost"


def test_version_incompatible_rejected():
    modules = {
        "a": FakeModule("a", version="2.5.0"),
        "b": FakeModule(
            "b",
            deps=(Dependency("a", max_version_exclusive=Version(2, 0, 0)),),
        ),
    }
    with pytest.raises(VersionIncompatibleError) as exc:
        resolve_load_order(modules)
    log_case("版本不兼容", "b 要求 a<2.0.0，实际 2.5.0", "版本区间校验", exc.value.reason)
    assert exc.value.reason is FailureReason.VERSION_INCOMPATIBLE
    assert exc.value.context["actual"] == "2.5.0"


def test_capability_incompatible_rejected():
    modules = {
        "a": FakeModule("a", capabilities=frozenset({"x"})),
        "b": FakeModule(
            "b", deps=(Dependency("a", required_capabilities=frozenset({"y"})),)
        ),
    }
    with pytest.raises(CapabilityIncompatibleError) as exc:
        resolve_load_order(modules)
    log_case("能力不兼容", "b 要求能力 y，a 仅有 x", "能力集合校验", exc.value.reason)
    assert exc.value.reason is FailureReason.CAPABILITY_INCOMPATIBLE
    assert exc.value.context["missing_capabilities"] == ["y"]


def test_dependency_cycle_rejected():
    modules = {
        "a": FakeModule("a", deps=(Dependency("b"),)),
        "b": FakeModule("b", deps=(Dependency("c"),)),
        "c": FakeModule("c", deps=(Dependency("a"),)),
    }
    with pytest.raises(DependencyCycleError) as exc:
        resolve_load_order(modules)
    log_case("依赖环", "a->b->c->a", "拓扑排序剩余节点即环成员", exc.value.context)
    assert exc.value.reason is FailureReason.DEPENDENCY_CYCLE
    assert set(exc.value.context["cycle_members"]) == {"a", "b", "c"}


def test_distinct_reasons_are_distinguishable():
    """四类失败原因互不相同，可程序化区分。"""
    reasons = {
        FailureReason.MISSING_DEPENDENCY,
        FailureReason.VERSION_INCOMPATIBLE,
        FailureReason.CAPABILITY_INCOMPATIBLE,
        FailureReason.DEPENDENCY_CYCLE,
    }
    log_case("失败原因可区分", "四类依赖失败", "reason 枚举互异", sorted(r.value for r in reasons))
    assert len(reasons) == 4
