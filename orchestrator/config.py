"""分层配置解析：默认值 < 服务配置 < 环境变量，含旧键迁移与严格校验。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


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
        raise NotImplementedError
