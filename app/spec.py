"""模块声明：标识、版本、依赖与能力需求。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Version:
    """语义化版本（仅 major.minor.patch）。"""

    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text: str) -> "Version":
        parts = text.strip().split(".")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise ValueError(f"非法版本字符串: {text!r}（期望 major.minor.patch）")
        return cls(*(int(p) for p in parts))

    def compatible_with(self, required: "Version") -> bool:
        """实际版本 self 是否满足需求 required（同 major 且不低于 required）。"""
        if self.major != required.major:
            return False
        return (self.minor, self.patch) >= (required.minor, required.patch)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class Dependency:
    """对另一模块的依赖声明。"""

    module_id: str
    min_version: Version | None = None
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConfigField:
    """配置字段声明：类型与默认值。"""

    type: type
    default: object


@dataclass(frozen=True)
class ModuleSpec:
    """模块的静态声明。"""

    module_id: str
    version: Version
    provides: tuple[str, ...] = ()
    dependencies: tuple[Dependency, ...] = ()
    config_schema: dict[str, ConfigField] = field(default_factory=dict)
    legacy_keys: dict[str, str] = field(default_factory=dict)  # 旧键 -> 新键
