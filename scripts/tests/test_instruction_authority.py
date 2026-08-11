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
            "operator": "prompts/operator.md",
            "planner": "prompts/planner.md",
            "worker": "prompts/worker.md",
            "cli_worker": "prompts/cli-worker.md",
        }

        self.assertEqual(self.config["prompts"], expected)
        for relative_path in expected.values():
            with self.subTest(relative_path=relative_path):
                self.assertTrue((ROOT / relative_path).is_file())
                self.assertIn(f"`{relative_path}`", self.agents)
                self.assertIn(f"`{relative_path}`", self.readme)

        self.assertEqual(self.config["codex_cli"]["prompt"], expected["cli_worker"])

    def test_runtime_authorities_are_documented_and_exist(self) -> None:
        authorities = (
            "scripts/roles/runner/codex_cli_runner.py",
            "scripts/roles/runner/agent_runtime.py",
            "scripts/loop_agent/providers/deepseek.py",
            "scripts/loopdb.py",
            "scripts/loopctl.py",
            "scripts/roles/supervisor/health_run.py",
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

    def test_authority_matrix_is_explicitly_non_configurational(self) -> None:
        self.assertIn("## 权威来源", self.readme)
        self.assertIn("不是第二份配置源", self.readme)
        self.assertIn("说明文字不得覆盖上述运行时来源", self.readme)

    def test_prompts_do_not_duplicate_deployment_values(self) -> None:
        prompt_paths = (
            "prompts/operator.md",
            "prompts/planner.md",
            "prompts/worker.md",
            "prompts/cli-worker.md",
            "prompts/README.md",
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
            "prompts/operator.md",
            "prompts/worker.md",
            "prompts/README.md",
        ):
            with self.subTest(config_reference=relative_path):
                text = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn("config/initialization.json", text.replace("\\", "/"))


if __name__ == "__main__":
    unittest.main()
