"""初始化配置加载与执行路由解析。

任何组件使用配置前，本模块都会验证完整的非敏感部署契约。这里不检查可选执行器是否
真实安装；新环境的能力探测属于初始化流程的独立职责。
"""

from __future__ import annotations

# 中文排查：本模块把 initialization.json 校验为运行时可用的非敏感配置。
# 配置加载失败先看 config_version 和必填字段，再检查登记文件是否存在及路径是否与仓库一致。
# 这里不探测工具是否真实安装，也不读取密钥；这两类问题分别属于初始化检查和 SecretStore。

import json
from pathlib import Path
from typing import Any

from .constants import (
    CAPABILITY_LEVELS,
    CANONICAL_RUNTIME_ENVIRONMENTS,
    PRIORITIES,
    SCHEMA_VERSION,
)
from .errors import LoopError
from .paths import BASE_DIR, CONFIG_PATH


def normalize_execution_target(
    runtime_environment: str,
    provider_id: str | None = None,
) -> tuple[str, str | None]:
    if runtime_environment not in CANONICAL_RUNTIME_ENVIRONMENTS:
        raise LoopError(f"运行环境无效: {runtime_environment}")
    if runtime_environment == "self_hosted_agent":
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise LoopError("self_hosted_agent 任务必须提供 provider_id")
        return runtime_environment, provider_id.strip()
    if provider_id is not None:
        raise LoopError(f"{runtime_environment} 任务不得保存 provider_id")
    return runtime_environment, None


