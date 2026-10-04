"""可插拔功能模块运行框架。"""

from .config import ConfigKeySpec, ConfigSource, LayeredConfig, ResolvedValue
from .dependencies import resolve_load_order
from .environments import DEFAULT_ENVIRONMENT, EnvironmentStore
from .errors import FailureReason, OrchestratorError
from .orchestrator import ModuleState, ModuleStatus, Orchestrator
from .registry import ModuleRegistry
from .runtime import ChangeLedger, ChangeOutcome, ChangeResult, ChangeStage
from .spec import Dependency, Module, ModuleContext, ModuleSpec, ReloadableModule, Version

__all__ = [
    "DEFAULT_ENVIRONMENT",
    "ChangeLedger",
    "ChangeOutcome",
    "ChangeResult",
    "ChangeStage",
    "ConfigKeySpec",
    "ConfigSource",
    "Dependency",
    "EnvironmentStore",
    "FailureReason",
    "LayeredConfig",
    "Module",
    "ModuleContext",
    "ModuleRegistry",
    "ModuleSpec",
    "ModuleState",
    "ModuleStatus",
    "Orchestrator",
    "OrchestratorError",
    "ReloadableModule",
    "ResolvedValue",
    "Version",
    "resolve_load_order",
]
