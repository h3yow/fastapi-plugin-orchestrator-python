"""依赖解析：兼容性校验与稳定拓扑排序。"""

from __future__ import annotations

from .spec import Module


def resolve_load_order(modules: dict[str, Module]) -> list[str]:
    """按依赖关系返回唯一且稳定的加载顺序（module_id 列表）。

    校验规则（启动阶段拒绝）：
    - 依赖的模块未注册 -> MissingDependencyError
    - 版本不在兼容区间 -> VersionIncompatibleError
    - 被依赖方未提供所需能力 -> CapabilityIncompatibleError
    - 存在依赖环 -> DependencyCycleError

    稳定性：同一输入集合下，按 module_id 字典序打破平局，保证结果唯一。
    """
    raise NotImplementedError
