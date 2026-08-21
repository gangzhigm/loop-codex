from __future__ import annotations

# 中文排查：这是控制面主回归，覆盖 Schema、迁移、Planner、Worker、依赖、锁、恢复和 Operator。
# 失败时先根据测试类定位角色，再检查临时数据库中的第一条不一致，不要直接改测试期望绕过规则。
# 并发测试具有时序含义，修改事务或锁代码后应单独重复运行相关测试。

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True

from _bootstrap import REPOSITORY_ROOT

from loopdb import (
    ALLOWED_TABLES,
    ARCHIVABLE_STATUSES,
    CAPABILITY_LEVELS,
    CANONICAL_RUNTIME_ENVIRONMENTS,
    DEFAULT_DB,
    RUNTIME_ENVIRONMENTS,
    SCHEMA_PATH,
    SCHEMA_USER_VERSION,
    LoopError,
    all_tasks,
    connect,
    initialize_schema,
    insert_task,
    load_initialization_config,
    now_shanghai,
    normalize_scope,
    resolve_execution_profile,
    scope_keys_conflict,
    state_payload,
    validate_database,
)


BASE_DIR = REPOSITORY_ROOT
LOOPCTL = BASE_DIR / "control" / "loopctl.py"


class LoopTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "test.sqlite3"
        database = connect(self.db_path)
        initialize_schema(database)
        database.close()
        self.project_paths = [f"project-{index}" for index in range(1, 13)] + ["local-agent-loop"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add_task(
        self,
        task_id: str,
        project: str,
        priority: str = "medium",
        capability_level: str = "L2",
        runtime_environment: str = "self_hosted_agent",
        *,
        provider_id: str | None = "deepseek",
        lock_mode: str = "project",
        scope: list[str] | None = None,
    ) -> None:
        database = connect(self.db_path)
        insert_task(
            database,
            {
                "id": task_id,
                "title": task_id,
                "description": "test",
                "status": "PENDING",
                "priority": priority,
                "capability_level": capability_level,
                "runtime_environment": runtime_environment,
                "provider_id": provider_id,
                "created_at": now_shanghai(),
                "scope": scope or [f"{project}/file.txt"],
                "lock_mode": lock_mode,
                "acceptance": ["test"],
            },
            actor="test",
            project_paths=self.project_paths,
        )
        database.close()

    def run_ctl(
        self,
        *arguments: str,
        input_text: str | None = None,
        db_path: Path | None = None,
    ) -> dict[str, object]:
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [sys.executable, str(LOOPCTL), "--db", str(db_path or self.db_path), *arguments],
            cwd=BASE_DIR,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            input=input_text,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def claim(
        self,
        execution_id: str,
        capability_level: str = "L2",
        runtime_environment: str = "self_hosted_agent",
        provider_id: str | None = "deepseek",
    ) -> dict[str, object]:
        database = connect(self.db_path)
        exists = database.execute(
            "SELECT 1 FROM executions WHERE execution_id=?", (execution_id,)
        ).fetchone()
        database.close()
        if exists is None:
            from scheduler.execution_dispatch import (
                EventLogger,
                ExecutionDispatcher,
                ExecutionDispatchSettings,
            )

            config = load_initialization_config()
            settings = replace(
                ExecutionDispatchSettings.from_config(config),
                database_path=self.db_path,
                supported_capability_levels=(capability_level,),
                max_tasks_per_cycle=1,
            )
            ExecutionDispatcher(
                settings,
                config,
                execution_id_factory=lambda _level: execution_id,
                logger=EventLogger(None),
                route_filter=(runtime_environment, provider_id),
            ).run()
        arguments = [
            "claim",
            execution_id,
            "--capability-level",
            capability_level,
            "--runtime-environment",
            runtime_environment,
        ]
        if provider_id is not None:
            arguments.extend(["--provider-id", provider_id])
        return self.run_ctl(*arguments)

    def run_ctl_error(
        self,
        *arguments: str,
        db_path: Path | None = None,
        input_text: str | None = None,
    ) -> dict[str, object]:
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [sys.executable, str(LOOPCTL), "--db", str(db_path or self.db_path), *arguments],
            cwd=BASE_DIR,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            input=input_text,
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        return json.loads(completed.stdout)

    def finish(self, execution_id: str, task_id: str) -> dict[str, object]:
        return self.run_ctl(
            "finish",
            execution_id,
            task_id,
            input_text=json.dumps({"status": "SUCCEEDED", "summary": "done", "verification": ["ok"]}),
        )

    @staticmethod
    def result_diagnostic() -> dict[str, object]:
        field_names = (
            "status", "summary", "verification", "completed", "error", "question",
            "options", "result", "message", "output",
        )
        return {
            "category": "final_schema",
            "http_status": None,
            "retryable": False,
            "retry_exhausted": False,
            "finish_reason": "stop",
            "agent_attempt": 1,
            "model_step": 1,
            "final_shape": {
                "finish_reason": "stop",
                "content_length": 84,
                "json_parse_state": "parsed",
                "top_level_type": "object",
                "allowed_fields": {
                    name: {
                        "present": name in {"status", "summary", "verification"},
                        "type": "string" if name in {"status", "summary", "verification"} else "unavailable",
                    }
                    for name in field_names
                },
                "unknown_field_count": 0,
                "unknown_fields_present": False,
            },
        }

    def enqueue_draft(
        self,
        task_id: str,
        *,
        capability: str = "L3",
        execution_policy: str = "automatic",
    ) -> dict[str, object]:
        path = Path(self.temporary.name) / f"{task_id}.json"
        path.write_text(
            json.dumps(
                {
                    "id": task_id,
                    "title": task_id,
                    "description": "Operator business description",
                    "priority": "critical",
                    "runtime_environment": "self_hosted_agent",
                    "provider_id": "deepseek",
                    "estimated_capability_level": capability,
                    "execution_policy": execution_policy,
                    "scope_hint": ["local-agent-loop/control/loopctl.py"],
                    "acceptance": ["business acceptance"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return self.run_ctl("enqueue", str(path))

    def planner_claim(self, execution_id: str) -> dict[str, object]:
        runtime_environment = load_initialization_config()["planner"][
            "worker_runtime_environment"
        ]
        return self.run_ctl(
            "preflight-claim", execution_id,
            "--runtime-environment", runtime_environment,
            "--sandbox", "read-only",
        )

    @staticmethod
    def ready_report(
        capability: str = "L3",
        *,
        lock_mode: str = "project",
        scope: list[str] | None = None,
    ) -> str:
        return json.dumps(
            {
                "summary": "静态检查通过",
                "capability_level": capability,
                "scope": scope or ["local-agent-loop/control/loopctl.py"],
                "lock_mode": lock_mode,
                "technical_acceptance": ["运行聚焦回归测试"],
                "evidence": ["已核对范围和依赖关系"],
            },
            ensure_ascii=False,
        )

    @staticmethod
    def schema_36() -> str:
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        schema = schema.replace("PRAGMA user_version = 30900;", "PRAGMA user_version = 30600;")
        schema = schema.replace("    'DRAFT', 'NEEDS_REVIEW', 'PENDING'", "    'DRAFT', 'PENDING'")
        schema = schema.replace(
            "  estimated_capability_level TEXT CHECK (estimated_capability_level IS NULL OR estimated_capability_level IN (\n"
            "    'L1', 'L2', 'L3', 'L4', 'L5'\n"
            "  )),\n"
            "  capability_level TEXT CHECK (capability_level IS NULL OR capability_level IN (\n"
            "    'L1', 'L2', 'L3', 'L4', 'L5'\n"
            "  )),\n",
            "  capability_level TEXT NOT NULL DEFAULT 'L2' CHECK (capability_level IN (\n"
            "    'L1', 'L2', 'L3', 'L4', 'L5'\n"
            "  )),\n",
        )
        schema = schema.replace(
            "  preflight_status TEXT NOT NULL DEFAULT 'UNINSPECTED' CHECK (preflight_status IN (\n"
            "    'UNINSPECTED', 'QUEUED', 'INSPECTING', 'READY', 'FAILED'\n"
            "  )),\n"
            "  preflight_execution_id TEXT,\n"
            "  preflight_started_at TEXT,\n"
            "  preflight_completed_at TEXT,\n"
            "  preflight_failure TEXT,\n"
            "  scope_hint_json TEXT NOT NULL DEFAULT '[]',\n"
            "  lock_mode TEXT CHECK (lock_mode IS NULL OR lock_mode IN ('file', 'module', 'project')),\n"
            "  split_suggestions_json TEXT NOT NULL DEFAULT '[]',\n",
            "",
        )
        start = schema.index("CREATE TABLE IF NOT EXISTS task_technical_acceptance")
        end = schema.index("CREATE TABLE IF NOT EXISTS task_completed_items")
        schema = schema[:start] + schema[end:]
        schema = schema.replace(
            "  execution_kind TEXT NOT NULL DEFAULT 'WORKER' CHECK (execution_kind = 'WORKER'),\n", ""
        )
        start = schema.index("CREATE TABLE IF NOT EXISTS preflight_executions")
        end = schema.index("CREATE TABLE IF NOT EXISTS scope_locks")
        schema = schema[:start] + schema[end:]
        schema = schema.replace(
            "CREATE INDEX IF NOT EXISTS idx_tasks_queue\n"
            "  ON tasks(status, preflight_status, runtime_environment, provider_id, capability_level,\n"
            "           execution_policy, priority, created_at, id);\n\n"
            "CREATE INDEX IF NOT EXISTS idx_tasks_preflight\n"
            "  ON tasks(status, preflight_status, priority, created_at, id);",
            "CREATE INDEX IF NOT EXISTS idx_tasks_queue\n"
            "  ON tasks(status, runtime_environment, provider_id, capability_level, execution_policy, priority, created_at, id);",
        )
        return schema

    @staticmethod
    def schema_35() -> str:
        schema = LoopTestCase.schema_36()
        schema = schema.replace("PRAGMA user_version = 30600;", "PRAGMA user_version = 30500;")
        schema = schema.replace("  result_diagnostic_json TEXT,\n", "")
        return schema

    @staticmethod
    def schema_34() -> str:
        schema = LoopTestCase.schema_35()
        schema = schema.replace("PRAGMA user_version = 30500;", "PRAGMA user_version = 30400;")
        schema = schema.replace(
            "  status TEXT NOT NULL CHECK (status IN (\n"
            "    'RUNNING', 'FINISHED', 'EXPIRED', 'STALLED', 'TIMED_OUT'\n"
            "  )),\n",
            "  status TEXT NOT NULL CHECK (status IN ('RUNNING', 'FINISHED', 'EXPIRED')),\n",
        )
        schema = schema.replace(
            "  termination_reason TEXT,\n"
            "  recovery_required INTEGER NOT NULL DEFAULT 0 CHECK (recovery_required IN (0, 1)),\n"
            "  recovered_at TEXT,\n"
            "  recovery_action TEXT CHECK (recovery_action IS NULL OR recovery_action IN (\n"
            "    'requeue', 'failed', 'wait'\n"
            "  )),\n",
            "",
        )
        schema = schema.replace(
            "  lease_expires_at TEXT NOT NULL,\n"
            "  status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'QUARANTINED')),\n"
            "  quarantined_at TEXT,\n"
            "  quarantine_reason TEXT,\n"
            "  CHECK (\n"
            "    (status = 'ACTIVE' AND quarantined_at IS NULL AND quarantine_reason IS NULL)\n"
            "    OR (status = 'QUARANTINED' AND quarantined_at IS NOT NULL AND length(trim(quarantine_reason)) > 0)\n"
            "  )\n",
            "  lease_expires_at TEXT NOT NULL\n",
        )
        return schema

    @staticmethod
    def schema_33() -> str:
        schema = LoopTestCase.schema_34()
        schema = schema.replace("PRAGMA user_version = 30400;", "PRAGMA user_version = 30300;")
        schema = schema.replace(
            "  capability_level TEXT NOT NULL DEFAULT 'L2' CHECK (capability_level IN (\n"
            "    'L1', 'L2', 'L3', 'L4', 'L5'\n"
            "  )),\n"
            "  runtime_environment TEXT NOT NULL CHECK (runtime_environment IN (\n"
            "    'codex_automation', 'codex_cli', 'self_hosted_agent'\n"
            "  )),\n"
            "  provider_id TEXT,\n"
            "  execution_policy TEXT NOT NULL DEFAULT 'automatic' CHECK (execution_policy IN (\n"
            "    'automatic', 'manual'\n"
            "  )),\n",
            "  execution_profile TEXT NOT NULL DEFAULT 'standard' CHECK (execution_profile IN (\n"
            "    'routine', 'standard', 'advanced', 'deep', 'complex', 'exceptional'\n"
            "  )),\n"
            "  runtime_environment TEXT NOT NULL CHECK (runtime_environment IN (\n"
            "    'codex_automation', 'codex_cli', 'deepseek'\n"
            "  )),\n",
        )
        schema = schema.replace(
            "  row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1),\n"
            "  CHECK (\n"
            "    (runtime_environment = 'self_hosted_agent' AND provider_id IS NOT NULL AND length(trim(provider_id)) > 0)\n"
            "    OR (runtime_environment <> 'self_hosted_agent' AND provider_id IS NULL)\n"
            "  )\n",
            "  row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1)\n",
        )
        schema = schema.replace(
            "  outcome TEXT,\n"
            "  runtime_environment TEXT NOT NULL CHECK (runtime_environment IN (\n"
            "    'codex_automation', 'codex_cli', 'self_hosted_agent'\n"
            "  )),\n"
            "  provider_id TEXT,\n"
            "  capability_level TEXT NOT NULL CHECK (capability_level IN ('L1', 'L2', 'L3', 'L4', 'L5')),\n"
            "  execution_policy TEXT NOT NULL CHECK (execution_policy IN ('automatic', 'manual')),\n"
            "  model TEXT NOT NULL,\n"
            "  reasoning TEXT NOT NULL CHECK (reasoning IN ('low', 'medium', 'high', 'xhigh')),\n"
            "  attempt_timeout_seconds INTEGER NOT NULL CHECK (attempt_timeout_seconds > 0),\n"
            "  max_retries INTEGER NOT NULL CHECK (max_retries >= 0),\n"
            "  CHECK (\n"
            "    (runtime_environment = 'self_hosted_agent' AND provider_id IS NOT NULL AND length(trim(provider_id)) > 0)\n"
            "    OR (runtime_environment <> 'self_hosted_agent' AND provider_id IS NULL)\n"
            "  )\n",
            "  outcome TEXT\n",
        )
        schema = schema.replace(
            "  lease_expires_at TEXT NOT NULL,\n"
            "  status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'QUARANTINED')),\n"
            "  quarantined_at TEXT,\n"
            "  quarantine_reason TEXT,\n"
            "  CHECK (\n"
            "    (status = 'ACTIVE' AND quarantined_at IS NULL AND quarantine_reason IS NULL)\n"
            "    OR (status = 'QUARANTINED' AND quarantined_at IS NOT NULL AND length(trim(quarantine_reason)) > 0)\n"
            "  )\n",
            "  lease_expires_at TEXT NOT NULL\n",
        )
        schema = schema.replace(
            "  ON tasks(status, runtime_environment, provider_id, capability_level, execution_policy, priority, created_at, id);",
            "  ON tasks(status, runtime_environment, execution_profile, priority, created_at, id);",
        )
        return schema

    @staticmethod
    def schema_32() -> str:
        schema = LoopTestCase.schema_33()
        schema = schema.replace("PRAGMA user_version = 30300;", "PRAGMA user_version = 30200;")
        schema = schema.replace(
            "  runtime_environment TEXT NOT NULL CHECK (runtime_environment IN (\n"
            "    'codex_automation', 'codex_cli', 'deepseek'\n"
            "  )),\n",
            "",
        )
        schema = schema.replace(
            "CREATE INDEX IF NOT EXISTS idx_tasks_queue\n"
            "  ON tasks(status, runtime_environment, execution_profile, priority, created_at, id);",
            "CREATE INDEX IF NOT EXISTS idx_tasks_queue\n"
            "  ON tasks(status, execution_profile, priority, created_at, id);",
        )
        return schema
