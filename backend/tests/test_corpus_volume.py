"""Building volume, and recovering what was there before.

At the free tier's fifty requests a day the studio produces roughly ten runs, and a
fine-tune wants thousands of samples. The harness turns a budget into corpus directly; the
backfill recovers what the studio already knew before any of this existed.

The thing both have to get right is not lying about provenance. A scripted answer is not a
person approving, and a reconstructed conversation is not a recorded one.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from backend import lang_graph_agent as agent
from backend.corpus import backfill, replay, store
from backend.tests.test_agent_flow import BROKEN_DSL, GOOD_DSL, planner_payload


def rows(table: str) -> list[dict]:
    conn = sqlite3.connect(store.settings.corpus_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
    finally:
        conn.close()


@pytest.fixture
def agent_answers(monkeypatch, tmp_path):
    """A provider that builds cleanly, or fails once and repairs."""

    def install(*, fail_first: bool = False):
        seen = {"n": 0}

        def dispatch(sys_prompt, messages):
            if sys_prompt is agent.PROMPT_INTENT:
                return '{"intent": "CREATE", "confidence": 1.0}'
            if sys_prompt in (agent.PROMPT_PLANNER, agent.PROMPT_BUILDER):
                seen["n"] += 1
                broken = fail_first and seen["n"] == 1
                return planner_payload(BROKEN_DSL if broken else GOOD_DSL)
            return json.dumps({
                "status": "READY_FOR_APPROVAL", "message": "Understood.",
                "options": ["Approve with above understanding & assumptions"],
            })

        monkeypatch.setattr(agent, "_dispatch", dispatch)
        monkeypatch.setattr(agent, "_repo_path", lambda *p: tmp_path.joinpath(*p))

    return install


def one(text="Free shipping over $50, else $6.", **kw) -> replay.Requirement:
    return replay.Requirement(id=kw.pop("id", "req-1"), text=text, **kw)


# ----------------------------------------------------------------- the harness

def test_the_fixture_file_loads():
    """It ships as the starting set, and doubles as the evaluation set."""
    requirements = replay.load()

    assert len(requirements) >= 5
    assert all(r.id and r.text for r in requirements)
    assert {r.difficulty for r in requirements} == {"simple", "moderate", "hard"}


def test_a_replayed_run_produces_a_full_set_of_samples(agent_answers):
    agent_answers()

    outcomes = asyncio.run(replay.replay([one()], replay.Budget()))

    assert [o.status for o in outcomes] == ["built"]
    assert outcomes[0].samples > 0
    assert rows("tool_results"), "the verdicts are the point, not just the completions"


def test_everything_a_replay_produces_says_it_was_a_replay():
    """A training split that silently mixes real use with synthetic drills is measuring
    something nobody asked about."""
    from backend import corpus

    with corpus.run_scope("r", thread_id="t", source="replay"):
        corpus.observe(outcome="completed")

    assert rows("runs")[0]["source"] == "replay"


def test_a_run_recorded_normally_is_marked_live():
    from backend import corpus

    with corpus.run_scope("r", thread_id="t"):
        corpus.observe(outcome="completed")

    assert rows("runs")[0]["source"] == "live"


def test_the_harness_never_claims_a_person_approved(agent_answers):
    """It answers the gates because otherwise nothing is ever built - but a scripted reply
    is not a person agreeing, and writing it down as one would poison every `human_kept`
    label in the corpus."""
    agent_answers()

    asyncio.run(replay.replay([one()], replay.Budget()))

    kinds = {r["kind"] for r in rows("interactions")}
    assert "approval" not in kinds
    assert "rejection" not in kinds
    assert kinds <= {"requirement", "clarification"}


def test_a_scripted_answer_is_marked_as_scripted(agent_answers):
    agent_answers()

    asyncio.run(replay.replay([one()], replay.Budget()))

    for row in rows("interactions"):
        assert json.loads(row["detail_json"])["scripted"] is True


def test_a_requirements_own_answers_are_used_before_approving():
    """A requirement can steer its own clarification, which is how an underspecified one
    becomes a useful sample instead of a dead end."""
    scripted = ["bands are 0-1kg, 1-5kg, 5kg+"]

    assert replay._answer("q?", ["Approve & Save"], scripted) == "bands are 0-1kg, 1-5kg, 5kg+"
    assert scripted == [], "spent"
    assert replay._answer("q?", ["Custom clarification", "Approve & Save"], []) == "Approve & Save"


def test_a_repair_loop_in_a_replay_still_yields_a_preference_pair(agent_answers):
    """The reason to replay hard requirements rather than easy ones: a run that succeeds
    first time produces no pair, and the pairs are the expensive half of the corpus."""
    from backend.corpus import export, score

    agent_answers(fail_first=True)
    asyncio.run(replay.replay([one()], replay.Budget()))
    score.score_all()

    pairs = list(export.export_preference(store._connect(), export.Filters()))
    assert pairs, "a failing attempt and its repair, from a headless run"
    assert pairs[0]["reason"]


# ------------------------------------------------------------------- the budget

def test_the_budget_stops_the_run_count(agent_answers):
    """This is the one thing here that spends real money. A loop that quietly burns a
    day's quota on a fixture file is not a tool anyone trusts twice."""
    agent_answers()
    requirements = [one(id=f"r{i}") for i in range(4)]

    outcomes = asyncio.run(replay.replay(requirements, replay.Budget(max_runs=2)))

    assert [o.status for o in outcomes[:2]] == ["built", "built"]
    assert all(o.status == "skipped" for o in outcomes[2:])


