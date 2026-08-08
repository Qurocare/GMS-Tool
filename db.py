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
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "gms.db"

STAGES = ["Research", "New Lead", "Contacted", "Interested", "Meeting Scheduled", "Demo Scheduled", "Demo Completed", "Converted", "Verification", "Agreement Sent", "Onboarding", "Active Provider", "Lost"]
PROVIDER_TYPES = ["Organisation", "Doctor", "Nurse", "Physiotherapist", "Phlebotomist", "Lab", "Other"]
SOURCES = ["Meta Ads", "Google Ads", "Referral", "Cold Outreach", "Website", "LinkedIn", "Other"]
PRIORITIES = ["High", "Medium", "Low"]
FEEDBACK_CATEGORIES = ["Bug", "Feature Request", "UI / UX", "Onboarding", "Pricing", "Training", "Other"]
FEEDBACK_STATUSES = ["New", "Reviewed", "Sent to Tech", "In Progress", "Resolved", "Deferred"]

ROLE_PSM = "PSM"
ROLE_MO = "Medical Officer"
ROLE_ML = "Market Lead"
ROLE_BDM = "Business Development Manager"
ROLE_PGA = "Product Growth Associate"
ROLE_CEO = "CEO/Admin"
ROLE_DISPLAY = "Display only"
ROLES = [ROLE_PSM, ROLE_MO, ROLE_ML, ROLE_BDM, ROLE_PGA, ROLE_CEO, ROLE_DISPLAY]
DEFAULT_TEAM = [
    ("Halifa", ROLE_ML),
    ("Rahul", ROLE_BDM),
    ("Dr. Asinsha", ROLE_MO),
    ("Reshma", ROLE_PSM),
    ("Simoy", ROLE_PGA),
]


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
            """
        )
        # Preserve existing pilot data while making ownership auditable.
        for column, definition in [
            ("created_by_user_id", "INTEGER"), ("assigned_to_user_id", "INTEGER"),
            ("updated_by_user_id", "INTEGER"), ("updated_at", "TEXT"),
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


def user_label(user: dict) -> str:
    return f"{user['name']}, {user['role']}"
