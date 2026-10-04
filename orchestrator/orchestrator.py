"""编排器：驱动模块生命周期，保证失败回滚与并发一致。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .config import ConfigKeySpec, ConfigSource, LayeredConfig
from .dependencies import resolve_load_order
from .environments import DEFAULT_ENVIRONMENT, EnvironmentStore
from .errors import (
    ConfigSourceRejectedError,
    DependencyUnavailableError,
    EnvironmentNotFoundError,
    FailureReason,
    ModuleInitError,
    OrchestratorError,
    ReloadFailedError,
)
from .registry import ModuleRegistry
from .runtime import ChangeLedger, ChangeResult, ChangeStage, ChangeOutcome
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
    config_values: dict[str, Any] = field(default_factory=dict)


class Orchestrator:
    """线程安全的模块编排器。

    保证：
    - 启动顺序由依赖拓扑决定；任一模块初始化失败即回滚本次已启动的模块，
      依赖它的模块不会继续启动。
    - 所有状态迁移都在同一把互斥锁内完成，并发 start/stop 被串行化，
      最终结果与某个串行执行顺序一致，无竞态。
    - 重复启动/停止为幂等操作，不产生重复副作用。
    """

    # 运行期调整只接受这些显式来源声明
    ALLOWED_RUNTIME_SOURCES = frozenset({"runtime"})

    def __init__(
        self,
        registry: ModuleRegistry,
        service_config: dict[str, dict[str, Any]] | None = None,
        env: dict[str, str] | None = None,
        environments: dict[str, dict[str, dict[str, Any]]] | None = None,
        active_environment: str = DEFAULT_ENVIRONMENT,
        allowed_runtime_sources: frozenset[str] | None = None,
    ) -> None:
        self._registry = registry
        self._service_config = service_config or {}
        self._env = env
        self._store = EnvironmentStore(environments, active=active_environment)
        self._allowed_sources = (
            allowed_runtime_sources
            if allowed_runtime_sources is not None
            else set(self.ALLOWED_RUNTIME_SOURCES)
        )
        self._ledger = ChangeLedger()
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

    # ------------------------------------------------------ 环境与生效值查询

    @property
    def environment(self) -> str:
        """当前部署环境名。"""
        return self._store.active

    def environments(self) -> list[str]:
        """全部已声明部署环境（字典序）。"""
        return self._store.names()

    def effective_config(self, module_id: str) -> dict[str, Any]:
        """模块当前实际生效的配置值（键 -> 值）。

        正在运行的模块取自最近一次生效的已校验快照；未运行则按当前环境
        现场分层解析（不改变任何状态）。
        """
        with self._lock:
            self._registry.get(module_id)
            if self._statuses[module_id].state is ModuleState.RUNNING:
                return dict(self._statuses[module_id].config_values)
            resolved = self._resolve_config(module_id)
            return {key: rv.value for key, rv in resolved.items()}

    def config_explanation(self, module_id: str) -> dict[str, dict[str, Any]]:
        """逐键解释当前生效值来自哪一层（含回落）。

        返回 {key: {"value": v, "source": 层名}}；未运行模块按当前环境现场解析。
        """
        with self._lock:
            self._registry.get(module_id)
            status = self._statuses[module_id]
            if status.state is ModuleState.RUNNING:
                return {
                    key: {
                        "value": status.config_values.get(key),
                        "source": status.config_sources[key].value,
                    }
                    for key in status.config_sources
                }
            resolved = self._resolve_config(module_id)
            return {
                key: {"value": rv.value, "source": rv.source.value}
                for key, rv in resolved.items()
            }

    def last_change(self) -> ChangeResult | None:
        """全局最近一次配置变更的结果（成功/失败、失败步骤）。"""
        with self._lock:
            return self._ledger.last_result()

    def last_change_for(self, module_id: str) -> ChangeResult | None:
        """指定模块最近一次被触及的变更结果。"""
        with self._lock:
            self._registry.get(module_id)
            return self._ledger.last_result_for(module_id)

    # -------------------------------------------------------------- 热更新

    def update_config(
        self,
        changes: dict[str, dict[str, Any]],
        *,
        source: str = "runtime",
        change_id: str | None = None,
    ) -> ChangeResult:
        """在不重启进程的前提下，对当前环境的一个或多个模块热更新配置。

        语义：
        - 每个键沿用启动时的分层与严格校验（未知键/非法值/新旧键冲突一律拒绝）；
          来源不在允许集合（默认仅 ``runtime``）同样拒绝。
        - ``changes`` 按键并入当前环境的环境层，其他环境完全不受影响。
        - 被依赖的模块更新时，其（传递性）依赖方按拓扑顺序一起重载。
        - 先对全部受影响模块做校验：任一不通过则整批拒绝，存储与模块都不动，
          旧配置完整生效；提交后的重载若中途失败，则逆序回滚到改动前状态。
        - 全流程持同一把互斥锁，多组并发调整等价于按 change 序号确定顺序串行。
        - 相同 ``change_id`` + 相同请求体的重复提交直接回放原结果，
          不重复生效、不报错；change_id 被不同请求复用则拒绝。

        返回可解释的 ChangeResult；校验/回滚类失败同时抛出对应异常。
        """
        with self._lock:
            requested = {mid: dict(patch) for mid, patch in changes.items()}
            targets = tuple(sorted(requested))
            # 幂等：重复提交在任何副作用之前判定
            request_key = (self._store.active, tuple(sorted(requested.items())))
            if change_id is not None:
                replayed = self._ledger.check_idempotent(change_id, request_key)
                if replayed is not None:
                    return replayed
            sequence = self._ledger.next_sequence()
            base = ChangeResult(
                outcome=ChangeOutcome.FAILED,
                change_id=change_id,
                environment=self._store.active,
                targets=targets,
                sequence=sequence,
                requested_changes=requested,
            )

            # 阶段 0：请求合法性（空请求、模块存在性、可信来源）
            try:
                self._validate_request(requested, source)
            except OrchestratorError as exc:
                self._ledger.record(self._failing(base, ChangeStage.REQUEST, exc))
                raise

            affected = self._cascade_set(set(targets))

            # 阶段 1：快照 + 全量预校验（不动存储、不动模块）
            snapshots = self._store.snapshot(self._store.active, targets)
            try:
                for mid in targets:
                    self._store.merge_into(self._store.active, mid, requested[mid])
                planned: dict[str, dict[str, Any]] = {}
                for mid in affected:
                    resolved = self._resolve_config(mid)
                    planned[mid] = {key: rv.value for key, rv in resolved.items()}
            except OrchestratorError as exc:
                # 预校验失败：恢复试探性写入，模块从未被触碰，旧配置完整生效
                self._store.restore(self._store.active, snapshots)
                self._ledger.record(self._failing(base, ChangeStage.VALIDATE, exc))
                raise
            except Exception as exc:  # 防御：任何意外也不能留下半套存储
                self._store.restore(self._store.active, snapshots)
                failure = ReloadFailedError(
                    f"配置预校验异常: {exc}",
                    context={"stage": ChangeStage.VALIDATE.value},
                )
                self._ledger.record(self._failing(base, ChangeStage.VALIDATE, failure))
                raise failure from exc

            # 阶段 2：存储已提交；按拓扑顺序逐模块重载
            reloaded: list[str] = []
            try:
                for mid in affected:
                    self._reload_one(mid, planned[mid])
                    reloaded.append(mid)
                    self._remember_effective(mid, self._resolve_config(mid))
            except Exception as exc:
                failed_mid = (
                    affected[len(reloaded)] if len(reloaded) < len(affected) else None
                )
                # 整批回滚到改动前；failed_mid 自身的新状态由回滚流程清理
                self._rollback_change(affected, reloaded, snapshots, env_name=None)
                if isinstance(exc, ReloadFailedError) and exc.context.get(
                    "failed_module"
                ) == failed_mid:
                    failure = exc
                else:
                    failure = ReloadFailedError(
                        f"模块 {failed_mid} 热更新失败: {type(exc).__name__}: {exc}",
                        context={"failed_module": failed_mid},
                    )
                self._ledger.record(
                    self._failing(
                        base, ChangeStage.RELOAD, failure, failed_module=failed_mid
                    )
                )
                raise failure from exc

            return self._ledger.record(
                ChangeResult(
                    outcome=ChangeOutcome.SUCCESS,
                    change_id=change_id,
                    environment=self._store.active,
                    targets=targets,
                    affected_modules=tuple(affected),
                    sequence=sequence,
                    requested_changes=requested,
                )
            )

    def switch_environment(self, name: str) -> ChangeResult:
        """切换当前部署环境，并对全部运行中模块原子地重载为新环境配置。

        - 环境未声明 -> EnvironmentNotFoundError（stage=request），状态不变。
        - 先在目标环境下对全部运行中模块做分层预校验，再切换并重载。
        - 重载中途失败：切回原环境，逆序把模块恢复为原环境旧配置，
          保证不出现"一半在新环境、一半在旧环境"的组合。
        - 同名环境重复切换为空操作，返回成功结果但不重复重载。
        """
        with self._lock:
            targets = tuple(self._load_order)
            sequence = self._ledger.next_sequence()
            base = ChangeResult(
                outcome=ChangeOutcome.FAILED,
                change_id=None,
                environment=self._store.active,
                targets=targets,
                sequence=sequence,
            )
            if name == self._store.active:
                return self._ledger.record(
                    ChangeResult(
                        outcome=ChangeOutcome.SUCCESS,
                        change_id=None,
                        environment=name,
                        targets=targets,
                        affected_modules=(),
                        sequence=sequence,
                    )
                )
            try:
                self._store.assert_known(name)
            except EnvironmentNotFoundError as exc:
                self._ledger.record(
                    self._failing(base, ChangeStage.REQUEST, exc)
                )
                raise

            running = [
                mid
                for mid in self._load_order
                if self._statuses[mid].state is ModuleState.RUNNING
            ]
            previous_env = self._store.active

            # 阶段 1：目标环境下全量预校验（不切换、不动模块）
            try:
                planned: dict[str, dict[str, Any]] = {}
                for mid in running:
                    resolved = self._resolve_config(mid, environment=name)
                    planned[mid] = {key: rv.value for key, rv in resolved.items()}
            except OrchestratorError as exc:
                self._ledger.record(
                    self._failing(base, ChangeStage.VALIDATE, exc)
                )
                raise

            # 阶段 2：切换 + 拓扑顺序重载
            self._store.switch(name)
            reloaded: list[str] = []
            try:
                for mid in running:
                    self._reload_one(mid, planned[mid])
                    reloaded.append(mid)
                    self._remember_effective(mid, self._resolve_config(mid))
            except Exception as exc:
                # 切回原环境，按原环境旧配置整批回滚（热更新型拓扑序、
                # 重建型逆序停+拓扑序起），杜绝一半新环境一半旧环境
                self._store.switch(previous_env)
                rollback_failures = self._restore_modules_to_env(running, previous_env)
                self._mark_rollback_failures(rollback_failures, "环境切换回滚失败")
                failed_mid = running[len(reloaded)] if len(reloaded) < len(running) else None
                failure = ReloadFailedError(
                    f"切换到环境 {name} 时模块 {failed_mid} 重载失败: "
                    f"{type(exc).__name__}: {exc}",
                    context={"failed_module": failed_mid, "environment": name},
                )
                self._ledger.record(
                    self._failing(
                        base,
                        ChangeStage.ENVIRONMENT_SWITCH,
                        failure,
                        failed_module=failed_mid,
                    )
                )
                raise failure from exc

            return self._ledger.record(
                ChangeResult(
                    outcome=ChangeOutcome.SUCCESS,
                    change_id=None,
                    environment=name,
                    targets=targets,
                    affected_modules=tuple(running),
                    sequence=sequence,
                )
            )

    def _validate_request(
        self, requested: dict[str, dict[str, Any]], source: str
    ) -> None:
        if not requested:
            raise ConfigSourceRejectedError(
                "配置变更为空，拒绝执行",
                context={"source": source},
            )
        if not isinstance(source, str) or source not in self._allowed_sources:
            raise ConfigSourceRejectedError(
                f"配置变更来源 {source!r} 不在可信集合 {sorted(self._allowed_sources)} 内",
                context={"source": source, "allowed": sorted(self._allowed_sources)},
            )
        for mid in requested:
            self._registry.get(mid)  # 未注册抛 ModuleNotFoundError

    @staticmethod
    def _failing(
        base: ChangeResult,
        stage: ChangeStage,
        exc: OrchestratorError,
        failed_module: str | None = None,
    ) -> ChangeResult:
        return ChangeResult(
            outcome=ChangeOutcome.FAILED,
            change_id=base.change_id,
            environment=base.environment,
            targets=base.targets,
            stage=stage,
            failure_reason=exc.reason.value,
            failure_detail=str(exc),
            failed_module=failed_module,
            sequence=base.sequence,
            requested_changes=base.requested_changes,
        )

    def _is_reloadable(self, module_id: str) -> bool:
        return callable(getattr(self._registry.get(module_id), "reload", None))

    def _restore_modules_to_env(
        self, module_ids: list[str], target_env: str
    ) -> dict[str, str]:
        """把给定模块（调用方保证拓扑序）统一恢复到 target_env 下的配置。

        热更新型按拓扑序回退（依赖先回，依赖方回退时看到的依赖已是旧状态）；
        重建型先逆序全部停止（依赖方先停），再拓扑序 init+start（依赖先启）。
        返回 {module_id: 失败详情}，成功回滚的模块不在其中。
        """
        old_configs: dict[str, dict[str, Any]] = {}
        for mid in module_ids:
            resolved = self._resolve_config(mid, environment=target_env)
            old_configs[mid] = {key: rv.value for key, rv in resolved.items()}

        reloadable = [mid for mid in module_ids if self._is_reloadable(mid)]
        rebuildable = [mid for mid in module_ids if not self._is_reloadable(mid)]

        failed: dict[str, str] = {}
        for mid in reloadable:
            try:
                self._reload_one(mid, old_configs[mid])
                self._remember_effective(
                    mid, self._resolve_config(mid, environment=target_env)
                )
            except Exception as exc:
                failed[mid] = f"{type(exc).__name__}: {exc}"

        for mid in reversed(rebuildable):
            self._safe_stop(self._registry.get(mid))
        for mid in rebuildable:
            try:
                module = self._registry.get(mid)
                module.init(old_configs[mid], self._running_context())
                module.start()
                self._remember_effective(
                    mid, self._resolve_config(mid, environment=target_env)
                )
            except Exception as exc:
                failed[mid] = f"{type(exc).__name__}: {exc}"
        return failed

    def _mark_rollback_failures(self, failed: dict[str, str], prefix: str) -> None:
        for mid, detail in failed.items():
            status = self._statuses[mid]
            status.state = ModuleState.FAILED
            status.failure_reason = FailureReason.RELOAD_FAILED
            status.failure_detail = f"{prefix}: {detail}"

    def _rollback_change(
        self,
        affected: list[str],
        reloaded: list[str],
        snapshots: dict[str, dict[str, Any]],
        env_name: str | None,
    ) -> None:
        """热更新失败后的整批回滚：恢复环境层存储，再让全部受影响模块回旧配置。"""
        target_env = env_name or self._store.active
        self._store.restore(target_env, snapshots)
        failed = self._restore_modules_to_env(affected, target_env)
        self._mark_rollback_failures(failed, "回滚到旧配置失败")

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

    def _resolve_config(
        self, module_id: str, environment: str | None = None
    ) -> dict[str, Any]:
        """按 env > environment > service > default 解析并校验。"""
        module = self._registry.get(module_id)
        env_name = environment or self._store.active
        layered = LayeredConfig(self._config_specs_for(module))
        return layered.resolve(
            service_config=self._service_config.get(module_id, {}),
            env=self._env,
            environment_config=self._store.config_for(env_name, module_id),
        )

    def _start_one(self, module_id: str) -> None:
        """解析配置并初始化+启动单个模块；失败时清理半生效状态。"""
        module = self._registry.get(module_id)
        status = self._statuses[module_id]
        try:
            resolved = self._resolve_config(module_id)
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
        status.config_values = dict(config)

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

    # ---------------------------------------------------------- 热更新内部

    def _running_context(self) -> ModuleContext:
        return ModuleContext(
            {
                mid: self._registry.get(mid)
                for mid, st in self._statuses.items()
                if st.state is ModuleState.RUNNING
            }
        )

    def _cascade_set(self, targets: set[str]) -> list[str]:
        """目标模块 + 传递性依赖方中当前正在运行的模块，按拓扑顺序返回。

        被依赖方配置变化后，依赖方必须按依赖顺序一起重载；
        未运行的模块不参与本轮级联（下次启动时自然读到新配置）。
        """
        modules = self._registry.all()
        affected: set[str] = set(targets)
        # 反向传播：沿"谁依赖谁"一层层扩展
        frontier = set(targets)
        while frontier:
            nxt: set[str] = set()
            for mid, module in modules.items():
                if mid in affected:
                    continue
                if any(dep.module_id in frontier for dep in module.spec.dependencies):
                    nxt.add(mid)
            affected |= nxt
            frontier = nxt
        return [
            mid
            for mid in self._load_order
            if mid in affected and self._statuses[mid].state is ModuleState.RUNNING
        ]

    def _reload_one(self, module_id: str, config: dict[str, Any]) -> None:
        """对单个运行中模块施加新配置。

        模块实现了 reload() 则原地热更新；否则框架按 stop->init->start
        在同一进程内重建，进程不中断。调用方负责失败后的整批回滚。
        """
        module = self._registry.get(module_id)
        context = self._running_context()
        reload_fn = getattr(module, "reload", None)
        if callable(reload_fn):
            reload_fn(config, context)
            return
        self._safe_stop(module)
        module.init(config, context)
        module.start()

    def _remember_effective(self, module_id: str, resolved: dict) -> None:
        status = self._statuses[module_id]
        status.config_sources = {key: rv.source for key, rv in resolved.items()}
        status.config_values = {key: rv.value for key, rv in resolved.items()}
        if status.state is ModuleState.FAILED:
            status.state = ModuleState.RUNNING
            status.failure_reason = None
            status.failure_detail = None
