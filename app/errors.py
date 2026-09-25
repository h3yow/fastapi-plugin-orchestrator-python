"""框架错误类型：所有启动期拒绝原因都必须可区分。"""
from __future__ import annotations


class FrameworkError(Exception):
    """框架错误基类。"""


class DuplicateRegistrationError(FrameworkError):
    """重复注册同一模块标识。"""


class MissingDependencyError(FrameworkError):
    """依赖的模块未注册。"""


class IncompatibleDependencyError(FrameworkError):
    """依赖的版本或能力不兼容。"""


class CyclicDependencyError(FrameworkError):
    """模块依赖关系存在环。"""


class ConfigError(FrameworkError):
    """配置解析失败的基类。"""


class UnknownConfigKeyError(ConfigError):
    """出现未知配置键。"""


class InvalidConfigValueError(ConfigError):
    """配置值非法。"""


class AmbiguousConfigError(ConfigError):
    """新旧配置键同时出现且取值冲突。"""


class ModuleStateError(FrameworkError):
    """生命周期状态机不允许的操作。"""
