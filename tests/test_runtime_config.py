"""运行期配置热更新、环境隔离、级联回滚与并发一致性。"""

from __future__ import annotations

import threading

import pytest

from orchestrator import Orchestrator
from orchestrator.config import ConfigKeySpec
from orchestrator.errors import (
    AmbiguousConfigOverrideError,
    ConfigSourceRejectedError,
    EnvironmentNotFoundError,
    FailureReason,
    InvalidConfigValueError,
    ReloadFailedError,
    UnknownConfigKeyError,
)
from orchestrator.registry import ModuleRegistry
from orchestrator.spec import Dependency, ModuleSpec, Version

from .conftest import ReloadableFake, log_case


def positive_int(value):
    iv = int(value)
    if iv <= 0:
        raise ValueError("必须为正整数")
    return iv


class ConfiguredReloadable(ReloadableFake):
    """带 workers 配置键（含旧键 worker_count）的可热更新模块。"""

    def config_specs(self):
        return [
            ConfigKeySpec(
                "workers", 2, positive_int, legacy_keys=("worker_count",)
            ),
            ConfigKeySpec("mode", "dev"),
        ]


def _make(*modules, service_config=None, env=None, environments=None, active="default"):
    registry = ModuleRegistry()
    for m in modules:
        registry.register(m)
    return Orchestrator(
        registry,
        service_config=service_config,
        env=env,
        environments=environments,
        active_environment=active,
    )


def chain(a, b, c=None):
    """构造 a <- b <- c 依赖链（被依赖方向）。"""
    b._spec = ModuleSpec(
        b.spec.module_id, b.spec.version, dependencies=(Dependency("a"),)
    )
    if c is not None:
        c._spec = ModuleSpec(
            c.spec.module_id, c.spec.version, dependencies=(Dependency("b"),)
        )
    return [m for m in (a, b, c) if m is not None]


def test_hot_update_succeeds_without_restart_and_value_source_visible():
    a = ConfiguredReloadable("a")
    orch = _make(a, service_config={"a": {"workers": 4}}, env={})
    orch.start_all()
    assert a.received_config == {"workers": 4, "mode": "dev"}

    result = orch.update_config({"a": {"workers": 8}}, change_id="hot-1")

    explanation = orch.config_explanation("a")
    log_case(
        "热更新成功",
        {"启动(service workers=4)": 4, "热更新(environment workers=8)": 8},
        "热更新并入当前环境层(优先级高于 service)，reload 原地生效不重启",
        {
            "result": result.as_dict()["outcome"],
            "module_config": a.received_config,
            "explanation": explanation,
            "reload_count": a.reload_count,
        },
    )
    assert result.outcome.value == "success"
    assert result.affected_modules == ("a",)
    assert a.received_config["workers"] == 8
    assert a.reload_count == 1
    # 进程内生命周期没有 stop/start：start 只发生一次、stop 为 0
    assert a.start_count == 1 and a.stop_count == 0
    # 当前生效值与逐层来源都可查
    assert explanation["workers"] == {"value": 8, "source": "environment"}
    # 未在环境层显式配置的键，来源仍是 service（回落可解释）
    assert explanation["mode"] == {"value": "dev", "source": "default"}


def test_hot_update_via_legacy_key_migrates_and_reloads():
    a = ConfiguredReloadable("a")
    orch = _make(a, env={})
    orch.start_all()
    result = orch.update_config({"a": {"worker_count": 5}}, change_id="legacy-1")
    log_case(
        "热更新走旧键",
        {"worker_count": 5},
        "旧键在环境层归一到 workers 后校验生效",
        {"workers": a.received_config["workers"], "source": orch.config_explanation("a")["workers"]["source"]},
    )
    assert result.outcome.value == "success"
    assert a.received_config["workers"] == 5
    assert orch.config_explanation("a")["workers"]["source"] == "environment"


