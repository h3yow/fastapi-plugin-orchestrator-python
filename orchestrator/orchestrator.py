"""编排器：驱动模块生命周期，保证失败回滚与并发一致。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .config import ConfigSource
from .errors import FailureReason
from .registry import ModuleRegistry


class ModuleState(str, Enum):
    REGISTERED = "registered"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass
class ModuleStatus:
    """对外可查询的模块最终状态。"""

    module_id: str
    state: ModuleState = ModuleState.REGISTERED
    failure_reason: FailureReason | None = None
    failure_detail: str | None = None
    config_sources: dict[str, ConfigSource] = field(default_factory=dict)


class Orchestrator:
    """线程安全的模块编排器。

    保证：
    - 启动顺序由依赖拓扑决定；任一模块初始化失败即回滚本次已启动的模块。
    - 并发 start/stop 通过互斥锁串行化，重复启动/停止为幂等无副作用操作。
    """

    def __init__(
        self,
        registry: ModuleRegistry,
        service_config: dict[str, dict[str, Any]] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._registry = registry
        self._service_config = service_config or {}
        self._env = env
        self._lock = threading.RLock()
        self._statuses: dict[str, ModuleStatus] = {}
        self._load_order: list[str] = []

    def start_all(self) -> None:
        """按依赖顺序启动全部模块；失败回滚并抛带 reason 的异常。"""
        raise NotImplementedError

    def stop_all(self) -> None:
        """按启动逆序停止全部已启动模块；幂等。"""
        raise NotImplementedError

    def start_module(self, module_id: str) -> None:
        """启动单个模块（含其未启动的依赖）；幂等。"""
        raise NotImplementedError

    def stop_module(self, module_id: str) -> None:
        """停止单个模块；若仍有运行中的依赖方则拒绝；幂等。"""
        raise NotImplementedError

    def status(self, module_id: str) -> ModuleStatus:
        """查询某模块最终状态。"""
        raise NotImplementedError

    def statuses(self) -> dict[str, ModuleStatus]:
        """查询全部模块状态。"""
        raise NotImplementedError
