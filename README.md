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
- `GET /modules/{id}/config` —— 查询模块逐键生效值/来源层与最近一次调整结果。
- `POST /modules/{id}/config` —— 不停机调整配置，body：
  `{"overrides": {...}, "request_id": "可选，用于幂等去重"}`。
- `POST /environment/{name}` —— 切换部署环境（示例内置 `dev`/`prod`，
  当前环境由进程环境变量 `ORCH_ENVIRONMENT` 指定，默认 `dev`）。

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
    # 可选：运行期配置调整时优先调用；未提供则框架退化为 stop->init->start 重载
    def reconfigure(self, config, context) -> None: ...
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
运行期调整 (runtime)  >  环境变量 (ORCH_<KEY>)  >  部署环境 (environment)
    >  服务配置 (service_config[module_id])  >  默认值 (default)
```

- **校验**：每个键经 `ConfigKeySpec.validator` 校验/转换，非法值抛
  `InvalidConfigValueError`（`reason=invalid_config_value`，`context` 含来源层）。
- **未知键**：未声明的键抛 `UnknownConfigKeyError`，绝不静默接受。
- **旧键迁移**：`legacy_keys` 中的旧键被视为同层新键的别名，归一后按正常层优先级
  生效，来源记录为旧键所在层——结果确定且可解释。
- **歧义拒绝**：同一层内新旧键同时出现且值不一致，抛
  `AmbiguousConfigOverrideError`，不做隐式选择。
- 模块只能读到经 `init(config, ...)` 注入的已校验配置，无法触及未经校验的来源。

## 运行期配置调整（不停机）

`Orchestrator.update_config(module_id, overrides, request_id=None)` 在服务不重启、
进程不中断的前提下调整指定模块的生效配置：

- **同一套规则**：`overrides` 进入该模块的 `runtime` 层（最高优先级），与其余各层
  一起按启动时完全相同的分层与校验规则重新解析——键名不对、取值非法、新旧写法
  冲突一律拒绝（沿用 `unknown_config_key` / `invalid_config_value` /
  `ambiguous_config_override` 三类 reason）。
- **原子性**：两阶段提交——先对整批（目标 + 运行中的传递依赖方）完成全部校验，
  再逐个应用。校验失败时旧配置分毫未动、完整继续生效，模块绝不会读到
  半新半旧的值；应用阶段失败则整批回滚（见下节）。
- **应用方式**：模块若提供 `reconfigure(config, context)` 钩子则优先调用；
  未提供时框架退化为 `stop -> init -> start` 的原子重载。
- **边界**：仅允许调整运行中（`running`）的模块，否则抛
  `ModuleNotRunningError`（`reason=module_not_running`）；已停止模块的配置
  在下次启动时按各层重新解析。
- **可查询**：
  - `effective_config(module_id)` —— 每个键当前的生效值与来源层；
  - `last_update(module_id)` —— 最近一次调整的 `UpdateRecord`：成功与否、
    失败卡在哪一步（`validate:<模块>` / `apply:<模块>` / `rollback:<模块>`）、
    失败详情、级联涉及的模块列表。
- **幂等**：携带同一 `request_id` 的重复提交直接返回首次结果，
  不重复生效、也不报错。

## 多环境配置隔离

构造 `Orchestrator` 时传入 `environment`（当前环境名）与
`environment_configs`（`{环境名: {module_id: {键: 值}}}`）：

- **隔离**：每个环境的生效配置只来自 `environment_configs[当前环境]` 这一份，
  切换环境不会把其他环境的值串进来；构造或切换到未声明的环境一律抛
  `UnknownEnvironmentError`（`reason=unknown_environment`），当前状态不受影响。
- **回落**：当前环境没有显式配置的键，自然回落到 service/default 等下层；
  回落后的来源层通过 `effective_config()` / `config_sources` 可查询、可解释。
- **切换**：`switch_environment(name)` 重解析全部运行中模块的配置并原子应用，
  与配置调整共享同一套两阶段校验 + 整批回滚保证；切到当前环境是幂等空操作。

## 依赖级联、回滚与并发

- **级联**：调整某模块配置时，所有依赖它的运行中模块按依赖拓扑顺序一起完成
  重载（`UpdateRecord.affected` 按应用顺序列出整批模块）。
- **回滚**：级联中任何一步校验或重载失败，整批（含已应用的模块）逆序回滚到
  改动前解析出的一致配置，不会出现一部分新、一部分旧的组合；回滚本身失败的
  模块被标记 `FAILED`（`reason=config_update_failed`）并记录详情。
- **并发**：所有调整与启停共用同一把互斥锁，多组并发调整被串行化，
  最终结果等价于按某个确定顺序逐条执行；`last_update` 记录与最终生效值
  一致，状态唯一且可解释。

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
uv run pytest -q                      # 37 个用例：依赖环/缺失/版本/能力、配置分层与
                                      # 旧键迁移、失败回滚、并发启停一致性、
                                      # 热更新、校验拒绝、环境隔离、级联回滚、并发调整
uv run pytest --log-cli-level=INFO    # 每个用例打印：输入 | 决策依据 | 最终状态
uv run python - <<'PY'
from fastapi.testclient import TestClient
from main import app
with TestClient(app) as c:
    print(c.get("/").json())          # {'Hello': 'World'} —— 默认行为未变
    print(c.get("/modules").json())   # core/web 均为 running

    # 热更新：runtime 层生效，来源可查
    print(c.post("/modules/core/config",
                 json={"overrides": {"workers": 5}, "request_id": "demo-1"}).json())
    print(c.get("/modules/core/config").json())

    # 校验失败：422 + reason，旧配置完整生效
    print(c.post("/modules/core/config",
                 json={"overrides": {"workers": -1}}).status_code)
    print(c.get("/modules/core/config").json())

    # 环境切换：dev <-> prod 互不串扰，未知环境被拒绝
    print(c.post("/environment/prod").json())
    print(c.get("/modules/core/config").json())
    print(c.post("/environment/nowhere").status_code)
PY
```
