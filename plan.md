# Planner Intent 与确定性编译技术方案

本文是开发方案，不表示功能已经实现。仅新增本文档，不修改现有源码、不调用实验 API、不执行数据库注入。

## 1. 项目目标与流程

让 LLM 负责选择复现策略、目标对象、负载参数及必要 SQL；由程序负责执行协议、默认参数、命令、事务脚本、校验和可确定的修正。Executor 继续接收现有 TaskSpec。

```text
Runtime 前置探查、baseline、memory、reflection
  -> Planner Context + 当前请求允许的 Intent JSON Schema
  -> LLM 生成 Intent
  -> Intent 解析、Schema 与语义校验
  -> Compiler 生成 TaskSpec 草案及字段来源映射
  -> _prepare_payload 执行关键字段检查、SQL 校验与 EXPLAIN
  -> duration 截断、路径规范化、DAG 与 SafetyChecker
  -> Executor -> 观测、评价、反思
```

格式错误或语义错误只在有界修复流程中重新调用 LLM。Compiler 自身错误直接报告，不要求 LLM 修复程序缺陷。全部校验通过后才能执行。

目标验收：正常规划一次 LLM 调用；模型输出无 TaskSpec 协议字段；已有异常链能编译到现有执行器；错误具有阶段、字段路径和原始响应证据；评价规则保持兼容。

## 2. LLM API 输入与输出

### 2.1 输入

继续复用前置收集的上下文：request、数据库版本、schema/索引、table stats、DB/OS metrics、workload 配置、baseline QPS/TPS、memory、上一轮 reflection 和字段白名单。

拆分两个上下文：发送给 LLM 的 PlanningContext 与仅供本地使用的 CompilationContext。后者持有凭据、工具绝对路径、运行目录、UID 分配器和编译默认值；这些内容无需模型生成，凭据不能进入 prompt 或 artifact。

PlanningContext 还包含当前 injected_nodes 对应的可用策略、参数含义和硬上限。先保留当前环境信息完整度，避免同时压缩上下文导致效果变化；后续依据 token 统计再做裁剪。反思输入同时提供上一轮 Intent、编译参数、执行结果和未命中证据。

### 2.2 输出协议

顶层固定为对象 `{"intents": [...]}`，支持现有多个 injected_nodes；每个根因首版恰好一个 Intent，复杂根因由 Compiler 生成多个协作步骤。Intent 只包含以下六类字段：

| 字段 | 语义与限制 |
| --- | --- |
| node | 必须是本次 injected_nodes 中可注入的真实图节点 |
| strategy | 对应 node 的已注册策略枚举，不接收任意命令 |
| target | 表、列、谓词等策略必要信息；数据库以 request 为准 |
| load | 并发、持续时间、持锁时间、强度；具体字段按策略定义 |
| sql | SQL 类策略的语句；非 SQL 策略为 null |
| expected_evidence | 预期现象标识，仅用于解释和诊断，不能覆盖 evaluator |

例如当前 hot_update 请求可输出：

```json
{
  "intents": [{
    "node": "hot_update",
    "strategy": "holder_waiter_same_hot_rows",
    "target": {"table": "district", "predicate": "d_w_id = 1 AND d_id = 1"},
    "load": {"concurrency": 8, "duration_sec": 15, "hold_sec": 10},
    "sql": {
      "holder_sql": "SELECT * FROM district WHERE d_w_id = 1 AND d_id = 1 FOR UPDATE",
      "waiter_sql": "UPDATE district SET d_ytd = d_ytd + 1 WHERE d_w_id = 1 AND d_id = 1"
    },
    "expected_evidence": ["lock_contention", "slow_query", "qps_drop"]
  }]
}
```

当前 graph.py 中 lock_contention 为 INTERMEDIATE、injectable=False，不能直接沿用示例中的 node=lock_contention，也不能静默映射为 table_lock。示例的 15 秒仅为示例，实际约束来自 request。

### 2.3 Schema 与 API 协议

保留现有 Chat Completions 和 OPENAI_* 配置，扩展 tools.llm_generate 的可选 response_format 参数。Planner 显式传入：

```python
response_format = {
    "type": "json_schema",
    "json_schema": {
        "name": "anomaly_intent_plan_v1",
        "strict": True,
        "schema": build_intent_schema(request.injected_nodes),
    },
}
```

Schema 根为 object，包含必填 intents 数组；items 使用嵌套 anyOf 表达当前请求允许的 node/strategy 分支。每个分支只列出所需 target/load/sql 字段，所有对象禁止 additionalProperties。避免向模型发送全图所有策略的巨大 Schema。

