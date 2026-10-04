"""运行期配置变更的结果记录、幂等去重与失败步骤追踪。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ChangeOutcome(str, Enum):
    """一次配置变更的最终结果。"""

    SUCCESS = "success"
    FAILED = "failed"


class ChangeStage(str, Enum):
    """变更流水线的阶段，失败时记录卡在哪一步。"""

    REQUEST = "request"
    VALIDATE = "validate"
    COMMIT_STORE = "commit_store"
    RELOAD = "reload"
    ENVIRONMENT_SWITCH = "environment_switch"


@dataclass(frozen=True)
class ChangeResult:
    """一次配置变更的可解释结果。"""

    outcome: ChangeOutcome
    change_id: str | None
    environment: str
    targets: tuple[str, ...]
    affected_modules: tuple[str, ...] = ()
    stage: ChangeStage | None = None
    failure_reason: str | None = None
    failure_detail: str | None = None
    failed_module: str | None = None
    sequence: int = 0
    replayed: bool = False
    requested_changes: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的纯数据（枚举转 value）。"""
        data = asdict(self)
        data["outcome"] = self.outcome.value
        data["stage"] = self.stage.value if self.stage else None
        return data


class ChangeLedger:
    """变更历史与幂等记录（由编排器持锁访问，自身不加锁）。"""

    def __init__(self) -> None:
        self._seq = 0
        self._last: ChangeResult | None = None
        self._last_by_module: dict[str, ChangeResult] = {}
        self._results_by_change_id: dict[str, ChangeResult] = {}
        self._requests_by_change_id: dict[str, tuple[Any, ...]] = {}

    def next_sequence(self) -> int:
        """领取一个单调递增序号，决定并发提交的确定先后。"""
        self._seq += 1
        return self._seq

    def check_idempotent(
        self, change_id: str, request_key: tuple[Any, ...]
    ) -> ChangeResult | None:
        """同一 change_id 的重复提交处理。

        - 未见过：登记请求签名，返回 None（调用方继续执行）。
        - 见过且请求签名一致：返回既有结果（replayed=True），调用方原样返回，
          既不重复生效也不报错。
        - 见过但请求签名不一致：change_id 被挪用于不同请求，抛 ValueError。
        """
        previous_key = self._requests_by_change_id.get(change_id)
        if previous_key is None:
            self._requests_by_change_id[change_id] = request_key
            return None
        if previous_key != request_key:
            raise ValueError(
                f"change_id={change_id!r} 已用于另一次不同的配置调整，拒绝复用"
            )
        previous = self._results_by_change_id[change_id]
        return ChangeResult(**{**asdict(previous), "replayed": True})

    def record(self, result: ChangeResult) -> ChangeResult:
        """登记一次最终结果：全局、按模块、按 change_id 各记一份。"""
        self._last = result
        for mid in result.targets:
            self._last_by_module[mid] = result
        if result.change_id is not None:
            self._results_by_change_id[result.change_id] = result
        return result

    def last_result(self) -> ChangeResult | None:
        return self._last

    def last_result_for(self, module_id: str) -> ChangeResult | None:
        return self._last_by_module.get(module_id)
