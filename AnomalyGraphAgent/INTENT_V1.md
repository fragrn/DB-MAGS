# Intent v1 契约与能力盘点

现已包含协议、校验器、静态能力及纯本地 Compiler，并已接入 Planner。
`intent.py` 是字段、策略、默认值及 Schema 的单一来源。

## 使用边界

- 顶层仅允许 `{"intents": [...]}`；每个请求根因恰好一个 Intent。
- Intent 必须包含 node、strategy、target、load、sql、expected_evidence 六个字段。
- 按策略定义嵌套字段，无需 target/sql 的策略使用 null；不接收自由字段。
- node 必须是真实且可注入的请求根因。hot_update 不映射为 lock_contention。
- `build_intent_schema(injected_nodes)` 生成请求专属 Schema；`validate_intent_plan` 验证本地契约并返回独立副本。
- SQL、表字段实际存在性、权限和注入效果不是本步契约校验的范围。
- SQL 工作负载 concurrency=null 的声明默认值为 4；校验不填值，Compiler 后续记录应用来源。
- duration_sec 必须显式提供有限正数；hold_sec 不能超过它。请求窗口截断仍由后续编译流程处理。
- rate=null 表示后续继承 BenchBase profile；资源 intensity 仅接受 low/medium/high，数值映射须在编译阶段确定。
- expected_evidence 是各策略允许的诊断/图节点标识，不改变 evaluator 的命中规则。

## 能力状态

`conditional` 表示现有执行动作可表达策略，但需要所列前置条件和步骤 2 的 Compiler。
`blocked` 表示现有执行链存在明确语义缺口；`disabled` 表示图禁用。
`require_compilable_strategy` 会拒绝 blocked/disabled，不选择替代策略。
13 种 conditional 根因已实现 Compiler；blocked/disabled 根因仍拒绝编译。
compiler_implemented 仅代表存在编译代码，此清单不宣称数据库实验已成功。

| 根因 | 策略 | 状态 | 主要前提或缺口 |
| --- | --- | --- | --- |
| traffic_surge | increase_client_concurrency | conditional | 继承有效 BenchBase profile、rate 与 mix |
| hot_update | holder_waiter_same_hot_rows | conditional | 同一真实热点行、角色并发分配及启动顺序 |
| long_tx | long_transaction_hot_rows | conditional | 同一热点行、长事务与等待者启动顺序 |
| missing_index | unindexed_filter | conditional | 缺索引、SQL/EXPLAIN 证据 |
| improper_sql | non_sargable_filter | conditional | 函数谓词 SQL 与 EXPLAIN |
| large_temp_table | sort_group_join_spill | conditional | 排序/临时表 SQL 与 EXPLAIN |
| backup | large_table_logical_backup | conditional | mysqldump 配置、受控输出及清理 |
| resource_cpu | cpu_pressure | conditional | 平台工具检查、明确强度映射 |
| resource_io | disk_io_pressure | conditional | 受控 burn、工具检查、强度映射 |
| resource_memory | memory_pressure | conditional | 工具检查、内存强度上限 |
| disk_full_or_pressure | bounded_disk_pressure | conditional | 仅受控 burn，不声称真实磁盘写满 |
| metadata_lock | transaction_blocks_ddl | conditional | 可逆 DDL、持锁顺序与有界等待 |
| table_lock | explicit_table_lock | conditional | 同一连接 LOCK/UNLOCK、等待者顺序 |
| network_latency | mysql_network_delay | blocked | 当前规范化强制 network drop，不能保持 delay 语义 |
| resource_network | mysql_network_delay | blocked | 同上；此节点在图中但当前没有对应请求 |
| deadlock_storm | opposite_order_updates | blocked | 事务角色仅跑一遍，缺少持续重复/重试能力 |
| redo_log_pressure | committed_write_pressure | blocked | SQL workload 未显式 commit，事务脚本没有持续循环 |
| connection_storm | short_connection_burst | blocked | SQL workload 每个 worker 复用连接，不产生短连接循环 |
| excessive_index | redundant_index_write_amplification | disabled | 图明确禁用，不补造索引放大证据 |

## 请求覆盖

`request_capability_matrix("anomalychainInput")` 动态遍历全部 `request_*.json`，通过
`ExperimentRequest.from_dict()` 解析，返回逐文件、逐根因的策略、动作、状态、来源和前置条件。
当前共 22 个请求、18 种根因：21 个请求的根因可注入，1 个 excessive_index 请求禁用。
目录中的 `traffic_surge_tpcc_plan.json` 是历史规划结果，不作为实验请求解析。
不存在策略的根因会显示 registered=false，并使覆盖测试失败。

## 验证

`tests/test_planner_intent.py` 覆盖请求完整性、全部注册策略、执行器动作匹配、
封闭 Schema、根因归属、重复/缺失根因、非法类型、额外/跨策略字段、非有限值、
默认参数不被静默应用以及 blocked/disabled 阻断。
测试只使用 mock-free 本地契约数据，不调用 LLM、MySQL 或 ChaosBlade。

## 本地编译接口

`intent_compiler.compile_intent_plan(payload, CompilationContext(...))` 返回
CompilationResult，包含 TaskSpec dataclass 列表、依赖、SQL 来源映射、参数修改记录和 warnings。
使用 `result.to_dict()` 可序列化，不会自动写文件或执行实验。

CompilationContext 必须显式传入 request、RuntimeConfig、run_id；round_no 与
compilation_id 参与确定性 task ID/ChaosBlade UID 计算。同一上下文重复编译结果一致；
运行或重试必须使用不同身份，不能复用同一身份并发执行两次计划。

