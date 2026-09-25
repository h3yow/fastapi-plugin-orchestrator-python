"""内置示例模块：metrics（依赖 core 的能力）。"""
from __future__ import annotations

from app.base import ModuleBase
from app.spec import ConfigField, Dependency, ModuleSpec, Version

SPEC = ModuleSpec(
    module_id="metrics",
    version=Version(0, 3, 1),
    provides=("metrics",),
    dependencies=(Dependency("core", min_version=Version(1, 0, 0),
                             capabilities=("eventbus",)),),
    config_schema={
        "interval_seconds": ConfigField(int, 60),
        "enabled": ConfigField(bool, True),
    },
)


class MetricsModule(ModuleBase):
    def on_start(self) -> None:
        assert self.config is not None
        self.interval = self.config.values["interval_seconds"]

    def on_stop(self) -> None:
        pass
