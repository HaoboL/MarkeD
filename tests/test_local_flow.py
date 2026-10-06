"""Focused checks for the local catalog, rollout, routing, and hook contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from marked.core import (
    _topical_score,
    admit_artifact,
    choose_route,
    export_route,
    import_artifact,
    load_route,
    open_store,
    recommend,
    record_evaluation,
    review_artifact,
    set_rollout,
    submit_feedback,
)
from marked.hooks import handle_event


class LocalFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "marked.sqlite3"
        self.conn = open_store(self.db)
        self.addCleanup(self.conn.close)

    def add(self, item_id: str, version: str, kind: str, content: str, *, tags: list[str], stage: str = "code-review", agents: list[str] | None = None, harnesses: list[str] | None = None) -> None:
        folder = self.root / f"{item_id}-{version}"
        folder.mkdir()
        (folder / "content.md").write_text(content, encoding="utf-8")
        (folder / "LICENSE.txt").write_bytes((Path(__file__).resolve().parents[1] / "LICENSE").read_bytes())
        (folder / "artifact.json").write_text(json.dumps({
            "id": item_id, "version": version, "kind": kind,
            "title": item_id, "description": f"{item_id} for {stage}",
            "author": "test", "license": "MIT", "source": "test fixture",
            "content_file": "content.md", "tags": tags, "stages": [stage],
            "compatible_agents": agents or [], "harnesses": harnesses or ["codex"],
            "support_files": ["LICENSE.txt"],
        }), encoding="utf-8")
        import_artifact(self.conn, folder / "artifact.json")

    def enable(self, item_id: str, version: str, state: str = "active", percent: int = 100) -> None:
        review_artifact(self.conn, item_id, version, verdict="approve", reviewer="test-reviewer",
                        evidence_ref="test-fixture", source_checked=True,
                        license_checked=True, content_checked=True)
        admit_artifact(self.conn, item_id, version)
        set_rollout(self.conn, item_id, version, state=state, percent=percent, reason="test")

    def test_description_only_overlap_abstains(self) -> None:
        row = {
            "title": "gh-fix-ci",
            "description": "Summarize failure context for the user",
            "tags_json": "[]",
            "stages_json": "[]",
        }
        self.assertEqual(
            _topical_score("Summarize meeting notes for the team", row, "general"), 0.0
        )
        self.assertGreater(
            _topical_score("Fix a failing GitHub CI check", row, "general"), 0.0
        )

    def test_admission_route_and_task_privacy(self) -> None:
        self.add("review-agent", "1.0.0", "agent", "review guidance", tags=["code review"])
        self.add("review-skill", "1.0.0", "skill", "skill guidance", tags=["code review"], agents=["review-agent"])
        sensitive = "code review private-user-secret-2718"
        self.assertEqual(recommend(self.conn, sensitive, harness="codex")["selected"], [])
        self.enable("review-agent", "1.0.0")
        self.enable("review-skill", "1.0.0")
        decision = recommend(self.conn, sensitive, harness="codex", session_id="session-1")
        self.assertEqual([item["id"] for item in decision["selected"]], ["review-agent", "review-skill"])
        self.assertNotIn("private-user-secret-2718", json.dumps(decision))
        route_row = self.conn.execute("SELECT * FROM routes WHERE route_id=?", (decision["route_id"],)).fetchone()
        self.assertNotIn("private-user-secret-2718", json.dumps(dict(route_row)))
        self.assertFalse(decision["raw_task_stored"])
        self.assertEqual(decision["selected"][0]["source"], "test fixture")
        self.assertEqual(decision["selected"][0]["license"], "MIT")
        self.assertNotIn("content", decision["selected"][0])

    def test_canary_is_sticky_and_audited_on_replacement(self) -> None:
        for version in ("1.0.0", "2.0.0", "3.0.0"):
            self.add("review-skill", version, "skill", version, tags=["code review"])
        self.enable("review-skill", "1.0.0")
        self.enable("review-skill", "2.0.0", state="canary", percent=50)
        assignments = [
            recommend(self.conn, "code review", harness="codex", session_id=f"s-{index}")["selected"][0]["version"]
            for index in range(40)
        ]
        self.assertIn("1.0.0", assignments)
        self.assertIn("2.0.0", assignments)
        first = recommend(self.conn, "code review", harness="codex", session_id="same")["selected"][0]["version"]
        second = recommend(self.conn, "code review", harness="codex", session_id="same")["selected"][0]["version"]
        self.assertEqual(first, second)
        self.enable("review-skill", "3.0.0", state="canary", percent=25)
        old = self.conn.execute("SELECT state FROM rollouts WHERE id='review-skill' AND version='2.0.0'").fetchone()
        audit = self.conn.execute("SELECT reason FROM rollout_events WHERE id='review-skill' AND version='2.0.0' AND new_state='paused'").fetchone()
        self.assertEqual(old["state"], "paused")
        self.assertTrue(audit["reason"].startswith("superseded:"))
        set_rollout(self.conn, "review-skill", "3.0.0", state="paused", percent=0, reason="rollback")
        for index in range(40):
            choice = recommend(self.conn, "code review", harness="codex", session_id=f"after-{index}")
            self.assertEqual(choice["selected"][0]["version"], "1.0.0")

    def test_specific_rollout_precedes_wildcard_and_canary_falls_back(self) -> None:
        for version in ("2.0.0", "10.0.0", "11.0.0"):
            self.add("review-skill", version, "skill", version, tags=["code review"])
            review_artifact(self.conn, "review-skill", version, verdict="approve", reviewer="test-reviewer",
                            evidence_ref="test-fixture", source_checked=True,
                            license_checked=True, content_checked=True)
            admit_artifact(self.conn, "review-skill", version)
        set_rollout(self.conn, "review-skill", "10.0.0", state="active", reason="global")
        set_rollout(self.conn, "review-skill", "2.0.0", state="active", task_type="code-review", reason="task override")
        selected = recommend(self.conn, "code review", harness="codex", task_type="code-review")
        self.assertEqual(selected["selected"][0]["version"], "2.0.0")
        general = recommend(self.conn, "code review", harness="codex")
        self.assertEqual(general["selected"][0]["version"], "10.0.0")
        set_rollout(self.conn, "review-skill", "11.0.0", state="canary", percent=20, task_type="code-review", reason="canary")
        choices = {
            recommend(self.conn, "code review", harness="codex", task_type="code-review", session_id=f"s-{index}")["selected"][0]["version"]
            for index in range(80)
        }
        self.assertEqual(choices, {"2.0.0", "11.0.0"})

    def test_task_evidence_and_feedback_do_not_create_quality_score(self) -> None:
        self.add("review-skill", "1.0.0", "skill", "guidance", tags=["code review"])
        self.enable("review-skill", "1.0.0")
        record_evaluation(self.conn, "review-skill", "1.0.0", task_type="code-review", method="human", verdict="pass", evidence_ref="example://review-1", reviewer="reviewer")
        for task_type, expected in (("code-review", 1), ("general", 0)):
            decision = recommend(self.conn, "code review", harness="codex", task_type=task_type)
            self.assertEqual(decision["selected"][0]["evidence"]["pass"], expected)
            self.assertNotIn("quality_score", decision["selected"][0])
        decision = recommend(self.conn, "code review", harness="codex")
        submit_feedback(self.conn, decision["route_id"], "unhelpful", "preference")
        after = recommend(self.conn, "code review", harness="codex")
        self.assertEqual(decision["selected"], after["selected"])

    def test_review_gate_and_rejection_pause(self) -> None:
        self.add("review-skill", "1.0.0", "skill", "guidance", tags=["code review"])
        with self.assertRaisesRegex(ValueError, "approving source/license/full-package review"):
            admit_artifact(self.conn, "review-skill", "1.0.0")
        with self.assertRaisesRegex(ValueError, "approval requires"):
            review_artifact(self.conn, "review-skill", "1.0.0", verdict="approve",
                            reviewer="person", evidence_ref="record-1", source_checked=True)
        self.enable("review-skill", "1.0.0")
        review_artifact(self.conn, "review-skill", "1.0.0", verdict="reject",
                        reviewer="person", evidence_ref="record-2", reason="unsafe instructions")
        self.assertEqual(recommend(self.conn, "code review", harness="codex")["selected"], [])
        state = self.conn.execute("SELECT state FROM rollouts WHERE id='review-skill'").fetchone()["state"]
        admission = self.conn.execute("SELECT admission FROM artifacts WHERE id='review-skill'").fetchone()["admission"]
        self.assertEqual((state, admission), ("paused", "rejected"))
        audit = self.conn.execute("SELECT COUNT(*) FROM rollout_events WHERE reason LIKE 'rejected review:%'").fetchone()[0]
        self.assertEqual(audit, 1)

    def test_first_round_license_gate_keeps_unsupported_and_missing_text_pending(self) -> None:
        self.add("unsupported-skill", "1.0.0", "skill", "guidance", tags=["code review"])
        self.conn.execute("UPDATE artifacts SET license='CC0-1.0' WHERE id='unsupported-skill'")
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "MIT or Apache-2.0"):
            review_artifact(self.conn, "unsupported-skill", "1.0.0", verdict="approve",
                            reviewer="person", evidence_ref="record", source_checked=True,
                            license_checked=True, content_checked=True)
        self.assertEqual(self.conn.execute(
            "SELECT admission FROM artifacts WHERE id='unsupported-skill'").fetchone()[0], "pending")

        folder = self.root / "missing-license"
        folder.mkdir()
        (folder / "SKILL.md").write_text("# Test", encoding="utf-8")
        (folder / "artifact.json").write_text(json.dumps({
            "id": "missing-license", "version": "1.0.0", "kind": "skill",
            "title": "Missing license", "description": "test", "author": "fixture",
            "license": "MIT", "source": "fixture", "content_file": "SKILL.md",
        }), encoding="utf-8")
        import_artifact(self.conn, folder / "artifact.json")
        with self.assertRaisesRegex(ValueError, "root LICENSE"):
            review_artifact(self.conn, "missing-license", "1.0.0", verdict="approve",
                            reviewer="person", evidence_ref="record", source_checked=True,
                            license_checked=True, content_checked=True)

    def test_apache_requires_recorded_notice_check_before_admission(self) -> None:
        folder = self.root / "apache-skill"
        folder.mkdir()
        (folder / "SKILL.md").write_text("# Test", encoding="utf-8")
        (folder / "LICENSE.txt").write_text(
            "Apache License\nVersion 2.0, January 2004\nEND OF TERMS AND CONDITIONS\n",
            encoding="utf-8",
        )
        (folder / "NOTICE.txt").write_text("Copyright fixture author\n", encoding="utf-8")
        (folder / "artifact.json").write_text(json.dumps({
            "id": "apache-skill", "version": "1.0.0", "kind": "skill",
            "title": "Apache test", "description": "test", "author": "fixture",
            "license": "Apache-2.0", "source": "fixture", "content_file": "SKILL.md",
            "support_files": ["LICENSE.txt", "NOTICE.txt"],
        }), encoding="utf-8")
        import_artifact(self.conn, folder / "artifact.json")
        with self.assertRaisesRegex(ValueError, "NOTICE check"):
            review_artifact(self.conn, "apache-skill", "1.0.0", verdict="approve",
                            reviewer="person", evidence_ref="record", source_checked=True,
                            license_checked=True, content_checked=True)
        review_artifact(self.conn, "apache-skill", "1.0.0", verdict="approve",
                        reviewer="person", evidence_ref="record", source_checked=True,
                        license_checked=True, content_checked=True, notice_checked=True)
        admit_artifact(self.conn, "apache-skill", "1.0.0")
        self.assertEqual(self.conn.execute(
            "SELECT admission FROM artifacts WHERE id='apache-skill'").fetchone()[0], "admitted")

    def test_schema_migration_revokes_undocumented_legacy_admission(self) -> None:
        self.add("review-skill", "1.0.0", "skill", "guidance", tags=["code review"])
        self.enable("review-skill", "1.0.0")
        self.conn.execute("UPDATE settings SET value='3' WHERE key='schema_version'")
        self.conn.commit()
        self.conn.close()
        reopened = open_store(self.db)
        self.addCleanup(reopened.close)
        artifact = reopened.execute("SELECT admission FROM artifacts WHERE id='review-skill'").fetchone()
        rollout = reopened.execute("SELECT state FROM rollouts WHERE id='review-skill'").fetchone()
        self.assertEqual((artifact["admission"], rollout["state"]), ("pending", "paused"))
        event = reopened.execute("SELECT reason FROM rollout_events WHERE reason LIKE 'schema-v4 migration:%'").fetchone()
        self.assertIsNotNone(event)

    def test_schema_v8_pauses_previously_admitted_cc0_record(self) -> None:
        self.add("legacy-skill", "1.0.0", "skill", "guidance", tags=["code review"])
        self.enable("legacy-skill", "1.0.0")
        self.conn.execute("UPDATE artifacts SET license='CC0-1.0' WHERE id='legacy-skill'")
        self.conn.execute("UPDATE settings SET value='7' WHERE key='schema_version'")
        self.conn.commit()
        self.conn.close()
        reopened = open_store(self.db)
        self.addCleanup(reopened.close)
        row = reopened.execute("SELECT admission FROM artifacts WHERE id='legacy-skill'").fetchone()
        rollout = reopened.execute("SELECT state FROM rollouts WHERE id='legacy-skill'").fetchone()
        self.assertEqual((row["admission"], rollout["state"]), ("pending", "paused"))
        event = reopened.execute("SELECT reason FROM rollout_events WHERE id='legacy-skill' ORDER BY rowid DESC LIMIT 1").fetchone()
        self.assertIn("schema-v8 migration", event["reason"])

    def test_preview_choice_switch_load_and_rollback_guard(self) -> None:
        self.add("review-agent", "1.0.0", "agent", "agent instructions", tags=["code review"])
        self.add("review-skill", "1.0.0", "skill", "first instructions", tags=["code review"], agents=["review-agent"])
        self.add("review-skill-alt", "1.0.0", "skill", "alternative instructions", tags=["code review"], agents=["review-agent"])
        for item_id in ("review-agent", "review-skill", "review-skill-alt"):
            self.enable(item_id, "1.0.0")
        decision = recommend(self.conn, "code review this change", harness="codex", session_id="s1")
        with self.assertRaisesRegex(ValueError, "not been accepted"):
            load_route(self.conn, decision["route_id"])
        switched = choose_route(self.conn, decision["route_id"], "switch", artifact_id="review-skill-alt", version="1.0.0")
        self.assertEqual(switched["chosen"][-1]["id"], "review-skill-alt")
        loaded = load_route(self.conn, decision["route_id"])
        self.assertIn("alternative instructions", loaded["loaded"][-1]["content"])
        self.assertNotIn("first instructions", json.dumps(loaded))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM route_loads").fetchone()[0], 1)
        choose_route(self.conn, decision["route_id"], "reject")
        with self.assertRaisesRegex(ValueError, "rejected"):
            load_route(self.conn, decision["route_id"])
        another = recommend(self.conn, "code review this change", harness="codex", session_id="s1")
        choose_route(self.conn, another["route_id"], "accept")
        picked = next(x for x in another["selected"] if x["kind"] == "skill")
        set_rollout(self.conn, picked["id"], picked["version"], state="paused", percent=0, reason="rollback")
        with self.assertRaisesRegex(ValueError, "no longer rollout-eligible"):
            load_route(self.conn, another["route_id"])

    def test_hook_preview_and_explicit_auto_load(self) -> None:
        self.add("review-skill", "1.0.0", "skill", "focused review steps", tags=["code review"])
        self.enable("review-skill", "1.0.0")
        event = handle_event({"prompt": "code review this change", "turn_id": "t1"}, "codex", self.db)
        context = event["hookSpecificOutput"]["additionalContext"]
        self.assertIn("recommendation preview", context)
        self.assertIn("claimed license", context)
        self.assertNotIn("focused review steps", context)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM route_loads").fetchone()[0], 0)
        from unittest.mock import patch
        with patch.dict("os.environ", {"MARKED_AUTO_LOAD": "1"}):
            automatic = handle_event({"prompt": "code review this change", "turn_id": "t2"}, "codex", self.db)
        loaded_context = automatic["hookSpecificOutput"]["additionalContext"]
        self.assertIn("focused review steps", loaded_context)
        self.assertIn("lower-trust", loaded_context)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM route_loads").fetchone()[0], 1)
        self.assertEqual(handle_event({"prompt": "unrelated weather"}, "codex", self.db), {})

    def test_shared_plugin_hook_entrypoint_for_two_hosts(self) -> None:
        self.add("review-skill", "1.0.0", "skill", "private instructions", tags=["code review"], harnesses=["codex", "claude-code"])
        self.enable("review-skill", "1.0.0")
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / ".claude-plugin/plugin.json").read_text())
        hooks = json.loads((root / "hooks/hooks.json").read_text())
        self.assertEqual(manifest["name"], "marked")
        self.assertIn("CLAUDE_PLUGIN_ROOT", hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"])
        for host in ("codex", "claude-code"):
            env = dict(os.environ)
            env.pop("PLUGIN_ROOT", None)
            env.pop("MARKED_AUTO_LOAD", None)
            env["MARKED_DB"] = str(self.db)
            env["CLAUDE_PLUGIN_ROOT"] = str(root)
            if host == "codex":
                env["PLUGIN_ROOT"] = str(root)
            result = subprocess.run(
                [sys.executable, str(root / "adapters/universal_hook.py")],
                input=json.dumps({"prompt": "code review this change", "turn_id": host}),
                text=True, capture_output=True, env=env, check=True,
            )
            preview = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("recommendation preview", preview)
            self.assertNotIn("private instructions", preview)
            route = self.conn.execute("SELECT harness FROM routes ORDER BY rowid DESC LIMIT 1").fetchone()
            self.assertEqual(route["harness"], host)


    def _multi_file_skill(self, item_id: str = "package-skill") -> Path:
        folder = self.root / item_id
        folder.mkdir()
        (folder / "SKILL.md").write_text("# Code review\nUse supporting files.", encoding="utf-8")
        (folder / "scripts").mkdir()
        (folder / "scripts/check.py").write_bytes(b"print('checked')\n")
        (folder / "references").mkdir()
        (folder / "references/guide.txt").write_bytes(b"review guide\n")
        (folder / "LICENSE.txt").write_bytes((Path(__file__).resolve().parents[1] / "LICENSE").read_bytes())
        manifest = folder / "artifact.json"
        manifest.write_text(json.dumps({
            "id": item_id, "version": "1.0.0", "kind": "skill",
            "title": "code review package", "description": "code review with resources",
            "author": "test", "license": "MIT", "source": "test fixture",
            "content_file": "SKILL.md", "tags": ["code review"],
            "stages": ["code-review"], "harnesses": ["codex"],
            "support_files": ["scripts/check.py", "references/guide.txt", "LICENSE.txt"],
        }), encoding="utf-8")
        return manifest

    def test_multifile_package_export_is_complete_and_never_auto_loaded(self) -> None:
        manifest = self._multi_file_skill()
        imported = import_artifact(self.conn, manifest)
        self.assertEqual(imported["support_files"], 3)
        self.enable("package-skill", "1.0.0")
        with patch.dict("os.environ", {"MARKED_AUTO_LOAD": "1"}):
            preview = handle_event({"prompt": "code review this change", "turn_id": "package"}, "codex", self.db)
        context = preview["hookSpecificOutput"]["additionalContext"]
        self.assertIn("support files 3", context)
        self.assertIn(" export ", context)
        self.assertNotIn("Use supporting files.", context)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM route_loads").fetchone()[0], 0)
        decision = recommend(self.conn, "code review this change", harness="codex")
        route_id = decision["route_id"]
        with self.assertRaisesRegex(ValueError, "not been accepted"):
            export_route(self.conn, route_id, self.root / "unaccepted")
        choose_route(self.conn, route_id, "accept")
        destination = self.root / "exported"
        result = export_route(self.conn, route_id, destination)
        folder = destination / "package-skill@1.0.0"
        self.assertEqual((folder / "SKILL.md").read_text(), "# Code review\nUse supporting files.")
        self.assertEqual((folder / "scripts/check.py").read_bytes(), b"print('checked')\n")
        self.assertEqual((folder / "references/guide.txt").read_bytes(), b"review guide\n")
        self.assertTrue((folder / "LICENSE.txt").is_file())
        self.assertFalse(result["executed"])
        self.assertFalse(result["installed"])
        self.assertEqual(json.loads((destination / "MARKED_EXPORT.json").read_text())["packages"][0]["package_sha256"], imported["package_sha256"])
        with self.assertRaisesRegex(ValueError, "already exists"):
            export_route(self.conn, route_id, destination)
        cli_destination = self.root / "cli-export"
        result_cli = subprocess.run(
            [sys.executable, "-m", "marked", "--db", str(self.db),
             "export", route_id, str(cli_destination)],
            capture_output=True, text=True, check=True,
        )
        self.assertFalse(json.loads(result_cli.stdout)["executed"])
        self.assertEqual((cli_destination / "package-skill@1.0.0" / "scripts/check.py").read_bytes(), b"print('checked')\n")

    def test_package_rejects_traversal_symlink_and_main_collision(self) -> None:
        manifest = self._multi_file_skill()
        data = json.loads(manifest.read_text())
        for unsafe in ("../outside.txt", "/tmp/outside.txt", "SKILL.md/subfile"):
            data["support_files"] = [unsafe]
            manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises((ValueError, FileNotFoundError)):
                import_artifact(self.conn, manifest)
        target = self.root / "outside.txt"
        target.write_text("outside", encoding="utf-8")
        link = manifest.parent / "linked.txt"
        link.symlink_to(target)
        data["support_files"] = ["linked.txt"]
        manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "symlinks"):
            import_artifact(self.conn, manifest)

    def test_package_tampering_blocks_load_and_v4_single_file_backfills(self) -> None:
        manifest = self._multi_file_skill()
        import_artifact(self.conn, manifest)
        self.enable("package-skill", "1.0.0")
        decision = recommend(self.conn, "code review this change", harness="codex")
        choose_route(self.conn, decision["route_id"], "accept")
        self.conn.execute("UPDATE artifact_files SET content=? WHERE id=? AND path=?", (b"changed", "package-skill", "scripts/check.py"))
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "integrity check"):
            load_route(self.conn, decision["route_id"])
        self.add("single-skill", "1.0.0", "skill", "single", tags=["code review"])
        self.enable("single-skill", "1.0.0")
        self.conn.execute("UPDATE settings SET value='4' WHERE key='schema_version'")
        self.conn.execute("UPDATE artifacts SET package_sha256='' WHERE id='single-skill'")
        self.conn.execute("UPDATE artifact_reviews SET package_sha256='' WHERE id='single-skill'")
        self.conn.commit()
        self.conn.close()
        reopened = open_store(self.db)
        self.addCleanup(reopened.close)
        row = reopened.execute("SELECT content_sha256,package_sha256,admission FROM artifacts WHERE id='single-skill'").fetchone()
        self.assertEqual(row["content_sha256"], row["package_sha256"])
        self.assertEqual(row["admission"], "admitted")


if __name__ == "__main__":
    unittest.main()
