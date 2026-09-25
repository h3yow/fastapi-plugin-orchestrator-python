"""配置分层：默认值 < 服务配置 < 环境变量，旧键迁移与严格校验。"""
import logging

import pytest

from app.config import ConfigResolver
from app.errors import (
    AmbiguousConfigError,
    InvalidConfigValueError,
    UnknownConfigKeyError,
)
from app.spec import ConfigField
from tests.helpers import make_spec

log = logging.getLogger(__name__)

SPEC = make_spec(
    "store",
    config_schema={
        "host": ConfigField(str, "localhost"),
        "port": ConfigField(int, 5432),
        "debug": ConfigField(bool, False),
    },
    legacy_keys={"db_host": "host"},
)


def test_defaults_layer():
    r = ConfigResolver().resolve(SPEC)
    log.info("输入: 无服务配置无环境变量; 决策: 全部取默认; 结果: %s 来源: %s",
             r.values, r.sources)
    assert r.values == {"host": "localhost", "port": 5432, "debug": False}
    assert set(r.sources.values()) == {"default"}


def test_layer_priority_env_over_service_over_default():
    r = ConfigResolver(
        service_config={"store": {"port": 1111, "host": "svc"}},
        env={"APP_STORE__PORT": "2222"},
    ).resolve(SPEC)
    log.info("输入: service port=1111 host=svc, env PORT=2222; "
             "决策: env>service>default; 结果: %s 来源: %s", r.values, r.sources)
    assert r.values["port"] == 2222 and r.sources["port"] == "env"
    assert r.values["host"] == "svc" and r.sources["host"] == "service"
    assert r.values["debug"] is False and r.sources["debug"] == "default"


def test_legacy_key_migration_deterministic():
    r = ConfigResolver(service_config={"store": {"db_host": "db.internal"}}).resolve(SPEC)
    log.info("输入: 旧键 db_host=db.internal; 决策: 迁移到新键 host; "
             "结果: %s 来源: %s", r.values, r.sources)
    assert r.values["host"] == "db.internal"
    assert r.sources["host"] == "legacy:db_host"


def test_legacy_and_new_key_consistent_accepted():
    r = ConfigResolver(service_config={"store": {"db_host": "h", "host": "h"}}).resolve(SPEC)
    log.info("输入: 新旧键取值一致; 决策: 确定采用新键; 结果: %s", r.values)
    assert r.values["host"] == "h"


def test_legacy_and_new_key_conflict_rejected():
    with pytest.raises(AmbiguousConfigError) as exc:
        ConfigResolver(service_config={"store": {"db_host": "a", "host": "b"}}).resolve(SPEC)
    log.info("输入: 新旧键冲突 a!=b; 拒绝原因: %s", exc.value)


def test_unknown_keys_rejected_in_both_layers():
    with pytest.raises(UnknownConfigKeyError) as exc1:
        ConfigResolver(service_config={"store": {"nope": 1}}).resolve(SPEC)
    with pytest.raises(UnknownConfigKeyError) as exc2:
        ConfigResolver(env={"APP_STORE__NOPE": "1"}).resolve(SPEC)
    log.info("未知键拒绝: service=%s; env=%s", exc1.value, exc2.value)


def test_invalid_values_rejected():
    with pytest.raises(InvalidConfigValueError) as exc1:
        ConfigResolver(service_config={"store": {"port": "abc"}}).resolve(SPEC)
    with pytest.raises(InvalidConfigValueError) as exc2:
        ConfigResolver(env={"APP_STORE__PORT": "not-a-number"}).resolve(SPEC)
    with pytest.raises(InvalidConfigValueError) as exc3:
        ConfigResolver(service_config={"store": {"port": True}}).resolve(SPEC)
    log.info("非法值拒绝: service类型=%s; env解析=%s; bool非int=%s",
             exc1.value, exc2.value, exc3.value)


def test_env_bool_parsing():
    r = ConfigResolver(env={"APP_STORE__DEBUG": "yes"}).resolve(SPEC)
    log.info("输入: env DEBUG=yes; 决策: 解析为布尔; 结果: %s", r.values["debug"])
    assert r.values["debug"] is True and r.sources["debug"] == "env"
