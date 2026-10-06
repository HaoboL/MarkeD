"""Local catalog, evidence, routing, rollout, and feedback.

This module does not call a model or send task text to a server.  The first
router is deliberately an inspectable baseline, not a calibrated quality
predictor.  Later rankers can use the same catalog and event contracts.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = 8
ROUTER_VERSION = "lexical-v1"
MAX_CONTENT_BYTES = 32_768
MAX_PACKAGE_FILES = 64
MAX_PACKAGE_BYTES = 4 * 1024 * 1024
SLUG = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9.-]+)?$")
VALID_KINDS = {"agent", "skill"}
VALID_ROLLOUT_STATES = {"active", "canary", "paused"}
VALID_FEEDBACK = {"helpful", "unhelpful", "unsure"}
VALID_FEEDBACK_REASONS = {"unspecified", "mismatch", "output_quality", "preference", "other"}
ADMITTED_LICENSES = {"MIT", "Apache-2.0"}
LICENSE_FILENAMES = {"LICENSE", "LICENSE.txt", "LICENSE.md"}
NOTICE_FILENAMES = {"NOTICE", "NOTICE.txt", "NOTICE.md"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def open_store(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS artifacts (
            id TEXT NOT NULL, version TEXT NOT NULL, kind TEXT NOT NULL,
            title TEXT NOT NULL, description TEXT NOT NULL,
            author TEXT NOT NULL, license TEXT NOT NULL, source TEXT NOT NULL,
            tags_json TEXT NOT NULL, stages_json TEXT NOT NULL,
            harnesses_json TEXT NOT NULL, compatible_agents_json TEXT NOT NULL,
            content TEXT NOT NULL, content_sha256 TEXT NOT NULL,
            package_sha256 TEXT NOT NULL DEFAULT '',
            admission TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
            PRIMARY KEY (id, version)
        );
        CREATE TABLE IF NOT EXISTS artifact_reviews (
            review_id TEXT PRIMARY KEY, id TEXT NOT NULL, version TEXT NOT NULL,
            verdict TEXT NOT NULL, reviewer TEXT NOT NULL,
            source_checked INTEGER NOT NULL, license_checked INTEGER NOT NULL,
            content_checked INTEGER NOT NULL, content_sha256 TEXT NOT NULL,
            package_sha256 TEXT NOT NULL DEFAULT '',
            evidence_ref TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
            notice_checked INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (id, version) REFERENCES artifacts(id, version)
        );
        CREATE TABLE IF NOT EXISTS artifact_files (
            id TEXT NOT NULL, version TEXT NOT NULL, path TEXT NOT NULL,
            content BLOB NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
            PRIMARY KEY (id, version, path),
            FOREIGN KEY (id, version) REFERENCES artifacts(id, version)
        );
        CREATE TABLE IF NOT EXISTS rollouts (
            id TEXT NOT NULL, version TEXT NOT NULL,
            task_type TEXT NOT NULL, harness TEXT NOT NULL,
            state TEXT NOT NULL, percent INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (id, version, task_type, harness),
            FOREIGN KEY (id, version) REFERENCES artifacts(id, version)
        );
        CREATE TABLE IF NOT EXISTS rollout_events (
            event_id TEXT PRIMARY KEY, id TEXT NOT NULL, version TEXT NOT NULL,
            task_type TEXT NOT NULL, harness TEXT NOT NULL,
            old_state TEXT, new_state TEXT NOT NULL, percent INTEGER NOT NULL,
            reason TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evaluations (
            eval_id TEXT PRIMARY KEY, id TEXT NOT NULL, version TEXT NOT NULL,
            task_type TEXT NOT NULL, method TEXT NOT NULL,
            verdict TEXT NOT NULL, evidence_ref TEXT NOT NULL,
            reviewer TEXT NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY (id, version) REFERENCES artifacts(id, version)
        );
        CREATE TABLE IF NOT EXISTS routes (
            route_id TEXT PRIMARY KEY, task_type TEXT NOT NULL,
            harness TEXT NOT NULL, session_hash TEXT NOT NULL,
            selected_json TEXT NOT NULL, candidates_json TEXT NOT NULL,
            explanation TEXT NOT NULL, router_version TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS feedback (
            feedback_id TEXT PRIMARY KEY, route_id TEXT NOT NULL,
            rating TEXT NOT NULL, reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (route_id) REFERENCES routes(route_id)
        );
        CREATE TABLE IF NOT EXISTS feedback_consent (
            session_hash TEXT PRIMARY KEY, enabled INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS hook_turns (
            session_hash TEXT NOT NULL, turn_id TEXT NOT NULL,
            route_id TEXT NOT NULL, stopped_at TEXT, feedback_checked_at TEXT,
            PRIMARY KEY (session_hash, turn_id),
            FOREIGN KEY (route_id) REFERENCES routes(route_id)
        );
        CREATE TABLE IF NOT EXISTS natural_feedback (
            event_id TEXT PRIMARY KEY, route_id TEXT NOT NULL,
            session_hash TEXT NOT NULL, source_turn_id TEXT NOT NULL,
            signal TEXT NOT NULL, rule_id TEXT NOT NULL,
            excerpt TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE (session_hash, source_turn_id, route_id),
            FOREIGN KEY (route_id) REFERENCES routes(route_id)
        );
        CREATE TABLE IF NOT EXISTS natural_feedback_audit (
            audit_id TEXT PRIMARY KEY, event_id TEXT NOT NULL,
            action TEXT NOT NULL, old_signal TEXT NOT NULL,
            new_signal TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS route_actions (
            action_id TEXT PRIMARY KEY, route_id TEXT NOT NULL,
            action TEXT NOT NULL, actor TEXT NOT NULL,
            chosen_json TEXT NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY (route_id) REFERENCES routes(route_id)
        );
        CREATE TABLE IF NOT EXISTS route_loads (
            load_id TEXT PRIMARY KEY, route_id TEXT NOT NULL,
            action_id TEXT NOT NULL, loaded_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (route_id) REFERENCES routes(route_id),
            FOREIGN KEY (action_id) REFERENCES route_actions(action_id)
        );
        CREATE TABLE IF NOT EXISTS points_ledger (
            entry_id TEXT PRIMARY KEY, contributor TEXT NOT NULL,
            points INTEGER NOT NULL, reason TEXT NOT NULL,
            evidence_ref TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """
    )
    if conn.execute("SELECT 1 FROM settings WHERE key='installation_salt'").fetchone() is None:
        with conn:
            conn.execute(
                "INSERT INTO settings(key,value) VALUES('installation_salt',?)",
                (secrets.token_hex(32),),
            )
    old_version_row = conn.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
    old_version = int(old_version_row["value"]) if old_version_row else 0
    with conn:
        if old_version < 4:
            # Older 'admit --reviewed' did not identify a reviewer or document checks.
            legacy = conn.execute(
                """SELECT r.id,r.version,r.task_type,r.harness,r.state
                FROM rollouts r JOIN artifacts a ON a.id=r.id AND a.version=r.version
                WHERE a.admission='admitted' AND r.state<>'paused'"""
            ).fetchall()
            for row in legacy:
                conn.execute(
                    """INSERT INTO rollout_events
                    (event_id,id,version,task_type,harness,old_state,new_state,percent,reason,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), row["id"], row["version"], row["task_type"],
                     row["harness"], row["state"], "paused", 0,
                     "schema-v4 migration: documented review required", _now()),
                )
            conn.execute(
                """UPDATE rollouts SET state='paused',percent=0,updated_at=?
                WHERE EXISTS (SELECT 1 FROM artifacts a WHERE a.id=rollouts.id
                AND a.version=rollouts.version AND a.admission='admitted')""",
                (_now(),),
            )
            conn.execute("UPDATE artifacts SET admission='pending' WHERE admission='admitted'")
        if old_version < 5:
            artifact_columns = {row["name"] for row in conn.execute("PRAGMA table_info(artifacts)")}
            review_columns = {row["name"] for row in conn.execute("PRAGMA table_info(artifact_reviews)")}
            if "package_sha256" not in artifact_columns:
                conn.execute("ALTER TABLE artifacts ADD COLUMN package_sha256 TEXT NOT NULL DEFAULT ''")
            if "package_sha256" not in review_columns:
                conn.execute("ALTER TABLE artifact_reviews ADD COLUMN package_sha256 TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE artifacts SET package_sha256=content_sha256 WHERE package_sha256=''")
            conn.execute("UPDATE artifact_reviews SET package_sha256=content_sha256 WHERE package_sha256=''")
        if old_version < 7:
            turn_columns = {row["name"] for row in conn.execute("PRAGMA table_info(hook_turns)")}
            if "feedback_checked_at" not in turn_columns:
                conn.execute("ALTER TABLE hook_turns ADD COLUMN feedback_checked_at TEXT")
        if old_version < 8:
            review_columns = {row["name"] for row in conn.execute("PRAGMA table_info(artifact_reviews)")}
            if "notice_checked" not in review_columns:
                conn.execute("ALTER TABLE artifact_reviews ADD COLUMN notice_checked INTEGER NOT NULL DEFAULT 0")
            # Earlier admissions did not apply the selected first-round license gate.
            legacy_unchecked = conn.execute(
                """SELECT r.id,r.version,r.task_type,r.harness,r.state
                FROM rollouts r JOIN artifacts a ON a.id=r.id AND a.version=r.version
                WHERE a.admission='admitted' AND r.state<>'paused' AND (
                    a.license NOT IN ('MIT','Apache-2.0') OR a.license='Apache-2.0'
                    OR NOT EXISTS (SELECT 1 FROM artifact_files f WHERE f.id=a.id
                       AND f.version=a.version AND f.path IN ('LICENSE','LICENSE.txt','LICENSE.md'))
                )"""
            ).fetchall()
            for row in legacy_unchecked:
                conn.execute(
                    """INSERT INTO rollout_events
                    (event_id,id,version,task_type,harness,old_state,new_state,percent,reason,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), row["id"], row["version"], row["task_type"],
                     row["harness"], row["state"], "paused", 0,
                     "schema-v8 migration: first-round redistribution review required", _now()),
                )
            conn.execute(
                """UPDATE rollouts SET state='paused',percent=0,updated_at=?
                WHERE EXISTS (SELECT 1 FROM artifacts a WHERE a.id=rollouts.id
                AND a.version=rollouts.version AND a.admission='admitted' AND (
                    a.license NOT IN ('MIT','Apache-2.0') OR a.license='Apache-2.0'
                    OR NOT EXISTS (SELECT 1 FROM artifact_files f WHERE f.id=a.id
                       AND f.version=a.version AND f.path IN ('LICENSE','LICENSE.txt','LICENSE.md'))
                ))""",
                (_now(),),
            )
            conn.execute(
                """UPDATE artifacts SET admission='pending' WHERE admission='admitted' AND (
                    license NOT IN ('MIT','Apache-2.0') OR license='Apache-2.0'
                    OR NOT EXISTS (SELECT 1 FROM artifact_files f WHERE f.id=artifacts.id
                       AND f.version=artifacts.version AND f.path IN ('LICENSE','LICENSE.txt','LICENSE.md'))
                )"""
            )
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
    return conn


