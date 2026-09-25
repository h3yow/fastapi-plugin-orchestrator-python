"""分层配置解析：默认值 < 服务配置 < 环境变量，含旧键迁移。"""
from __future__ import annotations

from dataclasses import dataclass

from app.errors import (
    AmbiguousConfigError,
    InvalidConfigValueError,
    UnknownConfigKeyError,
)
from app.spec import ModuleSpec

# 来源标识
SOURCE_DEFAULT = "default"
SOURCE_SERVICE = "service"
SOURCE_ENV = "env"


@dataclass(frozen=True)
class ResolvedConfig:
    """某模块解析后的配置及其来源。"""

    values: dict[str, object]
    sources: dict[str, str]  # key -> default | service | env | legacy:<旧键>


def _check_type(module_id: str, key: str, value: object, expected: type) -> None:
    """严格类型校验（bool 不被当作 int 接受）。"""
    if expected is bool:
        ok = isinstance(value, bool)
    elif expected is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif expected is float:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    else:
        ok = isinstance(value, expected)
    if not ok:
        raise InvalidConfigValueError(
            f"模块 {module_id!r} 配置 {key!r} 期望类型 {expected.__name__}，"
            f"实际为 {type(value).__name__}: {value!r}"
        )


def _parse_env_value(module_id: str, key: str, raw: str, expected: type) -> object:
    """把环境变量字符串解析为目标类型，失败即拒绝。"""
    if expected is bool:
        lowered = raw.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise InvalidConfigValueError(
            f"模块 {module_id!r} 配置 {key!r} 的环境变量值 {raw!r} 不是合法布尔值"
        )
    if expected is int:
        try:
            return int(raw.strip())
        except ValueError:
            raise InvalidConfigValueError(
                f"模块 {module_id!r} 配置 {key!r} 的环境变量值 {raw!r} 不是合法整数"
            ) from None
    if expected is float:
        try:
            return float(raw.strip())
        except ValueError:
            raise InvalidConfigValueError(
                f"模块 {module_id!r} 配置 {key!r} 的环境变量值 {raw!r} 不是合法浮点数"
            ) from None
    if expected is str:
        return raw
    raise InvalidConfigValueError(
        f"模块 {module_id!r} 配置 {key!r} 声明了不支持的类型 {expected!r}"
    )


class ConfigResolver:
    """按模块 schema 解析并校验配置。

    分层优先级：环境变量 > 服务配置 > 默认值。
    旧键在服务配置层内迁移：旧键存在且新键缺失时迁移；
    两者同时存在且取值冲突时报 AmbiguousConfigError。
    """

    def __init__(self, service_config: dict[str, dict[str, object]] | None = None,
                 env: dict[str, str] | None = None,
                 env_prefix: str = "APP_") -> None:
        self._service_config = service_config or {}
        self._env = env or {}
        self._env_prefix = env_prefix

    def _env_key(self, module_id: str, key: str) -> str:
        safe_mod = module_id.upper().replace("-", "_")
        return f"{self._env_prefix}{safe_mod}__{key.upper()}"

    def resolve(self, spec: ModuleSpec) -> ResolvedConfig:
        module_id = spec.module_id
        schema = spec.config_schema
        legacy = spec.legacy_keys
        values: dict[str, object] = {}
        sources: dict[str, str] = {}

        # 第 1 层：默认值
        for key, fld in schema.items():
            values[key] = fld.default
            sources[key] = SOURCE_DEFAULT

        # 第 2 层：服务配置（含旧键迁移与未知键拒绝）
        raw_service = dict(self._service_config.get(module_id, {}))
        for old_key in list(raw_service):
            if old_key in legacy:
                new_key = legacy[old_key]
                old_val = raw_service.pop(old_key)
                if new_key in raw_service:
                    if raw_service[new_key] != old_val:
                        raise AmbiguousConfigError(
                            f"模块 {module_id!r} 的旧键 {old_key!r} 与新键 {new_key!r} "
                            f"同时存在且取值冲突: {old_val!r} != {raw_service[new_key]!r}"
                        )
                    # 取值一致：确定性地采用新键，来源仍标记为 service
                else:
                    raw_service[new_key] = old_val
                    sources[new_key] = f"legacy:{old_key}"
        for key, val in raw_service.items():
            if key not in schema:
                raise UnknownConfigKeyError(
                    f"模块 {module_id!r} 的服务配置包含未知键 {key!r}"
                )
            _check_type(module_id, key, val, schema[key].type)
            values[key] = val
            sources.setdefault(key, SOURCE_SERVICE)
            if sources[key] == SOURCE_DEFAULT:
                sources[key] = SOURCE_SERVICE

        # 第 3 层：环境变量（前缀匹配，未知键同样拒绝）
        env_prefix_for_module = self._env_key(module_id, "")  # 形如 APP_MOD__
        for env_key, raw in self._env.items():
            if not env_key.startswith(env_prefix_for_module):
                continue
            key = env_key[len(env_prefix_for_module):].lower()
            if key not in schema:
                raise UnknownConfigKeyError(
                    f"模块 {module_id!r} 的环境变量 {env_key!r} 对应未知配置键 {key!r}"
                )
            values[key] = _parse_env_value(module_id, key, raw, schema[key].type)
            sources[key] = SOURCE_ENV

        return ResolvedConfig(values=values, sources=sources)