@pytest.mark.parametrize(
    "patch,expected_reason,expected_error",
    [
        ({"surprise": 1}, FailureReason.UNKNOWN_CONFIG_KEY, UnknownConfigKeyError),
        ({"workers": -3}, FailureReason.INVALID_CONFIG_VALUE, InvalidConfigValueError),
        (
            {"workers": 8, "worker_count": 9},
            FailureReason.AMBIGUOUS_CONFIG_OVERRIDE,
            AmbiguousConfigOverrideError,
        ),
    ],
)
def test_validation_failure_keeps_old_config_intact(
    patch, expected_reason, expected_error
):
    a = ConfiguredReloadable("a")
    orch = _make(a, service_config={"a": {"workers": 4}}, env={})
    orch.start_all()
    before = dict(a.received_config)

    with pytest.raises(expected_error) as exc:
        orch.update_config({"a": patch}, change_id=f"bad-{expected_reason.value}")

    last = orch.last_change()
    log_case(
        f"校验拒绝({expected_reason.value})",
        {"patch": patch, "旧配置": before},
        "严格校验在任何重载之前拒绝；试探写入被回滚，reload 不触发",
        {
            "stage": last.stage.value if last.stage else None,
            "reason": last.failure_reason,
            "module_config": a.received_config,
            "explain": orch.config_explanation("a"),
            "reload_count": a.reload_count,
        },
    )
    assert exc.value.reason is expected_reason
    assert last.stage.value == "validate"
    assert last.failure_reason == expected_reason.value
    # 旧配置完整生效：值、来源都没变，模块没有被重载
    assert a.received_config == before
    assert orch.effective_config("a")["workers"] == 4
    assert orch.config_explanation("a")["workers"]["source"] == "service"
    assert a.reload_count == 0


def test_unknown_source_rejected_before_any_change():
    a = ConfiguredReloadable("a")
    orch = _make(a, env={})
    orch.start_all()
    before = dict(a.received_config)

    with pytest.raises(ConfigSourceRejectedError) as exc:
        orch.update_config({"a": {"workers": 8}}, source="mystery")

    last = orch.last_change()
    log_case(
        "来源不明拒绝",
        {"source": "mystery"},
        "运行期只接受可信来源(默认 runtime)，来源不明在 request 阶段拒绝",
        {"stage": last.stage.value, "config": a.received_config},
    )
    assert exc.value.reason is FailureReason.CONFIG_SOURCE_REJECTED
    assert last.stage.value == "request"
    assert a.received_config == before
    assert a.reload_count == 0


def test_environments_do_not_leak_and_fallback_is_explainable():
    a = ConfiguredReloadable("a")
    b = ConfiguredReloadable("b")
    chain(a, b)
    orch = _make(
        a,
        b,
        service_config={"a": {"workers": 4, "mode": "svc"}},
        env={},
        environments={
            "dev": {"a": {"workers": 10}},
            "prod": {"a": {"workers": 20, "mode": "prod"}, "b": {"workers": 30}},
        },
        active="dev",
    )
    orch.start_all()
    # dev：workers 来自环境层；mode 未在 dev 显式配置，回落到 service
    log_case(
        "环境回落",
        {"service": {"a.workers": 4, "a.mode": "svc"}, "dev": {"a.workers": 10}},
        "dev 显式键取 environment，未显式键回落到 service",
        orch.config_explanation("a"),
    )
    assert orch.effective_config("a") == {"workers": 10, "mode": "svc"}
    assert orch.config_explanation("a")["workers"]["source"] == "environment"
    assert orch.config_explanation("a")["mode"]["source"] == "service"

    # 切到 prod：dev 的值不得串入
    orch.switch_environment("prod")
    log_case(
        "切换不串值",
        "dev.workers=10 -> prod.workers=20/prod.mode=prod",
        "切换后按 prod 层重新分层，与 dev 完全独立",
        {"a": orch.effective_config("a"), "b": orch.effective_config("b")},
    )
    assert orch.effective_config("a") == {"workers": 20, "mode": "prod"}
    assert orch.effective_config("b") == {"workers": 30, "mode": "dev"}

    # 在 prod 热更新，只动 prod；切回 dev 看不到这次修改
    orch.update_config({"a": {"workers": 21}}, change_id="prod-tune")
    orch.switch_environment("dev")
    log_case(
        "热更新按环境隔离",
        "prod 热更新 a.workers=21 后切回 dev",
        "热更新写入当前环境(prod)的环境层，dev 不受影响",
        {"dev.a": orch.effective_config("a"), "env": orch.environment},
    )
    assert orch.effective_config("a")["workers"] == 10
    orch.switch_environment("prod")
    assert orch.effective_config("a")["workers"] == 21

    # 再切到一个完全没有显式配置的 default：回落到 service/default
    orch.switch_environment("default")
    log_case(
        "空环境回落",
        "切到无任何显式配置的 default",
        "environment 层为空 -> workers 回落 service，mode 回落 default",
        orch.config_explanation("a"),
    )
    assert orch.effective_config("a") == {"workers": 4, "mode": "svc"}


