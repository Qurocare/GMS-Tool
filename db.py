"""SQLite storage for the local, role-aware Qurocare GMS pilot.

Before internet deployment, migrate this module to Supabase/Postgres and
replace the local password flow with Supabase Auth + Row Level Security.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import struct
from datetime import timedelta
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "gms.db"

STAGES = [
    "Provider Identified", "Provider Qualified", "Contacted for Demo Scheduling",
    "Demo Scheduled", "Demo Completed", "Converted/ACTIVE Provider",
    "Provider Active Onboarded (PAO)", "Provider Active Not-Onboarded (PANO)",
    "Provider Verified", "Provider Not Verified", "Lost",
]
PROVIDER_TYPES = ["Organisation", "Doctor", "Nurse", "Physiotherapist", "Phlebotomist", "Lab", "Other"]
SOURCES = ["Meta Ads", "Google Ads", "Referral", "Cold Outreach", "Website", "LinkedIn", "Other"]
PRIORITIES = ["High", "Medium", "Low"]
FEEDBACK_CATEGORIES = ["Bug", "Feature Request", "UI / UX", "Onboarding", "Pricing", "Training", "Other"]
FEEDBACK_STATUSES = ["New", "Reviewed", "Sent to Tech", "In Progress", "Resolved", "Deferred"]

# Sub-stage values, stored alongside (not instead of) the main stage. Some
# stages carry a checkbox/choice that changes ownership routing without
# being a stage of its own.
SUB_STAGE_DEMO_SCHEDULING = "Demo Scheduling"
SUB_STAGE_DEMO_NOT_SCHEDULING = "Demo not Scheduling"
SUB_STAGE_ONBOARDING_REQUESTED = "Onboarding requested"
SUB_STAGE_VERIFICATION_REQUESTED = "Verification requested"

ROLE_PSM = "Provider Success"
ROLE_MO = "Provider Verification"
ROLE_ML = "Market Intelligence"
ROLE_BDM = "Provider Partnerships"
ROLE_PGA = "Growth Associate"
ROLE_CEO = "CEO/Admin"
ROLE_DISPLAY = "Display only"
ROLES = [ROLE_PSM, ROLE_MO, ROLE_ML, ROLE_BDM, ROLE_PGA, ROLE_CEO, ROLE_DISPLAY]
# Kept for the "team member" dropdown when creating an account. No personal
# names are stored here on purpose - the person creating the account types
# the real name in, this just seeds sensible role choices.
DEFAULT_TEAM = [
    ("Market Intelligence", ROLE_ML),
    ("Provider Partnerships", ROLE_BDM),
    ("Provider Verification", ROLE_MO),
    ("Provider Success", ROLE_PSM),
    ("Growth Associate", ROLE_PGA),
]

# Best-effort mapping from the old workflow's role/stage names to the new
# ones, applied once on startup so existing pilot data keeps working under
# the rebuilt workflow rather than being orphaned by the rename. Stage
# mapping is necessarily approximate where the old stage had no exact new
# equivalent (e.g. the old in-progress "Verification"/"Onboarding" stages,
# whose outcome wasn't recorded) - those land on a safe, non-committal stage
# so the team can quickly re-triage the handful of affected records rather
# than losing them.
_ROLE_RENAMES = {
    "Market Lead": ROLE_ML,
    "Business Development Manager": ROLE_BDM,
    "Medical Officer": ROLE_MO,
    "PSM": ROLE_PSM,
    "Product Growth Associate": ROLE_PGA,
}
_STAGE_RENAMES = {
    "Research": "Provider Identified",
    "New Lead": "Provider Identified",
    "Interested": "Provider Qualified",
    "Contacted": "Contacted for Demo Scheduling",
    "Meeting Scheduled": "Contacted for Demo Scheduling",
    "Converted": "Converted/ACTIVE Provider",
    "Verification": "Converted/ACTIVE Provider",
    "Agreement Sent": "Converted/ACTIVE Provider",
    "Onboarding": "Provider Active Not-Onboarded (PANO)",
    "Active Provider": "Provider Active Not-Onboarded (PANO)",
}


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 210_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 210_000)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


def initialise() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name TEXT NOT NULL,
                provider_type TEXT NOT NULL,
                contact_name TEXT,
                phone TEXT,
                email TEXT,
                source TEXT,
                assigned_to TEXT NOT NULL,
                stage TEXT NOT NULL,
                priority TEXT NOT NULL,
                date_added TEXT NOT NULL,
                last_contact TEXT,
                next_follow_up TEXT,
                remarks TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                activity_date TEXT NOT NULL,
                team_member TEXT NOT NULL,
                calls INTEGER NOT NULL DEFAULT 0,
                meetings INTEGER NOT NULL DEFAULT 0,
                demos INTEGER NOT NULL DEFAULT 0,
                follow_ups INTEGER NOT NULL DEFAULT 0,
                new_leads INTEGER NOT NULL DEFAULT 0,
                notes TEXT
            );
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_name TEXT NOT NULL,
                submitted_by TEXT NOT NULL,
                feedback_date TEXT NOT NULL,
                category TEXT NOT NULL,
                priority TEXT NOT NULL,
                description TEXT NOT NULL,
                assigned_to TEXT,
                status TEXT NOT NULL,
                release_version TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                role TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS role_activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                activity_date TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                notes TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS handoff_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_id INTEGER NOT NULL,
                submitted_by_user_id INTEGER,
                reviewed_by_user_id INTEGER NOT NULL,
                handoff_type TEXT NOT NULL,
                outcome TEXT NOT NULL,
                review_date TEXT NOT NULL,
                evidence TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(provider_id) REFERENCES providers(id),
                FOREIGN KEY(submitted_by_user_id) REFERENCES users(id),
                FOREIGN KEY(reviewed_by_user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS stage_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_id INTEGER NOT NULL,
                from_stage TEXT,
                to_stage TEXT NOT NULL,
                sub_stage TEXT,
                changed_by_user_id INTEGER,
                changed_by_role TEXT,
                changed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(provider_id) REFERENCES providers(id),
                FOREIGN KEY(changed_by_user_id) REFERENCES users(id)
            );
            """
        )
        # Preserve existing pilot data while making ownership auditable.
        for column, definition in [
            ("created_by_user_id", "INTEGER"), ("assigned_to_user_id", "INTEGER"),
            ("updated_by_user_id", "INTEGER"), ("updated_at", "TEXT"),
            ("sub_stage", "TEXT"), ("org_contact_number", "TEXT"),
        ]:
            _ensure_column(conn, "providers", column, definition)
        for column, definition in [("submitted_by_user_id", "INTEGER"), ("updated_by_user_id", "INTEGER"), ("updated_at", "TEXT")]:
            _ensure_column(conn, "feedback", column, definition)
        # Dummy/demo provider rows (CareBridge, Dr. Nikhil Krishnan, WellMove) are
        # intentionally no longer seeded, and any left over from earlier pilot
        # runs are cleaned up so they don't linger in the pipeline or reports.
        conn.execute(
            """DELETE FROM providers WHERE company_name IN
            ('CareBridge Home Nursing', 'Dr. Nikhil Krishnan', 'WellMove Physiotherapy')
            AND assigned_to IN ('Growth Lead', 'Growth Executive 1', 'Growth Executive 2')"""
        )
        _repair_corrupted_owner_ids(conn)
        _migrate_role_and_stage_names(conn)
        # Orphaned stage_history rows (from providers deleted before this fix
        # started cleaning them up too) would otherwise keep silently
        # counting toward LQR/LCR/OSR/VCR forever. One-time cleanup, safe to
        # run repeatedly.
        conn.execute("DELETE FROM stage_history WHERE provider_id NOT IN (SELECT id FROM providers)")


