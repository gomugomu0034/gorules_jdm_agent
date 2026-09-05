"""Drive requirements through the agent headlessly, to build corpus volume.

At the free tier's fifty requests a day the studio produces roughly ten runs, and a
fine-tune wants thousands of samples. This turns a budget into corpus directly: a list of
requirements in, a full set of samples, tool verdicts and repair loops out, without anyone
sitting at the UI approving chips for an afternoon.

It doubles as the evaluation set. The same file measures whether a fine-tuned model is
better than the one it replaced, which is the question the corpus exists to answer.

**It never fabricates a human verdict.** The agent pauses for approval and the harness
answers, because otherwise nothing would ever get built - but a scripted answer is not a
person agreeing, so no `approval` interaction is written and every run it produces is
marked `source='replay'`. `--source live` on an export keeps real use separate from drills.

Runs the compiled graph directly rather than the HTTP API: there is no browser to serve,
no thread to keep alive, and a harness that needs the whole app running is a harness that
does not get used.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend import corpus
from backend.corpus import store

logger = logging.getLogger(__name__)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "requirements.yaml"

# What the agent offers at its two gates. Matched by prefix because the labels carry emoji
# and have been reworded before.
_APPROVALS = ("Approve", "approve")


@dataclass
class Outcome:
    requirement_id: str
    run_id: str
    status: str                    # built | failed | error | skipped
    samples: int = 0
    cost: float = 0.0
    seconds: float = 0.0
    detail: str = ""


@dataclass
class Budget:
    """What the harness is allowed to spend before it stops.

    Present because this is the one thing here that costs real money, and a loop that
    quietly burns a day's quota on a fixture file is not a tool anybody trusts twice.
    """

    max_runs: int | None = None
    max_cost: float | None = None
    spent: float = 0.0
    used: int = 0
    stopped: str = ""

    def exhausted(self) -> str:
        if self.max_runs is not None and self.used >= self.max_runs:
            return f"reached --max-runs {self.max_runs}"
        if self.max_cost is not None and self.spent >= self.max_cost:
            return f"reached --max-cost {self.max_cost}"
        return ""


@dataclass
class Requirement:
    id: str
    text: str
    difficulty: str = "unknown"
    answers: list[str] = field(default_factory=list)


def load(path: Path | str = FIXTURES) -> list[Requirement]:
    import yaml

    rows = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    out = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("text"):
            continue
        out.append(Requirement(
            id=str(row.get("id") or f"req-{len(out) + 1}"),
            text=str(row["text"]).strip(),
            difficulty=str(row.get("difficulty") or "unknown"),
            answers=[str(a) for a in (row.get("answers") or [])],
        ))
    return out


def _answer(prompt: str, options: list[str], scripted: list[str]) -> str:
    """What to reply to a gate.

    The script is spent first, so a requirement can steer its own clarification. After
    that the harness approves, because a run that never approves never reaches the builder
    and produces no samples at all - which is the opposite of the point.
    """
    if scripted:
        return scripted.pop(0)
    for option in options:
        if option.startswith(_APPROVALS):
            return option
    return options[0] if options else "Approve"


async def _run_one(graph, requirement: Requirement, *, max_turns: int = 8) -> Outcome:
    from langchain_core.messages import HumanMessage
    from langgraph.types import Command

    from backend.lang_graph_agent import RateLimited

    run_id = corpus.new_run_id()
    thread_id = f"replay-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread_id}}
    scripted = list(requirement.answers)
    started = time.monotonic()
    status, detail = "error", ""

    with corpus.run_scope(run_id, thread_id=thread_id, source="replay"):
        # A scripted requirement is still a requirement: it is the prompt half of every
        # example the run produces. The run's `source` is what marks it as not real use.
        corpus.record_interaction(
            kind="requirement", thread_id=thread_id, response=requirement.text,
            detail={"requirement_id": requirement.id, "difficulty": requirement.difficulty,
                    "scripted": True},
            run_id=run_id,
        )
        payload: Any = {"messages": [HumanMessage(content=requirement.text)],
                        "canvas_jdm_json": "", "thread_id": thread_id}
        try:
            for _ in range(max_turns):
                async for _chunk in graph.astream(payload, config=config):
                    pass

                state = await graph.aget_state(config)
                pending = _pending(state)
                if pending is None:
                    values = state.values
                    built = bool(values.get("jdm_json"))
                    status = "built" if built else "failed"
                    detail = "" if built else str(values.get("evaluation_feedback") or "")[:200]
                    corpus.observe(outcome="completed" if built else "error",
                                   intent=values.get("intent"),
                                   final_jdm=values.get("jdm_json"))
                    break

                reply = _answer(pending.get("prompt", ""),
                                list(pending.get("options") or []), scripted)
                # Recorded as the clarification it is, marked scripted so nobody later
                # mistakes the harness for a person.
                corpus.record_interaction(
                    kind="clarification", thread_id=thread_id,
                    prompt=pending.get("prompt", ""),
                    options=list(pending.get("options") or []),
                    response=reply, detail={"scripted": True}, run_id=run_id,
                )
                payload = Command(resume=reply)
            else:
                status, detail = "failed", f"still going after {max_turns} turns"
                corpus.observe(outcome="error")
        except RateLimited:
            # Has to escape: the caller stops the whole list on this, and a quota that is
            # gone for the day does not come back three requirements later.
            corpus.observe(outcome="error")
            raise
        except Exception as exc:  # noqa: BLE001
            status, detail = "error", f"{type(exc).__name__}: {exc}"[:200]
            corpus.observe(outcome="error")

    return Outcome(
        requirement_id=requirement.id,
        run_id=run_id,
        status=status,
        samples=_count(run_id, "SELECT COUNT(*) FROM samples WHERE run_id = ?"),
        cost=_count(run_id, "SELECT COALESCE(SUM(cost), 0) FROM samples WHERE run_id = ?") or 0.0,
        seconds=round(time.monotonic() - started, 1),
        detail=detail,
    )


def _pending(state) -> dict | None:
    """The interrupt this run is paused on, if it is paused."""
    for task in getattr(state, "tasks", ()) or ():
        for interrupt in getattr(task, "interrupts", ()) or ():
            value = getattr(interrupt, "value", None)
            if isinstance(value, dict):
                return value
    return None


def _count(run_id: str, sql: str) -> Any:
    if not store.enabled():
        return 0
    try:
        row = store._connect().execute(sql, (run_id,)).fetchone()
        return row[0] if row else 0
    except Exception:  # noqa: BLE001
        return 0


async def replay(requirements: list[Requirement], budget: Budget) -> list[Outcome]:
    from backend import lang_graph_agent as agent

    graph = agent.build_graph()
    results: list[Outcome] = []

    for requirement in requirements:
        stop = budget.exhausted()
        if stop:
            budget.stopped = stop
            results.append(Outcome(requirement.id, "", "skipped", detail=stop))
            continue

        print(f"  {requirement.id:22} ", end="", flush=True)
        try:
            outcome = await _run_one(graph, requirement)
        except agent.RateLimited as exc:
            # The single most likely failure on a free tier, and the one where carrying on
            # would only burn the rest of the list against a closed door.
            budget.stopped = f"the provider is rate limiting: {exc}"
            print("rate limited - stopping")
            results.append(Outcome(requirement.id, "", "skipped", detail=str(exc)[:120]))
            break

        budget.used += 1
        budget.spent += outcome.cost
        results.append(outcome)
        print(f"{outcome.status:8} {outcome.samples:>2} samples  {outcome.seconds:>5.1f}s"
              f"  {('$%.5f' % outcome.cost) if outcome.cost else ''}")
        if outcome.detail and outcome.status != "built":
            print(f"  {'':22} {outcome.detail[:70]}")

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.corpus.replay",
        description="Drive requirements through the agent to build corpus volume.",
        epilog=(
            "Run from the repository root. This spends real model quota: start with\n"
            "--dry-run to see the list, then --max-runs 2 before turning it loose.\n\n"
            "Everything it produces is marked source='replay', so an export can be held\n"
            "to real use with --source live."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--file", default=str(FIXTURES))
    parser.add_argument("--id", action="append", help="run only these (repeatable)")
    parser.add_argument("--difficulty", choices=["simple", "moderate", "hard"])
    parser.add_argument("--max-runs", type=int, help="stop after this many")
    parser.add_argument("--max-cost", type=float, help="stop once this much has been spent")
    parser.add_argument("--dry-run", action="store_true", help="list what would run")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    requirements = load(args.file)
    if args.id:
        wanted = set(args.id)
        requirements = [r for r in requirements if r.id in wanted]
    if args.difficulty:
        requirements = [r for r in requirements if r.difficulty == args.difficulty]

    if not requirements:
        print("Nothing matched.", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"{len(requirements)} requirement(s) would run:")
        for requirement in requirements:
            print(f"  {requirement.id:22} {requirement.difficulty:9} "
                  f"{requirement.text[:60]}...")
        return 0

    if not store.enabled():
        print("Capture is off (CORPUS_CAPTURE), so a replay would record nothing.",
              file=sys.stderr)
        return 1

    budget = Budget(max_runs=args.max_runs, max_cost=args.max_cost)
    print(f"Replaying {len(requirements)} requirement(s). Each one spends model quota.\n")
    results = asyncio.run(replay(requirements, budget))

    built = sum(1 for r in results if r.status == "built")
    samples = sum(r.samples for r in results)
    print(f"\n{built}/{len(results)} built, {samples} samples, "
          f"${budget.spent:.5f} spent")
    if budget.stopped:
        print(f"Stopped early: {budget.stopped}")
    print("Marked source='replay'. Export real use only with --source live.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