def test_the_budget_stops_on_cost():
    budget = replay.Budget(max_cost=0.01)
    assert budget.exhausted() == ""

    budget.spent = 0.02
    assert "max-cost" in budget.exhausted()


def test_a_rate_limit_stops_rather_than_burning_the_rest(monkeypatch, tmp_path):
    """Carrying on would only spend the remaining requirements against a closed door."""
    monkeypatch.setattr(agent, "_repo_path", lambda *p: tmp_path.joinpath(*p))

    def refuse(*_args, **_kwargs):
        raise agent.RateLimited("free-models-per-day exhausted")

    monkeypatch.setattr(agent, "_dispatch", refuse)
    budget = replay.Budget()

    outcomes = asyncio.run(replay.replay([one(id="a"), one(id="b"), one(id="c")], budget))

    assert len(outcomes) == 1, "it stopped rather than trying the other two"
    assert "rate limiting" in budget.stopped


def test_dry_run_spends_nothing(monkeypatch, capsys):
    def explode(*_args, **_kwargs):
        raise AssertionError("a dry run must not call the model")

    monkeypatch.setattr(agent, "_dispatch", explode)

    assert replay.main(["--dry-run"]) == 0
    assert "would run" in capsys.readouterr().out


# ------------------------------------------------------------------ the backfill

def studio(tmp_path: Path, *, messages: list, events: list[dict],
           thread_id: str = "t-old") -> str:
    """A studio database shaped like one that predates the corpus."""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    path = tmp_path / "studio.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE chat_threads (id TEXT PRIMARY KEY, graph_id TEXT, owner_id TEXT,"
        " created_at TEXT);"
        "CREATE TABLE chat_events (thread_id TEXT, seq INTEGER, type TEXT, payload TEXT);"
        "CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id TEXT, type TEXT,"
        " checkpoint BLOB);"
    )
    conn.execute("INSERT INTO chat_threads VALUES (?,?,?,?)",
                 (thread_id, "g-1", "guest:secret-token", "2026-01-01T00:00:00Z"))
    for seq, event in enumerate(events, start=1):
        conn.execute("INSERT INTO chat_events VALUES (?,?,?,?)",
                     (thread_id, seq, event["type"], json.dumps(event)))

    kind, blob = JsonPlusSerializer().dumps_typed({"channel_values": {"messages": messages}})
    conn.execute("INSERT INTO checkpoints VALUES (?,?,?,?)", (thread_id, "c1", kind, blob))
    conn.commit()
    conn.close()
    return str(path)


def old_thread(tmp_path, **kw) -> str:
    from langchain_core.messages import AIMessage, HumanMessage

    return studio(
        tmp_path,
        messages=kw.pop("messages", [
            HumanMessage(content="Refund policy by order age and tier"),
            AIMessage(content="Here is what I understood."),
            HumanMessage(content="gold customers get 90 days"),
        ]),
        events=kw.pop("events", [
            {"type": "run_started", "run_id": "old-run-1"},
            {"type": "interrupt", "prompt": "Please select an option",
             "options": ["Approve", "Custom clarification"]},
            {"type": "done", "status": "completed"},
        ]),
    )


def test_the_backfill_recovers_the_requirement_and_the_answer(tmp_path):
    """Both come out of the LangGraph checkpoint. The requirement was never emitted as an
    event at all - only assistant messages were - so this is the only place it survives."""
    path = old_thread(tmp_path)

    tally = backfill.backfill(studio_path=path)

    assert tally.threads == 1 and tally.runs == 1
    kinds = {r["kind"]: r for r in rows("interactions")}
    assert kinds["requirement"]["response"] == "Refund policy by order age and tier"
    assert kinds["clarification"]["prompt"] == "Please select an option"
    assert kinds["clarification"]["response"] == "gold customers get 90 days"


