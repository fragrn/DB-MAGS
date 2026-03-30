# 对话式 Skill + Multi-Agent 异常生成架构说明（V1）

本文档说明当前新增的对话式 agent 运行时如何组织、如何使用，以及它和现有 DB-MAGS 脚本之间的关系。

## 1. 目标

当前仓库原本的能力是：

- 通过预定义 SQL 注入锁冲突、慢 SQL、缺失索引等异常
- 通过 ChaosBlade 注入 CPU / IO / 网络 / 内存 / 磁盘类资源瓶颈
- 通过参数修改方式模拟流量和负载激增

新增的 V1 agent 运行时在此基础上增加了三类能力：

- 对话式交互：用户可以像和 agent 对话一样提出实验目标
- 计划确认：global agent 会先生成实验计划，用户确认后才执行
- 多 agent 分工：不同异常类型交给不同 task agent 生成和执行，互相隔离

V1 的定位不是替代原有脚本，而是在原有脚本与异常模板之上新增一个更高层的编排和交互层。

## 2. 总体架构

V1 运行时分为五层：

### 2.1 CLI Conversation Orchestrator

入口文件：`agent_cli.py`

职责：

- 接收用户输入
- 驱动一轮对话
- 展示 plan
- 接收用户的 `revise` / `confirm` / `cancel`
- 在确认后调度执行

它不直接做异常设计，也不直接执行 SQL 或 shell 命令。

### 2.2 Global Planner Agent

核心文件：`agent_runtime/planner.py`

职责：

- 调用 metadata / distribution skills 获取数据库画像
- 判断是否缺少必要信息
- 在必要时向用户追问
- 根据用户目标和数据库上下文生成 `ExperimentPlan`
- 将任务拆给各类 task agent

当前实现支持两种规划方式：

- 配置了 `OPENAI_API_KEY`：可调用 OpenAI Responses API 生成更自然的计划摘要和 SQL 候选
- 未配置 `OPENAI_API_KEY`：自动回退到本地规则和已有异常模板

### 2.3 Task Agents

目录：`agent_runtime/agents/`

V1 目前实现了三类 task agent：

#### SQLAnomalyAgent

文件：`agent_runtime/agents/sql_agent.py`

负责：

- 锁冲突类 SQL
- 慢 SQL 类注入
- 缺失索引
- 隐式转换
- join / order by / group by / 大表扫描

工作方式：

1. 调用 `generate_sql_candidate_skill` 生成候选 SQL
2. 调用 `validate_sql_skill` 做静态安全校验
3. 调用 `explain_sql_skill` 做 EXPLAIN 验证
4. 只把通过校验的 SQL 变成最终 task

也就是典型的：

- LLM / 模板生成
- 规则约束
- 验证筛选

#### ResourceAgent

文件：`agent_runtime/agents/resource_agent.py`

负责：

- CPU
- IO
- Disk
- Memory
- Network

工作方式：

- 规则驱动
- 调用 `chaosblade_injection_skill` 生成 ChaosBlade 命令
- 由统一执行器执行 shell 命令

#### TrafficAgent

文件：`agent_runtime/agents/traffic_agent.py`

负责：

- single SQL 流量提升
- overall workload 流量提升

工作方式：

- 规则驱动
- 调用 `workload_tuning_skill` 生成新的并发和 sleep 参数配置
- 由统一执行器执行或桥接到后续 workload 逻辑

### 2.4 Skills

目录：`agent_runtime/skills/`

skill 是单一能力单元，不负责全链路决策。

当前已实现的 skill 包括：

- `inspect_schema_skill`：读取 schema、表、列、索引、行数
- `inspect_distribution_skill`：读取有限分布信息/采样上下文
- `generate_sql_candidate_skill`：生成 SQL 候选
- `validate_sql_skill`：检查 SQL 是否安全、是否引用合法表
- `explain_sql_skill`：执行 EXPLAIN 验证 SQL
- `chaosblade_injection_skill`：生成 ChaosBlade 命令
- `workload_tuning_skill`：生成负载调优配置
- `run_injection_skill`：统一执行 SQL / shell / workload profile
- `collect_metrics_skill`：汇总任务级信号
- `cleanup_skill`：按 task 粒度执行清理

### 2.5 Scheduler / Executor

文件：

- `agent_runtime/scheduler.py`
- `agent_runtime/executor.py`

职责：

- 并发执行多个 task
- 收集每个 task 的结果
- 单个 task 失败时不影响其他 task
- 执行 cleanup

这部分保证了你要求的“一个 task agent 有问题，不影响另一个 task agent”。

## 3. 数据结构

核心类型在：`agent_runtime/types.py`

### 3.1 ExperimentRequest

表示用户请求，包括：

- 用户目标
- 目标数据库
- 允许的异常类型
- 执行窗口
- 风险等级
- 用户补充约束

### 3.2 ExperimentPlan

表示 global agent 输出的实验计划，包括：

- 实验摘要
- 数据库上下文摘要
- tasks 列表
- 预期指标变化
- 安全检查项
- 清理计划

### 3.3 TaskSpec

每个 task agent 的标准输出，包括：

- `task_id`
- `agent_type`
- `anomaly_type`
- `inputs`
- `prechecks`
- `execution_steps`
- `validation_steps`
- `rollback_steps`

### 3.4 TaskResult

统一记录每个任务执行结果，包括：

- 状态
- 产物
- 观测到的信号
- 错误
- cleanup 状态

## 4. 当前执行流程

一轮完整的 CLI 交互流程如下：

