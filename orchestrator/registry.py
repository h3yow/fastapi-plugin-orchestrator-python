"""模块注册表：声明、去重与查询。"""

from __future__ import annotations

from .spec import Module


class ModuleRegistry:
    """保存 module_id -> 模块实例 的映射，拒绝重复注册。"""

    def __init__(self) -> None:
        self._modules: dict[str, Module] = {}

    def register(self, module: Module) -> None:
        """注册模块；重复注册同一 module_id 抛 DuplicateRegistrationError。"""
        raise NotImplementedError

    def get(self, module_id: str) -> Module:
        """按 id 取模块；不存在抛 ModuleNotFoundError。"""
        raise NotImplementedError

    def all(self) -> dict[str, Module]:
        """返回全部已注册模块的副本。"""
        raise NotImplementedError
