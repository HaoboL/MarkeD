"""Synthetic end-to-end checks for the approved opt-in feedback pilot."""

from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from marked.core import (
    admit_artifact, choose_route, import_artifact, load_route, open_store, recommend,
    review_artifact, set_rollout,
)
from marked.dialogue import (
    classify_followup, combination_feedback_summary, consent_status,
    correct_feedback, delete_feedback, feedback_summary,
    list_feedback, list_feedback_audit,
)
from marked.hooks import handle_event
from marked.cli import run


class DialogueFeedbackTests(unittest.TestCase):
    def test_conservative_signal_rules(self) -> None:
        self.assertEqual(classify_followup("这个不合适，换一个")[0], "negative")
        self.assertEqual(classify_followup("你刚才的回答错了")[0], "correction")
        self.assertEqual(classify_followup("继续用刚才的技能")[0], "reuse")
        self.assertIsNone(classify_followup("明天下雨吗"))
        self.assertIsNone(classify_followup("如何实现没有帮助反馈按钮"))

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "marked.sqlite3"
        self.conn = open_store(self.db)
        self.addCleanup(self.conn.close)
        artifact = self.root / "artifact"
        artifact.mkdir()
        (artifact / "SKILL.md").write_text("Review the code carefully.", encoding="utf-8")
        (artifact / "LICENSE.txt").write_bytes((Path(__file__).resolve().parents[1] / "LICENSE").read_bytes())
        (artifact / "artifact.json").write_text(json.dumps({
            "id": "review-skill", "version": "1.0.0", "kind": "skill",
            "title": "review-skill", "description": "Code review help",
            "author": "fixture", "license": "MIT", "source": "test fixture",
            "content_file": "SKILL.md", "support_files": ["LICENSE.txt"],
            "tags": ["code review"],
            "stages": ["code-review"], "harnesses": ["codex"],
        }), encoding="utf-8")
        import_artifact(self.conn, artifact / "artifact.json")
        review_artifact(self.conn, "review-skill", "1.0.0", verdict="approve",
                        reviewer="fixture", evidence_ref="synthetic", source_checked=True,
                        license_checked=True, content_checked=True)
        admit_artifact(self.conn, "review-skill", "1.0.0")
        set_rollout(self.conn, "review-skill", "1.0.0", state="active", reason="synthetic")

    def prompt(self, text: str, turn: str, session: str = "session-A") -> dict:
        return handle_event({"hook_event_name": "UserPromptSubmit", "prompt": text,
                             "turn_id": turn, "session_id": session}, "codex", self.db)

    def stop(self, turn: str, session: str = "session-A") -> dict:
        return handle_event({"hook_event_name": "Stop", "turn_id": turn,
                             "session_id": session,
                             "last_assistant_message": "PRIVATE-ASSISTANT-RESULT"},
                            "codex", self.db)

    def latest_route(self) -> str:
        return self.conn.execute("SELECT route_id FROM routes ORDER BY rowid DESC LIMIT 1").fetchone()[0]

    def test_default_off_then_opt_in_loaded_positive_and_privacy(self) -> None:
        self.prompt("code review my PRIVATE-TASK-2718", "t0")
        self.assertEqual(self.stop("t0"), {})
        self.prompt("这个很有帮助", "t1")
        self.assertEqual(list_feedback(self.conn), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM hook_turns").fetchone()[0], 0)

        self.prompt("marked feedback on", "t2")
        self.assertTrue(consent_status(self.conn, "session-A"))
        self.prompt("code review the change PRIVATE-TASK-2718", "t3")
        route_id = self.latest_route()
        choose_route(self.conn, route_id, "accept")
        load_route(self.conn, route_id)
        self.assertEqual(self.stop("t3"), {})
        self.prompt("这个很有帮助，PRIVATE-FOLLOWUP-2819", "t4")
        events = list_feedback(self.conn)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["signal"], "positive")
        self.assertLessEqual(len(events[0]["excerpt"]), 32)
        self.assertNotIn("PRIVATE-FOLLOWUP", json.dumps(events))
        self.assertNotIn("PRIVATE-TASK", json.dumps([dict(row) for row in self.conn.execute("SELECT * FROM routes")]))
        db_text = self.db.read_bytes().decode("utf-8", errors="ignore")
        self.assertNotIn("PRIVATE-ASSISTANT-RESULT", db_text)
        self.assertNotIn("PRIVATE-FOLLOWUP-2819", db_text)
        self.assertEqual(feedback_summary(self.conn)[0]["positive"], 1)

        corrected = correct_feedback(self.conn, events[0]["event_id"], "negative")
        self.assertEqual(corrected["old_signal"], "positive")
        self.assertEqual(list_feedback_audit(self.conn, events[0]["event_id"])[0]["new_signal"], "negative")
        self.assertEqual(feedback_summary(self.conn)[0]["negative"], 1)
        self.assertTrue(delete_feedback(self.conn, events[0]["event_id"])["deleted"])
        self.assertEqual(list_feedback(self.conn), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM natural_feedback_audit").fetchone()[0], 0)

    def test_requires_same_session_finished_and_loaded_once(self) -> None:
        self.prompt("marked feedback on", "enable")
        self.prompt("code review this change", "t1")
        route_id = self.latest_route()
        self.stop("t1")
        self.prompt("这个没有帮助", "other", session="session-B")
        self.assertEqual(list_feedback(self.conn), [])
        self.prompt("这个没有帮助", "t2")
        self.assertEqual(list_feedback(self.conn), [])
        self.assertEqual(feedback_summary(self.conn)[0]["loaded"], 0)
        choose_route(self.conn, route_id, "accept")
        load_route(self.conn, route_id)
        self.prompt("这个没有帮助", "t2")
        self.assertEqual(list_feedback(self.conn), [])

    def test_switch_is_distinct_and_ambiguous_remains_unjudged(self) -> None:
        self.prompt("marked feedback on", "enable")
        self.prompt("code review this change", "t1")
        route_id = self.latest_route()
        choose_route(self.conn, route_id, "accept")
        load_route(self.conn, route_id)
        self.stop("t1")
        self.prompt("换一个", "t2")
        self.prompt("换一个", "t2")
        self.assertEqual([row["signal"] for row in list_feedback(self.conn)], ["switch"])
        self.assertEqual(feedback_summary(self.conn)[0]["negative"], 0)

        self.prompt("code review another change", "t3")
        next_route = self.latest_route()
        choose_route(self.conn, next_route, "accept")
        load_route(self.conn, next_route)
        self.stop("t3")
        self.prompt("明天下雨吗", "t4")
        self.assertEqual(len(list_feedback(self.conn)), 1)
        self.assertEqual(feedback_summary(self.conn)[0]["unjudged"], 1)

    def test_explicit_off_stops_future_feedback(self) -> None:
        self.prompt("marked feedback on", "enable")
        self.prompt("code review this change", "t1")
        route_id = self.latest_route()
        choose_route(self.conn, route_id, "accept")
        load_route(self.conn, route_id)
        self.stop("t1")
        self.prompt("marked feedback off", "off")
        self.assertFalse(consent_status(self.conn, "session-A"))
        self.prompt("这个没有帮助", "t2")
        self.assertEqual(list_feedback(self.conn), [])

    def test_user_command_loads_only_same_session_and_links_feedback(self) -> None:
        self.prompt("marked feedback on", "enable")
        self.prompt("code review this change", "task")
        route_id = self.latest_route()
        self.stop("task")
        denied = self.prompt(f"marked use {route_id}", "foreign", session="session-B")
        self.assertIn("current host session", denied["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM route_loads").fetchone()[0], 0)
        accepted = self.prompt(f"marked use {route_id}", "use")
        self.assertIn("Review the code carefully.", accepted["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM route_loads").fetchone()[0], 1)
        self.assertIsNotNone(self.conn.execute(
            "SELECT feedback_checked_at FROM hook_turns WHERE turn_id='task'").fetchone()[0])
        self.stop("use")
        self.prompt("这个很有帮助", "reaction")
        self.assertEqual(list_feedback(self.conn)[0]["signal"], "positive")

    def test_user_command_exports_full_support_package_without_execution(self) -> None:
        folder = self.root / "threat-model"
        (folder / "references").mkdir(parents=True)
        (folder / "SKILL.md").write_text("Use references/checklist.md for a threat model.", encoding="utf-8")
        (folder / "references/checklist.md").write_text("Check trust boundaries.", encoding="utf-8")
        (folder / "LICENSE.txt").write_bytes((Path(__file__).resolve().parents[1] / "LICENSE").read_bytes())
        (folder / "artifact.json").write_text(json.dumps({
            "id": "threat-model-skill", "version": "1.0.0", "kind": "skill",
            "title": "threat-model-skill", "description": "Threat model a codebase",
            "author": "fixture", "license": "MIT", "source": "fixture",
            "content_file": "SKILL.md", "support_files": ["references/checklist.md", "LICENSE.txt"],
            "tags": ["threat model"], "stages": ["security-review"], "harnesses": ["codex"],
        }), encoding="utf-8")
        import_artifact(self.conn, folder / "artifact.json")
        review_artifact(self.conn, "threat-model-skill", "1.0.0", verdict="approve",
                        reviewer="fixture", evidence_ref="synthetic", source_checked=True,
                        license_checked=True, content_checked=True)
        admit_artifact(self.conn, "threat-model-skill", "1.0.0")
        set_rollout(self.conn, "threat-model-skill", "1.0.0", state="active", reason="synthetic")
        self.prompt("marked feedback on", "enable")
        preview = self.prompt("threat model this codebase", "task")
        self.assertIn("threat-model-skill", preview["hookSpecificOutput"]["additionalContext"])
        route_id = self.latest_route()
        self.stop("task")
        accepted = self.prompt(f"marked use {route_id}", "use")
        context = accepted["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Use references/checklist.md", context)
        self.assertIn("Complete accepted packages were exported", context)
        exported = list((self.root / "exports").glob("*/threat-model-skill@1.0.0/references/checklist.md"))
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0].read_text(), "Check trust boundaries.")
        self.stop("use")
        self.prompt("这个很有帮助", "reaction")
        self.assertEqual(list_feedback(self.conn)[0]["signal"], "positive")

    def test_user_can_switch_or_reject_preview_without_implicit_load(self) -> None:
        folder = self.root / "alternative"
        folder.mkdir()
        (folder / "SKILL.md").write_text("Alternative review guidance.", encoding="utf-8")
        (folder / "LICENSE.txt").write_bytes((Path(__file__).resolve().parents[1] / "LICENSE").read_bytes())
        (folder / "artifact.json").write_text(json.dumps({
            "id": "review-alternative", "version": "1.0.0", "kind": "skill",
            "title": "review-alternative", "description": "Code review alternative",
            "author": "fixture", "license": "MIT", "source": "fixture",
            "content_file": "SKILL.md", "support_files": ["LICENSE.txt"],
            "tags": ["code review"],
            "stages": ["code-review"], "harnesses": ["codex"],
        }), encoding="utf-8")
        import_artifact(self.conn, folder / "artifact.json")
        review_artifact(self.conn, "review-alternative", "1.0.0", verdict="approve",
                        reviewer="fixture", evidence_ref="synthetic", source_checked=True,
                        license_checked=True, content_checked=True)
        admit_artifact(self.conn, "review-alternative", "1.0.0")
        set_rollout(self.conn, "review-alternative", "1.0.0", state="active", reason="synthetic")
        self.prompt("code review this change", "task")
        route = self.conn.execute("SELECT * FROM routes ORDER BY rowid DESC LIMIT 1").fetchone()
        self.stop("task")
        candidates = json.loads(route["candidates_json"])
        selected = json.loads(route["selected_json"])
        alternative = next(item for item in candidates if item["id"] != selected[0]["id"])
        switched = self.prompt(
            f"marked switch {route['route_id']} {alternative['id']} {alternative['version']}", "switch")
        self.assertIn(alternative["id"], switched["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM route_loads").fetchone()[0], 1)
        self.prompt("code review another change", "task2")
        rejected_id = self.latest_route()
        self.stop("task2")
        rejected = self.prompt(f"marked reject {rejected_id}", "reject")
        self.assertIn("rejected", rejected["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM route_loads").fetchone()[0], 1)

        alternative_row = next(row for row in combination_feedback_summary(self.conn)
                               if any(item["id"] == alternative["id"] for item in row["configuration"]))
        self.assertEqual((alternative_row["loaded"], alternative_row["not_observed"]), (1, 1))

        self.prompt("marked feedback on", "enable")
        self.prompt("code review a third change", "task3")
        ambiguous_route = self.latest_route()
        shown = json.loads(self.conn.execute(
            "SELECT candidates_json FROM routes WHERE route_id=?", (ambiguous_route,)
        ).fetchone()[0])
        initial = json.loads(self.conn.execute(
            "SELECT selected_json FROM routes WHERE route_id=?", (ambiguous_route,)
        ).fetchone()[0])
        other = next(item for item in shown if item["id"] != initial[0]["id"])
        choose_route(self.conn, ambiguous_route, "accept")
        load_route(self.conn, ambiguous_route)
        choose_route(self.conn, ambiguous_route, "switch", artifact_id=other["id"],
                     version=other["version"])
        load_route(self.conn, ambiguous_route)
        self.stop("task3")
        self.prompt("这个很有帮助", "reaction")
        self.assertEqual(list_feedback(self.conn), [])
        rows = combination_feedback_summary(self.conn)
        self.assertEqual(sum(row["ambiguous_attribution"] for row in rows), 2)
        self.assertEqual(sum(row["positive"] for row in rows), 0)

    def test_local_cli_controls_and_duplicate_route_are_safe(self) -> None:
        self.prompt("marked feedback on", "enable")
        self.prompt("code review this change", "t1")
        self.prompt("code review this change", "t1")
        route_id = self.latest_route()
        choose_route(self.conn, route_id, "accept")
        load_route(self.conn, route_id)
        self.stop("t1")
        self.prompt("这个没有帮助", "t2")
        self.assertEqual(list_feedback(self.conn), [])

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(run(["--db", str(self.db), "feedback-events"]), 0)
        self.assertEqual(json.loads(output.getvalue()), [])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(run(["--db", str(self.db), "feedback-clear"]), 0)
        self.assertEqual(json.loads(output.getvalue())["deleted_events"], 0)
        self.assertFalse(consent_status(self.conn, "session-A"))

    def test_combination_counts_separate_task_type_and_missing_followup(self) -> None:
        agent = self.root / "agent"
        agent.mkdir()
        (agent / "agent.md").write_text("Review code as an agent.", encoding="utf-8")
        (agent / "LICENSE.txt").write_bytes((Path(__file__).resolve().parents[1] / "LICENSE").read_bytes())
        (agent / "artifact.json").write_text(json.dumps({
            "id": "review-agent", "version": "1.0.0", "kind": "agent",
            "title": "review-agent", "description": "Code review agent",
            "author": "fixture", "license": "MIT", "source": "fixture",
            "content_file": "agent.md", "support_files": ["LICENSE.txt"],
            "tags": ["code review"], "harnesses": ["codex"],
        }), encoding="utf-8")
        import_artifact(self.conn, agent / "artifact.json")
        review_artifact(self.conn, "review-agent", "1.0.0", verdict="approve",
                        reviewer="fixture", evidence_ref="synthetic", source_checked=True,
                        license_checked=True, content_checked=True)
        admit_artifact(self.conn, "review-agent", "1.0.0")
        set_rollout(self.conn, "review-agent", "1.0.0", state="active", reason="synthetic")

        self.prompt("marked feedback on", "enable")
        self.prompt("code review this change", "t1")
        first = self.latest_route()
        choose_route(self.conn, first, "accept")
        load_route(self.conn, first)
        self.stop("t1")
        self.prompt("这个很有帮助", "t2")

        second = recommend(self.conn, "code review another change", harness="codex",
                           task_type="general", session_id="session-B")["route_id"]
        choose_route(self.conn, second, "accept")
        load_route(self.conn, second)
        third = recommend(self.conn, "code review a draft", harness="codex",
                          task_type="academic-editing", session_id="session-C")["route_id"]
        choose_route(self.conn, third, "accept")
        load_route(self.conn, third)

        rows = combination_feedback_summary(self.conn)
        code = next(row for row in rows if row["task_type"] == "general")
        academic = next(row for row in rows if row["task_type"] == "academic-editing")
        self.assertEqual([(item["kind"], item["id"]) for item in code["configuration"]],
                         [("agent", "review-agent"), ("skill", "review-skill")])
        self.assertEqual((code["exposures"], code["accepted"], code["loaded"]), (2, 2, 2))
        self.assertEqual((code["positive"], code["not_observed"], code["unjudged"]), (1, 1, 0))
        self.assertEqual((code["distinct_loaded_sessions"], code["observed_followups"]), (2, 1))
        self.assertEqual((academic["exposures"], academic["loaded"], academic["not_observed"]), (1, 1, 1))

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(run(["--db", str(self.db), "feedback-combinations"]), 0)
        self.assertEqual(json.loads(output.getvalue()), rows)
        event = list_feedback(self.conn)[0]
        correct_feedback(self.conn, event["event_id"], "negative")
        corrected = next(row for row in combination_feedback_summary(self.conn)
                         if row["task_type"] == "general")
        self.assertEqual((corrected["positive"], corrected["negative"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