def _migrate_role_and_stage_names(conn: sqlite3.Connection) -> None:
    """One-time rename from the old role/stage vocabulary to the new one.

    Role names map cleanly (old role -> new role, same person/account). Stage
    names are only an approximate mapping where the old stage had no exact
    equivalent under the new, more detailed workflow - see _STAGE_RENAMES.
    Safe to run repeatedly: once renamed, old values no longer exist to match.
    """
    for old_role, new_role in _ROLE_RENAMES.items():
        conn.execute("UPDATE users SET role=? WHERE role=?", (new_role, old_role))
    for old_stage, new_stage in _STAGE_RENAMES.items():
        conn.execute("UPDATE providers SET stage=? WHERE stage=?", (new_stage, old_stage))


def _repair_corrupted_owner_ids(conn: sqlite3.Connection) -> None:
    """One-time repair for a past bug that corrupted ownership.

    A previous version of the workflow logic could pass a pandas numpy
    integer straight into a SQLite update, which silently stored
    assigned_to_user_id as an 8-byte BLOB instead of an INTEGER. Once that
    happened, "WHERE assigned_to_user_id = ?" (used by My provider leads)
    could no longer match that row, so the provider appeared to vanish from
    its owner's page. This decodes any such BLOB back into a normal integer
    so previously affected records become visible again.
    """
    rows = conn.execute(
        "SELECT id, assigned_to_user_id FROM providers WHERE typeof(assigned_to_user_id)='blob'"
    ).fetchall()
    for row in rows:
        blob = row["assigned_to_user_id"]
        try:
            fixed_id = struct.unpack("<q", blob)[0]
        except (struct.error, TypeError):
            continue
        conn.execute("UPDATE providers SET assigned_to_user_id=? WHERE id=?", (fixed_id, row["id"]))


