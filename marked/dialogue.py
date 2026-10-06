"""Opt-in, local, high-precision follow-up signals for loaded recommendations.

The current prompt is examined in memory. Only a short matched span is stored;
no assistant message, transcript, raw task, or external model is used.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from typing import Any

from .core import _now, _session_digest


RULE_VERSION = "dialogue-rules-v1"
SIGNALS = {"positive", "negative", "switch", "correction", "reuse"}
MAX_EXCERPT = 32

# Deliberately narrow: uncertain phrasing stays unjudged in the first pilot.
_RULES = (
    ("negative", "not_helpful", re.compile(r"(?:^|[。.!?\s])(?:这个|刚才的(?:回答|推荐|技能)?|上一个(?:回答|推荐)?|that|your last (?:answer|recommendation))?\s*(?:没有帮助|没帮助|不太有用|没用|不合适|不匹配|not helpful|didn't help|doesn't fit|wasn't useful)", re.I)),
    ("correction", "explicit_correction", re.compile(r"(?:刚才|上一个|你刚才|your last|the previous).{0,24}(?:错了|有误|不对|需要纠正|was wrong|is incorrect|needs correction)", re.I)),
    ("positive", "explicit_helpful", re.compile(r"(?:^|[。.!?\s])(?:谢谢[，,\s]*)?(?:这个|刚才的(?:回答|推荐|技能)?|上一个(?:回答|推荐)?|that|your last (?:answer|recommendation))?\s*(?:很有帮助|有帮助|很合适|正是我要的|帮到我了|was helpful|helped me|worked well)", re.I)),
    ("switch", "explicit_switch", re.compile(r"(?:^|[。.!?\s])(?:请|帮我|can you\s+)?(?:换一个|换个(?:技能|推荐|agent)|试试别的|try another|switch to another)", re.I)),
    ("reuse", "explicit_reuse", re.compile(r"(?:^|[。.!?\s])(?:继续用|接着用|还用)(?:刚才|上一个|这个)(?:的)?(?:技能|推荐|agent|组合)|(?:keep using|continue with) (?:that|the same) (?:skill|agent|recommendation)", re.I)),
)


def classify_followup(message: str) -> tuple[str, str, str] | None:
    """Return one clear signal, rule identifier, and the matched short span."""
    text = " ".join(message.strip().split())
    if not text or len(text) > 240:
        return None
    # Questions about the phrases themselves are not feedback on prior use.
    if re.search(r"(?:如何|怎么|怎样|how to|write|实现|定义).{0,36}(?:反馈|按钮|评价|helpful|推荐)", text, re.I):
        return None
    for signal, rule, pattern in _RULES:
        match = pattern.search(text)
        if match:
            return signal, f"{RULE_VERSION}:{rule}", match.group(0).strip()[:MAX_EXCERPT]
    return None


def _digest(conn: sqlite3.Connection, session_id: str) -> str:
    if not session_id.strip():
        raise ValueError("session_id is required")
    return _session_digest(conn, session_id)


def consent_status(conn: sqlite3.Connection, session_id: str) -> bool:
    session_hash = _digest(conn, session_id)
    row = conn.execute("SELECT enabled FROM feedback_consent WHERE session_hash=?", (session_hash,)).fetchone()
    return bool(row and row["enabled"])


def set_consent(conn: sqlite3.Connection, session_id: str, enabled: bool) -> dict[str, Any]:
    session_hash = _digest(conn, session_id)
    with conn:
        conn.execute(
            "INSERT INTO feedback_consent(session_hash,enabled,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(session_hash) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at",
            (session_hash, int(enabled), _now()),
        )
        if not enabled:
            conn.execute("DELETE FROM hook_turns WHERE session_hash=?", (session_hash,))
    return {"feedback_enabled": enabled, "session_hash": session_hash,
            "existing_events_retained": True}


def note_route(conn: sqlite3.Connection, session_id: str, turn_id: str, route_id: str) -> None:
    if not turn_id or not consent_status(conn, session_id):
        return
    session_hash = _digest(conn, session_id)
    route = conn.execute("SELECT session_hash FROM routes WHERE route_id=?", (route_id,)).fetchone()
    if route is None or route["session_hash"] != session_hash:
        raise ValueError("route does not belong to this session")
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO hook_turns(session_hash,turn_id,route_id) VALUES(?,?,?)",
            (session_hash, turn_id, route_id),
        )
        existing = conn.execute(
            "SELECT route_id FROM hook_turns WHERE session_hash=? AND turn_id=?",
            (session_hash, turn_id),
        ).fetchone()
        if existing["route_id"] != route_id:
            # Multiple different routes in one host turn cannot be attributed safely.
            conn.execute("UPDATE hook_turns SET feedback_checked_at=? "
                         "WHERE session_hash=? AND turn_id=?",
                         (_now(), session_hash, turn_id))


def note_stop(conn: sqlite3.Connection, session_id: str, turn_id: str) -> bool:
    if not turn_id or not consent_status(conn, session_id):
        return False
    with conn:
        cursor = conn.execute(
            "UPDATE hook_turns SET stopped_at=COALESCE(stopped_at,?) "
            "WHERE session_hash=? AND turn_id=?",
            (_now(), _digest(conn, session_id), turn_id),
        )
    return cursor.rowcount > 0


def observe_followup(conn: sqlite3.Connection, session_id: str, source_turn_id: str,
                     message: str) -> dict[str, Any]:
    """Check only the most recent finished turn, once, within this session."""
    if not source_turn_id or not consent_status(conn, session_id):
        return {"status": "off_or_missing_turn"}
    session_hash = _digest(conn, session_id)
    row = conn.execute(
        "SELECT * FROM hook_turns WHERE session_hash=? AND stopped_at IS NOT NULL "
        "AND feedback_checked_at IS NULL AND turn_id<>? ORDER BY stopped_at DESC,rowid DESC LIMIT 1",
        (session_hash, source_turn_id),
    ).fetchone()
    if row is None:
        return {"status": "no_completed_route"}
    # A newer, unfinished or already checked route makes older attribution unsafe.
    latest = conn.execute(
        "SELECT turn_id FROM hook_turns WHERE session_hash=? ORDER BY rowid DESC LIMIT 1",
        (session_hash,),
    ).fetchone()
    if latest["turn_id"] != row["turn_id"]:
        return {"status": "ambiguous"}
    loads = conn.execute(
        "SELECT loaded_json FROM route_loads WHERE route_id=? ORDER BY rowid",
        (row["route_id"],),
    ).fetchall()
    with conn:
        conn.execute(
            "UPDATE hook_turns SET feedback_checked_at=? WHERE session_hash=? AND turn_id=?",
            (_now(), session_hash, row["turn_id"]),
        )
        if not loads or len({load["loaded_json"] for load in loads}) != 1:
            return {"status": "unjudged", "reason": "not_uniquely_loaded"}
        classified = classify_followup(message)
        if classified is None:
            return {"status": "unjudged", "reason": "no_clear_signal"}
        signal, rule_id, excerpt = classified
        event_id = str(uuid.uuid4())
        conn.execute(
            "INSERT OR IGNORE INTO natural_feedback(event_id,route_id,session_hash,source_turn_id,"
            "signal,rule_id,excerpt,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (event_id, row["route_id"], session_hash, source_turn_id,
             signal, rule_id, excerpt, _now()),
        )
    return {"status": "recorded", "event_id": event_id, "route_id": row["route_id"],
            "signal": signal, "feedback_only": message.strip() == excerpt}


def list_feedback(conn: sqlite3.Connection, session_id: str | None = None) -> list[dict[str, Any]]:
    query = ("SELECT event_id,route_id,source_turn_id,signal,rule_id,excerpt,created_at "
             "FROM natural_feedback")
    params: tuple[str, ...] = ()
    if session_id is not None:
        query += " WHERE session_hash=?"
        params = (_digest(conn, session_id),)
    rows = conn.execute(query + " ORDER BY created_at,rowid", params).fetchall()
    return [dict(row) for row in rows]


def list_feedback_audit(conn: sqlite3.Connection, event_id: str | None = None) -> list[dict[str, Any]]:
    query = ("SELECT audit_id,event_id,action,old_signal,new_signal,created_at "
             "FROM natural_feedback_audit")
    params: tuple[str, ...] = ()
    if event_id is not None:
        query += " WHERE event_id=?"
        params = (event_id,)
    return [dict(row) for row in conn.execute(query + " ORDER BY created_at,rowid", params)]


def correct_feedback(conn: sqlite3.Connection, event_id: str, signal: str) -> dict[str, Any]:
    if signal not in SIGNALS:
        raise ValueError("invalid signal")
    row = conn.execute("SELECT signal FROM natural_feedback WHERE event_id=?", (event_id,)).fetchone()
    if row is None:
        raise ValueError("feedback event not found")
    with conn:
        conn.execute("UPDATE natural_feedback SET signal=?,rule_id=? WHERE event_id=?",
                     (signal, "user-correction", event_id))
        conn.execute(
            "INSERT INTO natural_feedback_audit(audit_id,event_id,action,old_signal,new_signal,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()), event_id, "correct", row["signal"], signal, _now()),
        )
    return {"event_id": event_id, "old_signal": row["signal"], "signal": signal}


def delete_feedback(conn: sqlite3.Connection, event_id: str) -> dict[str, Any]:
    with conn:
        conn.execute("DELETE FROM natural_feedback_audit WHERE event_id=?", (event_id,))
        deleted = conn.execute("DELETE FROM natural_feedback WHERE event_id=?", (event_id,)).rowcount
    return {"event_id": event_id, "deleted": bool(deleted)}


def delete_session_feedback(conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    session_hash = _digest(conn, session_id)
    event_ids = [row[0] for row in conn.execute(
        "SELECT event_id FROM natural_feedback WHERE session_hash=?", (session_hash,))]
    with conn:
        for event_id in event_ids:
            conn.execute("DELETE FROM natural_feedback_audit WHERE event_id=?", (event_id,))
        conn.execute("DELETE FROM natural_feedback WHERE session_hash=?", (session_hash,))
        conn.execute("DELETE FROM hook_turns WHERE session_hash=?", (session_hash,))
        conn.execute("DELETE FROM feedback_consent WHERE session_hash=?", (session_hash,))
    return {"deleted_events": len(event_ids), "feedback_enabled": False,
            "route_history_retained": True}


def delete_all_feedback(conn: sqlite3.Connection) -> dict[str, Any]:
    count = conn.execute("SELECT COUNT(*) FROM natural_feedback").fetchone()[0]
    with conn:
        conn.execute("DELETE FROM natural_feedback_audit")
        conn.execute("DELETE FROM natural_feedback")
        conn.execute("DELETE FROM hook_turns")
        conn.execute("DELETE FROM feedback_consent")
    return {"deleted_events": count, "feedback_enabled": False,
            "route_history_retained": True}


def feedback_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Descriptive counts only; feedback never changes route ranking."""
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    def entry(task_type: str, harness: str, item: dict[str, Any]) -> dict[str, Any]:
        key = (task_type, harness, item["id"], item["version"])
        if key not in groups:
            groups[key] = {"task_type": task_type, "harness": harness,
                           "id": item["id"], "version": item["version"],
                           "exposures": 0, "accepted": 0, "loaded": 0,
                           "positive": 0, "negative": 0, "switch": 0,
                           "correction": 0, "reuse": 0, "unjudged": 0}
        return groups[key]

    routes = conn.execute("SELECT * FROM routes ORDER BY rowid").fetchall()
    for route in routes:
        route_id = route["route_id"]
        task_type, harness = route["task_type"], route["harness"]
        for item in json.loads(route["selected_json"]):
            entry(task_type, harness, item)["exposures"] += 1
        actions = conn.execute(
            "SELECT action,chosen_json FROM route_actions WHERE route_id=? ORDER BY rowid",
            (route_id,),
        ).fetchall()
        accepted_items = {
            (item["id"], item["version"]): item
            for action in actions if action["action"] in {"accept", "switch"}
            for item in json.loads(action["chosen_json"])
        }
        for item in accepted_items.values():
            entry(task_type, harness, item)["accepted"] += 1
        load = conn.execute(
            "SELECT loaded_json FROM route_loads WHERE route_id=? ORDER BY rowid DESC LIMIT 1",
            (route_id,),
        ).fetchone()
        if not load:
            continue
        items = json.loads(load["loaded_json"])
        event = conn.execute(
            "SELECT signal FROM natural_feedback WHERE route_id=? ORDER BY rowid DESC LIMIT 1",
            (route_id,),
        ).fetchone()
        checked = conn.execute(
            "SELECT 1 FROM hook_turns WHERE route_id=? AND feedback_checked_at IS NOT NULL LIMIT 1",
            (route_id,),
        ).fetchone()
        for item in items:
            counts = entry(task_type, harness, item)
            counts["loaded"] += 1
            if event:
                counts[event["signal"]] += 1
            elif checked:
                counts["unjudged"] += 1
    return sorted(groups.values(), key=lambda row: (
        row["task_type"], row["harness"], row["id"], row["version"]))


