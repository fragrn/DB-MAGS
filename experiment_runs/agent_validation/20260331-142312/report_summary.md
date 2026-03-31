# Agent Validation Summary

- Run directory: `experiment_runs/agent_validation/20260331-142312`
- Model: `gpt-5`
- OpenAI available: `True`
- Passed: `3`
- Failed: `2`

| Experiment | Agent | Status | Key Observation |
| --- | --- | --- | --- |
| E1 | GlobalPlannerAgent | fail | follow_up_count=3 ; initial_task_count=0 ; revised_task_count=0 |
| E2 | SQLAnomalyAgent | pass | task_count=1 |
| E3 | ResourceAgent | pass | command=.tools/chaosblade-1.8.0-darwin_arm64/blade create cpu fullload |
| E4 | TrafficAgent | pass | {"mode": "overall_workload", "sleep_time": 0.005, "thread_count": 500, "description": "Increase overall workload by reducing sleep time and raising concurrency."} |
| E5 | GlobalPlannerAgent+TaskAgents | fail | initial_agents=[] ; revised_agents=[] |

## Recommended Plots

- Pass/fail bar chart by agent
- Agent status heatmap for E1-E5
- Task profile comparison for Resource vs Traffic agents