严格结构化输出要求字段列入 required；语义上的可选字段通过 nullable 类型表达。因此非 SQL 策略使用 sql=null，不使用任意自由键对象。结构化输出只保证受支持的结构约束，不能保证 SQL 正确；拒答、截断也需单独处理。[官方 Structured Outputs 文档](https://developers.openai.com/api/docs/guides/structured-outputs)

本地使用同源 Schema 再验证一次，建议采用 jsonschema 校验器；策略语义另行校验。不要维护一套 prompt 字段、一套本地字段的独立协议。

显式 response_format 优先于旧 json_mode 分支，不再因模型名称前缀静默丢弃格式设置。其他 llm_generate 调用者未传新参数时保持既有行为。第三方端点是否真正支持 strict 需独立验证；不支持时明确失败，不静默降级。

## 3. 现有目录与复用位置

| 现有位置 | 已有职责与复用方式 |
| --- | --- |
| AnomalyGraphAgent/planner.py | _single_pass_planner、上下文构造、JSON/SQL 修复、_prepare_payload；改接 Intent，复用本地后处理 |
| AnomalyGraphAgent/tools.py | llm_generate 当前使用 json_object；复用 HTTP 请求、EXPLAIN、DAG、安全检查和路径规范化 |
| AnomalyGraphAgent/types.py | TaskSpec dataclass 与 ExecutableTaskDAG，继续作为编译输出契约 |
| AnomalyGraphAgent/executor.py | _run_action 分发 raw_sql_workload、raw_transaction_script、raw_command、backup 与 BenchBase 等动作 |
| AnomalyGraphAgent/dag.py、safety.py | DAG 构造、时序和安全约束，继续本地执行 |
| AnomalyGraphAgent/runtime.py、workload.py、probes/ | 实验窗口、BenchBase 生命周期、前置探查和结果落盘 |
| AnomalyGraphAgent/graph.py、evaluator.py | 节点可注入性与命中规则，Intent 不能修改 |
| anomalychainInput/、anomalychainOutput/ | 请求样例与运行结果，作为兼容性与只读回放样本 |
| tests/、memory/、.tools/ | 离线测试、历史记忆、BenchBase/ChaosBlade 等运行依赖 |

当前 _prepare_payload 在 SQL EXPLAIN 前已经做路径规范化、duration 处理和 DAG 构造。迁移时拆开这些阶段，让 SQL 修复发生在最终 DAG 和 UID 分配前；路径规范化只在最终编译阶段进行，避免重复更换 UID。

建议新增三个小模块：intent.py（契约、策略注册与 Schema）、intent_validation.py（解析和语义校验）、intent_compiler.py（编译函数与来源映射）。先不引入通用中间件框架。

## 4. 中间件与 Compiler 设计

推荐接口如下，属于设计示意，尚未实现：

```python
decode_intent_response(response, schema) -> IntentPlan
validate_intent_plan(plan, request, context) -> ValidatedIntentPlan
compile_intent_plan(plan, compilation_context) -> CompilationResult
```

CompilationResult 包含 task_specs、dependencies、source_map、normalizations 和 warnings。source_map 将编译 SQL 定位回 intents[i].sql.holder_sql 等稳定路径，用于 SQL 修复计数和审计。

解析只支持约定的顶层对象：优先检查 API 错误/拒答/截断；有 json_payload 时先验证其类型；否则严格解析完整 text。顶层数组、标量、多 JSON、旧 TaskSpec 均作为结构错误进入修复。一个路径命中即返回或抛错，不再尝试其他形态拼接、猜测或静默映射。

| Intent 策略族 | Compiler 输出及关键语义 |
| --- | --- |
| hot_update / long_tx 锁策略 | raw_transaction_script；生成 BEGIN、holder/waiter steps、等待与 ROLLBACK，明确并发分配和同一热点 |
| improper_sql / missing_index / large_temp_table | raw_sql_workload；复用验证后的 SQL，missing_index 必须有实际访问路径证据，不能仅按名字宣称缺索引 |
| traffic_surge | benchbase_burst；读取当前 workload profile，concurrency 映射 terminals，继承实际配置 |
| resource_cpu / resource_io / resource_memory 等 | raw_command；按平台能力生成配置路径下的 ChaosBlade argv、唯一 UID 与匹配 cleanup |
| backup | logical_backup_command；生成受控 mysqldump argv、目标表及 where 参数，输出路径由运行目录决定 |
| metadata_lock / table_lock / deadlock_storm | 专用事务/SQL 模板；保留持锁连接、等待者和反向更新语义，不能一律替换成行锁 |
| connection_storm / redo_log_pressure / 其他现有注入节点 | 单独注册策略并核对现有执行能力；未实现编译器的策略明确报 unsupported_strategy |

Compiler 根据显式 intent.node 写 metadata.root_cause，这是协议转换，不是推断缺失根因。缺失 node 必须失败；中间件不补 root_cause。既有 SafetyChecker 的缺失元数据警告可保留，但不能绕过 injected_nodes 归属校验。

参数规则：只对契约允许为空的参数应用版本化默认值，并记录来源。intensity 映射使用可审计的数值配置表；无效、零值、负值和布尔型 duration 均拒绝。超过窗口的有效正数沿用现有确定性截断，并同步检查 hold_sec、sleep 与动作总时序；不擅自降低 concurrency/rate。

_prepare_payload 是编译后执行关键字段检查的明确入口，保留 _validate_direct_task_spec 作为唯一 TaskSpec 结构检查；中间件不重复维护执行器协议。最终 TaskSpec 和生成 SQL 都必须经过安全检查。

## 5. 按开发顺序实施

### 步骤 1：盘点策略能力，冻结 Intent v1（任务 1 的契约部分）

修改计划：新增 intent.py；建立 node/strategy 注册表、所需字段、默认参数与能力矩阵。遍历现有请求中的全部 injected_nodes，逐个对照 graph 与 executor，明确是否能无行为降级编译。

测试：全部请求可解析；每个根因至少有编译策略；错误 node、未知策略、额外字段、非有限数值、重复或缺失根因被拒绝；策略专属字段不能跨类型混用。

验收：现有请求覆盖矩阵无遗漏；未经支持的动作不被静默替换；hot_update 使用真实根因标识。

### 步骤 2：实现纯本地 Compiler（任务 2）

修改计划：新增 intent_compiler.py，从手写合法 Intent 编译到现有 TaskSpec；优先覆盖锁、SQL、BenchBase、资源、backup，再覆盖步骤 1 的剩余节点。复用当前命令生成/规范化能力，集中生成 task_id、UID、cleanup 与 risk_assessment。

测试：编译结果通过 _validate_direct_task_spec 和 mocked executor 分发；holder/waiter 分属连接且锁同一目标；相同固定 CompilationContext 输出一致；不同运行 UID 不复用；create/destroy UID 一致；backup 不吞掉 predicate。

验收：不调用 LLM 即可证明 Intent 到 TaskSpec 的接口兼容；覆盖的现有请求没有执行语义缺失。编译器只构造计划，不执行 shell、SQL 注入或后台负载。

### 步骤 3：扩展结构化 API 请求（任务 3）

修改计划：扩展 llm_generate 接收显式 response_format，保留原调用默认行为；返回原始 text、parsed payload、finish_reason、refusal、usage 和请求标识（服务端提供时）。禁止记录 Authorization 和凭据。

测试：mock HTTP 断言请求包含正确 json_schema/strict；覆盖服务端不支持、超时、空 choices、拒答、截断、顶层数组和合法对象。无 usage 时记录 unavailable，不能虚构 token 数。

验收：格式设置实际进入 HTTP body；响应异常不再产生 list.get 等 Python 属性错误；其他调用者测试不回归。真实供应商能力验证单独进行，不启动实验。

### 步骤 4：接入 Intent Planner 与本地 Validation（任务 1 的集成部分）

修改计划：改写当前 system prompt 和 _single_pass_planner，使其只生成 Intent；保留环境、注入范围、时长、安全约束强度。接入解析、Schema、语义校验、Compiler 和 _prepare_payload。

表字段校验复用现有能力，但先审计其对别名、CTE、子查询、限定库名的覆盖；简单正则不能视为完整白名单证明。必要时引入 MySQL 方言 SQL 解析器，跨库访问仍须拒绝。EXPLAIN 前执行只读静态安全筛查；事务控制及非 EXPLAIN 支持语句走专门语法/能力校验。

测试：合法锁 Intent 仅一次 LLM 调用；15/30 秒窗口保持上限；非法数据库/字段/注入节点被拒绝；未通过验证时 executor 从未被调用；序列动作累计超窗继续拒绝。

验收：LLM 请求与响应不要求 task_id、action kind、steps、argv、路径、UID 等执行字段；最终 TaskSpec 与原 Executor 兼容。

### 步骤 5：迁移修复与异常边界

修改计划：JSON/Schema/Intent 语义修复共最多 2 次；SQL EXPLAIN 修复按 source_map 的每条 Intent SQL 最多 3 次。修复仍返回完整 IntentPlan，本地检查仅允许修改报错 SQL 及必要引用，拒绝偷偷改 node、strategy、load 或其他已通过语句。编译生成的固定 SQL 失败归类 compiler_error，不消耗 SQL repair。

每次修复重新验证 Intent 并编译；不再让模型修复 TaskSpec。限制 Intent 数量和每策略 SQL 槽位，避免通过新增 SQL 绕过重试计数。所有中间件意外异常在 planner 边界转换成带 cause 的 PlannerFallbackError；连接错误保留 PlannerLLMError 分类并立即终止。

测试：首次失败随后成功、3 次仍失败、两条 SQL 独立计数、修复修改其他字段、非法修复 JSON、Compiler 意外异常、API 中断；均断言调用数、错误类型及无执行副作用。

验收：所有终止有明确阶段与原因；不泄漏 AttributeError；SQL 合法时无需修复调用；不能因修复而改变实验范围。

### 步骤 6：落盘、反思和完整回归

修改计划：新增 planner_intent.json、compiled_task_specs.json、intent_validation.json、compilation_trace.json；继续写 planner_context.json、plan_validation.json、plan.json 和兼容的 react_trace.json。新增 planner_mode=intent_compiler_v1、schema/compiler 版本、调用数、分类修复数、API usage、分阶段耗时。失败同样落盘原始响应与字段错误。

反思继续参考实际执行结果；追加上一轮 Intent、参数默认/截断记录和编译来源，不让反思结果直接覆盖 executable task。保持慢日志、QPS、查询延迟和 reflection_comparison 的既有口径。

测试：新增 test_planner_intent.py、test_intent_compiler.py、test_llm_structured_output.py；改造现有 Planner mock 为 Intent；验证失败 artifact、反思轮次和成功报告字段。

推荐离线验收命令（新增测试实现后执行）：

```bash
python -m pytest tests/test_planner_intent.py tests/test_intent_compiler.py \
  tests/test_llm_structured_output.py tests/test_agent_react_planner.py \
  tests/test_agent_workload.py tests/test_slow_query_evidence.py \
  tests/test_agent_report.py -q
```

验收：所有当前请求的 mock 编译通过；历史 JSON 错误响应只读回放产生结构化错误；API smoke 通过后再执行 hot_update 实验。分别报告“协议跑通”“注入执行成功”“传播链命中”，不能混为同一成功指标。比较同类请求新旧输出 token、修复次数和规划耗时，以实测说明改善。

## 6. 不明确需求、风险与待确认事项

| 事项 | 建议默认决策与风险 |
| --- | --- |
| lock_contention 的语义 | 使用请求真实根因 hot_update/long_tx/table_lock；改变图中可注入性属于独立需求 |
| 多根因顺序 | 首版一根因一个 Intent，沿用现有本地依赖策略；传播边不自动等于任务串行边，复杂自定义调度暂不新增协议 |
| sql 可选 | 严格 API 层用 null 表达省略语义，避免模型输出无关空字段 |
| 强度与默认并发 | 上线前确定 low/medium/high 数值表和各策略并发分配；默认值必须可追溯，不能随意扩大压力 |
| 同一热点是否真实存在 | schema/EXPLAIN 无法证明目标行存在；需要沿用或补充只读目标行探测，无法确认时返回可解释的校验错误 |
| 锁策略语义 | holder/waiter 启动时序、连接数和 hold_sec 需通过执行器验证；正确 JSON 不代表会形成锁等待 |
| 网络、磁盘、DDL 能力 | 检查当前 Darwin 工具与执行器实际支持；能力缺失明确阻断，不能用其他根因压力替代 |
| expected_evidence 命名 | 图节点与诊断指标可用独立受控标识表，不能将任意别名当作新增 evaluator 节点 |
| 第三方 API | 当前配置不自动切换到示例 relayrouter 或指定模型；是否支持 strict、是否忽略 Schema 待最小请求验证 |
| SQL 验证范围 | EXPLAIN 不证明语义、耗时或安全；静态检查、目标检查、SafetyChecker 与执行隔离继续必要 |
| 编译可复现性 | 固定上下文应生成相同结构；时间、运行 ID 与新 UID 由上下文显式注入，不宣称跨运行字节一致 |
| InputAnalysisAgent | 不迁移其协议；共享 llm_generate 的默认兼容性必须保留 |
| 当前工作树 | 已有多处未提交源码修改；实现时基于现状增量改动，不回滚用户改动 |

以上默认值可用于启动开发；强度表、全部节点策略能力及第三方 strict 支持必须在相应步骤验收时确认。当前文档不声称已经验证真实 API 或异常注入效果。
