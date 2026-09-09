# 修复 Planner 首次 JSON 输出失败

## Summary

当前错误有两层原因：

1. LLM 输出在末尾缺少一个 `}`，导致 JSON 语法解析失败。
2. 输出内容仍使用旧版 TaskSpec 格式，例如 `node`、`anomaly_type`、`action_type`，不符合当前 Planner 要求的 `task_id`、`metadata.root_cause`、`kind` 等字段。

修改为：

```text
LLM 生成
  ↓
提取/解析 JSON
  ↓
JSON 结构校验
  ├─ 成功 → 本地 SQL EXPLAIN / DAG / SafetyChecker
  └─ 失败 → LLM 修复完整 Planner JSON，最多 2 次
```

不使用规则 fallback，也不直接篡改不完整 JSON。

## Implementation Changes

### 1. 增强 LLM JSON 输出约束

修改 `llm_generate()`：

- 为 Planner 请求设置足够的最大输出长度，避免 JSON 在末尾被截断。
- 对支持的模型继续启用 JSON response format。
- 保留完整原始响应文本。
- 对响应中的 Markdown 代码块和前后说明文字做安全剥离。
- 仅提取完整 JSON object，不通过简单补 `}` 的方式伪造结果。

新增配置：

```text
OPENAI_PLANNER_MAX_TOKENS=12000
```

默认值设为 `12000`。

### 2. Planner 增加完整 JSON 提取与校验

修改 `AnomalyGraphAgent/planner.py`：

- `_payload_from_response()` 按以下顺序处理：
  1. 优先使用 `json_payload`。
  2. 尝试解析完整响应文本。
  3. 去除 ```json 代码块。
  4. 从文本中提取第一个完整 JSON object。
  5. 如果 JSON 不完整或无法解析，进入 Planner JSON 修复流程。
- 校验完整 Planner 输出必须包含：
  - `task_specs`
  - `dependencies`
  - `reasoning`
  - `success_criteria`
- 每个 TaskSpec 必须包含：
  - `task_id`
  - `task_type`
  - `actions`
  - `expected_metrics`
  - `success_criteria`
  - `risk_assessment`
  - `metadata.root_cause`
- 每个 action 必须使用 `kind`，不接受旧版 `action_type`。
- 不自动把旧版字段静默转换成新版字段，避免隐藏 LLM 输出问题。

### 3. 新增 Planner JSON 修复调用

新增独立提示词：

```text
The planner output is invalid.

Return the complete corrected planner JSON.
Do not change target_path or injected_nodes.
Use exactly the required TaskSpec schema:
task_id, task_type, actions, expected_metrics,
success_criteria, risk_assessment, metadata.root_cause.

Each action must use "kind", not "action_type".
Do not include legacy fields such as node or anomaly_type.
Return JSON only.
```

修复上下文包含：

```json
{
  "repair_type": "planner_json_schema_failure",
  "attempt": 1,
  "max_attempts": 2,
  "target_path": [],
  "injected_nodes": [],
  "parse_error": "...",
  "raw_output": "...",
  "required_schema": {},
  "planner_context": {}
}
```

规则：

- 首次生成失败后最多调用 2 次 JSON 修复 LLM。
- 修复调用必须返回完整 Planner JSON，而不是只返回缺失片段。
- 每次修复后重新执行：
  1. JSON 解析
  2. Planner schema 校验
  3. injected node 校验
  4. action 校验
  5. SQL EXPLAIN
- 修复仍失败时终止规划，不执行实验。

### 4. 保护传播链约束

本地校验必须确保 LLM 修复过程中不能修改：

- `target_path`
- `injected_nodes`
- `target_database`
- `max_duration_sec`
- `injection_observe_sec`

如果修复结果改变这些字段，直接抛出 `PlannerFallbackError`。

### 5. 完善结果与 trace

在 `react_trace.json` 中记录：

```json
{
  "action": "llm_generate_plan",
  "llm": true,
  "attempt": 0
}
```

以及：

```json
{
  "action": "llm_repair_plan_json",
  "llm": true,
  "attempt": 1,
  "reason": "invalid_json_or_schema"
}
```

在 `plan.json` 中新增：

```json
{
  "planner_mode": "single_pass_with_json_repair_and_sql_repair",
  "planner_llm_calls": 2,
  "planner_json_repair_attempts": 1,
  "planner_json_repair_max_attempts": 2,
  "sql_repair_attempts": 0,
  "sql_repair_max_attempts": 3
}
```

失败时保存：

```text
planner_failure.json
planner_validation_failure.json
```

其中必须包含：

- 原始 LLM 输出
- JSON 解析错误
- schema 校验错误
- 每次修复请求
- 每次修复响应
- 最终失败原因

## Test Plan

新增离线测试：

1. 完整合法 Planner JSON 只调用 1 次 LLM。
2. JSON 末尾缺少 `}` 时触发 JSON 修复调用。
3. JSON 包含 Markdown 代码块时可以正确提取。
4. JSON 前有说明文字时可以正确提取。
5. 旧版 `node`、`anomaly_type`、`action_type` 格式触发 JSON schema 修复。
6. 修复后返回新版 TaskSpec 时继续执行 SQL EXPLAIN。
7. 修复结果修改 `target_path` 时被拒绝。
8. 连续 2 次 JSON 修复失败后抛出 `PlannerFallbackError`。
9. 失败目录中保存完整原始响应。
10. 合法 SQL 的流程仍只调用一次 Planner LLM。
11. SQL EXPLAIN 失败时继续使用原有最多 3 次 SQL 修复机制。
12. `planner_llm_calls`、JSON 修复次数和 SQL 修复次数统计准确。

验证命令：

```bash
python -m pytest tests/test_agent_react_planner.py -q
python -m pytest tests/test_agent_workload.py tests/test_slow_query_evidence.py tests/test_agent_report.py -q
```

## Assumptions

- JSON 语法修复最多 2 次，SQL EXPLAIN 修复仍最多 3 次。
- 不采用简单补括号、补逗号等不可验证的本地修复。
- 不恢复旧版 TaskSpec 字段兼容转换，统一要求 LLM 输出当前 schema。
- 不修改异常传播链、慢日志判定、QPS 判定和实验执行逻辑。
- 该问题修复后，`hot_update_lock` 首次规划应先通过 JSON/schema 校验，再进入 SQL EXPLAIN 阶段。
