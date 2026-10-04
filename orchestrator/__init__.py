"""可插拔功能模块运行框架。"""

from .config import ConfigKeySpec, ConfigSource, LayeredConfig, ResolvedValue
from .dependencies import resolve_load_order
from .errors import FailureReason, OrchestratorError
from .orchestrator import ModuleState, ModuleStatus, Orchestrator, UpdateRecord
from .registry import ModuleRegistry
from .spec import Dependency, Module, ModuleContext, ModuleSpec, Version

__all__ = [
    "ConfigKeySpec",
    "ConfigSource",
    "Dependency",
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
    "ResolvedValue",
    "UpdateRecord",
    "Version",
    "resolve_load_order",
]
