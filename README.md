# fastapi-plugin-orchestrator-python

在轻量 FastAPI 服务模板之上扩展出的**可插拔功能模块运行框架**：模块发现/声明/注册、
依赖拓扑加载、分层配置解析、运行期安全启停与失败回滚。默认启动行为（`GET /` 返回
`{"Hello": "World"}`）保持不变。

## 快速开始

```bash
uv sync                  # 安装依赖
uv run python main.py    # 启动服务（lifespan 中自动启动全部模块）
uv run pytest            # 运行全部测试
uv run pytest --log-cli-level=INFO   # 查看每个用例的输入/决策依据/最终状态日志
```

服务端点：

- `GET /` —— 模板默认行为，未改动。
- `GET /modules` —— 查询各模块最终状态、失败原因与生效配置来源。

## 模块契约

模块是实现 `orchestrator.spec.Module` 协议的任意对象：

```python
class MyModule:
    @property
    def spec(self) -> ModuleSpec: ...          # 静态声明
    def init(self, config, context) -> None: ...  # 注入已校验配置与依赖上下文
    def start(self) -> None: ...
    def stop(self) -> None: ...
    # 可选：声明配置键契约
    def config_specs(self) -> list[ConfigKeySpec]: ...
```

`ModuleSpec` 声明：

| 字段 | 含义 |
| --- | --- |
| `module_id` | 全局唯一标识 |
| `version` | 语义化版本 `major.minor.patch` |
| `dependencies` | `Dependency` 元组：依赖的模块、版本区间、所需能力 |
| `provides_capabilities` | 本模块提供的能力集合 |
| `config_keys` | 保留字段；配置契约建议通过 `config_specs()` 提供 |

注册通过 `ModuleRegistry.register(module)` 完成；重复注册同一 `module_id`
抛出 `DuplicateRegistrationError`（`reason=duplicate_registration`）。

## 依赖与兼容规则

`Orchestrator` 构造时即调用 `resolve_load_order` 做**启动前校验**，任一规则不满足
都会在启动阶段被拒绝，且失败原因可区分（`OrchestratorError.reason`）：

| 失败原因 | 触发条件 |
| --- | --- |
| `missing_dependency` | 依赖的模块未注册 |
| `version_incompatible` | 实际版本不在 `[min_version, max_version_exclusive)` 区间内 |
| `capability_incompatible` | 被依赖方未提供全部 `required_capabilities` |
| `dependency_cycle` | 依赖图存在环（`context["cycle_members"]` 列出环成员） |

加载顺序由 Kahn 拓扑排序得出，同层模块按 `module_id` 字典序打破平局，
因此同一输入集合的顺序**唯一且稳定**，与注册先后无关。

## 配置分层与迁移语义

`LayeredConfig` 对每个模块独立解析配置，层优先级（高覆盖低）：

```
环境变量 (ORCH_<KEY>)  >  服务配置 (service_config[module_id])  >  默认值
```

- **校验**：每个键经 `ConfigKeySpec.validator` 校验/转换，非法值抛
  `InvalidConfigValueError`（`reason=invalid_config_value`，`context` 含来源层）。
- **未知键**：未声明的键抛 `UnknownConfigKeyError`，绝不静默接受。
- **旧键迁移**：`legacy_keys` 中的旧键被视为同层新键的别名，归一后按正常层优先级
  生效，来源记录为旧键所在层——结果确定且可解释。
- **歧义拒绝**：同一层内新旧键同时出现且值不一致，抛
  `AmbiguousConfigOverrideError`，不做隐式选择。
- 模块只能读到经 `init(config, ...)` 注入的已校验配置，无法触及未经校验的来源。

## 生命周期与并发保证

- **启动**：`start_all()` 按拓扑顺序启动；`start_module(id)` 会先补齐未运行的依赖。
- **失败回滚**：任一模块 `init/start` 失败或配置校验失败时，本次调用中已启动的模块
  按逆序停止（尽力清理半生效状态），失败模块标记 `FAILED` 并记录
  `failure_reason`/`failure_detail`，依赖它的模块不会继续启动。
- **停止保护**：`stop_module(id)` 在仍有运行中的依赖方时抛
  `DependencyUnavailableError`；`stop_all()` 按启动逆序停止。
- **幂等**：重复 `start`/`stop` 是空操作，不产生重复副作用（测试断言
  `init_count == stop_count == 1`）。
- **并发**：所有状态迁移都在同一把互斥锁内完成，并发启停被串行化，
  最终状态必然等价于某个串行执行顺序，无竞态。
- **可查询**：`status(id)` / `statuses()` 返回每个模块的 `state`
  （`registered/running/stopped/failed`）、`failure_reason`、`config_sources`。

## 本地验证

```bash
uv run pytest -q                      # 19 个用例：依赖环/缺失/版本/能力、配置分层与
                                      # 旧键迁移、失败回滚、并发启停一致性
uv run pytest --log-cli-level=INFO    # 每个用例打印：输入 | 决策依据 | 最终状态
uv run python - <<'PY'
from fastapi.testclient import TestClient
from main import app
with TestClient(app) as c:
    print(c.get("/").json())          # {'Hello': 'World'} —— 默认行为未变
    print(c.get("/modules").json())   # core/web 均为 running
PY
```
