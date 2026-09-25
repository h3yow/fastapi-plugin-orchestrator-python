# FastAPI 可插拔模块运行框架

在轻量 FastAPI 模板之上扩展的可插拔功能模块运行框架。默认启动行为不变
（`GET /` 仍返回 `{"Hello": "World"}`），新增 `GET /modules` 查询各模块状态。

## 目录结构

```
main.py               # FastAPI 入口（lifespan 中启停全部模块）
app/
  spec.py             # 模块声明：Version / Dependency / ConfigField / ModuleSpec
  base.py             # 模块运行时基类 ModuleBase
  errors.py           # 可区分的失败原因（异常类型体系）
  registry.py         # 注册、依赖校验、稳定拓扑排序
  config.py           # 分层配置解析与旧键迁移
  orchestrator.py     # 启停生命周期、失败回滚、并发安全
  bootstrap.py        # 组装注册表 + 配置解析器 + 编排器
  modules/            # 内置示例模块 core / metrics
tests/                # 依赖、配置、生命周期与并发测试
```

## 模块契约

每个模块由两部分组成：

1. **静态声明 `ModuleSpec`**：
   - `module_id`：唯一标识；
   - `version`：语义化版本 `major.minor.patch`；
   - `provides`：本模块提供的能力（字符串集合）；
   - `dependencies`：`Dependency(module_id, min_version, capabilities)` 列表；
   - `config_schema`：`{键: ConfigField(类型, 默认值)}`；
   - `legacy_keys`：`{旧键: 新键}` 迁移表。
2. **运行时类**：继承 `ModuleBase`，实现 `on_start()` / `on_stop()`；
   启动前由框架注入 `self.config`（已校验的 `ResolvedConfig`）。

注册：`registry.register(spec, factory)`。同一标识重复注册且声明与工厂
完全一致时幂等（无副作用）；声明不一致抛 `DuplicateRegistrationError`。

## 依赖与兼容规则

- 加载顺序由 Kahn 拓扑排序确定，同层按模块标识字典序，**唯一且稳定**。
- 版本兼容：实际版本与 `min_version` 同 major 且不低于它。
- 能力兼容：依赖声明的每个能力都必须出现在被依赖方的 `provides` 中。
- 启动期拒绝（原因可区分，均为不同异常类型）：
  - `MissingDependencyError`：依赖未注册；
  - `IncompatibleDependencyError`：版本或能力不兼容；
  - `CyclicDependencyError`：依赖环（报出环上模块列表）。

## 配置分层与迁移语义

优先级：**环境变量 > 服务配置 > 默认值**。

- 环境变量命名：`APP_<模块ID大写>__<键大写>`（如 `APP_CORE__LOG_LEVEL=DEBUG`），
  按 schema 类型解析，布尔接受 `1/0/true/false/yes/no/on/off`。
- 旧键迁移（服务配置层内）：旧键存在且新键缺失 → 确定性地迁移，
  来源记为 `legacy:<旧键>`；新旧键同时存在且取值一致 → 采用新键；
  **取值冲突 → `AmbiguousConfigError`**。
- 严格校验，绝不静默接受：
  - 未知键（服务配置或环境变量）→ `UnknownConfigKeyError`；
  - 非法值 / 类型不符（`bool` 不被当作 `int`）→ `InvalidConfigValueError`。
- 模块只能读到经校验的 `ResolvedConfig`，每个键的生效来源
  （`default` / `service` / `env` / `legacy:*`）可通过
  `orchestrator.effective_config(module_id)` 查询。

## 生命周期与并发保证

状态机：`registered → starting → running → stopping → stopped`，
失败进入 `failed` 并记录原因。

- `start_all()`：按拓扑序启动；任一模块失败时**逆序回滚**本次已启动的
  模块，不留半生效状态；依赖失败时其依赖者不会启动。
- `start_module(id)`：连带启动未运行的依赖；失败同样回滚本次新启动者。
- `stop_module(id)`：先停所有（传递）依赖者，再停本模块。
- 幂等：重复启动/停止同一模块不产生重复副作用。
- 并发：所有状态迁移在同一把可重入锁内完成，并发启停同一模块或存在
  依赖关系的模块，结果与某个串行顺序一致，无竞态、无中间态泄漏。
- 查询：`state_of()` / `failure_reason()` / `effective_config()` /
  `snapshot()`。

## 本地验证

```bash
uv run pytest            # 全部测试（日志含输入、决策依据与最终状态）
uv run python main.py    # 启动服务；GET / 与 GET /modules
```
