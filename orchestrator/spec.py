"""模块契约：标识、版本、依赖与能力声明。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Version:
    """语义化版本（仅 major.minor.patch，足够表达兼容规则）。"""

    major: int
    minor: int
    patch: int = 0

    @classmethod
    def parse(cls, text: str) -> "Version":
        parts = text.split(".")
        if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
            raise ValueError(f"非法版本号: {text!r}")
        nums = [int(p) for p in parts] + [0] * (3 - len(parts))
        return cls(*nums)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class Dependency:
    """对另一个模块的依赖声明。

    - ``min_version``/``max_version_exclusive``：兼容区间 [min, max)。
    - ``required_capabilities``：被依赖模块必须提供的能力。
    """

    module_id: str
    min_version: Version | None = None
    max_version_exclusive: Version | None = None
    required_capabilities: frozenset[str] = frozenset()

    def accepts(self, version: Version) -> bool:
        """判断给定版本是否落在兼容区间 [min_version, max_version_exclusive) 内。"""
        v = (version.major, version.minor, version.patch)
        if self.min_version is not None:
            m = self.min_version
            if v < (m.major, m.minor, m.patch):
                return False
        if self.max_version_exclusive is not None:
            m = self.max_version_exclusive
            if v >= (m.major, m.minor, m.patch):
                return False
        return True


@dataclass(frozen=True)
class ModuleSpec:
    """模块的静态声明。"""

    module_id: str
    version: Version
    dependencies: tuple[Dependency, ...] = ()
    provides_capabilities: frozenset[str] = frozenset()
    # 模块声明的配置键集合，用于未知键校验
    config_keys: frozenset[str] = frozenset()


@runtime_checkable
class Module(Protocol):
    """模块运行期协议：框架只依赖该协议驱动生命周期。

    可选钩子（通过 ``getattr`` 探测，不属于本协议的必需方法）：
    - ``config_specs() -> list[ConfigKeySpec]``：声明配置键契约。
    - ``reconfigure(config, context) -> None``：运行期配置调整时优先调用；
      未提供时框架退化为 stop -> init -> start 的原子重载。
    """

    @property
    def spec(self) -> ModuleSpec: ...

    def init(self, config: dict[str, Any], context: "ModuleContext") -> None: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...


@dataclass
class ModuleContext:
    """注入给模块的运行期上下文：可访问已启动依赖的实例。"""

    _instances: dict[str, Any] = field(default_factory=dict)

    def get(self, module_id: str) -> Any:
        """取依赖模块实例；未启动时返回 None。"""
        return self._instances.get(module_id)
