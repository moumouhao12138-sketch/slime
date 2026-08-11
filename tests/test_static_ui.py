from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


STATIC_UI_PATH = (
    Path(__file__).parents[1] / "src" / "slime_cairn" / "server" / "static" / "index.html"
)


class StaticUiTests(unittest.TestCase):
    def test_inline_javascript_parses_with_node(self) -> None:
        html = STATIC_UI_PATH.read_text(encoding="utf-8")
        scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.DOTALL)

        self.assertEqual(len(scripts), 1)
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required to validate the static UI script")

        with tempfile.TemporaryDirectory() as temporary:
            script_path = Path(temporary) / "slime-cairn-ui.js"
            script_path.write_text(scripts[0], encoding="utf-8")
            completed = subprocess.run(
                [node, "--check", str(script_path)],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_delete_project_control_requires_exact_project_name(self) -> None:
        html = STATIC_UI_PATH.read_text(encoding="utf-8")

        self.assertIn('id="delete-button"', html)
        self.assertIn('id="delete-project-confirmation"', html)
        self.assertIn('id="delete-confirm"', html)
        self.assertIn('method: "DELETE"', html)
        self.assertIn('confirmation !== project.name', html)

    def test_bulk_delete_uses_project_checkboxes_and_single_confirmation(self) -> None:
        html = STATIC_UI_PATH.read_text(encoding="utf-8")

        for control_id in (
            "project-select-all",
            "selected-project-count",
            "bulk-delete-button",
            "bulk-delete-dialog-layer",
            "bulk-delete-confirmation",
            "bulk-delete-confirm",
        ):
            self.assertIn(f'id="{control_id}"', html)
        self.assertIn('className = "project-checkbox"', html)
        self.assertIn('request("/projects/bulk-delete"', html)
        self.assertIn("project_ids: projectIds", html)
        self.assertIn("async function bulkDeleteProjects", html)

    def test_new_project_dialog_posts_cairn_style_project_input(self) -> None:
        html = STATIC_UI_PATH.read_text(encoding="utf-8")

        for control_id in (
            "new-project-button",
            "new-project-dialog-layer",
            "new-project-name",
            "new-project-target",
            "new-project-goal",
            "new-project-growth",
            "new-project-direct",
            "new-project-add-hint",
            "new-project-hint-list",
            "new-project-confirm",
        ):
            self.assertIn(f'id="{control_id}"', html)

        self.assertIn("function addNewProjectHint", html)
        self.assertIn('await request("/projects", {', html)
        self.assertIn('bootstrap_enabled: byId("new-project-direct").checked', html)
        self.assertIn('id="new-project-growth" name="new-project-start-mode" type="radio" value="growth" checked', html)
        self.assertIn("hints: newProjectHints()", html)
        self.assertIn("state.selectedProjectId = project.id", html)

    def test_timeline_labels_reason_fanout_batches(self) -> None:
        html = STATIC_UI_PATH.read_text(encoding="utf-8")

        self.assertIn('event.kind === "reason.intent_batch_created"', html)
        self.assertIn("Reason 创建 ${count} 个并行分支", html)

    def test_intent_retry_control_uses_retry_metadata_and_endpoint(self) -> None:
        html = STATIC_UI_PATH.read_text(encoding="utf-8")

        self.assertIn("retry_not_before", html)
        self.assertIn("failure_streak", html)
        self.assertIn("function retrySelectedIntent", html)
        self.assertIn('id = "retry-intent-button"', html)
        self.assertIn('/intents/${encodeURIComponent(intent.entity_id)}/retry', html)
        self.assertIn("等待重试", html)

    def test_result_first_view_groups_readable_branch_outputs(self) -> None:
        html = STATIC_UI_PATH.read_text(encoding="utf-8")

        for control_id in (
            "view-switch",
            "results-view-button",
            "graph-view-button",
            "readable-view",
            "readable-content",
            "graph-view",
            "timeline-panel",
        ):
            self.assertIn(f'id="{control_id}"', html)
        self.assertIn('viewMode: "results"', html)
        self.assertIn("function renderReadableView", html)
        self.assertIn("function renderBranch", html)
        self.assertIn('branch.group !== "attempted"', html)
        self.assertIn('branch.group === "attempted"', html)
        self.assertIn('byId("timeline-panel").classList.toggle("hidden", resultsMode)', html)
        self.assertIn('.workbench.results-mode .inspector { display: none; }', html)
        self.assertIn('document.querySelector(".workbench").classList.toggle("results-mode", resultsMode)', html)
        self.assertIn('completion ? "最终答案"', html)
        self.assertIn('byId("readable-view").scrollTop = 0', html)

    def test_benchmark_dialog_exposes_full_platform_lifecycle(self) -> None:
        html = STATIC_UI_PATH.read_text(encoding="utf-8")

        for control_id in (
            "benchmark-button",
            "benchmark-dialog-layer",
            "benchmark-status-line",
            "benchmark-automation-line",
            "benchmark-automation-start",
            "benchmark-automation-stop",
            "benchmark-list",
            "benchmark-refresh",
        ):
            self.assertIn(f'id="{control_id}"', html)
        self.assertIn("function benchmarkStart", html)
        self.assertIn("function benchmarkAutomationStart", html)
        self.assertIn("function benchmarkAutomationStop", html)
        self.assertIn('request("/benchmark/automation/start"', html)
        self.assertIn('request("/benchmark/automation/stop"', html)
        self.assertIn("window.setInterval(() => loadBenchmarkChallenges(), 3000)", html)
        self.assertIn("function benchmarkPresentationKey", html)
        self.assertIn("function captureBenchmarkUiState", html)
        self.assertIn("function restoreBenchmarkUiState", html)
        self.assertIn("nextKey !== state.benchmarkRenderKey", html)
        self.assertNotIn("state.benchmarkLoading = true;\n        renderBenchmarkChallenges();", html)
        self.assertIn("function benchmarkHint", html)
        self.assertIn("function benchmarkSubmit", html)
        self.assertIn("function benchmarkClose", html)
        self.assertIn("confirm_score_penalty: true", html)
        self.assertIn("AI 也会自动提交", html)


if __name__ == "__main__":
    unittest.main()
