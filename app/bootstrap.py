"""组装注册表、配置解析器与编排器。"""
from __future__ import annotations

import os

from app.config import ConfigResolver
from app.orchestrator import Orchestrator
from app.registry import ModuleRegistry
from app.modules import core, metrics


def build_orchestrator(
    service_config: dict[str, dict[str, object]] | None = None,
    env: dict[str, str] | None = None,
) -> Orchestrator:
    """注册内置模块并返回编排器；env 缺省时读取进程环境变量。"""
    registry = ModuleRegistry()
    registry.register(core.SPEC, core.CoreModule)
    registry.register(metrics.SPEC, metrics.MetricsModule)
    resolver = ConfigResolver(
        service_config=service_config,
        env=env if env is not None else dict(os.environ),
    )
    return Orchestrator(registry, resolver)