def _string_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValueError(f"{key} must be a list of nonempty strings")
    return list(dict.fromkeys(x.strip() for x in value))


def _package_digest(main_sha256: str, support: list[tuple[str, str]]) -> str:
    if not support:
        return main_sha256
    inventory = [("@main", main_sha256)] + sorted(support)
    return hashlib.sha256(_json(inventory).encode("utf-8")).hexdigest()


def _read_limited(path: Path, limit: int, description: str) -> bytes:
    with path.open("rb") as source:
        content = source.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f"{description} exceeds {limit} bytes")
    return content


def _safe_package_file(parent: Path, relative: str) -> Path:
    if "\\" in relative:
        raise ValueError("package paths must use forward slashes")
    posix = PurePosixPath(relative)
    if (posix.is_absolute() or not posix.parts or
            relative != posix.as_posix() or
            any(part in {".", ".."} for part in relative.split("/"))):
        raise ValueError(f"unsafe package path: {relative}")
    current = parent
    for part in posix.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"symlinks are not allowed in package path: {relative}")
    resolved = current.resolve(strict=True)
    if not resolved.is_relative_to(parent) or not resolved.is_file():
        raise ValueError(f"package path must be a file inside the manifest directory: {relative}")
    return resolved


def import_artifact(conn: sqlite3.Connection, manifest_file: str | Path) -> dict[str, str]:
    manifest_path = Path(manifest_file).expanduser().resolve(strict=True)
    data = json.loads(_read_limited(manifest_path, 64 * 1024, "manifest").decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest must be a JSON object")
    for key in ("id", "version", "kind", "title", "description", "author", "license", "source", "content_file"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"{key} must be a nonempty string")
    if not SLUG.fullmatch(data["id"]):
        raise ValueError("id must be a lowercase slug")
    if not VERSION.fullmatch(data["version"]):
        raise ValueError("version must be a SemVer-like x.y.z string")
    if data["kind"] not in VALID_KINDS:
        raise ValueError("kind must be agent or skill")
    tags = _string_list(data, "tags")
    stages = _string_list(data, "stages")
    harnesses = _string_list(data, "harnesses")
    compatible_agents = _string_list(data, "compatible_agents")
    support_paths = _string_list(data, "support_files")
    if len(support_paths) > MAX_PACKAGE_FILES:
        raise ValueError(f"support_files exceeds {MAX_PACKAGE_FILES} files")
    if data["content_file"] in support_paths:
        raise ValueError("content_file cannot also be a support file")
    main_name = "SKILL.md" if data["kind"] == "skill" else "agent.md"
    if any(path == main_name or path.startswith(main_name + "/") for path in support_paths):
        raise ValueError(f"support_files cannot replace the exported {main_name}")
    parent = manifest_path.parent
    if "/" in data["content_file"] or "\\" in data["content_file"]:
        raise ValueError("content_file must be at the manifest directory root")
    content_path = _safe_package_file(parent, data["content_file"])
    raw = _read_limited(content_path, MAX_CONTENT_BYTES, "content")
    content = raw.decode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    if data.get("content_sha256") and data["content_sha256"] != digest:
        raise ValueError("content_sha256 does not match content_file")
    support_files = []
    total_bytes = len(raw)
    for relative in support_paths:
        file_bytes = _read_limited(
            _safe_package_file(parent, relative), MAX_PACKAGE_BYTES - total_bytes,
            "package",
        )
        total_bytes += len(file_bytes)
        support_files.append((relative, file_bytes, hashlib.sha256(file_bytes).hexdigest()))
    package_digest = _package_digest(digest, [(path, sha) for path, _, sha in support_files])
    with conn:
        conn.execute(
            """INSERT INTO artifacts
            (id,version,kind,title,description,author,license,source,tags_json,
             stages_json,harnesses_json,compatible_agents_json,content,
             content_sha256,package_sha256,admission,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (data["id"], data["version"], data["kind"], data["title"],
             data["description"], data["author"], data["license"], data["source"],
             _json(tags), _json(stages), _json(harnesses), _json(compatible_agents),
             content, digest, package_digest, "pending", _now()),
        )
        for path, file_bytes, file_digest in support_files:
            conn.execute(
                """INSERT INTO artifact_files(id,version,path,content,sha256,size)
                VALUES(?,?,?,?,?,?)""",
                (data["id"], data["version"], path, file_bytes, file_digest, len(file_bytes)),
            )
    return {"id": data["id"], "version": data["version"], "sha256": digest,
            "package_sha256": package_digest, "support_files": len(support_files),
            "admission": "pending"}


def _verified_artifact(
    conn: sqlite3.Connection, artifact_id: str, version: str
) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    row = conn.execute(
        "SELECT * FROM artifacts WHERE id=? AND version=?", (artifact_id, version)
    ).fetchone()
    if row is None:
        raise ValueError("artifact/version not found")
    main_bytes = row["content"].encode("utf-8")
    main_digest = hashlib.sha256(main_bytes).hexdigest()
    if main_digest != row["content_sha256"]:
        raise ValueError("main content failed integrity check")
    files = conn.execute(
        "SELECT path,content,sha256,size FROM artifact_files WHERE id=? AND version=? ORDER BY path",
        (artifact_id, version),
    ).fetchall()
    inventory = []
    main_name = "SKILL.md" if row["kind"] == "skill" else "agent.md"
    for item in files:
        relative = item["path"]
        posix = PurePosixPath(relative)
        if (posix.is_absolute() or not posix.parts or
                relative != posix.as_posix() or
                any(part in {".", ".."} for part in relative.split("/")) or
                "\\" in relative or relative == main_name or
                relative.startswith(main_name + "/")):
            raise ValueError("stored package path is unsafe")
        content = bytes(item["content"])
        digest = hashlib.sha256(content).hexdigest()
        if digest != item["sha256"] or len(content) != item["size"]:
            raise ValueError("support file failed integrity check")
        inventory.append((relative, digest))
    if _package_digest(main_digest, inventory) != row["package_sha256"]:
        raise ValueError("package inventory failed integrity check")
    return row, files


def _redistribution_gate(artifact: sqlite3.Row, files: list[sqlite3.Row]) -> None:
    """Check machine-verifiable license structure; rights and NOTICE need human review."""
    license_id = artifact["license"]
    if license_id not in ADMITTED_LICENSES:
        raise ValueError("admission license must be MIT or Apache-2.0")
    license_files = [item for item in files if item["path"] in LICENSE_FILENAMES]
    if len(license_files) != 1:
        raise ValueError("exactly one root LICENSE file must be in support_files")
    license_text = bytes(license_files[0]["content"]).decode("utf-8", errors="replace")
    if license_id == "MIT":
        required = ("Permission is hereby granted", "the above copyright notice",
                    "THE SOFTWARE IS PROVIDED")
    else:
        required = ("Apache License", "Version 2.0, January 2004",
                    "END OF TERMS AND CONDITIONS")
    if not all(fragment.lower() in license_text.lower() for fragment in required):
        raise ValueError("declared license text is incomplete or mismatched")
    notices = [item for item in files if item["path"] in NOTICE_FILENAMES]
    if len(notices) > 1 or any(not bytes(item["content"]).strip() for item in notices):
        raise ValueError("NOTICE support file is empty or ambiguous")


def admit_artifact(conn: sqlite3.Connection, artifact_id: str, version: str) -> None:
    """Admit only after an exact-package documented review."""
    artifact, files = _verified_artifact(conn, artifact_id, version)
    _redistribution_gate(artifact, files)
    if artifact["admission"] != "pending":
        raise ValueError("artifact/version not pending")
    review = conn.execute(
        """SELECT verdict,source_checked,license_checked,content_checked,notice_checked,package_sha256
        FROM artifact_reviews WHERE id=? AND version=? ORDER BY rowid DESC LIMIT 1""",
        (artifact_id, version),
    ).fetchone()
    if review is None or review["verdict"] != "approve" or not all(
        review[field] for field in ("source_checked", "license_checked", "content_checked")
    ) or review["package_sha256"] != artifact["package_sha256"] or (
        artifact["license"] == "Apache-2.0" and not review["notice_checked"]
    ):
        raise ValueError("a current approving source/license/full-package review is required")
    with conn:
        cur = conn.execute(
            "UPDATE artifacts SET admission='admitted' WHERE id=? AND version=? AND admission='pending'",
            (artifact_id, version),
        )
    if cur.rowcount != 1:
        raise ValueError("artifact/version not pending")


def review_artifact(
    conn: sqlite3.Connection, artifact_id: str, version: str, *,
    verdict: str, reviewer: str, evidence_ref: str,
    source_checked: bool = False, license_checked: bool = False,
    content_checked: bool = False, notice_checked: bool = False, reason: str = "",
) -> str:
    """Record a manual checklist; claims are not independently verified by this code."""
    if verdict not in {"approve", "reject"}:
        raise ValueError("verdict must be approve or reject")
    if not reviewer.strip() or not evidence_ref.strip():
        raise ValueError("reviewer and evidence_ref are required")
    if verdict == "approve" and not (source_checked and license_checked and content_checked):
        raise ValueError("approval requires source, license, and content checks")
    if verdict == "reject" and not reason.strip():
        raise ValueError("rejection requires a reason")
    artifact = conn.execute(
        "SELECT admission,content_sha256,package_sha256 FROM artifacts WHERE id=? AND version=?",
        (artifact_id, version),
    ).fetchone()
    if artifact is None or artifact["admission"] == "rejected":
        raise ValueError("artifact/version is missing or already rejected")
    if verdict == "approve":
        artifact, files = _verified_artifact(conn, artifact_id, version)
        _redistribution_gate(artifact, files)
        if artifact["license"] == "Apache-2.0" and not notice_checked:
            raise ValueError("Apache-2.0 approval requires an upstream NOTICE check")
    review_id = str(uuid.uuid4())
    with conn:
        conn.execute(
            """INSERT INTO artifact_reviews
            (review_id,id,version,verdict,reviewer,source_checked,license_checked,
             content_checked,content_sha256,package_sha256,evidence_ref,reason,created_at,notice_checked)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (review_id, artifact_id, version, verdict, reviewer.strip(),
             int(source_checked), int(license_checked), int(content_checked),
             artifact["content_sha256"], artifact["package_sha256"],
             evidence_ref.strip(), reason.strip(), _now(), int(notice_checked)),
        )
        if verdict == "reject":
            active = conn.execute(
                "SELECT task_type,harness,state FROM rollouts WHERE id=? AND version=? AND state<>'paused'",
                (artifact_id, version),
            ).fetchall()
            for row in active:
                conn.execute(
                    """INSERT INTO rollout_events
                    (event_id,id,version,task_type,harness,old_state,new_state,percent,reason,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), artifact_id, version, row["task_type"], row["harness"],
                     row["state"], "paused", 0, f"rejected review: {reason.strip()}", _now()),
                )
            conn.execute(
                "UPDATE rollouts SET state='paused',percent=0,updated_at=? WHERE id=? AND version=?",
                (_now(), artifact_id, version),
            )
            conn.execute(
                "UPDATE artifacts SET admission='rejected' WHERE id=? AND version=?",
                (artifact_id, version),
            )
    return review_id


def list_artifacts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT a.id,a.version,a.kind,a.title,a.description,a.author,a.license,a.source,
        a.content_sha256,a.package_sha256,a.admission,
        (SELECT COUNT(*) FROM artifact_files f WHERE f.id=a.id AND f.version=a.version) AS support_files
        FROM artifacts a ORDER BY a.id,a.version"""
    ).fetchall()
    return [dict(row) for row in rows]


def set_rollout(
    conn: sqlite3.Connection, artifact_id: str, version: str, *,
    task_type: str = "*", harness: str = "*", state: str,
    percent: int = 100, reason: str,
) -> str:
    if state not in VALID_ROLLOUT_STATES:
        raise ValueError("state must be active, canary, or paused")
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    if state == "active" and percent != 100:
        raise ValueError("active versions must receive 100% of non-canary traffic")
    if state == "paused" and percent != 0:
        raise ValueError("paused versions must have percent=0")
    if not reason.strip():
        raise ValueError("reason is required for rollout audit")
    artifact = conn.execute(
        "SELECT admission FROM artifacts WHERE id=? AND version=?", (artifact_id, version)
    ).fetchone()
    if artifact is None or artifact["admission"] != "admitted":
        raise ValueError("only admitted artifacts may be routed")
    event_id = str(uuid.uuid4())
    with conn:
        old = conn.execute(
            "SELECT state FROM rollouts WHERE id=? AND version=? AND task_type=? AND harness=?",
            (artifact_id, version, task_type, harness),
        ).fetchone()
        if state in {"active", "canary"}:
            other = "active" if state == "active" else "canary"
            replaced = conn.execute(
                "SELECT version,state FROM rollouts WHERE id=? AND task_type=? AND harness=? AND state=? AND version<>?",
                (artifact_id, task_type, harness, other, version),
            ).fetchall()
            conn.execute(
                "UPDATE rollouts SET state='paused',percent=0,updated_at=? "
                "WHERE id=? AND task_type=? AND harness=? AND state=? AND version<>?",
                (_now(), artifact_id, task_type, harness, other, version),
            )
            for prior in replaced:
                conn.execute(
                    """INSERT INTO rollout_events
                    (event_id,id,version,task_type,harness,old_state,new_state,percent,reason,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), artifact_id, prior["version"], task_type, harness,
                     prior["state"], "paused", 0, f"superseded: {reason.strip()}", _now()),
                )
        conn.execute(
            """INSERT INTO rollouts(id,version,task_type,harness,state,percent,updated_at)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(id,version,task_type,harness)
            DO UPDATE SET state=excluded.state,percent=excluded.percent,updated_at=excluded.updated_at""",
            (artifact_id, version, task_type, harness, state, percent, _now()),
        )
        conn.execute(
            """INSERT INTO rollout_events
            (event_id,id,version,task_type,harness,old_state,new_state,percent,reason,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (event_id, artifact_id, version, task_type, harness,
             old["state"] if old else None, state, percent, reason.strip(), _now()),
        )
    return event_id


def record_evaluation(
    conn: sqlite3.Connection, artifact_id: str, version: str, *,
    task_type: str, method: str, verdict: str, evidence_ref: str, reviewer: str,
) -> str:
    if method not in {"objective", "human"} or verdict not in {"pass", "fail", "unknown"}:
        raise ValueError("invalid evaluation method or verdict")
    if not evidence_ref.strip() or not reviewer.strip() or not task_type.strip():
        raise ValueError("task_type, evidence_ref and reviewer are required")
    eval_id = str(uuid.uuid4())
    with conn:
        conn.execute(
            """INSERT INTO evaluations
            (eval_id,id,version,task_type,method,verdict,evidence_ref,reviewer,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (eval_id, artifact_id, version, task_type, method, verdict,
             evidence_ref, reviewer, _now()),
        )
    return eval_id


def _terms(text: str) -> set[str]:
    lower = text.lower()
    # Split slugs such as gh-fix-ci into words, and remove function words that
    # otherwise create false matches for unrelated tasks.
    words = set(re.findall(r"[a-z][a-z0-9]+", lower))
    words.difference_update({
        "an", "and", "are", "as", "at", "be", "by", "for", "from",
        "in", "into", "is", "it", "of", "on", "or", "our", "the",
        "this", "to", "with",
    })
    for run in re.findall(r"[\u4e00-\u9fff]+", lower):
        words.update(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return words


def _topical_score(task: str, row: sqlite3.Row, task_type: str) -> float:
    lower = task.lower()
    tags = json.loads(row["tags_json"])
    tag_points = sum(3 for tag in tags if tag.lower() in lower)
    task_terms = _terms(task)
    # Description overlap alone is too weak: generic words can match a Skill
    # outside its domain. Abstain unless title or reviewed tags anchor it.
    if not (task_terms & _terms(row["title"])) and tag_points == 0:
        return 0.0
    overlap = len(task_terms & _terms(row["title"] + " " + row["description"]))
    word_points = min(2.0, 2.0 * overlap / max(1, min(4, len(task_terms))))
    stage_points = 2.0 if task_type != "general" and task_type in json.loads(row["stages_json"]) else 0.0
    return tag_points + word_points + stage_points


def _evidence(conn: sqlite3.Connection, artifact_id: str, version: str, task_type: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT verdict,COUNT(*) n FROM evaluations WHERE id=? AND version=? AND task_type=? GROUP BY verdict",
        (artifact_id, version, task_type),
    ).fetchall()
    counts = {"pass": 0, "fail": 0, "unknown": 0}
    counts.update({row["verdict"]: row["n"] for row in rows})
    return counts


def _effective_rollout(conn: sqlite3.Connection, task_type: str, harness: str) -> dict[str, list[sqlite3.Row]]:
    rows = conn.execute(
        """SELECT r.task_type,r.harness,r.state,r.percent,
        a.id,a.version,a.kind,a.title,a.description,a.author,a.license,a.source,
        a.tags_json,a.stages_json,
        a.harnesses_json,a.compatible_agents_json,a.content_sha256,
        a.package_sha256,
        (SELECT COUNT(*) FROM artifact_files f WHERE f.id=a.id AND f.version=a.version) AS support_files,
        (SELECT COUNT(*) FROM artifact_files f WHERE f.id=a.id AND f.version=a.version
         AND f.path NOT IN ('LICENSE','LICENSE.txt','LICENSE.md','NOTICE','NOTICE.txt','NOTICE.md'))
         AS operational_support_files
        FROM rollouts r JOIN artifacts a ON a.id=r.id AND a.version=r.version
        WHERE a.admission='admitted' AND r.task_type IN (?, '*') AND r.harness IN (?, '*')""",
        (task_type, harness),
    ).fetchall()
    best: dict[tuple[str, str], tuple[tuple[int, int], sqlite3.Row]] = {}
    for row in rows:
        specificity = (int(row["task_type"] == task_type), int(row["harness"] == harness))
        key = (row["id"], row["version"])
        if key not in best or specificity > best[key][0]:
            best[key] = (specificity, row)
    grouped: dict[str, list[sqlite3.Row]] = {}
    for _, row in best.values():
        grouped.setdefault(row["id"], []).append(row)
    return grouped


def _session_digest(conn: sqlite3.Connection, session_id: str) -> str:
    salt = conn.execute("SELECT value FROM settings WHERE key='installation_salt'").fetchone()["value"]
    return hashlib.sha256(f"{salt}:{session_id}".encode()).hexdigest()


def _rollout_priority(row: sqlite3.Row, task_type: str, harness: str) -> tuple[Any, ...]:
    base = row["version"].split("-", 1)
    version = tuple(int(part) for part in base[0].split("."))
    return (
        int(row["task_type"] == task_type),
        int(row["harness"] == harness),
        version,
        int(len(base) == 1),
        base[1] if len(base) > 1 else "",
    )


def _assigned_rows(conn: sqlite3.Connection, task_type: str, harness: str, session_hash: str) -> list[sqlite3.Row]:
    assigned: list[sqlite3.Row] = []
    for artifact_id, rows in _effective_rollout(conn, task_type, harness).items():
        active = sorted(
            (r for r in rows if r["state"] == "active"),
            key=lambda r: _rollout_priority(r, task_type, harness), reverse=True,
        )
        canary = sorted(
            (r for r in rows if r["state"] == "canary" and r["percent"] > 0),
            key=lambda r: _rollout_priority(r, task_type, harness), reverse=True,
        )
        bucket = int(hashlib.sha256(f"{session_hash}:{task_type}:{harness}:{artifact_id}".encode()).hexdigest()[:8], 16) % 100
        chosen = canary[0] if canary and bucket < canary[0]["percent"] else (active[0] if active else None)
        if chosen is not None:
            allowed = json.loads(chosen["harnesses_json"])
            if not allowed or harness in allowed:
                assigned.append(chosen)
    return assigned


def recommend(
    conn: sqlite3.Connection, task: str, *, harness: str,
    task_type: str = "general", session_id: str | None = None,
) -> dict[str, Any]:
    if not task.strip() or not harness.strip() or not task_type.strip():
        raise ValueError("task, harness and task_type are required")
    session_hash = _session_digest(conn, session_id or str(uuid.uuid4()))
    candidates = []
    for row in _assigned_rows(conn, task_type, harness, session_hash):
        topical = _topical_score(task, row, task_type)
        if topical <= 0:
            continue
        evidence = _evidence(conn, row["id"], row["version"], task_type)
        # Evidence is visible, but not an uncalibrated universal quality score.
        candidates.append({
            "id": row["id"], "version": row["version"], "kind": row["kind"],
            "title": row["title"], "author": row["author"],
            "license": row["license"], "source": row["source"],
            "topical_score": round(topical, 3),
            "evidence": evidence, "content_sha256": row["content_sha256"],
            "package_sha256": row["package_sha256"], "support_files": row["support_files"],
            "operational_support_files": row["operational_support_files"],
            "compatible_agents": json.loads(row["compatible_agents_json"]),
        })
    candidates.sort(key=lambda x: (-x["topical_score"], x["id"], x["version"]))
    skills = [x for x in candidates if x["kind"] == "skill"]
    agents = [x for x in candidates if x["kind"] == "agent"]
    selected = skills[:1]
    if agents:
        compatible = [a for a in agents if not selected or not selected[0]["compatible_agents"] or a["id"] in selected[0]["compatible_agents"]]
        if compatible:
            selected = compatible[:1] + selected
    public = lambda items: [{k: v for k, v in x.items() if k not in {"compatible_agents", "content"}} for x in items]
    explanation = (
        "Matched task text and stage metadata; evidence counts are shown separately and are not a quality probability."
        if selected else "No admitted, rollout-eligible content matched this task."
    )
    route_id = str(uuid.uuid4())
    with conn:
        conn.execute(
            """INSERT INTO routes(route_id,task_type,harness,session_hash,selected_json,
            candidates_json,explanation,router_version,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (route_id, task_type, harness, session_hash, _json(public(selected)),
             _json(public(candidates[:5])), explanation, ROUTER_VERSION, _now()),
        )
    return {
        "route_id": route_id, "task_type": task_type, "harness": harness,
        "selected": public(selected),
        "candidates": public(candidates[:5]), "explanation": explanation,
        "router_version": ROUTER_VERSION, "raw_task_stored": False,
    }


def _route(conn: sqlite3.Connection, route_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
    if row is None:
        raise ValueError("route_id not found")
    return row


def _current_assignment(conn: sqlite3.Connection, route: sqlite3.Row) -> set[tuple[str, str]]:
    rows = _assigned_rows(conn, route["task_type"], route["harness"], route["session_hash"])
    return {(row["id"], row["version"]) for row in rows}


def choose_route(
    conn: sqlite3.Connection, route_id: str, action: str, *,
    artifact_id: str | None = None, version: str | None = None,
    actor: str = "user-cli",
) -> dict[str, Any]:
    """Record a user's choice. This does not load contributed instructions."""
    if action not in {"accept", "reject", "switch"}:
        raise ValueError("action must be accept, reject, or switch")
    if not actor.strip():
        raise ValueError("actor is required")
    route = _route(conn, route_id)
    original = json.loads(route["selected_json"])
    candidates = json.loads(route["candidates_json"])
    if action == "reject":
        if artifact_id or version:
            raise ValueError("reject does not take an artifact")
        chosen: list[dict[str, Any]] = []
    elif action == "accept":
        if artifact_id or version:
            raise ValueError("accept uses the original recommendation")
        if not original:
            raise ValueError("there is no recommendation to accept")
        chosen = original
    else:
        if not artifact_id or not version:
            raise ValueError("switch requires artifact_id and version")
        target = next((x for x in candidates if x["id"] == artifact_id and x["version"] == version), None)
        if target is None:
            raise ValueError("switch target was not shown as a candidate")
        if any(x["id"] == artifact_id and x["version"] == version for x in original):
            raise ValueError("target is already selected; use accept")
        if target["kind"] == "skill":
            companion = next((x for x in original if x["kind"] == "agent"), None)
            allowed = conn.execute(
                "SELECT compatible_agents_json FROM artifacts WHERE id=? AND version=?",
                (artifact_id, version),
            ).fetchone()
            agent_ids = json.loads(allowed["compatible_agents_json"])
            chosen = ([companion] if companion and (not agent_ids or companion["id"] in agent_ids) else []) + [target]
        else:
            companion = next((x for x in original if x["kind"] == "skill"), None)
            if companion:
                allowed = conn.execute(
                    "SELECT compatible_agents_json FROM artifacts WHERE id=? AND version=?",
                    (companion["id"], companion["version"]),
                ).fetchone()
                agent_ids = json.loads(allowed["compatible_agents_json"])
                if agent_ids and artifact_id not in agent_ids:
                    raise ValueError("selected skill is incompatible with this agent")
            chosen = [target] + ([companion] if companion else [])
    if action != "reject":
        assigned = _current_assignment(conn, route)
        if any((item["id"], item["version"]) not in assigned for item in chosen):
            raise ValueError("a chosen version is no longer rollout-eligible")
    action_id = str(uuid.uuid4())
    with conn:
        conn.execute(
            "INSERT INTO route_actions(action_id,route_id,action,actor,chosen_json,created_at) VALUES(?,?,?,?,?,?)",
            (action_id, route_id, action, actor.strip(), _json(chosen), _now()),
        )
    return {"action_id": action_id, "route_id": route_id, "action": action, "actor": actor,
            "chosen": chosen, "content_loaded": False}


def load_route(conn: sqlite3.Connection, route_id: str) -> dict[str, Any]:
    """Return chosen content only after an explicit accept or switch action."""
    route = _route(conn, route_id)
    latest = conn.execute(
        "SELECT action_id,action,chosen_json FROM route_actions WHERE route_id=? ORDER BY rowid DESC LIMIT 1",
        (route_id,),
    ).fetchone()
    if latest is None or latest["action"] == "reject":
        raise ValueError("route has not been accepted, or was rejected")
    assigned = _current_assignment(conn, route)
    loaded = []
    for item in json.loads(latest["chosen_json"]):
        if (item["id"], item["version"]) not in assigned:
            raise ValueError("a chosen version is no longer rollout-eligible")
        row, files = _verified_artifact(conn, item["id"], item["version"])
        if row["admission"] != "admitted":
            raise ValueError("chosen content is unavailable")
        loaded.append({
            **item,
            "content": row["content"],
            "package_sha256": row["package_sha256"],
            "support_files": [
                {"path": file["path"], "sha256": file["sha256"], "size": file["size"]}
                for file in files
            ],
        })
    load_id = str(uuid.uuid4())
    with conn:
        conn.execute(
            "INSERT INTO route_loads(load_id,route_id,action_id,loaded_json,created_at) VALUES(?,?,?,?,?)",
            (load_id, route_id, latest["action_id"],
             _json([{"id": x["id"], "version": x["version"]} for x in loaded]), _now()),
        )
    return {"load_id": load_id, "route_id": route_id, "action": latest["action"], "loaded": loaded}



def export_route(conn: sqlite3.Connection, route_id: str, destination: str | Path) -> dict[str, Any]:
    """Materialize an accepted route's complete packages without executing them."""
    dest = Path(destination).expanduser().resolve()
    if dest.exists():
        raise ValueError("export destination already exists")
    loaded = load_route(conn, route_id)
    packages = []
    for item in loaded["loaded"]:
        if not SLUG.fullmatch(item["id"]) or not VERSION.fullmatch(item["version"]):
            raise ValueError("stored artifact identifier is unsafe for export")
        row, files = _verified_artifact(conn, item["id"], item["version"])
        main_name = "SKILL.md" if row["kind"] == "skill" else "agent.md"
        packages.append((item, row, files, main_name))
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".marked-export-", dir=dest.parent) as staging:
        root = Path(staging)
        manifest_items = []
        for item, row, files, main_name in packages:
            folder = root / f"{item['id']}@{item['version']}"
            folder.mkdir()
            (folder / main_name).write_text(row["content"], encoding="utf-8")
            for file in files:
                target = folder / file["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bytes(file["content"]))
            manifest_items.append({
                "id": item["id"], "version": item["version"], "kind": row["kind"],
                "source": row["source"], "license": row["license"],
                "package_sha256": row["package_sha256"],
                "files": 1 + len(files),
            })
        (root / "MARKED_EXPORT.json").write_text(
            json.dumps({
                "route_id": route_id, "load_id": loaded["load_id"],
                "created_at": _now(), "packages": manifest_items,
                "execution": "not executed or installed by MarkeD",
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if dest.exists():
            raise ValueError("export destination appeared during export")
        root.rename(dest)
    return {"route_id": route_id, "load_id": loaded["load_id"],
            "destination": str(dest), "packages": manifest_items,
            "executed": False, "installed": False}


def submit_feedback(conn: sqlite3.Connection, route_id: str, rating: str, reason: str = "unspecified") -> str:
    if rating not in VALID_FEEDBACK or reason not in VALID_FEEDBACK_REASONS:
        raise ValueError("invalid rating or reason category")
    feedback_id = str(uuid.uuid4())
    with conn:
        conn.execute(
            "INSERT INTO feedback(feedback_id,route_id,rating,reason,created_at) VALUES(?,?,?,?,?)",
            (feedback_id, route_id, rating, reason.strip(), _now()),
        )
    return feedback_id


def award_points(conn: sqlite3.Connection, contributor: str, points: int, reason: str, evidence_ref: str) -> str:
    if not contributor.strip() or points <= 0 or not reason.strip() or not evidence_ref.strip():
        raise ValueError("contributor, positive points, reason and evidence_ref are required")
    entry_id = str(uuid.uuid4())
    with conn:
        conn.execute(
            "INSERT INTO points_ledger(entry_id,contributor,points,reason,evidence_ref,created_at) VALUES(?,?,?,?,?,?)",
            (entry_id, contributor.strip(), points, reason.strip(), evidence_ref.strip(), _now()),
        )
    return entry_id


def local_report(conn: sqlite3.Connection) -> dict[str, Any]:
    counts = {}
    for table in ("artifacts", "artifact_files", "artifact_reviews", "rollouts", "routes", "route_actions", "route_loads", "feedback", "feedback_consent", "hook_turns", "natural_feedback", "natural_feedback_audit", "evaluations", "points_ledger"):
        counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    counts["raw_task_stored"] = False
    counts["points_redeemable"] = False
    return counts
