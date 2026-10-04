"""多部署环境的配置存储：每环境一份互不影响的配置覆盖层。"""

from __future__ import annotations

import copy
from typing import Any

from .errors import EnvironmentNotFoundError

DEFAULT_ENVIRONMENT = "default"


class EnvironmentStore:
    """保存 环境 -> (模块 -> 配置覆盖) 的两层映射。

    每个环境的配置相互独立；未显式配置的键回落由 LayeredConfig 负责解释。
    线程安全由编排器的互斥锁统一保证，本类自身不加锁。

    即便调用方未声明任何环境，也始终存在一个 ``default`` 环境；
    所有读写都带环境名，构造时与切换时都会校验环境已声明。
    """

    def __init__(
        self,
        environments: dict[str, dict[str, dict[str, Any]]] | None = None,
        active: str = DEFAULT_ENVIRONMENT,
    ) -> None:
        data: dict[str, dict[str, dict[str, Any]]] = {DEFAULT_ENVIRONMENT: {}}
        if environments:
            for env_name, module_configs in environments.items():
                data[env_name] = copy.deepcopy(module_configs)
        if active not in data:
            raise EnvironmentNotFoundError(
                f"初始环境不存在: {active}",
                context={"environment": active, "known": sorted(data)},
            )
        self._data = data
        self._active = active

    @property
    def active(self) -> str:
        """当前部署环境名。"""
        return self._active

    def names(self) -> list[str]:
        """全部已声明环境，按名字典序（结果稳定可解释）。"""
        return sorted(self._data)

    def assert_known(self, name: str) -> None:
        """环境未声明时抛 EnvironmentNotFoundError。"""
        if name not in self._data:
            raise EnvironmentNotFoundError(
                f"环境不存在: {name}",
                context={"environment": name, "known": self.names()},
            )

    def config_for(self, name: str, module_id: str) -> dict[str, Any]:
        """取某环境下某模块的环境层配置（副本，改副本不影响存储）。"""
        self.assert_known(name)
        return copy.deepcopy(self._data[name].get(module_id, {}))

    def snapshot(self, name: str, module_ids: list[str]) -> dict[str, dict[str, Any]]:
        """对若干模块的环境层配置做精确快照，供整批回滚使用。"""
        self.assert_known(name)
        return {
            mid: copy.deepcopy(self._data[name].get(mid, {})) for mid in module_ids
        }

    def merge_into(self, name: str, module_id: str, patch: dict[str, Any]) -> None:
        """把一次调整并入指定环境的模块配置（按键覆盖，不影响别的环境）。

        始终写入存储自己的副本：调用方随后继续持有/复用 patch 字典时，
        与存储之间不存在共享可变引用，restore 的删改不会回灌到调用方。
        """
        self.assert_known(name)
        bucket = self._data[name].setdefault(module_id, {})
        for key, value in patch.items():
            bucket[key] = copy.deepcopy(value)

    def restore(self, name: str, snapshots: dict[str, dict[str, Any]]) -> None:
        """按快照把环境层配置恢复到改动之前。"""
        self.assert_known(name)
        for module_id, previous in snapshots.items():
            if previous:
                self._data[name][module_id] = copy.deepcopy(previous)
            else:
                # 改动前不存在配置的模块，回滚后仍应"不存在"，杜绝串值
                self._data[name].pop(module_id, None)

    def switch(self, name: str) -> None:
        """把当前部署环境切换为 name（未声明即拒绝）。"""
        self.assert_known(name)
        self._active = name