def test_unknown_environment_rejected_without_state_change():
    a = ConfiguredReloadable("a")
    orch = _make(a, env={}, environments={"dev": {}}, active="default")
    orch.start_all()
    with pytest.raises(EnvironmentNotFoundError) as exc:
        orch.switch_environment("staging")
    last = orch.last_change()
    log_case(
        "未知环境",
        "切换到从未声明的 staging",
        "request 阶段拒绝，环境与模块配置均不变",
        {"environment": orch.environment, "stage": last.stage.value, "reason": exc.value.reason},
    )
    assert exc.value.reason is FailureReason.ENVIRONMENT_NOT_FOUND
    assert orch.environment == "default"
    assert orch.effective_config("a")["workers"] == 2


def test_environment_switch_validation_failure_rolls_back_whole_batch():
    """prod 存在非法值：预校验失败 -> 不切换、不重载任何模块。"""
    a = ConfiguredReloadable("a")
    b = ConfiguredReloadable("b")
    chain(a, b)
    orch = _make(
        a,
        b,
        env={},
        environments={"prod": {"a": {"workers": -1}}},
        active="default",
    )
    orch.start_all()
    reloads_before = (a.reload_count, b.reload_count)
    with pytest.raises(InvalidConfigValueError):
        orch.switch_environment("prod")
    log_case(
        "环境切换预校验失败",
        "prod.a.workers=-1（非法）",
        "切换前对全部运行模块预校验，任一失败整批拒绝，留在 default",
        {
            "env": orch.environment,
            "a": orch.effective_config("a"),
            "reloads": (a.reload_count, b.reload_count),
        },
    )
    assert orch.environment == "default"
    assert orch.effective_config("a")["workers"] == 2
    assert (a.reload_count, b.reload_count) == reloads_before


def test_cascade_reloads_dependents_in_topological_order():
    order: list[str] = []
    a = ConfiguredReloadable("a")
    b = ConfiguredReloadable("b")
    c = ConfiguredReloadable("c")
    chain(a, b, c)
    for m in (a, b, c):
        m.reload = (
            lambda cfg, ctx, mid=m.spec.module_id: (order.append(mid), setattr(m, "received_config", cfg))
        )
    orch = _make(a, b, c, env={})
    orch.start_all()
    order.clear()

    result = orch.update_config({"a": {"workers": 7}}, change_id="cascade-ok")
    log_case(
        "依赖级联顺序",
        "只改 a（b、c 传递依赖 a）",
        "a,b,c 全部重载，顺序必须与拓扑加载顺序一致：依赖先于依赖方",
        {"reload_order": order, "affected": result.affected_modules},
    )
    assert order == ["a", "b", "c"]
    assert result.affected_modules == ("a", "b", "c")


def test_cascade_rollback_restores_consistent_old_state_when_dependent_rejects():
    a = ConfiguredReloadable("a")
    b = ConfiguredReloadable(
        "b",
        fail_reload_when=lambda cfg, ctx: ctx.get("a") is not None
        and ctx.get("a").received_config.get("workers") == 99,
    )
    c = ConfiguredReloadable("c")
    chain(a, b, c)
    orch = _make(a, b, c, service_config={"a": {"workers": 4}}, env={})
    orch.start_all()
    old = {"a": dict(a.received_config), "b": dict(b.received_config), "c": dict(c.received_config)}

    with pytest.raises(ReloadFailedError) as exc:
        orch.update_config({"a": {"workers": 99}}, change_id="cascade-bad")

    last = orch.last_change()
    log_case(
        "级联失败整批回滚",
        {"a.workers": 99, "b 策略": "发现 a=99 即拒绝"},
        "a 先重载成功，b 拒绝 -> 存储回滚 + 模块按拓扑序回到旧配置，杜绝新a旧b组合",
        {
            "stage": last.stage.value,
            "failed_module": last.failed_module,
            "a": a.received_config,
            "b": b.received_config,
            "c": c.received_config,
            "states": {m: orch.status(m).state.value for m in ("a", "b", "c")},
        },
    )
    assert exc.value.reason is FailureReason.RELOAD_FAILED
    assert last.stage.value == "reload"
    assert last.failed_module == "b"
    # 所有模块都回到改动前的旧值，且仍是 running
    assert a.received_config == old["a"]
    assert b.received_config == old["b"]
    assert c.received_config == old["c"]
    assert {m: orch.status(m).state.value for m in ("a", "b", "c")} == {
        "a": "running",
        "b": "running",
        "c": "running",
    }
    # 环境层存储也被回滚：当前生效值重新回落 service
    assert orch.effective_config("a")["workers"] == 4


