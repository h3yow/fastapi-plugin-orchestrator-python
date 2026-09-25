import logging

import pytest


@pytest.fixture(autouse=True)
def _test_log(caplog):
    """让每个用例的日志（输入、决策依据、最终状态）进入 pytest 日志。"""
    caplog.set_level(logging.INFO)
    yield
