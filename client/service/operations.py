"""运维配置目录、工作区配置和公开 Provider 状态。

本模块函数是 Dashboard 中唯一允许改写 initialization.json 非敏感工作区路径的代码。
Provider 辅助函数只公开允许列表中的状态元数据；密钥值和密钥引用不会进入运维目录响应。
"""

from __future__ import annotations

# 中文排查：运维配置投影、工作区选择、Provider 状态和非敏感事件由本模块处理。
# 页面显示异常先检查 operations_config_payload；动作失败再检查路径约束和同源安全参数。
# 这里只写配置文件或健康事件，不得把部署配置、密钥状态写入任务 SQLite。

import copy
import json
import re
import threading
import uuid
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, Mapping

from loop_agent.providers.deepseek import DeepSeekSettings, verify_deepseek_credential
from loopdb import load_initialization_config, now_shanghai, parse_project_registry
from loop_agent.secrets.store import SecretStore


PROVIDER_ID_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
SECRET_EVENT_COMPONENT = "provider-secret"
SECRET_EVENT_STATUSES = {
    "configured": "configured_unverified",
    "rotated": "configured_unverified",
    "valid": "valid",
    "invalid": "invalid",
    "deleted": "not_configured",
}
_HEALTH_STATE_LOCK = threading.RLock()


class OperationsApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


def choose_task_root(initial_directory: Path) -> Path | None:
    """打开本机原生目录选择器，不接受浏览器直接提交的路径。"""
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError as error:
        raise OSError("当前系统没有可用的本机文件夹选择器") from error
    try:
        picker = tkinter.Tk()
        picker.withdraw()
        picker.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            parent=picker,
            initialdir=str(initial_directory),
            title="选择全局任务工作区",
            mustexist=True,
        )
    except tkinter.TclError as error:
        raise OSError("当前 Dashboard Server 无法打开本机文件夹选择器") from error
    finally:
        if "picker" in locals():
            picker.destroy()
    return Path(selected).resolve() if selected else None


def validate_task_root(candidate: Path, config: Mapping[str, Any]) -> tuple[Path, Path]:
    root = candidate.resolve()
    if root == Path(root.anchor):
        raise OperationsApiError(HTTPStatus.BAD_REQUEST, "全局任务工作区不能是磁盘根目录")
    if not root.is_dir():
        raise OperationsApiError(HTTPStatus.BAD_REQUEST, "所选全局任务工作区不存在")
    workspace = config.get("workspace")
    registry_value = workspace.get("project_registry") if isinstance(workspace, Mapping) else None
    if not isinstance(registry_value, str) or not registry_value:
        raise OperationsApiError(HTTPStatus.BAD_REQUEST, "项目清单配置无效")
    registry = root / Path(registry_value).name
    if not registry.is_file():
        raise OperationsApiError(HTTPStatus.BAD_REQUEST, "所选工作区缺少同名项目清单")
    try:
        projects = parse_project_registry(registry)
    except (OSError, ValueError) as error:
        raise OperationsApiError(HTTPStatus.BAD_REQUEST, "项目清单无法读取") from error
    missing = [project["path"] for project in projects if not (root / project["path"]).is_dir()]
    if missing:
        raise OperationsApiError(
            HTTPStatus.BAD_REQUEST,
            "所选工作区缺少已登记项目: " + ", ".join(missing),
        )
    return root, registry


