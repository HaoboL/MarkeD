"""Command-line access to the local MarkeD product slice."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .core import (
    admit_artifact,
    award_points,
    choose_route,
    export_route,
    import_artifact,
    list_artifacts,
    load_route,
    local_report,
    open_store,
    recommend,
    record_evaluation,
    review_artifact,
    set_rollout,
    submit_feedback,
)
from .dialogue import (
    consent_status, correct_feedback, delete_all_feedback, delete_feedback,
    combination_feedback_summary, delete_session_feedback, feedback_summary,
    list_feedback, list_feedback_audit,
    set_consent,
)


DEFAULT_DB = Path(__file__).resolve().parents[1] / ".marked" / "marked.sqlite3"


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marked")
    parser.add_argument("--db", default=os.environ.get("MARKED_DB", str(DEFAULT_DB)))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")

    add = sub.add_parser("import", help="import a contributor manifest as pending")
    add.add_argument("manifest")
    review = sub.add_parser("review", help="record a manual source/license/full-package review")
    review.add_argument("id")
    review.add_argument("version")
    review.add_argument("verdict", choices=("approve", "reject"))
    review.add_argument("--reviewer", required=True)
    review.add_argument("--evidence-ref", required=True)
    review.add_argument("--source-checked", action="store_true")
    review.add_argument("--license-checked", action="store_true")
    review.add_argument("--notice-checked", action="store_true",
                        help="attest upstream Apache NOTICE presence/absence and preserved notices")
    review.add_argument("--content-checked", action="store_true")
    review.add_argument("--reason", default="")
    admit = sub.add_parser("admit", help="admit only after an approving documented review")
    admit.add_argument("id")
    admit.add_argument("version")
    sub.add_parser("list")

    roll = sub.add_parser("rollout", help="set a version's distribution state")
    roll.add_argument("id")
    roll.add_argument("version")
    roll.add_argument("state", choices=("active", "canary", "paused"))
    roll.add_argument("--task-type", default="*")
    roll.add_argument("--harness", default="*")
    roll.add_argument("--percent", type=int)
    roll.add_argument("--reason", required=True)
    rollback = sub.add_parser("rollback", help="pause canaries and reactivate a known version")
    rollback.add_argument("id")
    rollback.add_argument("version")
    rollback.add_argument("--task-type", default="*")
    rollback.add_argument("--harness", default="*")
    rollback.add_argument("--reason", required=True)

    route = sub.add_parser("route")
    route.add_argument("task")
    route.add_argument("--harness", default="codex")
    route.add_argument("--task-type", default="general")
    route.add_argument("--session-id")

    choose = sub.add_parser("choose", help="accept, reject, or switch a route without loading content")
    choose.add_argument("route_id")
    choose.add_argument("action", choices=("accept", "reject", "switch"))
    choose.add_argument("--id")
    choose.add_argument("--version")
    load = sub.add_parser("load", help="retrieve chosen content after acceptance")
    load.add_argument("route_id")
    export = sub.add_parser("export", help="materialize accepted complete packages without installing or executing")
    export.add_argument("route_id")
    export.add_argument("destination")

    feedback = sub.add_parser("feedback")
    feedback.add_argument("route_id")
    feedback.add_argument("rating", choices=("helpful", "unhelpful", "unsure"))
    feedback.add_argument("--reason", default="unspecified", choices=("unspecified", "mismatch", "output_quality", "preference", "other"))

    consent = sub.add_parser("feedback-consent", help="opt in/out of local follow-up analysis for one host session")
    consent.add_argument("state", choices=("on", "off", "status"))
    consent.add_argument("--session-id", required=True)
    events = sub.add_parser("feedback-events", help="inspect locally stored short feedback excerpts")
    events.add_argument("--session-id")
    audit = sub.add_parser("feedback-audit", help="inspect local feedback corrections")
    audit.add_argument("--event-id")
    correction = sub.add_parser("feedback-correct", help="correct one inferred feedback signal")
    correction.add_argument("event_id")
    correction.add_argument("signal", choices=("positive", "negative", "switch", "correction", "reuse"))
    deletion = sub.add_parser("feedback-delete", help="delete one inferred feedback event and its correction audit")
    deletion.add_argument("event_id")
    clear = sub.add_parser("feedback-clear", help="delete follow-up feedback, pending turns and consent")
    clear.add_argument("--session-id", help="omit to clear all local sessions")
    sub.add_parser("feedback-summary", help="descriptive counts, never automatic ranking")
    sub.add_parser("feedback-combinations", help="counts by exact loaded Agent/Skill pair, including missing feedback")

    evaluation = sub.add_parser("record-eval", help="record pre-existing evidence, not run an experiment")
    evaluation.add_argument("id")
    evaluation.add_argument("version")
    evaluation.add_argument("--task-type", required=True)
    evaluation.add_argument("--method", choices=("objective", "human"), required=True)
    evaluation.add_argument("--verdict", choices=("pass", "fail", "unknown"), required=True)
    evaluation.add_argument("--evidence-ref", required=True)
    evaluation.add_argument("--reviewer", required=True)

    points = sub.add_parser("award-points", help="manual reputation points; no redemption value")
    points.add_argument("contributor")
    points.add_argument("points", type=int)
    points.add_argument("--reason", required=True)
    points.add_argument("--evidence-ref", required=True)
    sub.add_parser("report")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    conn = open_store(args.db)
    try:
        if args.command == "init":
            result = {"db": str(Path(args.db).resolve()), "initialized": True}
        elif args.command == "import":
            result = import_artifact(conn, args.manifest)
        elif args.command == "review":
            result = {"review_id": review_artifact(
                conn, args.id, args.version, verdict=args.verdict,
                reviewer=args.reviewer, evidence_ref=args.evidence_ref,
                source_checked=args.source_checked, license_checked=args.license_checked,
                content_checked=args.content_checked, notice_checked=args.notice_checked,
                reason=args.reason,
            ), "id": args.id, "version": args.version, "verdict": args.verdict,
                "quality_certified": False}
        elif args.command == "admit":
            admit_artifact(conn, args.id, args.version)
            result = {"id": args.id, "version": args.version, "admission": "admitted", "quality_certified": False}
        elif args.command == "list":
            result = list_artifacts(conn)
        elif args.command == "rollout":
            percent = args.percent
            if percent is None:
                if args.state == "canary":
                    raise ValueError("--percent is required for canary")
                percent = 100 if args.state == "active" else 0
            event_id = set_rollout(conn, args.id, args.version, task_type=args.task_type,
                                   harness=args.harness, state=args.state, percent=percent, reason=args.reason)
            result = {"event_id": event_id, "id": args.id, "version": args.version,
                      "state": args.state, "percent": percent}
        elif args.command == "rollback":
            canaries = conn.execute(
                "SELECT version FROM rollouts WHERE id=? AND task_type=? AND harness=? AND state='canary'",
                (args.id, args.task_type, args.harness),
            ).fetchall()
            paused = [set_rollout(conn, args.id, row["version"], task_type=args.task_type,
                                  harness=args.harness, state="paused", percent=0,
                                  reason="rollback: " + args.reason) for row in canaries]
            restored = set_rollout(conn, args.id, args.version, task_type=args.task_type,
                                   harness=args.harness, state="active", percent=100,
                                   reason="rollback: " + args.reason)
            result = {"paused_canary_events": paused, "restored_event": restored,
                      "id": args.id, "version": args.version}
        elif args.command == "route":
            result = recommend(conn, args.task, harness=args.harness,
                               task_type=args.task_type, session_id=args.session_id)
        elif args.command == "choose":
            result = choose_route(conn, args.route_id, args.action,
                                  artifact_id=args.id, version=args.version)
        elif args.command == "load":
            result = load_route(conn, args.route_id)
        elif args.command == "export":
            result = export_route(conn, args.route_id, args.destination)
        elif args.command == "feedback":
            result = {"feedback_id": submit_feedback(conn, args.route_id, args.rating, args.reason),
                      "route_id": args.route_id, "rating": args.rating, "reason": args.reason}
        elif args.command == "feedback-consent":
            result = ({"feedback_enabled": consent_status(conn, args.session_id)}
                      if args.state == "status" else
                      set_consent(conn, args.session_id, args.state == "on"))
        elif args.command == "feedback-events":
            result = list_feedback(conn, args.session_id)
        elif args.command == "feedback-audit":
            result = list_feedback_audit(conn, args.event_id)
        elif args.command == "feedback-correct":
            result = correct_feedback(conn, args.event_id, args.signal)
        elif args.command == "feedback-delete":
            result = delete_feedback(conn, args.event_id)
        elif args.command == "feedback-clear":
            result = (delete_session_feedback(conn, args.session_id) if args.session_id
                      else delete_all_feedback(conn))
        elif args.command == "feedback-summary":
            result = feedback_summary(conn)
        elif args.command == "feedback-combinations":
            result = combination_feedback_summary(conn)
        elif args.command == "record-eval":
            result = {"eval_id": record_evaluation(conn, args.id, args.version,
                      task_type=args.task_type, method=args.method, verdict=args.verdict,
                      evidence_ref=args.evidence_ref, reviewer=args.reviewer)}
        elif args.command == "award-points":
            result = {"entry_id": award_points(conn, args.contributor, args.points,
                      args.reason, args.evidence_ref), "redeemable": False}
        else:
            result = local_report(conn)
        _print(result)
        return 0
    finally:
        conn.close()


def main() -> None:
    try:
        raise SystemExit(run())
    except (ValueError, sqlite3.Error, OSError, json.JSONDecodeError) as exc:
        print(f"marked: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
