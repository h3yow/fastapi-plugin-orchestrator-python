"""模块注册表：发现、注册与依赖排序。"""
from __future__ import annotations

from collections.abc import Callable

from app.errors import (
    CyclicDependencyError,
    DuplicateRegistrationError,
    IncompatibleDependencyError,
    MissingDependencyError,
)
from app.base import ModuleBase
from app.spec import ModuleSpec

ModuleFactory = Callable[[], ModuleBase]


class ModuleRegistry:
    """保存模块声明与其工厂，负责依赖校验与拓扑排序。"""

    def __init__(self) -> None:
        self._specs: dict[str, ModuleSpec] = {}
        self._factories: dict[str, ModuleFactory] = {}

    def register(self, spec: ModuleSpec, factory: ModuleFactory) -> None:
        """注册模块；同一标识重复注册且声明与工厂均相同则幂等，否则报错。"""
        existing = self._specs.get(spec.module_id)
        if existing is not None:
            if existing == spec and self._factories[spec.module_id] is factory:
                return  # 幂等：重复注册不产生重复副作用
            raise DuplicateRegistrationError(
                f"模块 {spec.module_id!r} 已注册且声明不一致"
            )
        self._specs[spec.module_id] = spec
        self._factories[spec.module_id] = factory

    def get(self, module_id: str) -> ModuleSpec:
        return self._specs[module_id]

    def factory_of(self, module_id: str) -> ModuleFactory:
        return self._factories[module_id]

    def module_ids(self) -> list[str]:
        return sorted(self._specs)

    def validate(self) -> None:
        """校验缺失依赖、版本/能力兼容性（循环检测在排序时进行）。"""
        for spec in self._specs.values():
            for dep in spec.dependencies:
                target = self._specs.get(dep.module_id)
                if target is None:
                    raise MissingDependencyError(
                        f"模块 {spec.module_id!r} 依赖未注册的模块 {dep.module_id!r}"
                    )
                if dep.min_version is not None and not target.version.compatible_with(
                    dep.min_version
                ):
                    raise IncompatibleDependencyError(
                        f"模块 {spec.module_id!r} 需要 {dep.module_id!r} "
                        f">= {dep.min_version}，实际为 {target.version}"
                    )
                missing_caps = set(dep.capabilities) - set(target.provides)
                if missing_caps:
                    raise IncompatibleDependencyError(
                        f"模块 {spec.module_id!r} 需要 {dep.module_id!r} 提供能力 "
                        f"{sorted(missing_caps)}，实际提供 {sorted(target.provides)}"
                    )

    def resolve_load_order(self) -> list[str]:
        """按依赖关系返回唯一且稳定的拓扑序（同层按模块标识字典序）。"""
        self.validate()
        indegree: dict[str, int] = {mid: 0 for mid in self._specs}
        dependents: dict[str, list[str]] = {mid: [] for mid in self._specs}
        for spec in self._specs.values():
            for dep in spec.dependencies:
                indegree[spec.module_id] += 1
                dependents[dep.module_id].append(spec.module_id)
        # Kahn 算法，就绪集合始终取字典序最小者，保证顺序唯一且稳定
        ready = sorted(mid for mid, deg in indegree.items() if deg == 0)
        order: list[str] = []
        while ready:
            mid = ready.pop(0)
            order.append(mid)
            for nxt in dependents[mid]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    # 插入并保持 ready 有序
                    i = 0
                    while i < len(ready) and ready[i] < nxt:
                        i += 1
                    ready.insert(i, nxt)
        if len(order) != len(self._specs):
            remaining = sorted(mid for mid, deg in indegree.items() if deg > 0)
            raise CyclicDependencyError(
                f"模块依赖存在环，涉及模块: {remaining}"
            )
        return order