def frame(query: str, params: tuple = ()) -> pd.DataFrame:
    with connect() as conn:
        return pd.read_sql_query(query, conn, params=params)


def execute(query: str, params: tuple = ()) -> None:
    with connect() as conn:
        conn.execute(query, params)
        conn.commit()


def log_stage_change(provider_id: int, from_stage: str | None, to_stage: str, sub_stage: str, user: dict) -> None:
    """Record a provider stage/sub-stage change in the audit log.

    This is what makes every KPI (M, Q, C, RO, O, RV, V) countable
    automatically instead of manually typed in - each is just a count of
    matching rows here for the selected date range.
    """
    execute(
        """INSERT INTO stage_history (provider_id, from_stage, to_stage, sub_stage, changed_by_user_id, changed_by_role)
        VALUES (?, ?, ?, ?, ?, ?)""",
        (provider_id, from_stage, to_stage, sub_stage, user["id"], user["role"]),
    )


def count_stage_events(start_date, end_date, to_stage: str | None = None, sub_stage_contains: str | None = None) -> int:
    """Count stage_history rows in a date range (inclusive), optionally
    filtered by the resulting stage and/or a sub-stage flag being present."""
    conditions = ["changed_at >= ? AND changed_at < ?"]
    params: list = [start_date.isoformat(), (end_date + timedelta(days=1)).isoformat()]
    if to_stage:
        conditions.append("to_stage = ?")
        params.append(to_stage)
    if sub_stage_contains:
        conditions.append("sub_stage LIKE ?")
        params.append(f"%{sub_stage_contains}%")
    result = frame(f"SELECT COUNT(*) AS c FROM stage_history WHERE {' AND '.join(conditions)}", tuple(params))
    return int(result.iloc[0].c) if not result.empty else 0


def stage_log_for_user(user_id: int, start_date=None, end_date=None) -> pd.DataFrame:
    """Every stage change one person made, newest first, with the
    organization name joined in. Leave start_date/end_date as None for the
    person's full history."""
    conditions = ["sh.changed_by_user_id=?"]
    params: list = [user_id]
    if start_date:
        conditions.append("sh.changed_at >= ?")
        params.append(start_date.isoformat())
    if end_date:
        conditions.append("sh.changed_at < ?")
        params.append((end_date + timedelta(days=1)).isoformat())
    return frame(
        f"""SELECT sh.changed_at, p.company_name, sh.from_stage, sh.to_stage, sh.sub_stage
           FROM stage_history sh JOIN providers p ON p.id = sh.provider_id
           WHERE {' AND '.join(conditions)}
           ORDER BY sh.changed_at DESC""",
        tuple(params),
    )


def export_all_data() -> dict:
    """Return every row from every table as plain dicts, ready for JSON.

    Streamlit Community Cloud does not guarantee local disk persists (the
    platform may reset the filesystem at any time - see the note on the
    login page). Until this moves to a real persistent database, this is
    the safety net: a full manual backup someone can download and, if a
    reset happens, reload with import_all_data below.
    """
    with connect() as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        return {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()] for table in tables}


def import_all_data(data: dict) -> None:
    """Wipe every table included in a backup and reload it exactly as
    exported. Destructive by design - this is disaster recovery, not a
    routine operation. Tables not present in the current schema (e.g. from
    an older backup) are skipped rather than failing the whole restore.
    """
    with connect() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        for table, rows in data.items():
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                continue
            conn.execute(f"DELETE FROM {table}")
            if not rows:
                continue
            columns = list(rows[0].keys())
            column_list = ",".join(columns)
            placeholders = ",".join("?" for _ in columns)
            conn.executemany(
                f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})",
                [tuple(row.get(col) for col in columns) for row in rows],
            )
        conn.commit()


def user_count() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(name: str, role: str, email: str, password: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO users (name, role, email, password_hash) VALUES (?, ?, ?, ?)", (name, role, email.strip().lower(), hash_password(password)))
        conn.commit()


def authenticate(email: str, password: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT id, name, role, email, password_hash, is_active FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
    if row and row["is_active"] and verify_password(password, row["password_hash"]):
        return {"id": row["id"], "name": row["name"], "role": row["role"], "email": row["email"]}
    return None


def users_frame() -> pd.DataFrame:
    return frame("SELECT id, name, role, email, is_active, created_at FROM users ORDER BY id")
