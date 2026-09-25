"""内置示例模块：core（基础能力提供者）。"""
from __future__ import annotations

from app.base import ModuleBase
from app.spec import ConfigField, ModuleSpec, Version

SPEC = ModuleSpec(
    module_id="core",
    version=Version(1, 2, 0),
    provides=("logging", "eventbus"),
    config_schema={
        "log_level": ConfigField(str, "INFO"),
        "queue_size": ConfigField(int, 128),
    },
    legacy_keys={"loglevel": "log_level"},
)


class CoreModule(ModuleBase):
    def on_start(self) -> None:
        assert self.config is not None
        self.log_level = self.config.values["log_level"]

    def on_stop(self) -> None:
        pass
