"""测试公共设施：可观测的假模块与统一用例日志。"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from orchestrator.spec import Dependency, ModuleContext, ModuleSpec, Version

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("orchestrator.tests")


def log_case(case: str, inputs: Any, decision: Any, final_state: Any) -> None:
    """单测日志规范：打印输入、决策依据与最终状态。"""
    logger.info(
        "用例=%s | 输入=%s | 决策依据=%s | 最终状态=%s",
        case, inputs, decision, final_state,
    )


class FakeModule:
    """记录调用次数、可注入失败的假模块。"""

    def __init__(
        self,
        module_id: str,
        version: str = "1.0.0",
        deps: tuple[Dependency, ...] = (),
        capabilities: frozenset[str] = frozenset(),
        fail_on_init: bool = False,
    ) -> None:
        self._spec = ModuleSpec(
            module_id=module_id,
            version=Version.parse(version),
            dependencies=deps,
            provides_capabilities=capabilities,
        )
        self.fail_on_init = fail_on_init
        self.init_count = 0
        self.start_count = 0
        self.stop_count = 0
        self.received_config: dict[str, Any] | None = None

    @property
    def spec(self) -> ModuleSpec:
        return self._spec

    def init(self, config: dict[str, Any], context: ModuleContext) -> None:
        self.init_count += 1
        self.received_config = config
        if self.fail_on_init:
            raise RuntimeError(f"{self.spec.module_id} 初始化被注入失败")

    def start(self) -> None:
        self.start_count += 1

    def stop(self) -> None:
        self.stop_count += 1


class ReloadableFake(FakeModule):
    """带 reload() 的假模块：记录热更新次数，可注入重载失败。

    fail_reload_when 接收新 config，返回 True 即拒绝该配置（模拟模块自身
    在热更新阶段做业务校验失败）。
    """

    def __init__(
        self,
        module_id: str,
        version: str = "1.0.0",
        deps: tuple[Dependency, ...] = (),
        capabilities: frozenset[str] = frozenset(),
        fail_on_init: bool = False,
        fail_reload_when=None,
    ) -> None:
        super().__init__(
            module_id,
            version=version,
            deps=deps,
            capabilities=capabilities,
            fail_on_init=fail_on_init,
        )
        self.reload_count = 0
        self.fail_reload_when = fail_reload_when

    def reload(self, config: dict[str, Any], context: ModuleContext) -> None:
        self.reload_count += 1
        if self.fail_reload_when is not None and self.fail_reload_when(config, context):
            raise RuntimeError(f"{self.spec.module_id} 拒绝新配置: {config}")
        self.received_config = config


@pytest.fixture
def make_module():
    return FakeModule
