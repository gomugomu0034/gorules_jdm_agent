"""Recover what can be recovered from conversations that predate the corpus.

Partial by nature, and worth being precise about why. Three things are recoverable from
the studio's own database:

  the requirement   from the LangGraph checkpoint, which holds the messages
  the clarifications from the `interrupt` events, paired with the replies that followed
  the outcome       from the `done` and `error` events

And one thing is not: the **samples**. A training example is a system prompt, a request
and a completion, and the system prompt was never stored anywhere - which is precisely
the gap `backend/corpus` exists to close. Reconstructing samples without it would produce
rows where the first user turn reads as the instruction, which is worse than no rows.

Everything written here is marked `source='backfill'` so it can be kept out of a clean
training split, and re-running is safe: a thread already imported is skipped.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from backend.config import settings
from backend.corpus import store

logger = logging.getLogger(__name__)


@dataclass
class Imported:
    threads: int = 0
    runs: int = 0
    requirements: int = 0
    clarifications: int = 0
    skipped: int = 0


def _studio(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path or settings.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _already_imported(thread_id: str) -> bool:
    row = store._connect().execute(
        "SELECT 1 FROM interactions WHERE thread_id = ? LIMIT 1", (thread_id,)
    ).fetchone()
    return row is not None


def _messages(conn: sqlite3.Connection, thread_id: str) -> list[tuple[str, str]]:
    """The conversation as (role, content), newest checkpoint first.

    LangGraph owns this format, so it is read with LangGraph's own deserialiser rather
    than by unpicking the blob. A checkpoint that will not load is skipped: this is a
    best-effort recovery of old data, not something to fail a run over.
    """
    try:
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    except ImportError:
        return []

    serde = JsonPlusSerializer()
    row = conn.execute(
        "SELECT type, checkpoint FROM checkpoints WHERE thread_id = ?"
        " ORDER BY checkpoint_id DESC LIMIT 1", (thread_id,)
    ).fetchone()
    if row is None:
        return []
    try:
        state = serde.loads_typed((row["type"], row["checkpoint"]))
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(state, dict):
        # A truncated or foreign blob does not always raise - it can deserialise to a bare
        # int, which then fails on the first attribute access several lines further on.
        return []

    out = []
    for message in (state.get("channel_values") or {}).get("messages") or []:
        role = {"human": "user", "ai": "assistant"}.get(
            getattr(message, "type", ""), getattr(message, "type", "") or "user")
        content = getattr(message, "content", "")
        if isinstance(content, str) and content.strip():
            out.append((role, content))
    return out


# Not every HumanMessage in a checkpoint came from a human. The approval gates put words
# in the user's mouth so the model sees a coherent conversation, and those two shapes are
# the agent's own, not the person's. Recording them verbatim as what someone said would be
# quietly wrong; the second one at least carries the real answer inside it.
_APPROVED = "I approve the assumptions. Please proceed to build the plan."
_CHANGES = re.compile(
    r"^The user requested these specific changes:\s*'(?P<said>.*?)'\s*Please update",
    re.DOTALL,
)


def _as_the_person_put_it(reply: str) -> tuple[str, bool]:
    """The reply, and whether the agent wrote it rather than the person.

    Returns the user's own words where they can be recovered - the change gate wraps them
    in a sentence but keeps them intact.
    """
    if reply.strip() == _APPROVED:
        return "(approved)", True
    match = _CHANGES.match(reply.strip())
    if match:
        return match.group("said").strip(), True
    return reply, False


def _events(conn: sqlite3.Connection, thread_id: str) -> list[dict]:
    out = []
    for row in conn.execute(
        "SELECT type, payload FROM chat_events WHERE thread_id = ? ORDER BY seq",
        (thread_id,),
    ):
        try:
            out.append(json.loads(row["payload"]))
        except json.JSONDecodeError:
            continue
    return out


def backfill(*, studio_path: str | None = None, dry_run: bool = False) -> Imported:
    """Import every thread that is not already in the corpus."""
    tally = Imported()
    if not store.enabled():
        return tally

    conn = _studio(studio_path)
    try:
        threads = list(conn.execute(
            "SELECT id, graph_id, owner_id, created_at FROM chat_threads ORDER BY created_at"
        ))
    except sqlite3.OperationalError:
        logger.warning("No chat_threads table at %s; nothing to import.",
                       studio_path or settings.db_path)
        return tally

    for thread in threads:
        thread_id = thread["id"]
        events = _events(conn, thread_id)
        if not events:
            # A thread nobody ever sent anything to. There are a lot of these - the studio
            # mints one per visit - and importing them would be importing nothing.
            continue
        if _already_imported(thread_id):
            tally.skipped += 1
            continue

        messages = _messages(conn, thread_id)
        requirement = next((c for role, c in messages if role == "user"), None)
        replies = [c for role, c in messages if role == "user"][1:]
        questions = [e for e in events if e.get("type") == "interrupt"]
        outcome = next((e.get("status") for e in reversed(events)
                        if e.get("type") == "done"), None)
        run_id = next((e.get("run_id") for e in events
                       if e.get("type") == "run_started"), None)

        if dry_run:
            tally.threads += 1
            tally.requirements += bool(requirement)
            tally.clarifications += min(len(questions), len(replies))
            continue

        if run_id:
            store.start_run(run_id, thread_id=thread_id, graph_id=thread["graph_id"],
                            owner_hash=store_owner(thread["owner_id"]), source="backfill")
            store.finish_run(run_id, outcome=outcome or "error")
            tally.runs += 1

        if requirement:
            store.record_interaction(
                kind="requirement", thread_id=thread_id, graph_id=thread["graph_id"],
                response=requirement, run_id=run_id,
                detail={"backfilled": True, "from": "checkpoint"},
            )
            tally.requirements += 1

        # Paired by position: the reply to the first question is the next thing the person
        # typed. Approximate, because the studio recorded no link between them - which is
        # the whole reason `interactions` pairs them at the time now.
        for question, reply in zip(questions, replies):
            said, synthesized = _as_the_person_put_it(reply)
            store.record_interaction(
                kind="clarification", thread_id=thread_id,
                prompt=question.get("prompt", ""),
                options=list(question.get("options") or []),
                response=said, run_id=run_id,
                detail={"backfilled": True, "paired_by": "position",
                        "phrasing": "agent" if synthesized else "person"},
            )
            tally.clarifications += 1

        tally.threads += 1

    conn.close()
    return tally


def store_owner(owner: str | None) -> str:
    from backend.corpus.context import hash_owner

    return hash_owner(owner)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.corpus.backfill",
        description="Recover past conversations into the corpus.",
        epilog=(
            "Run from the repository root. Safe to re-run: threads already imported are\n"
            "skipped.\n\n"
            "Samples are NOT recovered. A training example needs the system prompt it was\n"
            "produced under, and that was never stored - which is what backend/corpus was\n"
            "added to fix. What comes back is the human side: requirements, the questions\n"
            "the agent asked, and how each conversation ended.\n\n"
            "Everything is marked source='backfill'."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--studio", help=f"path to studio.db (default {settings.db_path})")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not store.enabled():
        print("Capture is off (CORPUS_CAPTURE), so there is nowhere to import into.",
              file=sys.stderr)
        return 1
    if args.studio and not Path(args.studio).exists():
        print(f"No such database: {args.studio}", file=sys.stderr)
        return 1

    tally = backfill(studio_path=args.studio, dry_run=args.dry_run)
    verb = "would import" if args.dry_run else "imported"
    print(f"{verb} {tally.threads} thread(s): {tally.runs} run(s), "
          f"{tally.requirements} requirement(s), {tally.clarifications} clarification(s)")
    if tally.skipped:
        print(f"skipped {tally.skipped} already in the corpus")
    if not args.dry_run and tally.threads:
        print("Marked source='backfill'. Samples were not recovered - the system prompts "
              "they were produced under were never stored.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