def test_cascade_rollback_recovers_when_target_module_reload_fails():
    """目标模块自身 reload 失败：它与全部依赖方都保持旧配置。"""
    a = ConfiguredReloadable(
        "a", fail_reload_when=lambda cfg, ctx: cfg["workers"] == 99
    )
    b = ConfiguredReloadable("b")
    chain(a, b)
    orch = _make(a, b, env={})
    orch.start_all()
    with pytest.raises(ReloadFailedError) as exc:
        orch.update_config({"a": {"workers": 99}}, change_id="target-bad")
    log_case(
        "目标自身重载失败",
        {"a.workers": 99, "a 策略": "reload 直接拒绝"},
        "目标拒绝 -> 存储回滚、目标与依赖方均维持旧配置、状态 running",
        {
            "failed": exc.value.context.get("failed_module"),
            "a": a.received_config,
            "b": b.received_config,
            "state_a": orch.status("a").state.value,
        },
    )
    assert exc.value.context["failed_module"] == "a"
    assert a.received_config["workers"] == 2
    assert b.received_config["workers"] == 2
    assert orch.status("a").state.value == "running"


def test_duplicate_change_id_is_replayed_not_applied_twice():
    a = ConfiguredReloadable("a")
    orch = _make(a, env={})
    orch.start_all()
    payload = {"a": {"workers": 5}}

    first = orch.update_config(payload, change_id="idem-1")
    reloads_after_first = a.reload_count
    second = orch.update_config(payload, change_id="idem-1")

    log_case(
        "重复提交幂等",
        {"change_id": "idem-1", "payload": payload},
        "相同 change_id+相同请求 -> 回放原结果，不再次重载、不报错",
        {
            "first_seq": first.sequence,
            "second_seq": second.sequence,
            "second_replayed": second.replayed,
            "reload_count": a.reload_count,
        },
    )
    assert first.replayed is False
    assert second.replayed is True
    assert second.sequence == first.sequence
    assert a.reload_count == reloads_after_first == 1
    assert orch.effective_config("a")["workers"] == 5


def test_change_id_reused_for_different_request_is_rejected():
    a = ConfiguredReloadable("a")
    orch = _make(a, env={})
    orch.start_all()
    orch.update_config({"a": {"workers": 5}}, change_id="dup-id")
    with pytest.raises(ValueError):
        orch.update_config({"a": {"workers": 6}}, change_id="dup-id")
    log_case(
        "change_id 挪用拒绝",
        {"dup-id": ["workers=5", "workers=6"]},
        "同一 change_id 对应不同请求体 -> 拒绝，防止去重键被误复用",
        orch.effective_config("a"),
    )
    assert orch.effective_config("a")["workers"] == 5


