"""模块注册表：声明、去重与查询。"""

from __future__ import annotations

from .errors import DuplicateRegistrationError, ModuleNotFoundError
from .spec import Module


class ModuleRegistry:
    """保存 module_id -> 模块实例 的映射，拒绝重复注册。"""

    def __init__(self) -> None:
        self._modules: dict[str, Module] = {}

    def register(self, module: Module) -> None:
        """注册模块；重复注册同一 module_id 抛 DuplicateRegistrationError。"""
        module_id = module.spec.module_id
        if module_id in self._modules:
            raise DuplicateRegistrationError(
                f"模块重复注册: {module_id}",
                context={"module_id": module_id},
            )
        self._modules[module_id] = module

    def get(self, module_id: str) -> Module:
        """按 id 取模块；不存在抛 ModuleNotFoundError。"""
        try:
            return self._modules[module_id]
        except KeyError:
            raise ModuleNotFoundError(
                f"模块未注册: {module_id}",
                context={"module_id": module_id},
            ) from None

    def all(self) -> dict[str, Module]:
        """返回全部已注册模块的副本。"""
        return dict(self._modules)
