"""Turn what was recorded into judgements you can filter a training set on.

Nothing here observes anything new. Every label is derived from what the deterministic
tools and the human already said, which is the point: the linter and the test engine are a
reward model that needs no annotation budget, and a sample that parsed, linted clean and
passed its own suite is objectively better training data than one that did not.

Labels live apart from the facts and carry the version of the scorer that wrote them,
because standards move. Re-scoring is a re-run rather than a migration, and an old label
stays interpretable because it says who made it.

Read-only with respect to everything except `labels`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from typing import Any

from backend.corpus import store

logger = logging.getLogger(__name__)

# Bump when the meaning of a label changes, so previously written ones are not silently
# reinterpreted. Adding a new label does not need a bump; changing how one is computed does.
SCORER_VERSION = "1"

# What a sample has to clear to be worth training on. Deliberately conservative: an
# oversized training set of mediocre examples is worse than a small clean one.
QUALITY_LABELS = ("parsed_ok", "lint_clean", "tests_passed")


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, args))


def _json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def score_all(*, rescore: bool = False) -> dict[str, int]:
    """Compute every label the corpus can support. Returns what was written.

    `rescore` clears this scorer's previous output first, which is what you want after
    changing how a label is computed; without it, existing labels are left alone and only
    samples scored for the first time are written.
    """
    if not store.enabled():
        return {}

    conn = store._connect()
    written: dict[str, int] = {}

    with store._lock:
        if rescore:
            conn.execute("DELETE FROM labels WHERE scorer_version = ?", (SCORER_VERSION,))

        verdicts: dict[str, list[sqlite3.Row]] = {}
        for row in _rows(conn, "SELECT * FROM tool_results WHERE sample_id IS NOT NULL"):
            verdicts.setdefault(row["sample_id"], []).append(row)

        # A run's human verdict applies to every sample inside it: the person judged the
        # artifact the whole turn produced, not one call within it.
        # What the person did about each run, as a set - deliberately not "the last thing
        # they did", because the rows carry no reliable order relative to each other and a
        # verdict that flips depending on which query returns first is not a verdict.
        verdicts_by_run: dict[str, set[str]] = {}
        for row in _rows(conn, "SELECT run_id, kind FROM interactions"
                               " WHERE kind IN ('approval','rejection','correction')"
                               " AND run_id IS NOT NULL"):
            verdicts_by_run.setdefault(row["run_id"], set()).add(row["kind"])

        # A correction outranks an approval however they are ordered: taking the graph and
        # then changing it is not the same as being happy with it.
        kept = {
            run_id: ("correction" if "correction" in kinds
                     else "rejection" if "rejection" in kinds
                     else "approval")
            for run_id, kinds in verdicts_by_run.items()
        }

        pending: list[tuple] = []
        for sample in _rows(conn, "SELECT sample_id, run_id, node, attempt, error FROM samples"):
            for name, value, detail in _labels_for(sample, verdicts.get(sample["sample_id"], []),
                                                   kept.get(sample["run_id"])):
                pending.append((sample["sample_id"], sample["run_id"], name, value, detail))
                written[name] = written.get(name, 0) + 1

        conn.executemany(
            "INSERT OR REPLACE INTO labels"
            " (label_id, sample_id, run_id, name, value, detail, scorer_version, computed_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [
                (str(uuid.uuid4()), sample_id, run_id, name, float(value), detail,
                 SCORER_VERSION, store.now())
                for sample_id, run_id, name, value, detail in pending
            ],
        )

    logger.info("Scored %s label(s) across the corpus.", sum(written.values()))
    return written


def _labels_for(sample: sqlite3.Row, verdicts: list[sqlite3.Row],
                human: str | None) -> list[tuple[str, float, str | None]]:
    """Every judgement that can be made about one model call."""
    out: list[tuple[str, float, str | None]] = []

    if sample["error"]:
        # The provider refused. Not the model's fault and not usable as an example, so it
        # is marked rather than silently scoring zero on everything else.
        return [("call_failed", 1.0, sample["error"][:200])]

    by_tool = {v["tool"]: v for v in verdicts}

    if "parse_dsl" in by_tool or "extract_plan" in by_tool:
        verdict = by_tool.get("parse_dsl") or by_tool["extract_plan"]
        out.append(("parsed_ok", float(verdict["ok"]), verdict["error"]))

    lint = by_tool.get("lint")
    if lint is not None:
        counts = _json(lint["output_json"]) or {}
        errors = int(counts.get("errors") or 0)
        out.append(("lint_clean", float(errors == 0 and lint["ok"]), lint["error"]))
        # Warnings do not block a build, but a graph that merely warns is worse training
        # data than one that lints clean, and only a count can express that.
        out.append(("lint_warnings", float(counts.get("warnings") or 0), None))

    tests = by_tool.get("run_tests")
    if tests is not None:
        summary = _json(tests["output_json"]) or {}
        passed, total = summary.get("passed"), summary.get("total")
        out.append(("tests_passed", float(tests["ok"]), tests["error"]))
        if total:
            out.append(("tests_ratio", (passed or 0) / total, f"{passed}/{total}"))

    # Reaching the right answer first time is worth telling apart from reaching it after
    # three repairs: it is the behaviour a fine-tune is trying to produce.
    if out and int(sample["attempt"] or 1) == 1:
        out.append(("first_try", float(all(v == 1.0 for n, v, _ in out
                                           if n in QUALITY_LABELS)), None))

    if human:
        out.append(("human_kept", float(human == "approval"), human))

    return out


def quality(conn: sqlite3.Connection, sample_id: str) -> float | None:
    """1.0 when a sample cleared every objective check, 0.0 when it failed one.

    None when nothing checked it - a triage or explain reply has no parser or engine to
    judge it, and scoring that as a failure would quietly drop every conversational sample
    out of the training set.
    """
    rows = _rows(
        conn,
        "SELECT name, value FROM labels WHERE sample_id = ? AND scorer_version = ?"
        " AND name IN (%s)" % ",".join("?" * len(QUALITY_LABELS)),
        (sample_id, SCORER_VERSION, *QUALITY_LABELS),
    )
    if not rows:
        return None
    return min(float(r["value"]) for r in rows)


def summary() -> dict[str, Any]:
    """What the corpus holds, for deciding whether there is enough of it yet."""
    if not store.enabled():
        return {}
    conn = store._connect()

    def one(sql: str, args: tuple = ()) -> Any:
        row = conn.execute(sql, args).fetchone()
        return row[0] if row else None

    return {
        "runs": one("SELECT COUNT(*) FROM runs"),
        "samples": one("SELECT COUNT(*) FROM samples"),
        "prompts": one("SELECT COUNT(*) FROM prompts"),
        "tool_results": one("SELECT COUNT(*) FROM tool_results"),
        "interactions": one("SELECT COUNT(*) FROM interactions"),
        "labels": one("SELECT COUNT(*) FROM labels"),
        "by_node": {
            r["node"]: r["n"] for r in
            _rows(conn, "SELECT node, COUNT(*) n FROM samples GROUP BY node ORDER BY n DESC")
        },
        "by_model": {
            (r["model_served"] or r["model_requested"] or "unknown"): r["n"] for r in
            _rows(conn, "SELECT COALESCE(model_served, model_requested) model_served,"
                        " model_requested, COUNT(*) n FROM samples GROUP BY 1 ORDER BY n DESC")
        },
        "cost": one("SELECT ROUND(SUM(cost), 6) FROM samples WHERE cost IS NOT NULL"),
        "tokens": one("SELECT SUM(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0))"
                      " FROM samples"),
        # Which generations of each system prompt are in the corpus, and how much was
        # recorded under each. This is what `--prompt-hash` is chosen from: a training set
        # spanning two generations of a 46KB instruction is a training set of two tasks.
        "prompts_seen": [
            {"hash": r["hash"][:12], "kind": r["kind"], "chars": r["chars"],
             "samples": r["n"], "first_seen": r["first_seen_at"][:10],
             "last_seen": r["last_seen_at"][:10]}
            for r in _rows(conn,
                "SELECT p.hash, p.kind, p.chars, p.first_seen_at, p.last_seen_at,"
                " COUNT(s.sample_id) n FROM prompts p"
                " LEFT JOIN samples s ON s.prompt_hash = p.hash"
                " GROUP BY p.hash ORDER BY p.kind, p.last_seen_at DESC")
        ],
        "interactions_by_kind": {
            r["kind"]: r["n"] for r in
            _rows(conn, "SELECT kind, COUNT(*) n FROM interactions GROUP BY kind ORDER BY n DESC")
        },
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m backend.corpus.score",
        description="Recompute the quality labels over the recorded corpus.",
    )
    parser.add_argument("--rescore", action="store_true",
                        help="clear this scorer's previous labels first")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    written = score_all(rescore=args.rescore)
    if not written:
        print("Nothing to score.")
        return 0
    for name in sorted(written):
        print(f"  {name:16} {written[name]:>6}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
