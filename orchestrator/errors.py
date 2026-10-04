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
    RELOAD_FAILED = "reload_failed"
    CONFIG_SOURCE_REJECTED = "config_source_rejected"
    ENVIRONMENT_NOT_FOUND = "environment_not_found"


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


class ReloadFailedError(OrchestratorError):
    """模块运行期热更新（reload）失败。"""

    reason = FailureReason.RELOAD_FAILED


class ConfigSourceRejectedError(OrchestratorError):
    """运行期调整未声明可信来源，或来源不在允许集合内。"""

    reason = FailureReason.CONFIG_SOURCE_REJECTED


class EnvironmentNotFoundError(OrchestratorError):
    """切换/查询了一个从未声明的部署环境。"""

    reason = FailureReason.ENVIRONMENT_NOT_FOUND
