"""模块运行时基类。"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import ResolvedConfig


class ModuleBase:
    """模块运行时基类：子类实现 on_start/on_stop。"""

    def __init__(self) -> None:
        self.config: ResolvedConfig | None = None

    def on_start(self) -> None:
        pass

    def on_stop(self) -> None:
        pass