def test_concurrent_changes_are_serializable_and_consistent():
    a = ConfiguredReloadable("a")
    b = ConfiguredReloadable("b")
    chain(a, b)
    orch = _make(a, b, env={})
    orch.start_all()

    # 20 个线程，各以不同 change_id 把 a.workers 设成自己的线程编号
    n = 20
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []
    results: list = []

    def worker(i: int):
        try:
            barrier.wait()
            results.append(
                orch.update_config({"a": {"workers": 100 + i}}, change_id=f"t-{i}")
            )
        except BaseException as exc:  # noqa: BLE001 - 收集线程内失败
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final_workers = orch.effective_config("a")["workers"]
    last = orch.last_change()
    log_case(
        "并发热更新",
        "20 线程同时对 a 提不同值(100..119)",
        "互斥锁串行化 -> 最终值唯一，等于某个线程的输入；序号 1..20 无重复",
        {
            "errors": len(errors),
            "final_workers": final_workers,
            "last_change_id": last.change_id,
            "seq_range": (min(r.sequence for r in results), max(r.sequence for r in results)),
            "b_stays_in_sync": orch.effective_config("b"),
        },
    )
    assert not errors
    # 最终值必然是某一个线程提交的值（等价于某一确定串行顺序）
    assert final_workers in {100 + i for i in range(n)}
    # 最后生效的 change_id 就是设置最终值的那次；序号唯一连续
    assert last.change_id == f"t-{final_workers - 100}"
    sequences = sorted(r.sequence for r in results)
    assert sequences == list(range(1, n + 1))
    # 每一次成功的变更都级联到 b：a/b reload 次数一致（1 次启动 + n 次热更新）
    assert a.reload_count == b.reload_count == n


