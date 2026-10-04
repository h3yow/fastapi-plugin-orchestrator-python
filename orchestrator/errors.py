"""可区分的失败原因类型体系。

所有框架层面的失败都抛出 OrchestratorError 的子类，
每个子类带有一个稳定的 ``reason`` 字符串，便于测试与对外查询区分。
"""

from __future__ import annotations

from enum import Enum


class FailureReason(str, Enum):
    """稳定的失败原因码，对外查询与断言均以此为准。"""

    DUPLICATE_REGISTRATION = "duplicate_registration"
    MISSING_DEPENDENCY = "missing_dependency"
    VERSION_INCOMPATIBLE = "version_incompatible"
    CAPABILITY_INCOMPATIBLE = "capability_incompatible"
    DEPENDENCY_CYCLE = "dependency_cycle"
    UNKNOWN_CONFIG_KEY = "unknown_config_key"
    INVALID_CONFIG_VALUE = "invalid_config_value"
    AMBIGUOUS_CONFIG_OVERRIDE = "ambiguous_config_override"
    MODULE_INIT_FAILED = "module_init_failed"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    MODULE_NOT_FOUND = "module_not_found"
    MODULE_NOT_RUNNING = "module_not_running"
    UNKNOWN_ENVIRONMENT = "unknown_environment"
    CONFIG_UPDATE_FAILED = "config_update_failed"


class OrchestratorError(Exception):
    """框架错误基类，携带可区分的 reason 与结构化上下文。"""

    reason: FailureReason = FailureReason.MODULE_INIT_FAILED

    def __init__(self, message: str, *, context: dict | None = None) -> None:
        super().__init__(message)
        self.context = context or {}


class DuplicateRegistrationError(OrchestratorError):
    reason = FailureReason.DUPLICATE_REGISTRATION


class MissingDependencyError(OrchestratorError):
    reason = FailureReason.MISSING_DEPENDENCY


class VersionIncompatibleError(OrchestratorError):
    reason = FailureReason.VERSION_INCOMPATIBLE


class CapabilityIncompatibleError(OrchestratorError):
    reason = FailureReason.CAPABILITY_INCOMPATIBLE


class DependencyCycleError(OrchestratorError):
    reason = FailureReason.DEPENDENCY_CYCLE


class UnknownConfigKeyError(OrchestratorError):
    reason = FailureReason.UNKNOWN_CONFIG_KEY


class InvalidConfigValueError(OrchestratorError):
    reason = FailureReason.INVALID_CONFIG_VALUE


class AmbiguousConfigOverrideError(OrchestratorError):
    reason = FailureReason.AMBIGUOUS_CONFIG_OVERRIDE


class ModuleInitError(OrchestratorError):
    reason = FailureReason.MODULE_INIT_FAILED


class DependencyUnavailableError(OrchestratorError):
    reason = FailureReason.DEPENDENCY_UNAVAILABLE


class ModuleNotFoundError(OrchestratorError):
    reason = FailureReason.MODULE_NOT_FOUND


class ModuleNotRunningError(OrchestratorError):
    reason = FailureReason.MODULE_NOT_RUNNING


class UnknownEnvironmentError(OrchestratorError):
    reason = FailureReason.UNKNOWN_ENVIRONMENT


class ConfigUpdateError(OrchestratorError):
    """运行期配置调整在应用/级联/回滚阶段失败。"""

    reason = FailureReason.CONFIG_UPDATE_FAILED
