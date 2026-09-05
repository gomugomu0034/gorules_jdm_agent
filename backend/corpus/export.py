"""Turn the recorded corpus into training files.

Three shapes, because the corpus supports three genuinely different things:

  sft         one example per model call that passed every objective check
  preference  a rejected answer and an accepted one for the same task, which this
              pipeline produces for free every time the repair loop runs
  rejection   the first prompt paired with the artifact the run finally settled on -
              the export that teaches the model to produce in one shot what currently
              takes it four attempts

Filtering by prompt hash is not a convenience. `PROMPT_PLANNER` renders to 46KB and gets
edited; samples recorded either side of an edit answer different instructions, and a
training set that mixes them is a training set of noise. `--prompt-hash` is how you keep
one generation of the instructions.

Nothing here writes to the corpus except through the scorer.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.corpus import score, store
from backend.corpus.redact import redact

logger = logging.getLogger(__name__)


@dataclass
class Filters:
    node: str | None = None
    prompt_hash: str | None = None
    model: str | None = None
    since: str | None = None
    min_quality: float | None = None
    limit: int | None = None
    source: str | None = None

    def where(self) -> tuple[str, list]:
        """SQL for the sample-level filters, as a clause and its arguments."""
        # A sample whose system prompt cannot be resolved is not a training example - it
        # is half of one, and a trainer would read the first user turn as the instruction.
        clauses, args = ["s.error IS NULL",
                         "s.prompt_hash IN (SELECT hash FROM prompts)"], []
        if self.source:
            clauses.append("s.run_id IN (SELECT run_id FROM runs WHERE source = ?)")
            args.append(self.source)
        if self.node:
            clauses.append("s.node = ?")
            args.append(self.node)
        if self.prompt_hash:
            # A prefix, so a short hash off the stats table is enough to type.
            clauses.append("s.prompt_hash LIKE ?")
            args.append(self.prompt_hash + "%")
        if self.model:
            clauses.append("COALESCE(s.model_served, s.model_requested) LIKE ?")
            args.append(f"%{self.model}%")
        if self.since:
            clauses.append("s.created_at >= ?")
            args.append(self.since)
        return " AND ".join(clauses), args


def _json(text: str | None, fallback: Any = None) -> Any:
    if not text:
        return fallback
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return fallback


def _conversation(conn: sqlite3.Connection, sample: sqlite3.Row) -> list[dict]:
    """The request as it was actually sent: the system prompt, then the turns."""
    row = conn.execute("SELECT text FROM prompts WHERE hash = ?",
                       (sample["prompt_hash"],)).fetchone()
    messages = []
    if row is not None:
        messages.append({"role": "system", "content": row["text"]})
    for message in _json(sample["messages_json"], []):
        # `internal` marks the builder's own retry scaffolding. It belongs in the context
        # a repair was produced under, and nowhere near a supervised example of one turn.
        messages.append({"role": message.get("role", "user"),
                         "content": message.get("content", "")})
    return messages


def _meta(sample: sqlite3.Row, **extra) -> dict:
    return {
        "sample_id": sample["sample_id"],
        "run_id": sample["run_id"],
        "node": sample["node"],
        "attempt": sample["attempt"],
        "purpose": sample["purpose"],
        "prompt_hash": (sample["prompt_hash"] or "")[:12],
        "model": sample["model_served"] or sample["model_requested"],
        "provider": sample["provider"],
        **extra,
    }


# ------------------------------------------------------------------------- sft

def export_sft(conn: sqlite3.Connection, filters: Filters) -> Iterator[dict]:
    """One example per call, filtered to the ones that objectively worked.

    The filter is the whole point. The linter and the test engine already decided which
    outputs were good, so the training set can be selected without anyone labelling
    anything.
    """
    where, args = filters.where()
    rows = conn.execute(f"SELECT s.* FROM samples s WHERE {where} ORDER BY s.created_at", args)

    emitted = 0
    for sample in rows:
        if filters.limit is not None and emitted >= filters.limit:
            return
        graded = score.quality(conn, sample["sample_id"])
        if filters.min_quality is not None:
            # None means nothing checked this sample - a triage or explain reply has no
            # parser to judge it. Excluded when a quality bar is asked for, because
            # "unmeasured" is not "passed".
            if graded is None or graded < filters.min_quality:
                continue
        yield {
            "messages": _conversation(conn, sample) + [
                {"role": "assistant", "content": sample["completion"] or ""}
            ],
            "metadata": _meta(sample, quality=graded),
        }
        emitted += 1


# ------------------------------------------------------------------ preference

def _verdicts(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    out: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute("SELECT * FROM tool_results WHERE sample_id IS NOT NULL"
                            " ORDER BY seq"):
        out.setdefault(row["sample_id"], []).append(row)
    return out


def _failure_reason(verdicts: list[sqlite3.Row]) -> str | None:
    for verdict in verdicts:
        if verdict["ok"]:
            continue
        diagnostics = _json(verdict["diagnostics_json"], [])
        code = diagnostics[0].get("code") if diagnostics else None
        return f"{verdict['tool']}:{code}" if code else verdict["tool"]
    return None


def export_preference(conn: sqlite3.Connection, filters: Filters) -> Iterator[dict]:
    """A rejected answer and an accepted one for the same task.

    Built from the repair loop, which produces exactly this shape every time it runs and
    was discarding it: within one run, take the last output that passed every check as the
    accepted answer, and pair it with each earlier output that failed one.

    The prompt is the *rejected* sample's own context - the situation in which the bad
    answer was actually produced, which is the prompt that should not produce it again.
    The accepted completion generally came from a later call that had seen the error, and
    `chosen_sample` in the metadata says which, so this is never mistaken for two answers
    to one identical prompt.
    """
    verdicts = _verdicts(conn)
    where, args = filters.where()
    by_run: dict[str, list[sqlite3.Row]] = {}
    for sample in conn.execute(
        f"SELECT s.* FROM samples s WHERE {where} AND s.run_id IS NOT NULL"
        " ORDER BY s.run_id, s.seq", args
    ):
        by_run.setdefault(sample["run_id"], []).append(sample)

    emitted = 0
    for samples in by_run.values():
        graded = [(s, [v for v in verdicts.get(s["sample_id"], [])]) for s in samples]
        judged = [(s, v) for s, v in graded if v]
        if len(judged) < 2:
            continue

        winners = [s for s, v in judged if all(x["ok"] for x in v)]
        if not winners:
            continue
        chosen = winners[-1]

        for sample, sample_verdicts in judged:
            if sample["sample_id"] == chosen["sample_id"]:
                continue
            if all(v["ok"] for v in sample_verdicts):
                continue
            if sample["seq"] > chosen["seq"]:
                continue
            if filters.limit is not None and emitted >= filters.limit:
                return
            yield {
                "prompt": _conversation(conn, sample),
                "chosen": chosen["completion"] or "",
                "rejected": sample["completion"] or "",
                "reason": _failure_reason(sample_verdicts),
                "metadata": _meta(sample, chosen_sample=chosen["sample_id"],
                                  chosen_node=chosen["node"],
                                  chosen_attempt=chosen["attempt"]),
            }
            emitted += 1


# ----------------------------------------------------------- rejection sampling

def export_rejection_sampling(conn: sqlite3.Connection, filters: Filters) -> Iterator[dict]:
    """The opening prompt paired with the artifact the run finally settled on.

    The most direct route from "the agent struggles" to "the agent does not": whatever it
    took four attempts to reach becomes the answer to the question that was asked first.
    """
    verdicts = _verdicts(conn)
    where, args = filters.where()
    by_run: dict[str, list[sqlite3.Row]] = {}
    for sample in conn.execute(
        f"SELECT s.* FROM samples s WHERE {where} AND s.run_id IS NOT NULL"
        " ORDER BY s.run_id, s.seq", args
    ):
        by_run.setdefault(sample["run_id"], []).append(sample)

    emitted = 0
    for samples in by_run.values():
        winners = [s for s in samples
                   if verdicts.get(s["sample_id"])
                   and all(v["ok"] for v in verdicts[s["sample_id"]])]
        if not winners:
            continue
        final = winners[-1]
        # The first sample that anything judged at all: the opening ask, before any repair
        # scaffolding entered the context.
        opening = next((s for s in samples if verdicts.get(s["sample_id"])), None)
        if opening is None:
            continue
        if filters.limit is not None and emitted >= filters.limit:
            return
        attempts = sum(1 for s in samples if verdicts.get(s["sample_id"]))
        yield {
            "messages": _conversation(conn, opening) + [
                {"role": "assistant", "content": final["completion"] or ""}
            ],
            "metadata": _meta(opening, took_attempts=attempts,
                              answer_from_sample=final["sample_id"],
                              answer_from_node=final["node"]),
        }
        emitted += 1


FORMATS = {
    "sft": export_sft,
    "preference": export_preference,
    "rejection": export_rejection_sampling,
}


# --------------------------------------------------------------------------- cli

def write(rows: Iterator[dict], destination: str, *, scrub: bool, bare: bool) -> int:
    handle = sys.stdout if destination == "-" else open(destination, "w", encoding="utf-8")
    written = 0
    try:
        for row in rows:
            if bare:
                row = {k: v for k, v in row.items() if k != "metadata"}
            if scrub:
                row = redact(row)
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
    finally:
        if handle is not sys.stdout:
            handle.close()
    return written


def print_stats() -> None:
    facts = score.summary()
    if not facts:
        print("Capture is off, or the corpus does not exist yet.")
        return
    print(f"corpus: {store.settings.corpus_db_file}\n")
    for key in ("runs", "samples", "prompts", "tool_results", "interactions", "labels"):
        print(f"  {key:16} {facts[key]:>8,}")
        if key == "runs" and len(facts.get("runs_by_source") or {}) > 1:
            # Only worth showing once there is more than one kind. A corpus of nothing but
            # real use does not need to be told that.
            for source, count in facts["runs_by_source"].items():
                print(f"    {source:14} {count:>8,}")
    if facts.get("tokens"):
        print(f"  {'tokens':16} {facts['tokens']:>8,}")
    if facts.get("cost") is not None:
        print(f"  {'cost':16} {facts['cost']:>8}")
    if facts.get("prompts_seen"):
        print("\n  system prompts (filter with --prompt-hash)")
        print(f"    {'hash':14} {'kind':16} {'chars':>7} {'samples':>8}  last seen")
        for row in facts["prompts_seen"]:
            print(f"    {row['hash']:14} {row['kind'][:16]:16} {row['chars']:>7,} "
                  f"{row['samples']:>8}  {row['last_seen']}")

    for title, key in (("samples by node", "by_node"), ("samples by model", "by_model"),
                       ("human input", "interactions_by_kind")):
        if facts.get(key):
            print(f"\n  {title}")
            for name, count in facts[key].items():
                print(f"    {name[:44]:44} {count:>6,}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.corpus.export",
        description="Export the recorded corpus as JSONL for fine-tuning.",
        epilog=(
            "Run from the repository root, so `backend` is importable.\n\n"
            "  --format stats                      what the corpus holds\n"
            "  --format sft --min-quality 1.0      only what objectively worked\n"
            "  --format preference                 rejected/accepted pairs\n"
            "  --format rejection                  first prompt, final artifact\n\n"
            "Filter to one generation of a prompt with --prompt-hash; the hashes are in\n"
            "the stats output. Mixing samples from before and after a prompt edit is how\n"
            "a training set becomes noise."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--format", choices=[*FORMATS, "stats"], default="stats")
    parser.add_argument("--out", default="-", help="output file, or - for stdout")
    parser.add_argument("--node", help="only calls made by this node, e.g. planner_node")
    parser.add_argument("--prompt-hash", help="only calls made under this system prompt "
                                              "(a prefix is enough)")
    parser.add_argument("--model", help="substring match on the model that served the call")
    parser.add_argument("--since", help="ISO date, e.g. 2026-01-01")
    parser.add_argument("--source", choices=["live", "replay", "backfill"],
                        help="only runs from this source; live is real use of the studio")
    parser.add_argument("--min-quality", type=float,
                        help="1.0 keeps only samples that passed every objective check")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--bare", action="store_true",
                        help="omit the metadata key, for trainers that reject extra fields")
    parser.add_argument("--no-redact", action="store_true",
                        help="skip the scrubbing pass (see corpus/redact.py for its limits)")
    parser.add_argument("--no-score", action="store_true",
                        help="do not refresh the labels before exporting")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    if not store.enabled():
        print("Capture is off (CORPUS_CAPTURE), so there is nothing to export.",
              file=sys.stderr)
        return 1

    if not args.no_score:
        score.score_all()

    if args.format == "stats":
        print_stats()
        return 0

    rows = FORMATS[args.format](
        store._connect(),
        Filters(node=args.node, prompt_hash=args.prompt_hash, model=args.model,
                since=args.since, min_quality=args.min_quality, limit=args.limit,
                source=args.source),
    )
    written = write(rows, args.out, scrub=not args.no_redact, bare=args.bare)

    where = "stdout" if args.out == "-" else args.out
    print(f"{written} {args.format} example(s) -> {where}", file=sys.stderr)
    if written and not args.no_redact:
        print("Scrubbed for addresses, key-shaped tokens and card-length numbers. "
              "Best effort, not a guarantee - read it before sending it anywhere.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
