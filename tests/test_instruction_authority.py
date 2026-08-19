from __future__ import annotations

# 中文排查：验证 AGENTS、README、初始化配置和角色提示词之间的权威来源关系。
# 文件移动或角色调整后本测试通常最先失败，应同步更新真实路径和文档而不是删除断言。
# 它只读取 UTF-8 文本，不检查业务运行状态。

import json
import sys
import unittest
from pathlib import Path


sys.dont_write_bytecode = True

from _bootstrap import REPOSITORY_ROOT


ROOT = REPOSITORY_ROOT


class InstructionAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.config = json.loads(
            (ROOT / "config" / "initialization.json").read_text(encoding="utf-8")
        )

    def test_all_role_prompts_are_registered_and_documented(self) -> None:
        expected = {
            "operator": "operator/operator.md",
            "planner": "planner/planner.md",
            "worker": "worker/worker.md",
        }

        self.assertEqual(self.config["prompts"], expected)
        for relative_path in expected.values():
            with self.subTest(relative_path=relative_path):
                self.assertTrue((ROOT / relative_path).is_file())
                self.assertIn(f"`{relative_path}`", self.agents)
                self.assertIn(f"`{relative_path}`", self.readme)

    def test_runtime_authorities_are_documented_and_exist(self) -> None:
        authorities = (
            "runner/agent_runtime.py",
            "planner/execution_dispatch.py",
            "control/loop_agent/providers/deepseek.py",
            "control/loopdb.py",
            "control/loopctl.py",
            "supervisor/health_run.py",
            "schemas/loop-agent.sql",
            "config/initialization.json",
        )

        for relative_path in authorities:
            with self.subTest(relative_path=relative_path):
                self.assertTrue((ROOT / relative_path).is_file())
                self.assertIn(f"`{relative_path}`", self.readme)

    def test_agents_file_stays_a_thin_stable_entrypoint(self) -> None:
        self.assertLessEqual(len(self.agents.splitlines()), 50)
        self.assertNotIn("\ufffd", self.agents)

        volatile_or_role_specific_terms = (
            "gpt-",
            "Terra",
            "Luna",
            "Sol",
            "5 分钟",
            "20 分钟",
            "最多 8",
            "preflight-claim",
            "extend-scope",
            "__pycache__",
        )
        for term in volatile_or_role_specific_terms:
            with self.subTest(term=term):
                self.assertNotIn(term, self.agents)

    def test_readme_is_human_navigation_not_role_input(self) -> None:
        self.assertLessEqual(len(self.readme.splitlines()), 140)
        self.assertIn("## 权威来源", self.readme)
        self.assertIn("不是第二份配置源", self.readme)
        self.assertIn("说明文字不得覆盖上述运行时来源", self.readme)
        self.assertIn("本文只供人工快速了解和排障", self.readme)
        self.assertNotIn("docs/architecture.md", self.readme)
        self.assertNotIn("docs/initialization.md", self.readme)
        for relative_path in ("planner/planner.md", "worker/worker.md"):
            with self.subTest(role_context=relative_path):
                text = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertNotIn("README.md", text)
                self.assertNotIn("docs\\architecture.md", text)

    def test_prompts_do_not_duplicate_deployment_values(self) -> None:
        prompt_paths = (
            "operator/operator.md",
            "planner/planner.md",
            "worker/worker.md",
        )
        prompt_text = "\n".join(
            (ROOT / relative_path).read_text(encoding="utf-8")
            for relative_path in prompt_paths
        )

        forbidden_patterns = (
            r"gpt-[0-9]",
            r"\b(?:Luna|Terra|Sol)\b",
            r"\d+\s*分钟(?:周期|轮询|运行)",
            r"(?:routine|standard|advanced|deep|complex)\s*->\s*L[1-5]",
            r"全局\s*\d+.*平台\s*\d+.*并发",
            r"当前\s*`local-agent-loop`\s*项目默认",
            r"五条.*Worker",
        )
        for pattern in forbidden_patterns:
            with self.subTest(pattern=pattern):
                self.assertNotRegex(prompt_text, pattern)

        for relative_path in (
            "operator/operator.md",
            "planner/planner.md",
            "worker/worker.md",
        ):
            with self.subTest(config_reference=relative_path):
                text = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn("config/initialization.json", text.replace("\\", "/"))

    def test_workers_record_only_safe_rejected_test_temporary_cleanup(self) -> None:
        for relative_path, actor in (("worker/worker.md", "execution"),):
            with self.subTest(role_prompt=relative_path):
                text = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn("普通测试日志或可丢弃测试临时文件", text)
                self.assertIn("宿主策略或工具权限拒绝", text)
                self.assertIn("只记录精确路径、生成命令和拒绝原因到 verification", text)
                self.assertIn("不得仅因此返回 `WAITING_HUMAN`", text)
                self.assertIn(
                    f"{actor} 前已存在、归属不明、已跟踪、源码、配置、凭据、数据库、用户数据、目录、符号链接/重解析点或可能影响其他任务的文件",
                    text,
                )
                self.assertIn("任一条件无法确认时仍不得删除并返回 `WAITING_HUMAN`", text)


if __name__ == "__main__":
    unittest.main()