def test_concurrent_changes_with_one_invalid_batch_leave_consistent_state():
    """并发中混入非法批次：只有合法批次生效，任一时刻读到的都是完整一套值。"""
    a = ConfiguredReloadable("a")
    b = ConfiguredReloadable("b")
    chain(a, b)
    orch = _make(a, b, env={})
    orch.start_all()

    payloads = [
        {"a": {"workers": 50}},
        {"a": {"workers": -1}},   # 非法
        {"a": {"nope": 1}},       # 未知键
        {"a": {"workers": 60}},
        {"a": {"workers": 70}},
    ]
    barrier = threading.Barrier(len(payloads))
    outcomes: list[tuple[str, str]] = []

    def worker(idx: int, change: dict):
        barrier.wait()
        try:
            r = orch.update_config(change, change_id=f"mix-{idx}")
            outcomes.append((f"mix-{idx}", r.outcome.value))
        except Exception:
            outcomes.append((f"mix-{idx}", "failed"))
    threads = [
        threading.Thread(target=worker, args=(i, change))
        for i, change in enumerate(payloads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final_workers = orch.effective_config("a")["workers"]
    log_case(
        "并发混批一致性",
        payloads,
        "非法批次在 validate 阶段拒绝且不提交；最终值为某个合法输入，b 同步一致",
        {"outcomes": sorted(outcomes), "a": orch.effective_config("a"), "b": orch.effective_config("b")},
    )
    assert final_workers in {50, 60, 70}
    # 非法值绝无可能成为最终态
    assert orch.status("a").state.value == "running"
    assert orch.status("b").state.value == "running"


def test_concurrent_changes_to_different_modules_do_not_corrupt_store():
    """不同模块的并发热更新：存储不串键，每模块最终值来自各自某次提交。

    回归覆盖：存储与调用方字典共享可变引用、restore 删改回灌导致的串位问题。
    """
    mods = [ConfiguredReloadable(f"m{i}") for i in range(8)]
    orch = _make(*mods, env={})
    orch.start_all()

    rounds = 30
    barrier = threading.Barrier(len(mods))
    errors: list[BaseException] = []

    def round_worker(midx: int, round_no: int):
        patch = {"workers": 1000 + midx * 100 + round_no}
        try:
            barrier.wait()
            orch.update_config({f"m{midx}": patch}, change_id=f"m{midx}-r{round_no}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    for round_no in range(rounds):
        threads = [
            threading.Thread(target=round_worker, args=(i, round_no))
            for i in range(len(mods))
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    log_case(
        "并发跨模块存储隔离",
        "8 模块 x 30 轮并发，每模块每轮提交自己命名空间内的值",
        "存储按模块隔离 + 写入深拷贝 -> 无串键、无异常",
        {
            "errors": len(errors),
            "final": {
                f"m{i}": orch.effective_config(f"m{i}")["workers"]
                for i in range(len(mods))
            },
        },
    )
    assert not errors
    for i in range(len(mods)):
        value = orch.effective_config(f"m{i}")["workers"]
        assert 1000 + i * 100 <= value < 1000 + i * 100 + rounds
        assert orch.status(f"m{i}").state.value == "running"


def test_update_for_stopped_module_takes_effect_on_next_start_without_reload():
    from .conftest import FakeModule

    class Plain(FakeModule):
        def config_specs(self):
            return [ConfigKeySpec("workers", 2, positive_int)]

    a = Plain("a")
    orch = _make(a, env={})
    # 模块尚未启动：没有运行实例可重载，配置只写入环境层
    result = orch.update_config({"a": {"workers": 11}}, change_id="pre-start")
    log_case(
        "未运行模块预置配置",
        {"a.state": orch.status("a").state.value, "change": {"workers": 11}},
        "无运行实例则不级联重载，配置落环境层，下次启动读到新值",
        {"affected": result.affected_modules, "reload_possible": hasattr(a, "reload")},
    )
    assert result.affected_modules == ()
    orch.start_all()
    assert a.received_config["workers"] == 11
    assert a.init_count == 1


def test_rebuildable_module_hot_update_and_cascade_rollback():
    """无 reload() 的模块走 stop->init->start 重建；失败同样整批回滚。"""
    from .conftest import FakeModule

    class PlainA(FakeModule):
        def config_specs(self):
            return [ConfigKeySpec("workers", 2, positive_int)]

    class PlainB(FakeModule):
        def config_specs(self):
            return [ConfigKeySpec("workers", 2, positive_int)]

        def init(self, config, context):
            super().init(config, context)
            if context.get("a") and context.get("a").received_config.get("workers") == 99:
                raise RuntimeError("b 无法在 a=99 下重建")

    a = PlainA("a")
    b = PlainB("b")
    chain(a, b)
    orch = _make(a, b, env={})
    orch.start_all()

    # 成功路径：重建型模块热更新 = stop+init+start，进程不重启
    r = orch.update_config({"a": {"workers": 5}}, change_id="rebuild-ok")
    log_case(
        "重建型热更新",
        "a 无 reload，改 workers=5",
        "框架 stop->init->start 同进程重建，b 级联重建",
        {"a": a.received_config, "b": b.received_config, "a.stops": a.stop_count},
    )
    assert r.outcome.value == "success"
    assert a.received_config["workers"] == 5
    assert a.stop_count == 1 and a.start_count == 2

    # 失败路径：b 重建失败 -> 整批回滚到旧值，状态恢复 running
    with pytest.raises(ReloadFailedError):
        orch.update_config({"a": {"workers": 99}}, change_id="rebuild-bad")
    log_case(
        "重建型级联回滚",
        {"a.workers": 99, "b": "init 拒绝 a=99"},
        "逆序停+拓扑序用旧配置重建，a/b 回到 workers=5 且 running",
        {
            "a": a.received_config,
            "b": b.received_config,
            "states": {m: orch.status(m).state.value for m in ("a", "b")},
        },
    )
    assert a.received_config["workers"] == 5
    assert b.received_config["workers"] == 2
    assert orch.status("a").state.value == "running"
    assert orch.status("b").state.value == "running"


def test_environment_switch_reload_failure_switches_back_and_rolls_back():
    """切换后重载途中失败：切回原环境，模块整批回到原配置且 running。"""
    a = ConfiguredReloadable("a")
    b = ConfiguredReloadable(
        "b", fail_reload_when=lambda cfg, ctx: cfg.get("workers") == 50
    )
    chain(a, b)
    orch = _make(
        a,
        b,
        env={},
        environments={"prod": {"b": {"workers": 50}}},
        active="default",
    )
    orch.start_all()
    old_a, old_b = dict(a.received_config), dict(b.received_config)

    with pytest.raises(ReloadFailedError) as exc:
        orch.switch_environment("prod")
    last = orch.last_change()
    log_case(
        "环境切换重载失败回滚",
        {"prod": {"b.workers": 50}, "b 策略": "workers=50 即拒绝"},
        "重载 b 失败 -> 切回 default，模块按原配置整批回滚，无跨环境混合态",
        {
            "stage": last.stage.value,
            "failed": last.failed_module,
            "env": orch.environment,
            "a": a.received_config,
            "b": b.received_config,
        },
    )
    assert exc.value.reason is FailureReason.RELOAD_FAILED
    assert last.stage.value == "environment_switch"
    assert last.failed_module == "b"
    assert orch.environment == "default"
    assert a.received_config == old_a and b.received_config == old_b
    assert orch.status("a").state.value == "running"
    assert orch.status("b").state.value == "running"