def write_task_root_config(
    config_path: Path,
    config: Mapping[str, Any],
    root: Path,
    registry: Path,
) -> dict[str, Any]:
    updated = copy.deepcopy(dict(config))
    workspace = updated.get("workspace")
    if not isinstance(workspace, dict):
        raise OperationsApiError(HTTPStatus.BAD_REQUEST, "工作区配置无效")
    workspace["task_root"] = str(root)
    workspace["project_registry"] = str(registry)
    temporary = config_path.with_name(f"{config_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(config_path)
        return load_initialization_config(config_path)
    except (OSError, ValueError) as error:
        temporary.unlink(missing_ok=True)
        raise OperationsApiError(HTTPStatus.SERVICE_UNAVAILABLE, "全局任务工作区保存失败") from error


def write_scheduler_automation_config(
    config_path: Path,
    config: Mapping[str, Any],
    chain: str,
    enabled: bool,
) -> dict[str, Any]:
    """原子更新 Scheduler 单条调度链的自动化开关。"""
    chain_key = {"planner": "preflight", "dispatcher": "execution"}.get(chain)
    if chain_key is None or not isinstance(enabled, bool):
        raise OperationsApiError(HTTPStatus.BAD_REQUEST, "Scheduler 自动化配置无效")
    updated = copy.deepcopy(dict(config))
    scheduler = updated.get("scheduler")
    if not isinstance(scheduler, dict) or not isinstance(scheduler.get(chain_key), dict):
        raise OperationsApiError(HTTPStatus.BAD_REQUEST, "Scheduler 自动化配置缺失")
    scheduler[chain_key]["scheduled"] = enabled
    temporary = config_path.with_name(f"{config_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(config_path)
        return load_initialization_config(config_path)
    except (OSError, ValueError) as error:
        temporary.unlink(missing_ok=True)
        raise OperationsApiError(HTTPStatus.SERVICE_UNAVAILABLE, "Scheduler 自动化配置保存失败") from error


def provider_secret_refs(config: Mapping[str, Any]) -> dict[str, str]:
    execution_profiles = config.get("execution_profiles")
    self_hosted = execution_profiles.get("self_hosted_agent") if isinstance(execution_profiles, Mapping) else None
    providers = self_hosted.get("providers") if isinstance(self_hosted, Mapping) else None
    if not isinstance(providers, Mapping) or not providers:
        raise ValueError("self-hosted Provider configuration is missing")
    result: dict[str, str] = {}
    for provider_id in providers:
        if not isinstance(provider_id, str) or PROVIDER_ID_PATTERN.fullmatch(provider_id) is None:
            raise ValueError("Provider id is invalid")
        provider_config = config.get(provider_id)
        secret_ref = provider_config.get("secret_ref") if isinstance(provider_config, Mapping) else None
        if not isinstance(secret_ref, str) or not secret_ref:
            raise ValueError(f"Provider {provider_id} secret_ref is missing")
        result[provider_id] = secret_ref
    return result


def connection_verifiers(config: Mapping[str, Any]) -> dict[str, Callable[[str], bool | None]]:
    verifiers: dict[str, Callable[[str], bool | None]] = {}
    if "deepseek" in provider_secret_refs(config):
        settings = DeepSeekSettings.from_config(config)
        verifiers["deepseek"] = lambda candidate: verify_deepseek_credential(candidate, settings)
    return verifiers


def _read_health_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"events": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"events": []}
    return value if isinstance(value, dict) else {"events": []}


def _write_health_state(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.secret-api.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def record_provider_secret_event(
    path: Path,
    provider_id: str,
    event_status: str,
    *,
    operation: str,
    validation_scope: str | None = None,
) -> str:
    if event_status not in SECRET_EVENT_STATUSES:
        raise ValueError("secret event status is invalid")
    recorded_at = now_shanghai()
    event = {
        "at": recorded_at,
        "component": SECRET_EVENT_COMPONENT,
        "status": event_status.upper(),
        "message": "Provider Secret 状态已更新。",
        "details": {
            "provider_id": provider_id,
            "operation": operation,
            "validation_scope": validation_scope,
        },
    }
    with _HEALTH_STATE_LOCK:
        state = _read_health_state(path)
        events = [item for item in (state.get("events") or []) if isinstance(item, dict)]
        state["events"] = [event, *events][:100]
        _write_health_state(path, state)
    return recorded_at


def latest_provider_secret_event(path: Path, provider_id: str) -> dict[str, Any] | None:
    with _HEALTH_STATE_LOCK:
        events = _read_health_state(path).get("events") or []
    for event in events:
        if not isinstance(event, dict) or event.get("component") != SECRET_EVENT_COMPONENT:
            continue
        details = event.get("details")
        if isinstance(details, dict) and details.get("provider_id") == provider_id:
            return event
    return None


def provider_secret_status(
    store: SecretStore,
    provider_id: str,
    secret_ref: str,
    health_state_path: Path,
) -> dict[str, Any]:
    status = store.status(secret_ref)
    event = latest_provider_secret_event(health_state_path, provider_id)
    event_status = str(event.get("status", "")).casefold() if event else ""
    event_details = event.get("details") if event and isinstance(event.get("details"), dict) else {}
    if status.state == "missing":
        public_status, configured = "not_configured", False
    elif status.state == "ready":
        public_status = (
            SECRET_EVENT_STATUSES[event_status]
            if event_status in {"configured", "rotated", "valid", "invalid"}
            else "configured_unverified"
        )
        configured = True
    else:
        public_status, configured = "storage_unavailable", None
    last_validated_at = None
    validation_scope = None
    if event_status in {"valid", "invalid"}:
        last_validated_at = event.get("at")
        validation_scope = event_details.get("validation_scope")
    repair = None
    if status.state == "account_mismatch":
        repair = "请用配置的 SecretStore 账户运行 Dashboard Server；不要把密钥静默复制到其他账户。"
    elif status.state in {"access_denied", "backend_unavailable"}:
        repair = "请检查当前运行账户的系统密钥库会话与权限。"
    return {
        "provider_id": provider_id,
        "configured": configured,
        "backend": status.backend,
        "status": public_status,
        "last_validated_at": last_validated_at,
        "validation_scope": validation_scope,
        "persistent": status.persistent,
        "mutable": status.mutable,
        "repair": repair,
    }


def _config_value(config: Mapping[str, Any], *path: str, fallback: object = "未配置") -> object:
    current: object = config
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return fallback
        current = current[key]
    return current if isinstance(current, (str, int, float, bool)) or current is None else fallback


def operations_config_payload(
    config: Mapping[str, Any],
    secret_store: SecretStore,
    provider_secret_refs_by_id: Mapping[str, str],
    health_state_path: Path,
) -> dict[str, object]:
    """返回显式公开目录，绝不转发完整运行时配置。"""

    def item(
        key: str,
        label: str,
        value: object,
        source: str,
        description: str,
        activation: str,
        validation: str,
        state: str = "current",
        editable: bool = False,
    ) -> dict[str, object]:
        return {
            "key": key,
            "label": label,
            "value": value,
            "source": source,
            "description": description,
            "activation": activation,
            "validation": validation,
            "state": state,
            "editable": editable,
        }

    capabilities: list[dict[str, object]] = []
    profiles = config.get("execution_profiles")
    if isinstance(profiles, Mapping):
        for environment, profile in sorted(profiles.items()):
            if not isinstance(profile, Mapping):
                continue
            if environment == "self_hosted_agent":
                providers = profile.get("providers")
                if isinstance(providers, Mapping):
                    for provider_id, provider in sorted(providers.items()):
                        levels = provider.get("capabilities") if isinstance(provider, Mapping) else None
                        if isinstance(levels, Mapping):
                            for level, details in sorted(levels.items()):
                                if isinstance(details, Mapping):
                                    capabilities.append({
                                        "environment": environment,
                                        "provider": provider_id,
                                        "level": level,
                                        "model": details.get("model", "未配置"),
                                        "reasoning": details.get("reasoning", "未配置"),
                                        "attempt_timeout_seconds": details.get("attempt_timeout_seconds", "未配置"),
                                    })
                continue
            levels = profile.get("capabilities")
            if isinstance(levels, Mapping):
                for level, details in sorted(levels.items()):
                    if isinstance(details, Mapping):
                        capabilities.append({
                            "environment": environment,
                            "provider": None,
                            "level": level,
                            "model": details.get("model", "未配置"),
                            "reasoning": details.get("reasoning", "未配置"),
                            "attempt_timeout_seconds": details.get("attempt_timeout_seconds", "未配置"),
                        })

    environments: list[dict[str, object]] = []
    configured_environments = config.get("runtime_environments")
    if isinstance(configured_environments, Mapping):
        for environment, details in sorted(configured_environments.items()):
            if isinstance(details, Mapping):
                environments.append({"id": environment, "name": details.get("name", environment)})

    secret_statuses = [
        provider_secret_status(secret_store, provider_id, secret_ref, health_state_path)
        for provider_id, secret_ref in sorted(provider_secret_refs_by_id.items())
    ]
    task_execution = config.get("task_execution")
    planner = config.get("planner")
    scheduler = config.get("scheduler")
    dashboard = config.get("dashboard")
    self_hosted_agent = config.get("self_hosted_agent")
    health = config.get("health")
    priority_policy = config.get("priority_policy")
    prompts = config.get("prompts")
    workspace = config.get("workspace")
    platform_limits = (
        task_execution.get("platform_max_active_executions", {})
        if isinstance(task_execution, Mapping) else {}
    )
    platform_capacities = [
        {"environment": environment, "max_active_executions": limit}
        for environment, limit in sorted(platform_limits.items())
    ] if isinstance(platform_limits, Mapping) else []
    approval_actions = [
        {"action": action}
        for action in (task_execution.get("require_human_approval_for", []) if isinstance(task_execution, Mapping) else [])
        if isinstance(action, str)
    ]
    self_hosted_limits = [
        {"name": key, "value": value}
        for key, value in (self_hosted_agent.items() if isinstance(self_hosted_agent, Mapping) else [])
        if key in {"max_steps", "max_final_repairs", "model_timeout_seconds", "tool_timeout_seconds"}
    ]
    scheduler_preflight = scheduler.get("preflight") if isinstance(scheduler, Mapping) else {}
    scheduler_execution = scheduler.get("execution") if isinstance(scheduler, Mapping) else {}
    return {
        "ok": True,
        "generated_at": now_shanghai(),
        "sections": [
            {
                "id": "system",
                "title": "系统管理",
                "items": [
                    item("task-root", "全局任务工作区", _config_value(workspace if isinstance(workspace, Mapping) else {}, "task_root"), "config/initialization.json", "所有任务可修改范围的全局上界；每个任务仍受自身 scope 和 scope lock 约束。", "保存后对新任务生效", "目录、项目清单和活动 execution 校验", editable=True),
                    item("project-registry", "项目清单", _config_value(workspace if isinstance(workspace, Mapping) else {}, "project_registry"), "config/initialization.json", "项目路由实时读取的初始化登记清单。", "受保护", "配置加载时校验"),
                    item("dashboard-listener", "Dashboard 监听地址", f"{_config_value(dashboard if isinstance(dashboard, Mapping) else {}, 'host')}:{_config_value(dashboard if isinstance(dashboard, Mapping) else {}, 'port')}", "config/initialization.json", "本机任务面板和运维配置页面的服务地址。", "需重启", "启动时绑定校验"),
                    item("health-schedule", "Dashboard 健康检查", f"每 {_config_value(health if isinstance(health, Mapping) else {}, 'interval_minutes')} 分钟", "config/initialization.json", "Windows 计划任务检查 Dashboard，并在服务不可用时执行恢复。", "计划任务生效", "健康任务运行结果"),
                    item("schema", "任务库 Schema", _config_value(config, "database", "schema_version"), "config/initialization.json", "任务数据库的兼容性目标版本。", "受保护", "loopctl validate"),
                ],
            },
            {
                "id": "ai-configuration",
                "title": "AI 配置",
                "items": [
                    item("provider-status", "Provider 密钥状态", secret_statuses, "SecretStore", "仅显示公开状态；不含密钥值或引用。", "受保护", "SecretStore 状态检查"),
                    item("environments", "已登记运行环境", environments, "config/initialization.json", "任务领取时必须显式声明的执行环境。", "需重启", "配置加载时校验"),
                    item("capability-routes", "能力等级路由", capabilities, "config/initialization.json", "按运行环境、Provider 和等级声明的模型与推理参数。", "需重启", "配置加载时校验"),
                ],
            },
            {
                "id": "operator",
                "title": "Operator 管理",
                "items": [
                    item("operator-prompt", "Operator 提示词", _config_value(prompts if isinstance(prompts, Mapping) else {}, "operator"), "config/initialization.json", "人工任务管理对话的任务创建、更新、状态和归档约束。", "读取时生效", "文件存在性检查"),
                    item("priority-policy", "任务优先级策略", priority_policy.get("levels", "未配置") if isinstance(priority_policy, Mapping) else "未配置", "config/initialization.json", "任务优先级层级和项目默认优先级策略。", "新建或更新任务时生效", "配置加载时校验"),
                ],
            },
            {
                "id": "planner",
                "title": "Planner 管理",
                "items": [
                    item("planner-prompt", "Planner 协议", _config_value(prompts if isinstance(prompts, Mapping) else {}, "planner"), "config/initialization.json", "Planner 预检阶段的状态、结果和安全边界。", "读取时生效", "文件存在性检查"),
                    item("planner-runtime", "预检运行环境", _config_value(planner if isinstance(planner, Mapping) else {}, "default_runtime_environment"), "config/initialization.json", "预检 execution 只处理匹配运行环境与 Provider 的草稿任务。", "需重启预检 Runner", "配置加载时校验"),
                    item("planner-capacity", "预检并发上限", _config_value(planner if isinstance(planner, Mapping) else {}, "max_active_executions"), "config/initialization.json", "QUEUED 与 INSPECTING 预检 execution 共享的容量上限。", "下次排队生效", "排队事务校验"),
                    item("planner-boundary", "预检安全边界", _config_value(planner if isinstance(planner, Mapping) else {}, "client_boundary", "sandbox"), "config/initialization.json", "预检 Runner 只读检查，并通过受控 loopctl 写回。", "需重启预检 Runner", "配置加载时校验"),
                ],
            },
            {
                "id": "scheduler",
                "title": "Scheduler 管理",
                "items": [
                    item("scheduler-service", "常驻 Scheduler", "main.py serve", "scheduler/main.py", "统一维护 heartbeat，并独立推进预检与正式执行排队。", "当前生效", "data/runtime/health-state.json", "active"),
                    item("scheduler-preflight-cadence", "预检排队周期", f"每 {_config_value(scheduler_preflight if isinstance(scheduler_preflight, Mapping) else {}, 'interval_minutes')} 分钟", "config/initialization.json", "选择 DRAFT/UNINSPECTED 任务并通过 loopctl 原子排入 Planner 预检队列。", "需重启 Scheduler", "Scheduler 日志"),
                    item("scheduler-execution-cadence", "正式排队周期", f"每 {_config_value(scheduler_execution if isinstance(scheduler_execution, Mapping) else {}, 'interval_minutes')} 分钟", "config/initialization.json", "选择 PENDING/READY 自动任务并原子创建 WORKER/QUEUED execution。", "需重启 Scheduler", "正式排队日志"),
                    item("scheduler-heartbeat", "Heartbeat 周期", f"每 {_config_value(scheduler if isinstance(scheduler, Mapping) else {}, 'heartbeat_interval_seconds')} 秒", "config/initialization.json", "两条调度链共享一个 Scheduler 进程 heartbeat。", "需重启 Scheduler", "heartbeat 文件"),
                ],
            },
            {
                "id": "supervisor",
                "title": "Supervisor 管理",
                "items": [
                    item("supervisor-service", "常驻 Supervisor", "main.py serve", "supervisor/main.py", "周期检查并恢复独立 Dashboard、Scheduler 与 Runner 的可核实状态。", "当前生效", "data/runtime/health-state.json", "active"),
                    item("supervisor-health", "服务恢复边界", "Windows 健康任务", "config/initialization.json", "健康任务只探测并恢复 Supervisor 主进程，不领取或执行任务。", "当前生效", "健康任务运行结果"),
                ],
            },
            {
                "id": "worker",
                "title": "Worker 管理",
                "items": [
                    item("worker-prompt", "Worker 提示词", _config_value(prompts if isinstance(prompts, Mapping) else {}, "worker"), "config/initialization.json", "不同执行入口共同遵循的单任务、scope 锁和结果写回协议。", "读取时生效", "文件存在性检查"),
                    item("global-capacity", "全局并发上限", _config_value(task_execution if isinstance(task_execution, Mapping) else {}, "global_max_active_executions"), "config/initialization.json", "所有运行环境共享的活动 execution 上限。", "需重启", "领取事务校验"),
                    item("platform-capacity", "平台并发上限", platform_capacities, "config/initialization.json", "每个运行环境的活动 execution 上限。", "需重启", "领取事务校验"),
                    item("human-approvals", "人工批准动作", approval_actions, "config/initialization.json", "Worker 必须由人工明确批准的高风险动作。", "受保护", "领取与 finish 事务校验"),
                ],
            },
            {
                "id": "runner",
                "title": "Runner 管理",
                "items": [
                    item("self-hosted-limits", "自建 Agent 运行上限", self_hosted_limits or "未配置", "config/initialization.json", "自建 Agent 的步骤、模型、工具和输出边界。", "需重启", "配置加载时校验"),
                    item("runner-service", "独立 Runner 服务", "main.py serve", "runner/agent_runtime.py", "常驻读取两类队列、计算容量并选择候选；当前明确不启动 AI Worker。", "当前生效", "data/runtime/runner-queue-state.json", "active"),
                ],
            },
        ],
    }