def resolve_execution_profile(
    runtime_environment: str,
    provider_id: str | None,
    capability_level: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value = config or load_initialization_config()
    runtime_environment, provider_id = normalize_execution_target(runtime_environment, provider_id)
    if capability_level not in CAPABILITY_LEVELS:
        raise LoopError(f"任务能力等级无效: {capability_level}")
    runtime_profiles = (value.get("execution_profiles") or {}).get(runtime_environment) or {}
    if runtime_environment == "self_hosted_agent":
        profile = ((runtime_profiles.get("providers") or {}).get(provider_id) or {}).get(
            "capabilities", {}
        ).get(capability_level)
    else:
        if runtime_profiles.get("provider_id") is not None:
            raise LoopError(f"{runtime_environment} execution profile 不得配置 provider_id")
        profile = (runtime_profiles.get("capabilities") or {}).get(capability_level)
    if not isinstance(profile, dict):
        raise LoopError(
            f"没有匹配的 execution profile: {runtime_environment}/{provider_id or '-'}/{capability_level}"
        )
    return {
        "runtime_environment": runtime_environment,
        "provider_id": provider_id,
        "capability_level": capability_level,
        "model": profile["model"],
        "reasoning": profile["reasoning"],
        "attempt_timeout_seconds": int(profile["attempt_timeout_seconds"]),
        "max_retries": int(profile["max_retries"]),
    }


def load_initialization_config(path: Path | str = CONFIG_PATH) -> dict[str, Any]:
    config_path = Path(path).resolve()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LoopError(f"初始化配置无效: {config_path}: {error}") from error
    workspace = config.get("workspace") or {}
    database = config.get("database") or {}
    prompts = config.get("prompts") or {}
    execution = config.get("task_execution") or {}
    planner = config.get("planner") or {}
    planner_boundary = planner.get("client_boundary") or {}
    planner_writeback = planner_boundary.get("writeback") or {}
    self_hosted_agent = config.get("self_hosted_agent") or {}
    deepseek = config.get("deepseek") or {}
    priority_policy = config.get("priority_policy") or {}
    dashboard = config.get("dashboard") or {}
    planner_scheduler = planner.get("scheduler") or {}
    dispatcher = config.get("dispatcher") or {}
    health = config.get("health") or {}
    supervisor = config.get("supervisor") or {}
    supervisor_components = supervisor.get("components") or {}
    platform_limits = execution.get("platform_max_active_executions") or {}
    runtime_environments = config.get("runtime_environments") or {}
    project_defaults = priority_policy.get("project_defaults") or {}
    expected_supervisor_components = {
        "planner": {
            "entry": "planner/main.py",
            "pid_path": "runtime/planner.pid",
            "heartbeat_path": "runtime/planner-heartbeat.json",
            "stop_path": "runtime/planner-stop-request.json",
            "log_path": "runtime/planner-scheduler.log",
        },
        "dispatcher": {
            "entry": "dispatcher/main.py",
            "pid_path": "runtime/dispatcher.pid",
            "heartbeat_path": "runtime/dispatcher-heartbeat.json",
            "stop_path": "runtime/dispatcher-stop-request.json",
            "log_path": "runtime/dispatcher-scheduler.log",
        },
    }
    valid_supervisor_components = (
        set(supervisor_components) == set(expected_supervisor_components)
        and all(
            isinstance(supervisor_components[name], dict)
            and all(
                supervisor_components[name].get(key) == expected
                for key, expected in contract.items()
            )
            and (BASE_DIR / supervisor_components[name]["entry"]).is_file()
            and all(
                (BASE_DIR / supervisor_components[name][key]).resolve().is_relative_to(BASE_DIR)
                for key in ("entry", "pid_path", "heartbeat_path", "stop_path", "log_path")
            )
            and isinstance(
                supervisor_components[name].get("heartbeat_timeout_seconds"), int
            )
            and supervisor_components[name]["heartbeat_timeout_seconds"] >= 1
            for name, contract in expected_supervisor_components.items()
        )
    )
    valid_runtime_environment_config = set(runtime_environments) == set(CANONICAL_RUNTIME_ENVIRONMENTS) and all(
        isinstance(runtime_environments[environment], dict)
        and isinstance(runtime_environments[environment].get("name"), str)
        and bool(runtime_environments[environment]["name"].strip())
        and isinstance(runtime_environments[environment].get("entry"), dict)
        and runtime_environments[environment]["entry"].get("type") == environment
        and runtime_environments[environment]["entry"].get("claim_argument") == environment
        for environment in CANONICAL_RUNTIME_ENVIRONMENTS
    )
    execution_profiles = config.get("execution_profiles") or {}
    valid_profile_fields = lambda item: (
        isinstance(item, dict)
        and isinstance(item.get("model"), str) and bool(item["model"].strip())
        and item.get("reasoning") in {"low", "medium", "high", "xhigh"}
        and isinstance(item.get("attempt_timeout_seconds"), int)
        and item["attempt_timeout_seconds"] > 0
        and isinstance(item.get("max_retries"), int)
        and 0 <= item["max_retries"] <= 10
    )
    self_hosted_profiles = execution_profiles.get("self_hosted_agent") or {}
    providers = self_hosted_profiles.get("providers") or {}
    provider_factories = self_hosted_agent.get("provider_factories") or {}
    self_hosted_profiles_valid = (
        isinstance(providers, dict) and bool(providers)
        and all(
            isinstance(provider, str) and bool(provider.strip())
            and isinstance(item, dict)
            and set((item.get("capabilities") or {})) == set(CAPABILITY_LEVELS)
            and all(valid_profile_fields(profile) for profile in item["capabilities"].values())
            for provider, item in providers.items()
        )
        and set(provider_factories) == set(providers)
        and all(
            isinstance(specification, str)
            and specification.count(":") == 1
            and all(part.strip() for part in specification.split(":"))
            for specification in provider_factories.values()
        )
    )
    valid = (
        config.get("config_version") == "5.0.0"
        and workspace.get("timezone") == "Asia/Shanghai"
        and isinstance(workspace.get("name"), str)
        and isinstance(workspace.get("task_root"), str)
        and isinstance(workspace.get("project_registry"), str)
        and database.get("path") == "data/loop-agent.sqlite3"
        and database.get("schema_version") == SCHEMA_VERSION
        and prompts.get("operator") == "operator/operator.md"
        and prompts.get("planner") == "planner/planner.md"
        and prompts.get("worker") == "worker/worker.md"
        and (BASE_DIR / prompts["operator"]).is_file()
        and (BASE_DIR / prompts["planner"]).is_file()
        and (BASE_DIR / prompts["worker"]).is_file()
        and isinstance(execution.get("heartbeat_interval_seconds"), int)
        and execution["heartbeat_interval_seconds"] >= 1
        and isinstance(execution.get("stalled_after_seconds"), int)
        and execution["stalled_after_seconds"] >= 1
        and isinstance(execution.get("task_lease_seconds"), int)
        and execution["task_lease_seconds"] >= 60
        and isinstance(execution.get("max_attempts"), int)
        and execution["max_attempts"] >= 1
        and isinstance(execution.get("global_max_active_executions"), int)
        and execution["global_max_active_executions"] >= 1
        and set(platform_limits) == set(CANONICAL_RUNTIME_ENVIRONMENTS)
        and all(
            isinstance(platform_limits[platform], int) and platform_limits[platform] >= 1
            for platform in CANONICAL_RUNTIME_ENVIRONMENTS
        )
        and execution.get("scope_conflict_mode") == "project"
        and isinstance(execution.get("require_human_approval_for"), list)
        and planner.get("execution_kind") == "PLANNER"
        and planner.get("default_runtime_environment") == "self_hosted_agent"
        and planner.get("provider_id") in providers
        and planner.get("provider_id") in provider_factories
        and planner.get("capability_level") in CAPABILITY_LEVELS
        and isinstance(planner.get("max_retries"), int)
        and planner["max_retries"] >= 0
        and isinstance(planner.get("max_active_executions"), int)
        and planner["max_active_executions"] >= 1
        and isinstance(planner.get("heartbeat_interval_seconds"), int)
        and planner["heartbeat_interval_seconds"] >= 1
        and isinstance(planner.get("stalled_after_seconds"), int)
        and planner["stalled_after_seconds"] >= planner["heartbeat_interval_seconds"]
        and isinstance(planner.get("lease_seconds"), int)
        and planner["lease_seconds"] >= 60
        and isinstance(planner.get("attempt_timeout_seconds"), int)
        and planner["attempt_timeout_seconds"] >= planner["lease_seconds"]
        and isinstance(planner_scheduler.get("scheduled"), bool)
        and isinstance(planner_scheduler.get("interval_minutes"), int)
        and planner_scheduler["interval_minutes"] >= 1
        and isinstance(planner_scheduler.get("heartbeat_interval_seconds"), int)
        and planner_scheduler["heartbeat_interval_seconds"] >= 1
        and planner_scheduler.get("runner_log_path") == "runtime/planner-runner.log"
        and (BASE_DIR / planner_scheduler["runner_log_path"]).resolve().is_relative_to(BASE_DIR)
        and planner_boundary.get("sandbox") == "read-only"
        and planner_boundary.get("approval_policy") == "never"
        and planner_boundary.get("network_access") is False
        and planner_boundary.get("default_tool_action") == "deny"
        and planner_boundary.get("source_access") == "read-only"
        and planner_writeback.get("transport") == "host_controlled_loopctl_stdin"
        and planner_writeback.get("payload_encoding") == "utf-8"
        and planner_writeback.get("integrity_policy") == "reject_suspicious_question_mark_corruption"
        and planner_writeback.get("controller") == str(BASE_DIR / "control" / "loopctl.py")
        and planner_writeback.get("allowed_commands") == [
            "preflight-claim", "preflight-heartbeat", "preflight-ready",
            "preflight-needs-review", "preflight-fail",
        ]
        and planner_writeback.get("direct_sql") is False
        and planner_writeback.get("report_files") is False
        and isinstance(self_hosted_agent.get("max_steps"), int)
        and 1 <= self_hosted_agent["max_steps"] <= 200
        and isinstance(self_hosted_agent.get("max_final_repairs"), int)
        and self_hosted_agent["max_final_repairs"] in {0, 1}
        and isinstance(self_hosted_agent.get("model_timeout_seconds"), (int, float))
        and self_hosted_agent["model_timeout_seconds"] > 0
        and isinstance(self_hosted_agent.get("tool_timeout_seconds"), (int, float))
        and self_hosted_agent["tool_timeout_seconds"] > 0
        and isinstance(self_hosted_agent.get("max_file_bytes"), int)
        and self_hosted_agent["max_file_bytes"] >= 1024
        and isinstance(self_hosted_agent.get("max_tool_output_chars"), int)
        and self_hosted_agent["max_tool_output_chars"] >= 1024
        and isinstance(deepseek.get("api_base_url"), str)
        and deepseek["api_base_url"].startswith("https://")
        and isinstance(deepseek.get("model"), str)
        and bool(deepseek["model"].strip())
        and isinstance(deepseek.get("timeout_seconds"), (int, float))
        and deepseek["timeout_seconds"] > 0
        and isinstance(deepseek.get("max_retries"), int)
        and 0 <= deepseek["max_retries"] <= 10
        and isinstance(deepseek.get("retry_backoff_seconds"), (int, float))
        and deepseek["retry_backoff_seconds"] >= 0
        and isinstance(deepseek.get("max_retry_backoff_seconds"), (int, float))
        and deepseek["max_retry_backoff_seconds"] >= deepseek["retry_backoff_seconds"]
        and isinstance(deepseek.get("api_key_environment_variable"), str)
        and bool(deepseek["api_key_environment_variable"].strip())
        and priority_policy.get("levels") == list(PRIORITIES)
        and all(priority in PRIORITIES for priority in project_defaults.values())
        and isinstance(dashboard.get("host"), str)
        and isinstance(dashboard.get("port"), int)
        and 1 <= dashboard["port"] <= 65535
        and isinstance(dashboard.get("poll_interval_ms"), int)
        and dashboard["poll_interval_ms"] >= 500
        and isinstance(dispatcher.get("scheduled"), bool)
        and isinstance(dispatcher.get("interval_minutes"), int)
        and dispatcher["interval_minutes"] >= 1
        and isinstance(dispatcher.get("heartbeat_interval_seconds"), int)
        and dispatcher["heartbeat_interval_seconds"] >= 1
        and dispatcher.get("working_directory") == str(BASE_DIR)
        and dispatcher.get("log_path") == "runtime/agent-dispatcher.log"
        and (BASE_DIR / dispatcher["log_path"]).resolve().is_relative_to(BASE_DIR)
        and dispatcher.get("runtime_environment") == "self_hosted_agent"
        and dispatcher.get("provider_id") in providers
        and isinstance(dispatcher.get("supported_capability_levels"), list)
        and set(dispatcher["supported_capability_levels"]) == set(CAPABILITY_LEVELS)
        and valid_runtime_environment_config
        and set(execution_profiles) == set(CANONICAL_RUNTIME_ENVIRONMENTS)
        and self_hosted_profiles_valid
        and health.get("scheduler") == "windows_task_scheduler"
        and isinstance(health.get("task_name"), str)
        and bool(health["task_name"].strip())
        and isinstance(health.get("interval_minutes"), int)
        and health["interval_minutes"] >= 1
        and isinstance(health.get("failure_threshold"), int)
        and health["failure_threshold"] >= 1
        and isinstance(health.get("heartbeat_timeout_seconds"), int)
        and health["heartbeat_timeout_seconds"] >= 1
        and isinstance(supervisor.get("monitor_interval_seconds"), (int, float))
        and supervisor["monitor_interval_seconds"] >= 1
        and isinstance(supervisor.get("component_start_timeout_seconds"), (int, float))
        and supervisor["component_start_timeout_seconds"] >= 1
        and isinstance(supervisor.get("component_stop_timeout_seconds"), (int, float))
        and supervisor["component_stop_timeout_seconds"] >= 1
        and isinstance(supervisor.get("runner_heartbeat_timeout_seconds"), (int, float))
        and supervisor["runner_heartbeat_timeout_seconds"] >= max(
            execution["heartbeat_interval_seconds"], planner["heartbeat_interval_seconds"]
        )
        and valid_supervisor_components
        and supervisor_components["planner"]["heartbeat_timeout_seconds"]
        >= planner_scheduler["heartbeat_interval_seconds"]
        and supervisor_components["dispatcher"]["heartbeat_timeout_seconds"]
        >= dispatcher["heartbeat_interval_seconds"]
    )
    if not valid:
        raise LoopError(f"初始化配置字段或取值无效: {config_path}")
    return config
