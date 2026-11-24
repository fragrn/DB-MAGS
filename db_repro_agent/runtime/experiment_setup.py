"""Experiment-scoped MySQL instrumentation setup and restoration."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..config import InstrumentationProfile


class MySQLInstrumentationState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: dict[str, str] = Field(default_factory=dict)


class MySQLInstrumentation:
    VARIABLES = ("slow_query_log", "long_query_time", "log_output")

    def __init__(self, adapter) -> None:
        self.adapter = adapter

    def prepare(self, profile: InstrumentationProfile) -> MySQLInstrumentationState:
        state = self.capture()
        desired = {
            "slow_query_log": "ON" if profile.slow_query_log else "OFF",
            "long_query_time": str(profile.long_query_time_seconds),
            "log_output": profile.log_output.upper(),
        }
        try:
            with self.adapter.connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"SET GLOBAL slow_query_log = {desired['slow_query_log']}")
                    cursor.execute("SET GLOBAL long_query_time = %s", (float(desired["long_query_time"]),))
                    cursor.execute("SET GLOBAL log_output = %s", (desired["log_output"],))
        except Exception:
            self.restore(state)
            raise
        return state

    def capture(self) -> MySQLInstrumentationState:
        values: dict[str, str] = {}
        for variable in self.VARIABLES:
            result = self.adapter.execute_readonly(f"SHOW VARIABLES LIKE '{variable}'")
            if not result.success or not result.data:
                raise RuntimeError(result.error_message or f"unable to read {variable}")
            values[variable] = str(result.data[0].get("Value", ""))
        return MySQLInstrumentationState(values=values)

    def restore(self, state: MySQLInstrumentationState) -> None:
        with self.adapter.connect() as connection:
            with connection.cursor() as cursor:
                if "slow_query_log" in state.values:
                    cursor.execute(f"SET GLOBAL slow_query_log = {'ON' if state.values['slow_query_log'].upper() == 'ON' else 'OFF'}")
                if "long_query_time" in state.values:
                    cursor.execute("SET GLOBAL long_query_time = %s", (float(state.values["long_query_time"]),))
                if "log_output" in state.values:
                    cursor.execute("SET GLOBAL log_output = %s", (state.values["log_output"],))
