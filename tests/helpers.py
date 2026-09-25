"""测试辅助：可记录启停事件的模块工厂。"""
from __future__ import annotations

from app.base import ModuleBase
from app.spec import Dependency, ModuleSpec, Version


def make_spec(module_id: str, version: str = "1.0.0",
              deps: tuple[Dependency, ...] = (),
              provides: tuple[str, ...] = (), **kwargs) -> ModuleSpec:
    return ModuleSpec(module_id=module_id, version=Version.parse(version),
                      dependencies=deps, provides=provides, **kwargs)


def recording_factory(events: list[str], module_id: str, fail: bool = False):
    class _Module(ModuleBase):
        def on_start(self) -> None:
            events.append(f"start:{module_id}")
            if fail:
                raise RuntimeError(f"{module_id} 初始化失败")

        def on_stop(self) -> None:
            events.append(f"stop:{module_id}")

    return _Module