BenchBase 需要显式传入既有 profile 和 transaction_types。资源类需要提供
resource_profiles[node][intensity]：CPU/内存为 percent，磁盘为受控 path 和 size。
Compiler 不读取环境变量、工具能力或 XML，也不猜测资源强度。

Backup 需要 mysqldump_path 和预先配置的 mysql_defaults_file 绝对路径，凭据不写入
argv；predicate 作为一个完整 --where 参数传递。输出由现有 executor 的 stdout artifact 保留。

锁任务分配一个 holder 和 concurrency-1 个 waiter，分别使用独立连接。
事务以 ROLLBACK 收尾；显式表锁在 holder 的同一连接 UNLOCK。
MDL 仅支持 ALTER TABLE target FORCE，任意其他 DDL 必须拒绝，不自动换成行锁。
FORCE 仍有表重建成本，现有 executor 没有 holder 就绪屏障或可靠的 SQL 超时取消，
调用方必须验证这些运行条件，不能把正确编译等同于稳定形成阻塞或严格超时清理。

ChaosBlade 复用 canonicalize_resource_chaosblade_action，显式 assigned_uid 保证确定性。
旧调用者不传 assigned_uid 时继续生成全新 UID。未来 Planner 集成不可再次无条件重写已分配 UID。
命令 cleanup 由 executor 的 finally 执行；事务异常依赖连接关闭释放锁，避免重复 cleanup_actions。

默认并发和 duration/hold 截断会记录在 normalizations，资源强度映射也记录实际数值。
所有最终任务仍需经过 _prepare_payload、EXPLAIN、DAG 时序和 SafetyChecker 后才可执行。
本步 dependencies 为空，允许多个独立根因同时注入；自定义依赖需要后续显式调度策略。

## Planner 集成与 SQL 校验

Planner 现在通过 json_schema/strict 请求生成 Intent；正常路径只调用一次 LLM。
本地顺序为 Intent 契约/目标检查、Compiler、全部 SQL 静态筛查、TaskSpec/时序检查、
EXPLAIN 和 SafetyChecker。静态筛查有一条失败，就不向数据库发送任何 EXPLAIN。
模型只修复 Intent，不再接收或生成 TaskSpec；EXPLAIN 修复沿 SQL source_map 定位。

依赖固定为 SQLGlot 28.10.1，安装命令：

```bash
python -m pip install -r requirements-planner.txt
```

验证使用 MySQL AST 与作用域解析，支持 SELECT 的别名、CTE、派生表和相关子查询。
限定数据库名必须精确匹配目标库；任何层级的跨库表/字段引用都拒绝。
支持单表、带字段谓词的 UPDATE/DELETE，以及显式列名的 INSERT。
未知/歧义字段、未知表、DML 子查询、多语句、变量、未批准函数和写文件语句均拒绝。
事务控制语句及单表 LOCK/UNLOCK 只做本地检查，不发送 EXPLAIN；SET、CALL、任意 DDL 不被跳过。
语法树验证不证明注入效果，也不替代 SafetyChecker。MDL ALTER FORCE 仍被当前安全规则阻断。

普通 plan() 调用可自动构造本地 CompilationContext；也可显式传入 compilation_context。
默认资源策略表：CPU low/medium/high=40/65/90%，受 config.max_cpu_usage 限制；
内存=20/40/60%；磁盘=32M/64M/128M，目录 /tmp/dbmags，运行前仍须检查工具和目录可用性。
这些值记录在发给模型的资源策略中及编译 normalizations，不使用模型提供的命令或路径。
Backup 需配置 DBMAGS_MYSQLDUMP_PATH 和 DBMAGS_MYSQL_DEFAULTS_FILE，后者是预置的客户端凭据文件。
BenchBase 从既有 XML 读取 mix/rate，多个 workload phase 会明确报错，不能猜测当前 phase。

Runtime 继续写原有 plan.json/plan_validation.json；plan.json 内新增 planner_intent、compilation，
planner_mode 为 intent_compiler_v1。planner_context.json 保存实际发送的脱敏上下文，
工具路径、认证字段及执行协议字段不会作为模型必填输入输出。

## Step 5: Repair Boundaries

JSON、Schema 与 Intent 语义错误共用最多 2 次修复调用；SQL EXPLAIN 错误按
source_map 指向的 `intents[index].sql.slot` 各最多修复 3 次。
Intent 按请求根因顺序归一化，根因数量和策略 SQL 槽位由 v1 契约限定，不能通过
重排根因、新增 Intent 或 SQL 字段重置计数。合法 SQL 不产生额外修复调用。

修复始终返回完整 IntentPlan，并重新验证、编译、EXPLAIN 和检查安全性。
进入 SQL 修复后，格式错误的响应也占用该 SQL 的额度；为纠正它再次调用模型时，
同时消耗公共格式/语义额度和 SQL 额度，不允许嵌套重试突破上限。

只有报错 SQL 与其使用的原本无效的目标表/字段引用可被修改。合法目标引用、
热点 predicate、node、strategy、load、其他 SQL 和预期证据均受保护。
越界修复立即以 repair_scope_violation 终止，不把修改后的实验交给执行器。

编译器固定 SQL 的 EXPLAIN 失败、错误 source_map 和编译异常归类 compiler_error，
不调用模型修复 TaskSpec。探针连接故障归类 database_connection_error；意外异常
在 planner 边界包装为 PlannerFallbackError，保留原始 cause、stage 与 error_type。
LLM 连接异常保留 PlannerLLMError，不触发 planner 修复循环。

plan.json 记录 planner_llm_calls、planner_json_repair_attempts、sql_repair_attempts
和 sql_repair_counts。失败文件保留阶段、原因、每次响应和验证历史，凭据脱敏。
测试使用 mock API/EXPLAIN，并断言 Executor.execute 从未被调用。
