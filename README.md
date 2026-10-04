# fastapi-plugin-orchestrator-python

在轻量 FastAPI 服务模板之上扩展出的**可插拔功能模块运行框架**：模块发现/声明/注册、
依赖拓扑加载、四层配置解析、运行期安全启停与失败回滚，以及**不停机热更新配置、
多部署环境隔离、依赖感知级联更新与并发安全**。默认启动行为（`GET /` 返回
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
- `GET /modules` —— 查询各模块最终状态、失败原因、生效值与配置来源。
- `POST /modules/reload` —— 不停机热更新配置（级联、回滚、幂等）。
- `POST /environment` / `GET /environment` —— 切换/查询当前部署环境。
- `GET /modules/{id}/config` —— 逐键查生效值、来源层与最近一次变更结果。

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
    # 可选：声明即表示支持不停机原地热更新（ReloadableModule 协议）
    def reload(self, config, context) -> None: ...
```

- 实现了 `reload(config, context)` 的模块在配置变更时被**原地热更新**，
  不发生 stop/start，模块应在 `reload` 内自行校验新配置，不可接受时抛异常。
- 未实现 `reload` 的模块由框架在**同一进程内**按 `stop -> init -> start` 重建：
  进程不中断、服务不需要重启，但该模块实例会经历一次重建。若要求"请求零中断"，
  模块应自行实现 `reload` 并保证切换对在途请求安全。

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

## 配置分层、环境隔离与迁移语义

`LayeredConfig` 对每个模块独立解析配置，层优先级（高覆盖低）：

```
环境变量 (ORCH_<KEY>)  >  部署环境层 (environments[env][module])  >  服务配置 (service_config[module])  >  默认值
```

- **部署环境层**：构造编排器时传入
  `environments={"dev": {...}, "prod": {...}}, active_environment="dev"`。
  每个环境是一份**完全独立**的环境层覆盖，未声明的键逐层向 service/default
  **回落**；环境之间互不串值。始终存在一个 `default` 环境。
- **运行期热更新写入哪一层**：`update_config` 把变更按键并入**当前环境**的
  环境层，因此天然按环境隔离——在 prod 上的热更新不会出现在 dev 中。
- **校验**：每个键经 `ConfigKeySpec.validator` 校验/转换，非法值抛
  `InvalidConfigValueError`（`reason=invalid_config_value`，`context` 含来源层）。
- **未知键**：未声明的键抛 `UnknownConfigKeyError`，绝不静默接受。
- **旧键迁移**：`legacy_keys` 中的旧键被视为同层新键的别名，归一后按正常层优先级
  生效，来源记录为旧键所在层——结果确定且可解释。
- **歧义拒绝**：同一层内新旧键同时出现且值不一致，抛
  `AmbiguousConfigOverrideError`，不做隐式选择。
- **来源可信**：运行期调整必须带显式 `source`，且在编排器可信来源集合内
  （默认仅 `runtime`，可通过 `allowed_runtime_sources` 配置）；来源缺失或
  不在集合内抛 `ConfigSourceRejectedError`（`reason=config_source_rejected`）。
- 模块只能读到经 `init/reload(config, ...)` 注入的已校验配置，无法触及未经校验的来源。
- **可查询、可解释**：
  - `effective_config(id)`：当前实际生效的键值；
  - `config_explanation(id)`：逐键的 `{value, source}`，`source` 为
    `env/environment/service/default` 之一，回落来源一目了然；
  - `last_change()` / `last_change_for(id)`：最近一次变更的成功/失败、
    卡在哪个阶段（`request/validate/reload/environment_switch`）、失败模块与原因。

## 运行期热更新、级联与回滚

```python
result = orch.update_config(
    {"core": {"workers": 8}, "web": {"mode": "prod"}},
    source="runtime",
    change_id="deploy-2026-10-04-001",   # 幂等键，建议由调用方生成
)
```

**执行模型（两阶段）**：

1. **request 校验**：请求非空、模块都已注册、来源可信。
2. **validate 预校验**：对环境层做试探合并后，对**全部受影响模块**重新分层解析。
   未知键、非法值、新旧键冲突在这一步整体拒绝——试探写入立即回滚，
   模块从未被触碰，**旧配置完整生效，读不到一半新一半旧的值**。
3. **reload 提交**：预校验通过后，按拓扑顺序逐模块热更新。

**依赖感知级联**：修改被依赖模块时，其（传递性）依赖方一并进入本次批次，
并严格按依赖拓扑顺序重载（依赖先于依赖方）；未运行的模块不参与级联，
下次启动时自然读到新配置。

**提交后失败的整批回滚**：重载途中任一模块失败（如模块在 `reload` 中拒绝新配置），
框架先恢复环境层存储，再让所有受影响模块回到改动前配置——热更新型按拓扑序回退
（依赖先回），重建型先逆序停止再拓扑序启动。回滚后不允许出现"一部分新、一部分旧"
的组合；极端情况下若连旧配置都回不去，相关模块被显式标记为 `failed` 并记录原因，
绝不留下"看似正常"的状态。异常为 `ReloadFailedError`（`reason=reload_failed`），
`ChangeResult` 记录 `stage=reload` 与 `failed_module`。

**边界**：

- 热更新改变的是配置值与模块持有的运行态；依赖拓扑、版本兼容在注册时固定，
  不能通过热更新增删模块或改变依赖关系。
- 环境变量（`ORCH_*`）是进程级最高优先级层，对所有环境生效；需要按环境区分的值
  应放在环境层而不是环境变量。

## 多环境切换

```python
orch.switch_environment("prod")   # 未知环境 -> EnvironmentNotFoundError，状态不变
```

切换语义与热更新一致地保证原子性：先在目标环境下对所有运行中模块做分层预校验，
再切换并重载；重载中途失败则切回原环境并整批回滚，杜绝跨环境的混合状态。
切到同名环境是空操作（不重复重载）。`orch.environment` / `orch.environments()`
可查询当前环境与全部已声明环境。

## 并发行为与幂等

- **串行化**：热更新、环境切换、start/stop 全部在同一把互斥锁内完成，
  多组并发调整的结果**等价于按确定顺序串行执行**。每次成功变更领取一个单调递增
  `sequence`，最终状态唯一、可解释（最终值必然来自某次具体提交）。
- **幂等去重**：相同 `change_id` + 相同请求体的重复提交直接回放第一次的结果
  （`replayed=True`、相同 `sequence`），**不重复生效、也不报错**；
  同一 `change_id` 被不同请求体复用会被拒绝（防止去重键误用）。
  不带 `change_id` 的提交不做去重，按普通并发更新串行执行。

## 生命周期与并发保证（启动/停止）

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
  （`registered/running/stopped/failed`）、`failure_reason`、`config_sources`、
  `config_values`。

## 本地验证

```bash
uv run pytest -q                      # 39 个用例，全部通过、无回归：
                                      # 依赖环/缺失/版本/能力、四层配置与旧键迁移、
                                      # 启停失败回滚/幂等/并发（19 个既有用例），
                                      # 热更新成功、校验失败旧配置不变、环境互不串扰、
                                      # 依赖级联回滚、环境切换回滚、并发结果一致、
                                      # change_id 幂等、重建型/未运行模块边界
                                      # （20 个新增用例）
uv run pytest --log-cli-level=INFO    # 每个用例打印：输入 | 决策依据 | 最终状态
uv run python - <<'PY'
from fastapi.testclient import TestClient
from main import app
with TestClient(app) as c:
    print(c.get("/").json())                 # {'Hello': 'World'} —— 默认行为未变
    print(c.get("/modules").json())          # core/web 均为 running
    print(c.post("/modules/reload", json={   # 热更新 core，级联到 web
        "changes": {"core": {"workers": 8}}, "change_id": "demo-1"}).json()["outcome"])
    print(c.get("/modules/core/config").json())   # workers=8, source=environment
    print(c.post("/modules/reload", json={        # 重复提交：replayed=True，不重复生效
        "changes": {"core": {"workers": 8}}, "change_id": "demo-1"}).json()["replayed"])
    print(c.post("/modules/reload", json={        # 非法值：400，旧配置继续生效
        "changes": {"core": {"workers": -1}}}).status_code)
PY
```
