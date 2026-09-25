"""编排器：驱动模块生命周期，保证失败回滚与并发一致。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .config import ConfigKeySpec, ConfigSource, LayeredConfig
from .dependencies import resolve_load_order
from .errors import (
    DependencyUnavailableError,
    FailureReason,
    ModuleInitError,
    OrchestratorError,
)
from .registry import ModuleRegistry
from .spec import Module, ModuleContext


class ModuleState(str, Enum):
    REGISTERED = "registered"
    RUNNING = "running"
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
    - 启动顺序由依赖拓扑决定；任一模块初始化失败即回滚本次已启动的模块，
      依赖它的模块不会继续启动。
    - 所有状态迁移都在同一把互斥锁内完成，并发 start/stop 被串行化，
      最终结果与某个串行执行顺序一致，无竞态。
    - 重复启动/停止为幂等操作，不产生重复副作用。
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
        self._statuses: dict[str, ModuleStatus] = {
            mid: ModuleStatus(module_id=mid) for mid in registry.all()
        }
        self._load_order: list[str] = resolve_load_order(registry.all())

    # ------------------------------------------------------------------ 查询

    def status(self, module_id: str) -> ModuleStatus:
        """查询某模块最终状态。"""
        with self._lock:
            self._registry.get(module_id)  # 未注册时抛 ModuleNotFoundError
            return self._statuses[module_id]

    def statuses(self) -> dict[str, ModuleStatus]:
        """查询全部模块状态（副本）。"""
        with self._lock:
            return dict(self._statuses)

    @property
    def load_order(self) -> list[str]:
        """启动阶段算出的稳定加载顺序。"""
        return list(self._load_order)

    # ------------------------------------------------------------------ 启动

    def start_all(self) -> None:
        """按依赖顺序启动全部模块；失败回滚并抛带 reason 的异常。"""
        with self._lock:
            started_this_call: list[str] = []
            try:
                for mid in self._load_order:
                    if self._statuses[mid].state is ModuleState.RUNNING:
                        continue  # 幂等：已运行的不重复启动
                    self._start_one(mid)
                    started_this_call.append(mid)
            except Exception:
                self._rollback(started_this_call)
                raise

    def start_module(self, module_id: str) -> None:
        """启动单个模块（先确保其依赖已运行）；幂等。"""
        with self._lock:
            self._registry.get(module_id)
            started_this_call: list[str] = []
            try:
                for mid in self._load_order:
                    if mid == module_id or self._is_dependency_of(module_id, mid):
                        if self._statuses[mid].state is not ModuleState.RUNNING:
                            self._start_one(mid)
                            started_this_call.append(mid)
            except Exception:
                self._rollback(started_this_call)
                raise

    # ------------------------------------------------------------------ 停止

    def stop_all(self) -> None:
        """按启动逆序停止全部已启动模块；幂等。"""
        with self._lock:
            for mid in reversed(self._load_order):
                if self._statuses[mid].state is ModuleState.RUNNING:
                    self._stop_one(mid)

    def stop_module(self, module_id: str) -> None:
        """停止单个模块；若仍有运行中的依赖方则拒绝；幂等。"""
        with self._lock:
            self._registry.get(module_id)
            status = self._statuses[module_id]
            if status.state is not ModuleState.RUNNING:
                return  # 幂等：未运行的停止是无副作用空操作
            blockers = [
                mid
                for mid in self._load_order
                if self._statuses[mid].state is ModuleState.RUNNING
                and self._is_dependency_of(mid, module_id)
            ]
            if blockers:
                raise DependencyUnavailableError(
                    f"模块 {module_id} 仍被运行中的模块 {blockers} 依赖，拒绝停止",
                    context={"module_id": module_id, "dependents": blockers},
                )
            self._stop_one(module_id)

    # ------------------------------------------------------------------ 内部

    def _is_dependency_of(self, module_id: str, candidate_dep: str) -> bool:
        """candidate_dep 是否为 module_id 的（直接或间接）依赖。"""
        modules = self._registry.all()
        seen: set[str] = set()
        stack = [module_id]
        while stack:
            current = stack.pop()
            for dep in modules[current].spec.dependencies:
                if dep.module_id == candidate_dep:
                    return True
                if dep.module_id not in seen:
                    seen.add(dep.module_id)
                    stack.append(dep.module_id)
        return False

    def _config_specs_for(self, module: Module) -> list[ConfigKeySpec]:
        """模块可选地声明 config_specs()；未声明则无配置键。"""
        provider = getattr(module, "config_specs", None)
        return list(provider()) if callable(provider) else []

    def _start_one(self, module_id: str) -> None:
        """解析配置并初始化+启动单个模块；失败时清理半生效状态。"""
        module = self._registry.get(module_id)
        status = self._statuses[module_id]
        try:
            layered = LayeredConfig(self._config_specs_for(module))
            resolved = layered.resolve(
                service_config=self._service_config.get(module_id, {}),
                env=self._env,
            )
            config = {key: rv.value for key, rv in resolved.items()}
            context = ModuleContext(
                {
                    mid: self._registry.get(mid)
                    for mid, st in self._statuses.items()
                    if st.state is ModuleState.RUNNING
                }
            )
            module.init(config, context)
            module.start()
        except OrchestratorError as exc:
            self._safe_stop(module)
            status.state = ModuleState.FAILED
            status.failure_reason = exc.reason
            status.failure_detail = str(exc)
            raise
        except Exception as exc:
            self._safe_stop(module)
            status.state = ModuleState.FAILED
            status.failure_reason = FailureReason.MODULE_INIT_FAILED
            status.failure_detail = f"{type(exc).__name__}: {exc}"
            raise ModuleInitError(
                f"模块 {module_id} 初始化失败: {exc}",
                context={"module_id": module_id},
            ) from exc
        status.state = ModuleState.RUNNING
        status.failure_reason = None
        status.failure_detail = None
        status.config_sources = {key: rv.source for key, rv in resolved.items()}

    def _stop_one(self, module_id: str) -> None:
        module = self._registry.get(module_id)
        self._safe_stop(module)
        self._statuses[module_id].state = ModuleState.STOPPED

    @staticmethod
    def _safe_stop(module: Module) -> None:
        """尽力停止，清理半生效状态；stop 自身的异常不掩盖原始失败。"""
        try:
            module.stop()
        except Exception:
            pass

    def _rollback(self, started_this_call: list[str]) -> None:
        """按逆序停止本次调用中已启动的模块，消除半生效状态。"""
        for mid in reversed(started_this_call):
            if self._statuses[mid].state is ModuleState.RUNNING:
                self._stop_one(mid)
