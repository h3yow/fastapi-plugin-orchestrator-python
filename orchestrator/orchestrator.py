"""编排器：驱动模块生命周期，保证失败回滚与并发一致。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .config import ConfigKeySpec, ConfigSource, LayeredConfig, ResolvedValue
from .dependencies import resolve_load_order
from .errors import (
    ConfigUpdateError,
    DependencyUnavailableError,
    FailureReason,
    ModuleInitError,
    ModuleNotRunningError,
    OrchestratorError,
    UnknownEnvironmentError,
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


@dataclass(frozen=True)
class UpdateRecord:
    """一次配置调整的可查询结果（成功或失败都可解释）。

    - ``failed_step``：失败卡在哪一步，形如 ``validate:<module>`` /
      ``apply:<module>`` / ``rollback:<module>``；成功时为 None。
    - ``affected``：本次级联批次涉及的模块（按依赖顺序）。
    """

    request_id: str | None
    target: str
    success: bool
    failed_step: str | None
    detail: str | None
    affected: tuple[str, ...]


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
        environment: str | None = None,
        environment_configs: dict[str, dict[str, dict[str, Any]]] | None = None,
    ) -> None:
        self._registry = registry
        self._service_config = service_config or {}
        self._env = env
        # 多环境：environment_configs[环境名][module_id][key] = value
        self._environment_configs = environment_configs or {}
        self._known_environments = frozenset(self._environment_configs)
        self._environment = environment
        self._lock = threading.RLock()
        self._statuses: dict[str, ModuleStatus] = {
            mid: ModuleStatus(module_id=mid) for mid in registry.all()
        }
        self._load_order: list[str] = resolve_load_order(registry.all())
        if environment is not None and environment not in self._known_environments:
            raise UnknownEnvironmentError(
                f"未知部署环境: {environment}（已知: {sorted(self._known_environments)}）",
                context={
                    "environment": environment,
                    "known": sorted(self._known_environments),
                },
            )
        # 运行期调整层：module_id -> {原始键: 值}，作为最高层参与解析
        self._runtime_overrides: dict[str, dict[str, Any]] = {}
        # 每个运行中模块最近一次解析结果（用于回滚快照与来源查询）
        self._resolved: dict[str, dict[str, ResolvedValue]] = {}
        # 每个模块最近一次调整记录；request_id 去重表
        self._last_updates: dict[str, UpdateRecord] = {}
        self._processed_requests: dict[str, UpdateRecord] = {}

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

    @property
    def environment(self) -> str | None:
        """当前生效的部署环境名（未启用多环境时为 None）。"""
        return self._environment

    def effective_config(self, module_id: str) -> dict[str, ResolvedValue]:
        """查询模块当前生效配置（值+来源层）；模块未运行过则为空。"""
        with self._lock:
            self._registry.get(module_id)
            return dict(self._resolved.get(module_id, {}))

    def last_update(self, module_id: str) -> UpdateRecord | None:
        """查询模块最近一次配置调整的结果（成功/失败/卡在哪一步）。"""
        with self._lock:
            self._registry.get(module_id)
            return self._last_updates.get(module_id)

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

    # -------------------------------------------------------------- 运行期配置

    def update_config(
        self,
        module_id: str,
        overrides: dict[str, Any],
        *,
        request_id: str | None = None,
    ) -> UpdateRecord:
        """不停机调整指定模块的配置，并级联更新依赖它的运行中模块。

        语义：
        - ``overrides`` 进入该模块的 runtime 层（最高优先级），与其余层一起
          按启动时同一套分层与校验规则重新解析；未知键/非法值/新旧键冲突一律拒绝。
        - 两阶段提交：先对整批（目标 + 运行中的传递依赖方，按依赖序）完成校验，
          再逐个应用；任一环节失败即整批回滚到改动前的一致状态。
        - 校验失败时旧配置完整继续生效，模块不会读到半新半旧的值。
        - 同一 ``request_id`` 重复提交直接返回首次结果，不重复生效、不报错。
        - 全部调整在同一把互斥锁内串行化，并发结果等价于某个串行顺序。
        """
        with self._lock:
            self._registry.get(module_id)
            if request_id is not None and request_id in self._processed_requests:
                return self._processed_requests[request_id]  # 幂等：重复提交
            if self._statuses[module_id].state is not ModuleState.RUNNING:
                raise ModuleNotRunningError(
                    f"模块 {module_id} 未在运行，拒绝运行期配置调整",
                    context={
                        "module_id": module_id,
                        "state": self._statuses[module_id].state.value,
                    },
                )

            batch = self._cascade_batch(module_id)
            old_runtime = dict(self._runtime_overrides.get(module_id, {}))
            old_resolved = {mid: self._resolved[mid] for mid in batch}

            # 阶段一：整批校验（目标模块换上合并后的 runtime 层）
            merged = dict(old_runtime)
            merged.update(overrides)
            self._runtime_overrides[module_id] = merged
            new_resolved: dict[str, dict[str, ResolvedValue]] = {}
            failed_mid: str | None = None
            try:
                for mid in batch:
                    try:
                        new_resolved[mid] = self._resolve(mid)
                    except OrchestratorError:
                        failed_mid = mid
                        raise
            except OrchestratorError as exc:
                # 校验失败：还原 runtime 层，旧配置分毫未动、继续生效
                self._runtime_overrides[module_id] = old_runtime
                record = UpdateRecord(
                    request_id, module_id, False,
                    f"validate:{failed_mid}", str(exc), tuple(batch),
                )
                self._record_update(batch, record, request_id)
                raise

            # 阶段二：按依赖序应用；失败则整批回滚
            try:
                self._apply_batch(batch, new_resolved, old_resolved)
            except Exception as exc:
                self._runtime_overrides[module_id] = old_runtime
                record = UpdateRecord(
                    request_id, module_id, False,
                    getattr(exc, "_failed_step", None) or f"apply:{module_id}",
                    str(exc), tuple(batch),
                )
                self._record_update(batch, record, request_id)
                raise

            record = UpdateRecord(
                request_id, module_id, True, None, None, tuple(batch)
            )
            self._record_update(batch, record, request_id)
            return record

    def switch_environment(self, name: str) -> None:
        """切换部署环境并重解析全部运行中模块的配置。

        - 未知环境名一律拒绝（UnknownEnvironmentError），当前环境不受影响。
        - 切换到当前环境是幂等空操作。
        - 与配置调整同一套保证：先整批校验再按依赖序应用，失败整批回滚，
          回滚后仍处在切换前的一致状态。
        - 各环境的配置只来自 ``environment_configs[name]``，不会串入其他环境；
          环境中未显式配置的键自然回落到 service/default 等下层，来源可查询。
        """
        with self._lock:
            if name not in self._known_environments:
                raise UnknownEnvironmentError(
                    f"未知部署环境: {name}（已知: {sorted(self._known_environments)}）",
                    context={
                        "environment": name,
                        "known": sorted(self._known_environments),
                    },
                )
            if name == self._environment:
                return  # 幂等
            batch = [
                mid
                for mid in self._load_order
                if self._statuses[mid].state is ModuleState.RUNNING
            ]
            old_environment = self._environment
            old_resolved = {mid: self._resolved[mid] for mid in batch}
            self._environment = name
            try:
                new_resolved = {mid: self._resolve(mid) for mid in batch}
            except OrchestratorError:
                self._environment = old_environment  # 校验失败：环境名也回滚
                raise
            try:
                self._apply_batch(batch, new_resolved, old_resolved)
            except Exception:
                self._environment = old_environment
                raise

    def _cascade_batch(self, module_id: str) -> list[str]:
        """级联批次：目标模块 + 依赖它的运行中模块，按依赖顺序排列。"""
        return [
            mid
            for mid in self._load_order
            if self._statuses[mid].state is ModuleState.RUNNING
            and (mid == module_id or self._is_dependency_of(mid, module_id))
        ]

    def _record_update(
        self, batch: list[str], record: UpdateRecord, request_id: str | None
    ) -> None:
        """把调整结果记录到批次内每个模块，并按 request_id 去重。"""
        for mid in batch:
            self._last_updates[mid] = record
        if request_id is not None:
            self._processed_requests[request_id] = record

    def _apply_batch(
        self,
        batch: list[str],
        new_resolved: dict[str, dict[str, ResolvedValue]],
        old_resolved: dict[str, dict[str, ResolvedValue]],
    ) -> None:
        """按依赖序应用新配置；任一步失败即逆序回滚到旧配置。"""
        applied: list[str] = []
        failed_mid: str | None = None
        try:
            for mid in batch:
                try:
                    self._apply(mid, new_resolved[mid])
                except Exception:
                    failed_mid = mid
                    raise
                applied.append(mid)
        except Exception as exc:
            # 回滚：先尽力恢复失败模块本身，再逆序恢复已应用的模块
            rollback_failed: str | None = None
            for mid in [failed_mid, *reversed(applied)]:
                if mid is None:
                    continue
                try:
                    self._apply(mid, old_resolved[mid])
                except Exception:
                    rollback_failed = mid
                    status = self._statuses[mid]
                    status.state = ModuleState.FAILED
                    status.failure_reason = FailureReason.CONFIG_UPDATE_FAILED
                    status.failure_detail = f"回滚模块 {mid} 时再次失败"
            err = ConfigUpdateError(
                f"配置级联更新在模块 {failed_mid} 应用失败，已整批回滚: {exc}",
                context={
                    "failed_module": failed_mid,
                    "rolled_back": [m for m in [failed_mid, *reversed(applied)] if m],
                    "rollback_failed": rollback_failed,
                },
            )
            step = (
                f"rollback:{rollback_failed}"
                if rollback_failed
                else f"apply:{failed_mid}"
            )
            err._failed_step = step  # type: ignore[attr-defined]
            raise err from exc

    def _apply(self, module_id: str, resolved: dict[str, ResolvedValue]) -> None:
        """把一份已解析配置应用到运行中的模块。

        模块若提供 ``reconfigure(config, context)`` 钩子则优先调用；
        否则退化为 stop -> init -> start 的原子重载。
        """
        module = self._registry.get(module_id)
        config = {key: rv.value for key, rv in resolved.items()}
        context = ModuleContext(
            {
                mid: self._registry.get(mid)
                for mid, st in self._statuses.items()
                if st.state is ModuleState.RUNNING
            }
        )
        reconfigure = getattr(module, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(config, context)
        else:
            module.stop()
            module.init(config, context)
            module.start()
        self._resolved[module_id] = resolved
        self._statuses[module_id].config_sources = {
            key: rv.source for key, rv in resolved.items()
        }

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

    def _resolve(self, module_id: str) -> dict[str, ResolvedValue]:
        """按当前全部层（含环境层与运行期层）解析某模块的生效配置。"""
        module = self._registry.get(module_id)
        layered = LayeredConfig(self._config_specs_for(module))
        env_layer = (
            self._environment_configs.get(self._environment, {}).get(module_id, {})
            if self._environment is not None
            else {}
        )
        return layered.resolve(
            service_config=self._service_config.get(module_id, {}),
            env=self._env,
            environment_config=env_layer,
            runtime_config=self._runtime_overrides.get(module_id, {}),
        )

    def _start_one(self, module_id: str) -> None:
        """解析配置并初始化+启动单个模块；失败时清理半生效状态。"""
        module = self._registry.get(module_id)
        status = self._statuses[module_id]
        resolved: dict[str, ResolvedValue] = {}
        try:
            resolved = self._resolve(module_id)
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
        self._resolved[module_id] = resolved

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