def combination_feedback_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Count route-level observations by exact Agent/Skill configuration.

    A route contributes at most once to each configuration's exposure, choice,
    and load counts. Several different configurations loaded on one route are
    kept visible, but its follow-up cannot be credited to either one. Silence
    or disabled collection is distinct from an observed unclear next turn.
    This is descriptive local evidence, never a quality score or rank input.
    """
    groups: dict[tuple[str, str, tuple[tuple[str, str, str], ...]], dict[str, Any]] = {}
    sessions: dict[tuple[str, str, tuple[tuple[str, str, str], ...]], set[str]] = {}

    def entry(route: sqlite3.Row, signature: tuple[tuple[str, str, str], ...]) -> dict[str, Any]:
        key = (route["task_type"], route["harness"], signature)
        if key not in groups:
            groups[key] = {
                "task_type": key[0], "harness": key[1],
                "configuration": [
                    {"kind": kind, "id": artifact_id, "version": version}
                    for kind, artifact_id, version in signature
                ],
                "exposures": 0, "accepted": 0, "loaded": 0, "load_events": 0,
                "positive": 0, "negative": 0, "switch": 0,
                "correction": 0, "reuse": 0, "unjudged": 0,
                "not_observed": 0, "ambiguous_attribution": 0,
            }
            sessions[key] = set()
        return groups[key]

    routes = conn.execute("SELECT * FROM routes ORDER BY rowid").fetchall()
    for route in routes:
        route_id = route["route_id"]
        selected = json.loads(route["selected_json"])
        candidates = json.loads(route["candidates_json"])
        kinds = {(item["id"], item["version"]): item["kind"]
                 for item in candidates + selected}

        def signature(items: list[dict[str, Any]]) -> tuple[tuple[str, str, str], ...]:
            parts = set()
            for item in items:
                artifact_key = (item["id"], item["version"])
                kind = item.get("kind") or kinds.get(artifact_key)
                if kind not in {"agent", "skill"}:
                    raise ValueError(f"route {route_id} lacks a kind for {artifact_key}")
                parts.add((kind, *artifact_key))
            return tuple(sorted(parts, key=lambda part: (part[0] != "agent", part[1], part[2])))

        recommended = signature(selected)
        if recommended:
            entry(route, recommended)["exposures"] += 1
        actions = conn.execute(
            "SELECT action,chosen_json FROM route_actions WHERE route_id=? ORDER BY rowid",
            (route_id,),
        ).fetchall()
        accepted = {signature(json.loads(action["chosen_json"]))
                    for action in actions if action["action"] in {"accept", "switch"}}
        for chosen in accepted:
            if chosen:
                entry(route, chosen)["accepted"] += 1

        loads = conn.execute(
            "SELECT loaded_json FROM route_loads WHERE route_id=? ORDER BY rowid",
            (route_id,),
        ).fetchall()
        load_counts: dict[tuple[tuple[str, str, str], ...], int] = {}
        for load in loads:
            loaded = signature(json.loads(load["loaded_json"]))
            if loaded:
                load_counts[loaded] = load_counts.get(loaded, 0) + 1
        for loaded, count in load_counts.items():
            row = entry(route, loaded)
            row["loaded"] += 1
            row["load_events"] += count
            sessions[(route["task_type"], route["harness"], loaded)].add(route["session_hash"])
        if not load_counts:
            continue
        if len(load_counts) != 1:
            for loaded in load_counts:
                entry(route, loaded)["ambiguous_attribution"] += 1
            continue

        loaded = next(iter(load_counts))
        counts = entry(route, loaded)
        events = conn.execute(
            "SELECT signal FROM natural_feedback WHERE route_id=? ORDER BY rowid",
            (route_id,),
        ).fetchall()
        checked = conn.execute(
            "SELECT 1 FROM hook_turns WHERE route_id=? AND feedback_checked_at IS NOT NULL LIMIT 1",
            (route_id,),
        ).fetchone()
        if len(events) > 1:
            counts["ambiguous_attribution"] += 1
        elif events:
            signal = events[0]["signal"]
            if signal not in SIGNALS:
                raise ValueError(f"route {route_id} has an unknown feedback signal")
            counts[signal] += 1
        elif checked:
            counts["unjudged"] += 1
        else:
            counts["not_observed"] += 1

    for key, row in groups.items():
        row["distinct_loaded_sessions"] = len(sessions[key])
        row["clear_signals"] = sum(row[signal] for signal in SIGNALS)
        row["observed_followups"] = row["clear_signals"] + row["unjudged"]
    return [groups[key] for key in sorted(groups)]
