"""Schema creation and one-time import of the legacy on-disk graphs."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from backend.config import settings
from backend.db import connection, dao

logger = logging.getLogger(__name__)

BOOTSTRAP_KEY = "bootstrap_v1"
SCHEMA_V2_KEY = "schema_v2_owners"
ADMIN_ID = "user:admin"
SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"


def humanise(stem: str) -> str:
    """'RefundPolicy_jdm' -> 'Refund Policy'."""
    name = re.sub(r"_jdm$", "", stem)
    name = name.replace("_", " ")
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    return re.sub(r"\s+", " ", name).strip() or stem


async def apply_schema() -> None:
    await connection.script(SCHEMA_FILE.read_text(encoding="utf-8"))


async def _column_names(table: str) -> set[str]:
    async with connection.read() as conn:
        cur = await conn.execute(f"PRAGMA table_info({table})")
        return {row["name"] for row in await cur.fetchall()}


async def migrate_owners() -> None:
    """Add owner columns to a database created before multi-user support.

    Everything that predates ownership belongs to the admin, which is what the
    DEFAULT backfills. `graphs` needs a full table rebuild rather than a plain
    ADD COLUMN because its `slug` carried a *global* UNIQUE constraint, and
    uniqueness must now be per owner - otherwise the second guest to save a
    "Refund Policy" collides with the first.
    """
    if await dao.get_meta(SCHEMA_V2_KEY):
        return

    if "owner_id" not in await _column_names("chat_threads"):
        async with connection.write() as conn:
            await conn.execute(
                "ALTER TABLE chat_threads ADD COLUMN owner_id TEXT NOT NULL"
                f" DEFAULT '{ADMIN_ID}'"
            )

    if "owner_id" not in await _column_names("graphs"):
        # SQLite cannot drop a column constraint, so rebuild the table. Foreign
        # keys are disabled for the swap: `graph_versions`, `test_cases` and
        # `test_runs` all cascade from graphs(id), and dropping the old table
        # with them enforced would delete every version and test in the file.
        await connection.script(
            """
            PRAGMA foreign_keys=OFF;
            BEGIN;
            CREATE TABLE graphs_v2 (
              id              TEXT PRIMARY KEY,
              owner_id        TEXT NOT NULL DEFAULT 'user:admin',
              name            TEXT NOT NULL,
              slug            TEXT NOT NULL,
              description     TEXT NOT NULL DEFAULT '',
              current_version INTEGER NOT NULL DEFAULT 0,
              created_at      TEXT NOT NULL,
              updated_at      TEXT NOT NULL,
              archived_at     TEXT
            );
            INSERT INTO graphs_v2 (id, owner_id, name, slug, description,
                                   current_version, created_at, updated_at, archived_at)
              SELECT id, 'user:admin', name, slug, description,
                     current_version, created_at, updated_at, archived_at
                FROM graphs;
            DROP TABLE graphs;
            ALTER TABLE graphs_v2 RENAME TO graphs;
            CREATE INDEX IF NOT EXISTS idx_graphs_owner
              ON graphs(owner_id, updated_at DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_graphs_owner_slug
              ON graphs(owner_id, slug);
            COMMIT;
            PRAGMA foreign_keys=ON;
            """
        )
        logger.info("Migrated `graphs` to per-owner ownership.")

    # Deferred from schema.sql, which runs before the column is guaranteed.
    await connection.script(
        "CREATE INDEX IF NOT EXISTS idx_graphs_owner"
        " ON graphs(owner_id, updated_at DESC);"
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_graphs_owner_slug"
        " ON graphs(owner_id, slug);"
    )
    await dao.set_meta(SCHEMA_V2_KEY, "done")


async def seed_admin() -> None:
    """Create or update the admin from the environment.

    The password is never stored in the repository: it comes from
    ADMIN_PASSWORD in backend/.env. With it unset, no admin row exists and
    login is refused, leaving the guest flow fully usable.
    """
    if not settings.admin_password:
        logger.info("ADMIN_PASSWORD is not set; admin login is disabled.")
        return
    await dao.upsert_user(ADMIN_ID, settings.admin_email, settings.admin_password)
    logger.info("Admin user ready (%s).", settings.admin_email)


async def reset_stale_runs() -> int:
    """Release conversations pinned to `running` by a process that died mid-turn.

    The run itself is an in-memory task and cannot outlive the process, so on a fresh boot
    every `running` row is stale by definition. Left alone the row is permanent: the client
    reads the status on load, disables the composer and offers a Stop that finds no task to
    cancel, so the conversation can never be used again.
    """
    released = await dao.reset_running_threads()
    if released:
        logger.info(
            "Released %s conversation(s) left mid-run by a previous process.", released
        )
    return released


async def sweep_guests() -> int:
    """Delete guest data untouched for `guest_ttl_days`; cascades to children."""
    removed = await dao.delete_stale_guests(settings.guest_ttl_days)
    if removed:
        logger.info("Swept %s stale guest graph(s).", removed)
    return removed


async def import_legacy_graphs() -> int:
    """Import backend/jdm_graphs/*.json and their matching test suites."""
    graphs_dir = Path(settings.legacy_graphs_dir)
    tests_dir = Path(settings.legacy_tests_dir)
    if not graphs_dir.is_dir():
        return 0

    imported = 0
    for path in sorted(graphs_dir.glob("*_jdm.json")):
        name = humanise(path.stem)
        if await dao.find_graph_by_name(name, owner=ADMIN_ID):
            continue

        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping %s: %s", path.name, exc)
            continue

        graph = await dao.create_graph(
            owner=ADMIN_ID,
            name=name,
            content=content,
            description=f"Imported from {path.name}",
            author="import",
            message=f"Imported from {path.name}",
        )
        imported += 1

        test_file = tests_dir / f"{path.stem.removesuffix('_jdm')}_tests.json"
        tests: list = []
        if test_file.is_file():
            try:
                tests = json.loads(test_file.read_text(encoding="utf-8"))
                await dao.replace_tests(graph["id"], tests)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not import tests for %s: %s", name, exc)

        _warn_if_failing(name, content, tests)

    return imported


def _warn_if_failing(name: str, content: dict, tests: list) -> None:
    """Log up front when an imported graph does not satisfy its own suite.

    The shipped examples all pass now, so this should stay quiet. It is kept because an
    example that fails its own tests teaches the model to write broken graphs - the two
    originals did exactly that, and it went unnoticed for as long as the evaluator
    reported any run that did not raise as a success.
    """
    if not tests:
        return
    try:
        from backend.tools.zen_evaluator import run_test_suite

        summary = run_test_suite(content, tests, trace=False)["summary"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Imported %r but could not evaluate it: %s", name, exc)
        return

    if summary["failed"] or summary["errored"]:
        logger.warning(
            "Imported %r with failing tests: %s/%s passed (%s failed, %s errored). "
            "These graphs do not produce their declared output fields.",
            name, summary["passed"], summary["total"],
            summary["failed"], summary["errored"],
        )


async def bootstrap() -> None:
    await apply_schema()
    await migrate_owners()
    # Before anything can be served: a request that arrives first would otherwise read a
    # status describing a task from a process that no longer exists.
    await reset_stale_runs()
    await seed_admin()
    # Runs on every boot, not just the first. `import_legacy_graphs` already skips any
    # graph the admin owns by that name, so this is idempotent - and gating it behind a
    # one-shot flag meant an example added later never reached an existing install.
    count = await import_legacy_graphs()
    await dao.set_meta(BOOTSTRAP_KEY, "done")
    if count:
        logger.info("Imported %s example graph(s).", count)
    await sweep_guests()
