"""编排器：模块启停生命周期、失败回滚与并发安全。"""
from __future__ import annotations

import enum
import threading

from app.base import ModuleBase
from app.config import ConfigResolver, ResolvedConfig
from app.errors import FrameworkError, ModuleStateError
from app.registry import ModuleRegistry


class ModuleState(enum.Enum):
    REGISTERED = "registered"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class Orchestrator:
    """按依赖顺序启动模块，失败时回滚，支持运行期并发安全启停。

    并发保证：所有状态迁移都在同一把可重入锁内完成，
    并发启停同一模块或存在依赖关系的模块时结果与某个串行顺序一致。
    """

    def __init__(self, registry: ModuleRegistry, resolver: ConfigResolver) -> None:
        self._registry = registry
        self._resolver = resolver
        self._lock = threading.RLock()
        self._states: dict[str, ModuleState] = {}
        self._instances: dict[str, ModuleBase] = {}
        self._configs: dict[str, ResolvedConfig] = {}
        self._failures: dict[str, str] = {}

    # ---- 内部迁移（调用方必须已持有锁） ----

    def _state(self, module_id: str) -> ModuleState:
        return self._states.get(module_id, ModuleState.REGISTERED)

    def _require_known(self, module_id: str) -> None:
        if module_id not in self._registry.module_ids():
            raise ModuleStateError(f"未注册的模块: {module_id!r}")

    def _start_one(self, module_id: str, started_here: list[str]) -> None:
        state = self._state(module_id)
        if state is ModuleState.RUNNING:
            return  # 幂等：重复启动不产生重复副作用
        if state is ModuleState.FAILED:
            raise ModuleStateError(
                f"模块 {module_id!r} 处于失败状态: {self._failures.get(module_id)}"
            )
        spec = self._registry.get(module_id)
        for dep in spec.dependencies:  # 依赖先行；失败则依赖者不会启动
            self._start_one(dep.module_id, started_here)
        self._states[module_id] = ModuleState.STARTING
        try:
            config = self._resolver.resolve(spec)  # 只下发校验过的配置
            instance = self._registry.factory_of(module_id)()
            instance.config = config
            instance.on_start()
        except Exception as exc:
            self._states[module_id] = ModuleState.FAILED
            self._failures[module_id] = f"{type(exc).__name__}: {exc}"
            raise
        self._instances[module_id] = instance
        self._configs[module_id] = config
        self._states[module_id] = ModuleState.RUNNING
        started_here.append(module_id)

    def _stop_one(self, module_id: str, stopped_here: list[str]) -> None:
        state = self._state(module_id)
        if state is not ModuleState.RUNNING:
            return  # 幂等：重复停止不产生重复副作用
        self._states[module_id] = ModuleState.STOPPING
        try:
            self._instances[module_id].on_stop()
        except Exception as exc:
            self._states[module_id] = ModuleState.FAILED
            self._failures[module_id] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if self._state(module_id) is ModuleState.STOPPING:
                self._states[module_id] = ModuleState.STOPPED
        self._instances.pop(module_id, None)
        stopped_here.append(module_id)

    def _rollback(self, started_here: list[str]) -> None:
        """按逆序停止本次调用中已启动的模块，不留半生效状态。"""
        for mid in reversed(started_here):
            try:
                self._stop_one(mid, [])
            except Exception:
                pass  # 回滚尽力而为，原始失败原因优先

    # ---- 对外接口 ----

    def start_all(self) -> list[str]:
        """按稳定拓扑序启动全部模块；任一失败则回滚本次已启动者。"""
        with self._lock:
            order = self._registry.resolve_load_order()
            started: list[str] = []
            try:
                for mid in order:
                    self._start_one(mid, started)
            except Exception:
                self._rollback(started)
                raise
            return started

    def start_module(self, module_id: str) -> None:
        """启动指定模块及其未运行的依赖；失败时回滚本次新启动者。"""
        with self._lock:
            self._require_known(module_id)
            started: list[str] = []
            try:
                self._start_one(module_id, started)
            except Exception:
                self._rollback(started)
                raise

    def stop_module(self, module_id: str) -> None:
        """停止指定模块及仍依赖它的运行中模块（先停依赖者）。"""
        with self._lock:
            self._require_known(module_id)
            order = self._registry.resolve_load_order()
            dependents = {
                spec.module_id
                for spec in (self._registry.get(m) for m in order)
                for dep in spec.dependencies
                if dep.module_id == module_id
            }
            # 传递闭包：依赖者的依赖者也要停
            changed = True
            while changed:
                changed = False
                for m in order:
                    if m in dependents:
                        continue
                    if any(d.module_id in dependents for d in self._registry.get(m).dependencies):
                        dependents.add(m)
                        changed = True
            targets = [m for m in reversed(order) if m in dependents or m == module_id]
            errors: list[Exception] = []
            for m in targets:
                try:
                    self._stop_one(m, [])
                except Exception as exc:
                    errors.append(exc)
            if errors:
                raise FrameworkError(
                    f"停止模块 {module_id!r} 时部分模块停止失败: "
                    + "; ".join(str(e) for e in errors)
                )

    def stop_all(self) -> None:
        """按拓扑序逆序停止全部运行中模块。"""
        with self._lock:
            order = self._registry.resolve_load_order()
            for m in reversed(order):
                self._stop_one(m, [])

    # ---- 状态查询 ----

    def state_of(self, module_id: str) -> ModuleState:
        with self._lock:
            self._require_known(module_id)
            return self._state(module_id)

    def failure_reason(self, module_id: str) -> str | None:
        with self._lock:
            return self._failures.get(module_id)

    def effective_config(self, module_id: str) -> ResolvedConfig | None:
        with self._lock:
            return self._configs.get(module_id)

    def snapshot(self) -> dict[str, str]:
        """所有已注册模块的当前状态，便于日志与断言。"""
        with self._lock:
            return {m: self._state(m).value for m in self._registry.module_ids()}
