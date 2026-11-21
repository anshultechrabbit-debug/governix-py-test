"""Wipe all application data but KEEP the existing master admin accounts.

Kept:
  - every user with role = 'master_admin' (same id, email, password, 2FA secret)
  - those master admins' MFA recovery codes
  - platform_settings (SMTP / platform config set by the master admin)
  - alembic_version (migration state)

Deleted: every other row in every other table in the public schema
(organizations, branches, departments, all non-master users, policies, documents,
chunks, AI usage/conversations, tickets, support staff, notifications, audit log,
refresh tokens, queues, caches ...). Everything runs in ONE transaction: if any
step fails, nothing is changed.

Usage (run on the server, from the backend folder, with the server's .env):
    python reset_keep_master_admin.py                # dry run: shows what would go
    python reset_keep_master_admin.py --yes          # actually wipe the database
    python reset_keep_master_admin.py --yes --wipe-files   # also delete uploaded files

Take a backup first:
    sudo -u postgres pg_dump -Fc governix > /backup/governix-before-reset-$(date +%F-%H%M).dump
"""

import argparse
import shutil
import sys
from pathlib import Path

from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import create_db_engine

KEEP_TABLES = {"alembic_version"}  # never touched
STORAGE_SUBDIRS = ["originals", "extracted", "ocr", "pages", "temporary"]


def list_tables(conn) -> list[str]:
    rows = conn.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
    ).scalars()
    return [t for t in rows if t not in KEEP_TABLES]


def row_counts(conn, tables: list[str]) -> dict[str, int]:
    return {t: conn.execute(text(f'SELECT count(*) FROM "{t}"')).scalar_one() for t in tables}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="actually delete (default is a dry run)")
    parser.add_argument("--wipe-files", action="store_true", help="also empty the uploaded-file storage folders")
    args = parser.parse_args()

    settings = get_settings()
    engine = create_db_engine(settings)
    db_name = engine.url.database
    print(f"Database: {engine.url.host}:{engine.url.port}/{db_name}")

    with engine.connect() as conn:
        admins = conn.execute(
            text("SELECT id, email, full_name FROM users WHERE role = 'master_admin' ORDER BY created_at")
        ).all()
        if not admins:
            print("ABORT: no master_admin user found. Refusing to wipe (you would be locked out).")
            return 1
        print(f"\nMaster admins that will be KEPT ({len(admins)}):")
        for a in admins:
            print(f"  - {a.email}  ({a.full_name})")

        tables = list_tables(conn)
        counts = row_counts(conn, tables)
        kept = {
            "users": len(admins),
            "platform_settings": counts.get("platform_settings", 0),
            "mfa_recovery_codes": conn.execute(text(
                "SELECT count(*) FROM mfa_recovery_codes WHERE user_id IN "
                "(SELECT id FROM users WHERE role = 'master_admin')"
            )).scalar_one(),
        }

    print("\nRows that will be DELETED:")
    for t, n in counts.items():
        n -= kept.get(t, 0)
        if n:
            print(f"  {t:<32} {n}")

    if not args.yes:
        print("\nDry run only. Re-run with --yes to delete.")
        return 0

    with engine.begin() as conn:  # one transaction: all or nothing
        conn.execute(text(
            "CREATE TEMP TABLE _keep_users ON COMMIT DROP AS "
            "SELECT * FROM users WHERE role = 'master_admin'"
        ))
        conn.execute(text(
            "CREATE TEMP TABLE _keep_mfa ON COMMIT DROP AS "
            "SELECT * FROM mfa_recovery_codes WHERE user_id IN (SELECT id FROM _keep_users)"
        ))
        conn.execute(text("CREATE TEMP TABLE _keep_settings ON COMMIT DROP AS SELECT * FROM platform_settings"))
        conn.execute(text(
            "UPDATE _keep_settings SET updated_by_id = NULL "
            "WHERE updated_by_id IS NOT NULL AND updated_by_id NOT IN (SELECT id FROM _keep_users)"
        ))

        quoted = ", ".join(f'"{t}"' for t in tables)
        conn.execute(text(f"TRUNCATE TABLE {quoted} CASCADE"))

        conn.execute(text("INSERT INTO users SELECT * FROM _keep_users"))
        conn.execute(text("INSERT INTO mfa_recovery_codes SELECT * FROM _keep_mfa"))
        conn.execute(text("INSERT INTO platform_settings SELECT * FROM _keep_settings"))
        # Force every kept admin to sign in again (old access tokens become invalid).
        conn.execute(text("UPDATE users SET token_version = token_version + 1"))

        left = conn.execute(text("SELECT count(*) FROM users")).scalar_one()
        if left != len(admins):
            raise RuntimeError(f"expected {len(admins)} users after reset, found {left}; rolling back")

    print(f"\n✓ Database wiped. {len(admins)} master admin account(s) kept.")

    if args.wipe_files:
        storage = Path(settings.LOCAL_STORAGE_PATH).resolve()
        print(f"Emptying storage folders under {storage} ...")
        for sub in STORAGE_SUBDIRS:
            target = storage / sub
            if target.is_dir():
                for item in target.iterdir():
                    shutil.rmtree(item) if item.is_dir() else item.unlink()
        print("✓ Uploaded files removed.")

    print("\nRestart the backend (and workers) so in-memory caches are cleared.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