1. 用户输入实验目标
2. Global planner 读取数据库上下文
3. 若信息不足，则追问用户
4. 生成 `ExperimentPlan`
5. 用户通过 `show plan` 查看计划
6. 用户可通过 `revise <文本>` 修改目标
7. 用户执行 `confirm` 后才真正运行 tasks
8. scheduler 并发执行各 task
9. 返回聚合后的结果

## 5. 如何运行

## 5.1 最简单的运行方式

```bash
python3 agent_cli.py "Plan a missing-index and cpu contention experiment" --db tpcc10_test --anomalies missing_index,cpu
```

进入 CLI 后可使用：

```text
show plan
revise reduce the risk and keep only missing_index
confirm
cancel
```

## 5.2 OpenAI 相关环境变量

如果希望 global agent 和 SQL 候选生成使用 OpenAI：

```bash
export OPENAI_API_KEY=<your_key>
export OPENAI_MODEL=gpt-5
```

可选参数：

```bash
export OPENAI_BASE_URL=https://api.openai.com/v1
export OPENAI_PLANNER_TEMPERATURE=0.2
export OPENAI_SQL_TEMPERATURE=0.1
export ENABLE_OPENAI_PLANNER=1
export ENABLE_OPENAI_SQL=1
```

如果没有设置 `OPENAI_API_KEY`，系统会自动退回本地规则和已有模板。

## 6. 与原有 DB-MAGS 脚本的关系

当前 V1 并没有把原系统完全重写，而是采用“桥接”的方式。

### 6.1 已复用的现有能力

主要复用了以下已有资产：

- `tpcc_operation_set.py` 中的大量异常模板
- `single_anomaly.py` / `multi_anomaly.py` 中的执行逻辑思路
- `Connection/Connection.py` 中的数据库连接方式
- `Parameter_Modification/Parameter_Modification.py` 的参数修改能力

### 6.2 当前 bridge 的方式

V1 通过 `run_injection_skill` 统一执行：

- SQL 类型 step
- shell 类型 step
- workload profile 类型 step

这是一个过渡实现。后续如果你希望和原脚本完全打通，可以继续做两类增强：

- 把 `single_anomaly.py` / `multi_anomaly.py` 中的注入流程拆成更细粒度的可复用函数
- 让 `run_injection_skill` 直接调用这些函数，而不是当前这种统一的轻量桥接

## 7. 当前支持的异常类型

V1 架构面向全分类，但目前真正落地的是三大类：

### 7.1 SQL 类

- `record_lock`
- `table_lock`
- `metadata_lock`
- `missing_index`
- `implicit_conversion`
- `multi_table_join`
- `order_by`
- `group_by`
- `large_table_scan`

### 7.2 资源类

- `cpu`
- `io`
- `disk`
- `memory`
- `network`

### 7.3 流量类

- `single_sql`
- `overall_workload`
- `flow`
- `traffic`

## 8. 当前限制

V1 已经能作为一个完整的 agent runtime 骨架使用，但仍有几个现实限制：

### 8.1 SQL 验证还是轻量级

当前验证主要是：

- 静态规则检查
- EXPLAIN 验证

它还没有做到：

- 更严格的执行代价建模
- 真实慢查询阈值判断
- 自动选择最优慢 SQL 候选

### 8.2 Traffic agent 目前以配置描述为主

当前 `TrafficAgent` 已能生成 workload profile task，但和原来的 tpcc 压测执行流程仍是轻量桥接，还没有把所有参数改动真正注入到原有 workload 生命周期中。

### 8.3 Cleanup 仍然偏保守

当前 cleanup 机制是 task 级别的统一回收框架，但像 ChaosBlade destroy、临时参数恢复、长事务释放等逻辑，还可以继续细化。

### 8.4 LLM 不是强依赖

这是一个刻意设计：

- 没有 OpenAI key 时，系统也应能工作
- 所以当前很多逻辑都带有 fallback

这保证了系统不会因为 LLM 不可用而完全失效，但也意味着“最智能”的规划和 SQL 生成能力还有提升空间。

## 9. 建议的下一步演进

如果你接下来继续做，我建议按下面顺序推进：

### 9.1 第一优先级

把这三个最关键 agent 做深：

- `SQLAnomalyAgent`
- `ResourceAgent`
- `TrafficAgent`

重点是让它们与原有注入生命周期更紧密结合。

### 9.2 第二优先级

增强 SQL agent：

- 基于更真实的 metadata 和统计信息选列
- 增加更严格的 SQL 风险审计
- 增加慢 SQL 试跑与超时验证

### 9.3 第三优先级

增加更多 task agent：

- 备份类 agent
- 复合异常 agent
- 因果链异常 agent

### 9.4 第四优先级

扩展交互面：

- 在现有 Dash 页面上增加聊天式前端
- 让 Web 和 CLI 共享同一套 planner / scheduler / skills

## 10. 测试

当前测试文件：`tests/test_agent_runtime.py`

已覆盖的内容：

- 缺信息时 planner 会追问
- 用户确认前不会执行
- scheduler 能隔离单 task 失败
- runtime 可构建

运行方式：

```bash
python3 -m unittest discover -s tests -v
```

## 11. 总结

当前 V1 已经把项目从“脚本驱动的异常生成”推进到了“可对话、可确认、可分发、可扩展”的 agent 架构：

- 用户通过 CLI 与 global agent 对话
- global agent 结合 schema 和用户目标生成计划
- 不同 task agent 独立准备任务
- skills 提供单一能力
- scheduler/executor 负责并发执行和失败隔离

从工程角度看，这是一套适合继续演进的骨架，而不是一次性写死的最终系统。后续你可以继续往更强的 SQL 生成、更严格的验证、更完整的 Web 交互和更丰富的异常分类上扩展。
