from __future__ import annotations

from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from slime_cairn.domain.models import FactCandidate
from slime_cairn.server import api
from slime_cairn.server.blackboard import Blackboard
from slime_cairn.server.writeups import (
    CATEGORY_ORDER,
    collect_team_entries,
    generate_team_writeup,
    generate_writeup,
    normalize_team_writeup,
    validate_writeup,
)
from slime_cairn.workers.writeup import _extract_text, build_prompt, queue_team_writeup


class WriteupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.board = Blackboard(Path(self.temporary.name) / "board.db")
        self.previous_board = api.board
        api.board = self.board
        self.worker_config_patch = patch.object(api, "writeup_worker_configured", return_value=False)
        self.worker_config_patch.start()
        self.client = TestClient(api.app)
        self.project = self.board.create_project(
            "writeup-fixture",
            "fixture.local",
            "solve the fixture",
            {
                "targets": ["fixture.local"],
                "agent_match": {
                    "managed": True,
                    "task_key": "fixture",
                    "exercise_id": 7,
                    "name": "fixture web",
                    "category_name": "Web",
                    "description": "Inspect the fixture",
                    "difficulty": "easy",
                    "score": 100,
                },
            },
        )

    def tearDown(self) -> None:
        self.worker_config_patch.stop()
        api.board = self.previous_board
        self.board.close()
        self.temporary.cleanup()

    def test_generate_is_evidence_backed_and_persisted_in_snapshot(self) -> None:
        self.board.register_evidence(self.project.id, "evidence://fixture", "fixture")
        fact, _ = self.board.add_fact(
            self.project.id,
            FactCandidate("fixture.local", "observed", "verified response", 1.0, ["evidence://fixture"]),
        )
        verified, _ = self.board.add_fact(
            self.project.id,
            FactCandidate("fixture.local", "agent_match_completion_verified", "fixture", 1.0, ["evidence://fixture"]),
        )
        self.board.complete_project(self.project.id, [fact.id, verified.id], "fixture solved", "fixture")
        payload = self.client.post(f"/projects/{self.project.id}/writeup/generate", json={})
        self.assertEqual(payload.status_code, 200)
        writeup = payload.json()["writeup"]
        self.assertEqual(writeup["project_id"], self.project.id)
        self.assertIn("verified response", writeup["markdown"])
        self.assertIn("关键原理与漏洞分析", writeup["markdown"])
        self.assertNotIn("Worker 活动摘要", writeup["markdown"])
        self.assertEqual(self.client.get(f"/projects/{self.project.id}/view").json()["writeup"]["id"], writeup["id"])
        team = self.client.get(f"/projects/{self.project.id}/team-writeup").json()["writeup"]
        self.assertEqual(team["validation"]["challenge_count"], 1)
        self.assertIn("fixture web", team["markdown"])
        self.assertEqual(self.client.get("/projects").json()["projects"][0]["writeup_status"], "incomplete")

    def test_generated_team_writeup_uses_template_skeleton(self) -> None:
        self.board.register_evidence(self.project.id, "evidence://fixture", "fixture")
        fact, _ = self.board.add_fact(
            self.project.id,
            FactCandidate("fixture.local", "vulnerability", "verified response", 1.0, ["evidence://fixture"]),
        )
        self.board.complete_project(self.project.id, [fact.id], "fixture solved", "fixture")
        markdown, _ = generate_team_writeup(
            collect_team_entries(self.board, "agent_match:fixture"),
            team={"name": "team", "rank": 1, "model": "fixture-model"},
        )
        self.assertIn("1. 解题过程中，关键步骤不可省略", markdown)
        self.assertNotIn("#### 题目一名称", markdown)
        positions = [markdown.index(f"### {category}") for category in CATEGORY_ORDER]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("#### fixture web", markdown)

    def test_generated_team_writeup_groups_platform_category_aliases(self) -> None:
        misc = self.board.create_project(
            "fixture misc",
            "fixture-misc.local",
            "solve misc",
            {
                "targets": ["fixture-misc.local"],
                "agent_match": {"managed": True, "task_key": "fixture", "exercise_id": 8, "name": "fixture misc", "category_name": "Misc"},
            },
        )
        pwn = self.board.create_project(
            "fixture pwn",
            "fixture-pwn.local",
            "solve pwn",
            {
                "targets": ["fixture-pwn.local"],
                "agent_match": {"managed": True, "task_key": "fixture", "exercise_id": 9, "name": "fixture pwn", "category_name": "Pwn"},
            },
        )
        for project in (self.project, misc, pwn):
            fact, _ = self.board.add_fact(
                project.id,
                FactCandidate(project.target, "observed", "verified response", 1.0, []),
            )
            self.board.complete_project(project.id, [fact.id], "fixture solved", "fixture")
        markdown, _ = generate_team_writeup(collect_team_entries(self.board, "agent_match:fixture"))
        self.assertLess(markdown.index("#### fixture web"), markdown.index("### MISC"))
        self.assertLess(markdown.index("#### fixture misc"), markdown.index("### PWN"))
        self.assertIn("#### fixture pwn", markdown)

    def test_ai_markdown_is_normalized_to_template(self) -> None:
        self.board.register_evidence(self.project.id, "evidence://fixture", "fixture")
        fact, _ = self.board.add_fact(
            self.project.id,
            FactCandidate("fixture.local", "vulnerability", "verified response", 1.0, ["evidence://fixture"]),
        )
        self.board.complete_project(self.project.id, [fact.id], "fixture solved", "fixture")
        entries = collect_team_entries(self.board, "agent_match:fixture")
        raw = """# 任意标题

## 一、团队信息

### 二、解题过程

### Web

#### 题目一名称

1. XXXXX

### MISC

#### fixture web

##### 漏洞原理

模型生成的完整技术过程。
"""
        normalized = normalize_team_writeup(raw, entries, team={"name": "team", "rank": 1, "model": "fixture-model"})
        self.assertIn("1. 解题过程中，关键步骤不可省略", normalized)
        self.assertEqual([normalized.index(f"### {category}") for category in CATEGORY_ORDER], sorted(
            normalized.index(f"### {category}") for category in CATEGORY_ORDER
        ))
        self.assertNotIn("# 任意标题", normalized)
        self.assertNotIn("#### 题目一名称", normalized)
        self.assertNotIn("1. XXXXX", normalized)
        self.assertIn("模型生成的完整技术过程", normalized)
        self.assertIn("##### 自编脚本\n\n未记录", normalized)
        self.assertIn("##### 关键步骤截图\n\n未记录", normalized)

    def test_responses_parser_ignores_reasoning_items(self) -> None:
        payload = {
            "output": [
                {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "内部推理"}]},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "# 最终正文"}],
                },
            ]
        }
        self.assertEqual(_extract_text(payload), "# 最终正文")

    def test_manual_update_validates_and_downloads_markdown(self) -> None:
        markdown = """# Fixture web

## 一、团队信息

- 名称：team
- 排名：1
- 解题数量：1
- 消耗token数：10
- 模型名称：fixture-model

### 二、解题过程

### Web

#### Fixture web

1. 观察响应并验证结果。

自编脚本

```python
print('fixture')
```

关键步骤截图

![evidence](evidence.png)

### MISC

### Crypto

### REVERSE

### PWN
"""
        response = self.client.put(f"/projects/{self.project.id}/writeup", json={"markdown": markdown})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["writeup"]["status"], "ready")
        download = self.client.get(f"/projects/{self.project.id}/writeup/download")
        self.assertEqual(download.status_code, 200)
        self.assertIn("text/markdown", download.headers.get("content-type", ""))
        self.assertIn("Fixture web", download.text)

    def test_validator_flags_missing_work_and_secrets(self) -> None:
        report = validate_writeup("# x\n## 一、团队信息\napi_key: secret-value")
        self.assertFalse(report["ready"])
        self.assertTrue(report["sensitive_content"])
        self.assertIn("### 二、解题过程", report["missing_sections"])

    def test_delete_removes_writeup_record(self) -> None:
        self.client.post(f"/projects/{self.project.id}/writeup/generate", json={})
        self.board.request_project_deletion(self.project.id)
        self.board.delete_project(self.project.id)
        count = self.board._connection.execute(
            "SELECT COUNT(*) AS count FROM project_writeups WHERE project_id = ?", (self.project.id,)
        ).fetchone()["count"]
        self.assertEqual(count, 0)

    def test_writeup_worker_prompt_contains_technical_facts_and_script(self) -> None:
        self.board.register_evidence(self.project.id, "evidence://fixture", "fixture")
        fact, _ = self.board.add_fact(
            self.project.id,
            FactCandidate(
                "fixture.local",
                "vulnerability",
                "PHP unserialize reaches passthru through a POP chain",
                1.0,
                ["evidence://fixture"],
            ),
        )
        self.board.complete_project(self.project.id, [fact.id], "flag verified by platform", "fixture")
        workspace = Path(self.temporary.name) / "workspaces" / self.project.id
        workspace.mkdir(parents=True)
        (workspace / "solve.py").write_text("print('payload')\n", encoding="utf-8")
        with patch.dict(os.environ, {"SLIME_WRITEUP_WORKSPACES_ROOT": str(workspace.parent)}):
            prompt = build_prompt(collect_team_entries(self.board, "agent_match:fixture"))
        self.assertIn("PHP unserialize reaches passthru", prompt)
        self.assertIn("solve.py", prompt)
        self.assertIn("print('payload')", prompt)
        self.assertIn("全文使用中文", prompt)

    def test_writeup_worker_persists_ai_team_markdown(self) -> None:
        self.board.register_evidence(self.project.id, "evidence://fixture", "fixture")
        fact, _ = self.board.add_fact(
            self.project.id,
            FactCandidate("fixture.local", "vulnerability", "verified response", 1.0, ["evidence://fixture"]),
        )
        self.board.complete_project(self.project.id, [fact.id], "flag verified by platform", "fixture")
        detail = "通过响应差异确认入口，构造参数触发漏洞，运行脚本获得结果，并由平台确认提交正确。"
        markdown = f"""# 比赛解题报告

## 一、团队信息

- 名称：测试队伍
- 排名：1
- 解题数量：1
- 消耗token数：10
- 模型名称：fixture-model

### 二、解题过程

### Web

#### fixture web

##### 题目说明

{detail * 4}

##### 漏洞原理

{detail * 4}

##### 解题步骤

1. {detail}
2. {detail}

##### 自编脚本

```python
print('fixture')
```

##### Flag 获取与平台验证

平台已经确认最终提交正确。

##### 关键步骤截图

![关键步骤截图](screenshots/fixture.png)
"""

        class ImmediateExecutor:
            @staticmethod
            def submit(callback, *args):
                callback(*args)
                return None

        with patch("slime_cairn.workers.writeup._EXECUTOR", ImmediateExecutor()), patch(
            "slime_cairn.workers.writeup.call_model",
            return_value=(markdown, {"model": "fixture-model", "status": 200}),
        ):
            queue_team_writeup(self.board, self.project.id, force=True)
        result = self.board.get_competition_writeup("agent_match:fixture")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["validation"]["worker_status"], "ready")
        self.assertIn("漏洞原理", result["markdown"])
        self.assertNotIn("Worker 活动", result["markdown"])


if __name__ == "__main__":
    unittest.main()
