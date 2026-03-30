from __future__ import annotations

import os
from dataclasses import dataclass

from agent_runtime.env_loader import load_dotenv_files


@dataclass
class RuntimeConfig:
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-5"
    planner_temperature: float = 0.2
    sql_temperature: float = 0.1
    planner_enabled: bool = True
    sql_llm_enabled: bool = True
    default_database: str = "tpcc10_test"
    max_concurrency: int = 3

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        load_dotenv_files()
        return cls(
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5"),
            planner_temperature=float(os.getenv("OPENAI_PLANNER_TEMPERATURE", "0.2")),
            sql_temperature=float(os.getenv("OPENAI_SQL_TEMPERATURE", "0.1")),
            planner_enabled=os.getenv("ENABLE_OPENAI_PLANNER", "1") != "0",
            sql_llm_enabled=os.getenv("ENABLE_OPENAI_SQL", "1") != "0",
            default_database=os.getenv("DBMAGS_DEFAULT_DATABASE", "tpcc10_test"),
            max_concurrency=int(os.getenv("DBMAGS_MAX_CONCURRENCY", "3")),
        )
