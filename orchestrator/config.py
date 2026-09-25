"""分层配置解析：默认值 < 服务配置 < 环境变量，含旧键迁移与严格校验。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from typing import Any, Callable

from .errors import (
    AmbiguousConfigOverrideError,
    InvalidConfigValueError,
    UnknownConfigKeyError,
)


class ConfigSource(str, Enum):
    """配置生效来源，用于对外解释。"""

    DEFAULT = "default"
    SERVICE = "service"
    ENV = "env"


@dataclass(frozen=True)
class ConfigKeySpec:
    """单个配置键的契约。

    - ``validator``：值校验/转换函数，非法值抛 ValueError。
    - ``legacy_keys``：旧键名，命中时按确定规则迁移。
    """

    key: str
    default: Any
    validator: Callable[[Any], Any] = lambda v: v
    legacy_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedValue:
    """解析结果：最终值 + 生效来源。"""

    value: Any
    source: ConfigSource


class LayeredConfig:
    """对单模块的配置做分层解析。

    层优先级：default < service < env。
    环境变量键形如 ``{ENV_PREFIX}{KEY大写}``。
    """

    ENV_PREFIX = "ORCH_"

    def __init__(self, specs: list[ConfigKeySpec]) -> None:
        self._specs = {s.key: s for s in specs}
        self._legacy_map = {
            legacy: s.key for s in specs for legacy in s.legacy_keys
        }

    def resolve(
        self,
        service_config: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
    ) -> dict[str, ResolvedValue]:
        """解析并校验，返回 key -> ResolvedValue。

        规则：
        - 未知键（既非声明键也非旧键）-> UnknownConfigKeyError
        - 校验失败 -> InvalidConfigValueError
        - 同一层内新旧键同时出现且值不一致 -> AmbiguousConfigOverrideError
        - 旧键迁移：旧键视为同层的新键别名，结果确定且来源可解释
        """
        service_config = service_config or {}
        env = env if env is not None else dict(os.environ)

        service_layer = self._normalize_layer(service_config, ConfigSource.SERVICE)
        env_layer = self._normalize_layer(
            self._extract_env(env), ConfigSource.ENV
        )

        resolved: dict[str, ResolvedValue] = {}
        for key, spec in self._specs.items():
            if key in env_layer:
                raw, source = env_layer[key], ConfigSource.ENV
            elif key in service_layer:
                raw, source = service_layer[key], ConfigSource.SERVICE
            else:
                raw, source = spec.default, ConfigSource.DEFAULT
            resolved[key] = ResolvedValue(self._validate(spec, raw, source), source)
        return resolved

    def _normalize_layer(
        self, raw: dict[str, Any], source: ConfigSource
    ) -> dict[str, Any]:
        """把一层的原始键归一到声明键上，处理旧键迁移与歧义检测。"""
        normalized: dict[str, Any] = {}
        for raw_key, value in raw.items():
            if raw_key in self._specs:
                canonical = raw_key
            elif raw_key in self._legacy_map:
                canonical = self._legacy_map[raw_key]
            else:
                raise UnknownConfigKeyError(
                    f"未知配置键: {raw_key}（来源 {source.value}）",
                    context={"key": raw_key, "source": source.value},
                )
            if canonical in normalized and normalized[canonical] != value:
                raise AmbiguousConfigOverrideError(
                    f"配置键 {canonical} 在 {source.value} 层被新旧键同时指定且值不一致",
                    context={
                        "key": canonical,
                        "source": source.value,
                        "conflicting_value": value,
                        "existing_value": normalized[canonical],
                    },
                )
            normalized[canonical] = value
        return normalized

    def _extract_env(self, env: dict[str, str]) -> dict[str, Any]:
        """从环境变量中挑出本模块相关键，剥离前缀并还原旧键名。"""
        picked: dict[str, Any] = {}
        known_env_names: dict[str, str] = {}
        for spec in self._specs.values():
            known_env_names[self.ENV_PREFIX + spec.key.upper()] = spec.key
            for legacy in spec.legacy_keys:
                known_env_names[self.ENV_PREFIX + legacy.upper()] = legacy
        for env_name, value in env.items():
            if env_name in known_env_names:
                picked[known_env_names[env_name]] = value
        return picked

    def _validate(self, spec: ConfigKeySpec, raw: Any, source: ConfigSource) -> Any:
        try:
            return spec.validator(raw)
        except (ValueError, TypeError) as exc:
            raise InvalidConfigValueError(
                f"配置键 {spec.key} 的值 {raw!r} 非法（来源 {source.value}）: {exc}",
                context={"key": spec.key, "value": raw, "source": source.value},
            ) from exc