def test_backfilled_rows_say_they_were_reconstructed(tmp_path):
    backfill.backfill(studio_path=old_thread(tmp_path))

    assert rows("runs")[0]["source"] == "backfill"
    assert all(json.loads(r["detail_json"])["backfilled"] for r in rows("interactions"))


def test_the_backfill_does_not_invent_samples(tmp_path):
    """A training example needs the system prompt it was produced under, and that was
    never stored - which is the gap the corpus was added to close. Reconstructing samples
    without it would make rows whose first user turn reads as the instruction."""
    backfill.backfill(studio_path=old_thread(tmp_path))

    assert rows("samples") == []


def test_running_the_backfill_twice_imports_nothing_the_second_time(tmp_path):
    path = old_thread(tmp_path)

    first = backfill.backfill(studio_path=path)
    second = backfill.backfill(studio_path=path)

    assert first.threads == 1
    assert (second.threads, second.skipped) == (0, 1)
    assert len(rows("interactions")) == 2


def test_a_guest_identity_is_not_carried_over_raw(tmp_path):
    backfill.backfill(studio_path=old_thread(tmp_path))

    assert "guest:secret-token" not in json.dumps(rows("runs"))
    assert rows("runs")[0]["owner_hash"]


def test_the_agents_own_words_are_not_recorded_as_the_persons(tmp_path):
    """The approval gate puts a sentence in the user's mouth so the model sees a coherent
    conversation. Recording that verbatim as what someone said would be quietly wrong."""
    from langchain_core.messages import AIMessage, HumanMessage

    path = old_thread(tmp_path, messages=[
        HumanMessage(content="Refund policy"),
        AIMessage(content="Understood."),
        HumanMessage(content="I approve the assumptions. Please proceed to build the plan."),
    ])

    backfill.backfill(studio_path=path)

    clarification = next(r for r in rows("interactions") if r["kind"] == "clarification")
    assert clarification["response"] == "(approved)"
    assert json.loads(clarification["detail_json"])["phrasing"] == "agent"


def test_the_users_words_are_recovered_from_the_change_gate(tmp_path):
    """That gate wraps the real answer in a sentence but keeps it intact."""
    from langchain_core.messages import AIMessage, HumanMessage

    path = old_thread(tmp_path, messages=[
        HumanMessage(content="Refund policy"),
        AIMessage(content="Understood."),
        HumanMessage(content="The user requested these specific changes:\n\n"
                             "'make the window 90 days for gold'\n\n"
                             "Please update the implementation plan, DSL, and test cases "
                             "accordingly."),
    ])

    backfill.backfill(studio_path=path)

    clarification = next(r for r in rows("interactions") if r["kind"] == "clarification")
    assert clarification["response"] == "make the window 90 days for gold"


def test_a_thread_nobody_used_is_not_imported(tmp_path):
    """The studio mints a thread per visit and there are a lot of empty ones."""
    path = studio(tmp_path, messages=[], events=[])

    assert backfill.backfill(studio_path=path).threads == 0


def test_an_unreadable_checkpoint_does_not_stop_the_import(tmp_path):
    """Best-effort recovery of old data is not something to fail a run over."""
    path = old_thread(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE checkpoints SET checkpoint = ?", (b"not a checkpoint",))
    conn.commit()
    conn.close()

    tally = backfill.backfill(studio_path=path)

    assert tally.runs == 1, "the events still came through"
    assert tally.requirements == 0, "and the parts that needed the checkpoint did not"


# ------------------------------------------------------------- keeping them apart

def test_an_export_can_be_held_to_real_use():
    """The whole reason `source` exists."""
    from backend import corpus
    from backend.corpus import export

    for source in ("live", "replay"):
        with corpus.run_scope(f"run-{source}", thread_id="t", source=source):
            corpus.record_llm_call(node="planner_node", system_prompt="SYS",
                                   messages=[{"role": "user", "content": "x"}],
                                   completion="a design")
            corpus.observe(outcome="completed")

    conn = store._connect()
    assert len(list(export.export_sft(conn, export.Filters()))) == 2
    assert len(list(export.export_sft(conn, export.Filters(source="live")))) == 1
    assert len(list(export.export_sft(conn, export.Filters(source="replay")))) == 1
