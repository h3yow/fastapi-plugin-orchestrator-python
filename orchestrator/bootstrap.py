"""默认装配：构建一个带示例模块的编排器，供服务启动时使用。"""

from __future__ import annotations

import logging
from typing import Any

from .config import ConfigKeySpec
from .orchestrator import Orchestrator
from .registry import ModuleRegistry
from .spec import Dependency, ModuleContext, ModuleSpec, Version

logger = logging.getLogger("orchestrator.bootstrap")


def _positive_int(value: Any) -> int:
    iv = int(value)
    if iv <= 0:
        raise ValueError("必须为正整数")
    return iv


class CoreModule:
    """示例基础模块：声明一个带旧键的配置项。"""

    def __init__(self) -> None:
        self._spec = ModuleSpec(
            module_id="core",
            version=Version(1, 0, 0),
            provides_capabilities=frozenset({"config", "logging"}),
        )
        self.config: dict[str, Any] = {}

    @property
    def spec(self) -> ModuleSpec:
        return self._spec

    def config_specs(self) -> list[ConfigKeySpec]:
        return [
            ConfigKeySpec(
                "workers", 2, _positive_int, legacy_keys=("worker_count",)
            ),
        ]

    def init(self, config: dict[str, Any], context: ModuleContext) -> None:
        self.config = config
        logger.info("core 初始化，生效配置: %s", config)

    def start(self) -> None:
        logger.info("core 已启动")

    def stop(self) -> None:
        logger.info("core 已停止")


class WebModule:
    """示例上层模块：依赖 core 及其 config 能力。"""

    def __init__(self) -> None:
        self._spec = ModuleSpec(
            module_id="web",
            version=Version(1, 0, 0),
            dependencies=(
                Dependency(
                    "core",
                    min_version=Version(1, 0, 0),
                    max_version_exclusive=Version(2, 0, 0),
                    required_capabilities=frozenset({"config"}),
                ),
            ),
        )

    @property
    def spec(self) -> ModuleSpec:
        return self._spec

    def init(self, config: dict[str, Any], context: ModuleContext) -> None:
        core = context.get("core")
        logger.info("web 初始化，core 可用: %s", core is not None)

    def start(self) -> None:
        logger.info("web 已启动")

    def stop(self) -> None:
        logger.info("web 已停止")


def build_default_orchestrator(
    service_config: dict[str, dict[str, Any]] | None = None,
    env: dict[str, str] | None = None,
) -> Orchestrator:
    """注册示例模块并返回编排器（尚未启动）。"""
    registry = ModuleRegistry()
    registry.register(CoreModule())
    registry.register(WebModule())
    return Orchestrator(registry, service_config=service_config, env=env)
