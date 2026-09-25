"""依赖解析：兼容性校验与稳定拓扑排序。"""

from __future__ import annotations

import heapq

from .errors import (
    CapabilityIncompatibleError,
    DependencyCycleError,
    MissingDependencyError,
    VersionIncompatibleError,
)
from .spec import Module


def _validate(modules: dict[str, Module]) -> None:
    """逐条校验依赖的存在性、版本区间与能力，失败即抛可区分异常。"""
    for module_id in sorted(modules):
        spec = modules[module_id].spec
        for dep in spec.dependencies:
            target = modules.get(dep.module_id)
            if target is None:
                raise MissingDependencyError(
                    f"模块 {module_id} 依赖未注册的模块 {dep.module_id}",
                    context={"module_id": module_id, "dependency": dep.module_id},
                )
            target_spec = target.spec
            if not dep.accepts(target_spec.version):
                raise VersionIncompatibleError(
                    f"模块 {module_id} 要求 {dep.module_id} 版本在 "
                    f"[{dep.min_version}, {dep.max_version_exclusive})，"
                    f"实际为 {target_spec.version}",
                    context={
                        "module_id": module_id,
                        "dependency": dep.module_id,
                        "required_min": str(dep.min_version),
                        "required_max_exclusive": str(dep.max_version_exclusive),
                        "actual": str(target_spec.version),
                    },
                )
            missing_caps = dep.required_capabilities - target_spec.provides_capabilities
            if missing_caps:
                raise CapabilityIncompatibleError(
                    f"模块 {module_id} 要求 {dep.module_id} 提供能力 "
                    f"{sorted(missing_caps)}，实际仅提供 "
                    f"{sorted(target_spec.provides_capabilities)}",
                    context={
                        "module_id": module_id,
                        "dependency": dep.module_id,
                        "missing_capabilities": sorted(missing_caps),
                    },
                )


def resolve_load_order(modules: dict[str, Module]) -> list[str]:
    """按依赖关系返回唯一且稳定的加载顺序（module_id 列表）。

    校验规则（启动阶段拒绝）：
    - 依赖的模块未注册 -> MissingDependencyError
    - 版本不在兼容区间 -> VersionIncompatibleError
    - 被依赖方未提供所需能力 -> CapabilityIncompatibleError
    - 存在依赖环 -> DependencyCycleError

    稳定性：Kahn 算法 + 最小堆按 module_id 字典序打破平局，结果唯一。
    """
    _validate(modules)

    # indegree[m] = m 的依赖个数；dependents[d] = 依赖 d 的模块集合
    indegree: dict[str, int] = {mid: 0 for mid in modules}
    dependents: dict[str, list[str]] = {mid: [] for mid in modules}
    for mid, module in modules.items():
        for dep in module.spec.dependencies:
            indegree[mid] += 1
            dependents[dep.module_id].append(mid)

    heap = [mid for mid, deg in indegree.items() if deg == 0]
    heapq.heapify(heap)
    order: list[str] = []
    while heap:
        mid = heapq.heappop(heap)
        order.append(mid)
        for dependent in dependents[mid]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(heap, dependent)

    if len(order) != len(modules):
        remaining = sorted(mid for mid, deg in indegree.items() if deg > 0)
        raise DependencyCycleError(
            f"检测到依赖环，涉及模块: {remaining}",
            context={"cycle_members": remaining},
        )
    return order
