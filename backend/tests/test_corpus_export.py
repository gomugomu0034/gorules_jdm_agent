"""Scoring the corpus, and turning it into files a trainer can read.

The scorer invents nothing: every label is derived from what the deterministic tools and
the human already said. That is what makes the training set selectable without an
annotation budget - the linter and the test engine are the reward model.

The exports are three genuinely different things, not three renderings of one. `sft` is
"do this"; `preference` is "this rather than that"; `rejection` is "reach in one step what
took you four".
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from backend import corpus
from backend.corpus import export, score, store
from backend.corpus.redact import redact

SYSTEM = "You design decision graphs."


def conn() -> sqlite3.Connection:
    return store._connect()


def sample(node="planner_node", *, completion="a design", attempt=1, run="run-1",
           system=SYSTEM, **kw) -> str:
    with corpus.run_scope(run, thread_id="t-1"):
        return corpus.record_llm_call(
            node=node, system_prompt=system, messages=[{"role": "user", "content": "build it"}],
            completion=completion, attempt=attempt, provider="openrouter",
            model_requested="asked/model", **kw,
        )


def verdict(run: str, tool: str, ok: bool, *, node="builder_node", attempt=1,
            output=None, diagnostics=None) -> None:
    """A tool verdict against the sample most recently recorded in `run`."""
    row = conn().execute(
        "SELECT sample_id FROM samples WHERE run_id = ? ORDER BY seq DESC LIMIT 1", (run,)
    ).fetchone()
    store.record_tool_result(tool=tool, node=node, ok=ok, attempt=attempt, output=output,
                             diagnostics=diagnostics, run_id=run,
                             sample_id=row["sample_id"] if row else None)


def labels(sample_id: str) -> dict[str, float]:
    return {r["name"]: r["value"] for r in
            conn().execute("SELECT name, value FROM labels WHERE sample_id = ?", (sample_id,))}


# --------------------------------------------------------------------- scoring

def test_the_objective_checks_become_labels():
    sid = sample()
    verdict("run-1", "parse_dsl", True)
    verdict("run-1", "lint", True, output={"errors": 0, "warnings": 2, "hints": 1})
    verdict("run-1", "run_tests", True, output={"total": 4, "passed": 4})

    score.score_all()

    assert labels(sid) == {
        "parsed_ok": 1.0, "lint_clean": 1.0, "lint_warnings": 2.0,
        "tests_passed": 1.0, "tests_ratio": 1.0, "first_try": 1.0,
    }


def test_a_graph_that_only_warns_is_still_marked_as_warning():
    """It does not block a build, but it is worse training data than one that lints clean,
    and only a count can say so."""
    sid = sample()
    verdict("run-1", "lint", True, output={"errors": 0, "warnings": 3, "hints": 0})

    score.score_all()

    assert labels(sid)["lint_clean"] == 1.0
    assert labels(sid)["lint_warnings"] == 3.0


def test_reaching_it_first_time_is_told_apart_from_repairing_into_it():
    """The behaviour a fine-tune is trying to produce."""
    first = sample(attempt=1)
    verdict("run-1", "run_tests", True, output={"total": 2, "passed": 2})
    repaired = sample(node="builder_node", attempt=3, completion="fixed")
    verdict("run-1", "run_tests", True, output={"total": 2, "passed": 2})

    score.score_all()

    assert labels(first)["first_try"] == 1.0
    assert "first_try" not in labels(repaired)


def test_a_refused_call_is_marked_not_scored_zero():
    """A rate limit is not the model answering badly. Scoring it as a failure would teach
    the corpus that the provider's quota is a modelling mistake."""
    sid = sample(completion=None, error="RateLimited: free-models-per-day")

    score.score_all()

    assert list(labels(sid)) == ["call_failed"]


@pytest.mark.parametrize("order", [
    ("approval", "correction"),
    ("correction", "approval"),
])
def test_a_correction_outranks_an_approval_whichever_order_they_arrive(order):
    """Someone took the graph and then changed it, which is not the same as being happy
    with it. Both orders, because the rows carry no reliable order relative to each other
    and a verdict that flips depending on which the query returns first is not a verdict.
    """
    sid = sample()
    verdict("run-1", "run_tests", True, output={"total": 1, "passed": 1})
    for kind in order:
        store.record_interaction(kind=kind, run_id="run-1", response=kind)

    score.score_all()

    assert labels(sid)["human_kept"] == 0.0


def test_an_untouched_approval_counts_as_kept():
    sid = sample()
    verdict("run-1", "run_tests", True, output={"total": 1, "passed": 1})
    store.record_interaction(kind="approval", run_id="run-1", response="accepted")

    score.score_all()

    assert labels(sid)["human_kept"] == 1.0


def test_scoring_twice_does_not_double_the_labels():
    sid = sample()
    verdict("run-1", "parse_dsl", True)

    score.score_all()
    score.score_all()

    rows = conn().execute("SELECT COUNT(*) c FROM labels WHERE sample_id = ?", (sid,)).fetchone()
    assert rows["c"] == len(labels(sid))


def test_a_sample_nothing_judged_is_unmeasured_not_failed():
    """A triage or explain reply has no parser or engine to judge it. Scoring that as zero
    would quietly drop every conversational sample out of the training set."""
    sid = sample(node="triage_node")

    score.score_all()

    assert score.quality(conn(), sid) is None


# ---------------------------------------------------------------------- sft

def test_sft_carries_the_system_prompt_the_call_was_made_under():
    """Stored once by hash, so it has to be resolved back on the way out - and a sample
    whose prompt is missing is not a training example, it is half of one."""
    sample()
    verdict("run-1", "parse_dsl", True)
    score.score_all()

    row, = export.export_sft(conn(), export.Filters())

    assert row["messages"][0] == {"role": "system", "content": SYSTEM}
    assert row["messages"][-1] == {"role": "assistant", "content": "a design"}


def test_sft_can_be_held_to_the_objective_checks():
    good = sample(completion="works")
    verdict("run-1", "run_tests", True, output={"total": 1, "passed": 1})
    sample(run="run-2", completion="broken")
    verdict("run-2", "run_tests", False, output={"total": 1, "passed": 0})
    score.score_all()

    kept = list(export.export_sft(conn(), export.Filters(min_quality=1.0)))

    assert [r["metadata"]["sample_id"] for r in kept] == [good]


def test_an_unmeasured_sample_is_excluded_by_a_quality_bar():
    """"Nothing checked it" is not "it passed"."""
    sample(node="triage_node")
    score.score_all()

    assert list(export.export_sft(conn(), export.Filters(min_quality=1.0))) == []
    assert len(list(export.export_sft(conn(), export.Filters()))) == 1


@pytest.mark.parametrize("filters,expected", [
    (dict(node="planner_node"), 1),
    (dict(node="builder_node"), 1),
    (dict(model="asked"), 2),
    (dict(model="something-else"), 0),
    (dict(limit=1), 1),
])
def test_the_filters_select(filters, expected):
    sample(node="planner_node")
    sample(node="builder_node", run="run-2")
    score.score_all()

    assert len(list(export.export_sft(conn(), export.Filters(**filters)))) == expected


def test_filtering_by_prompt_keeps_one_generation_of_the_instructions():
    """`PROMPT_PLANNER` renders to 46KB and gets edited. Samples recorded either side of an
    edit answer different instructions, and a training set that mixes them is noise."""
    sample(system="You design decision graphs.")
    sample(run="run-2", system="You design decision graphs. Prefer switches.")
    score.score_all()

    hashes = [r["metadata"]["prompt_hash"] for r in export.export_sft(conn(), export.Filters())]
    assert len(set(hashes)) == 2

    only_one = list(export.export_sft(conn(), export.Filters(prompt_hash=hashes[0])))
    assert len(only_one) == 1


# --------------------------------------------------------------- preference

def test_a_repair_loop_becomes_a_preference_pair():
    """The shape this pipeline produces for free every time the builder repairs something,
    and discarded entirely before the corpus existed."""
    sample(node="planner_node", completion="the wrong threshold")
    verdict("run-1", "run_tests", False, diagnostics=[{"code": "WRONG_VALUE"}],
            output={"total": 2, "passed": 1})
    sample(node="builder_node", completion="the right threshold", attempt=2)
    verdict("run-1", "run_tests", True, attempt=2, output={"total": 2, "passed": 2})
    score.score_all()

    pair, = export.export_preference(conn(), export.Filters())

    assert pair["rejected"] == "the wrong threshold"
    assert pair["chosen"] == "the right threshold"
    assert pair["reason"] == "run_tests:WRONG_VALUE"
    assert pair["prompt"][0]["content"] == SYSTEM


def test_a_preference_pair_says_where_its_accepted_answer_came_from():
    """The accepted completion generally came from a later call that had seen the error.
    Recorded plainly, so this is never mistaken for two answers to one identical prompt."""
    sample(node="planner_node", completion="wrong")
    verdict("run-1", "parse_dsl", False, diagnostics=[{"code": "DSL_ERROR"}])
    sample(node="builder_node", completion="right", attempt=2)
    verdict("run-1", "parse_dsl", True, attempt=2)
    score.score_all()

    pair, = export.export_preference(conn(), export.Filters())

    assert pair["metadata"]["node"] == "planner_node"
    assert pair["metadata"]["chosen_node"] == "builder_node"
    assert pair["metadata"]["chosen_attempt"] == 2


def test_a_run_that_never_succeeded_yields_no_pair():
    """There is no accepted answer to prefer, and inventing one would be fabrication."""
    sample(completion="wrong")
    verdict("run-1", "parse_dsl", False)
    sample(completion="still wrong", attempt=2)
    verdict("run-1", "parse_dsl", False, attempt=2)
    score.score_all()

    assert list(export.export_preference(conn(), export.Filters())) == []


def test_a_run_that_worked_first_time_yields_no_pair():
    sample(completion="right")
    verdict("run-1", "run_tests", True, output={"total": 1, "passed": 1})
    score.score_all()

    assert list(export.export_preference(conn(), export.Filters())) == []


# -------------------------------------------------------- rejection sampling

def test_the_opening_prompt_is_paired_with_the_final_artifact():
    """The most direct route from "the agent struggles" to "the agent does not": whatever
    it took four attempts to reach becomes the answer to the question asked first."""
    sample(node="planner_node", completion="attempt one")
    verdict("run-1", "run_tests", False, diagnostics=[{"code": "WRONG_VALUE"}])
    sample(node="builder_node", completion="attempt two", attempt=2)
    verdict("run-1", "run_tests", False, attempt=2, diagnostics=[{"code": "WRONG_VALUE"}])
    sample(node="builder_node", completion="the working one", attempt=3)
    verdict("run-1", "run_tests", True, attempt=3, output={"total": 2, "passed": 2})
    score.score_all()

    row, = export.export_rejection_sampling(conn(), export.Filters())

    assert row["messages"][-1]["content"] == "the working one"
    assert row["metadata"]["node"] == "planner_node", "asked of the opening prompt"
    assert row["metadata"]["took_attempts"] == 3
    assert row["metadata"]["answer_from_node"] == "builder_node"


def test_a_run_with_no_working_artifact_is_not_exported():
    sample(completion="never worked")
    verdict("run-1", "parse_dsl", False)
    score.score_all()

    assert list(export.export_rejection_sampling(conn(), export.Filters())) == []


# ------------------------------------------------------------------ redaction

@pytest.mark.parametrize("text,expected", [
    ("email ops@acme.co.uk", "email [email]"),
    ("token sk-abcdef0123456789abcdef", "token [key]"),
    ("Bearer eyJhbGciOiJIUzI1NiJ9abcd", "[bearer]"),
    ("card 4111 1111 1111 1111 declined", "card [number] declined"),
    ("https://user:hunter2@host.example/x", "https://[credentials]@host.example/x"),
])
def test_secrets_and_addresses_are_scrubbed(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize("policy", [
    "Free shipping over $50, otherwise $6",
    "20% off for students, 25% for seniors",
    "orders above 1500 ship free from 2026-01-01",
    "tier 3 customers get 10 free returns",
])
def test_the_numbers_a_policy_is_made_of_survive(policy):
    """A pass aggressive enough to catch every phone number would destroy the content that
    makes this data worth training on. Thresholds are the policy."""
    assert redact(policy) == policy


def test_redaction_reaches_inside_an_exported_row():
    row = {"messages": [{"role": "user", "content": "mail ops@acme.co"}], "metadata": {"a": 1}}

    assert redact(row)["messages"][0]["content"] == "mail [email]"


def test_export_scrubs_by_default(tmp_path):
    sample(completion="reach me at ops@acme.co")
    verdict("run-1", "parse_dsl", True)
    score.score_all()
    out = tmp_path / "sft.jsonl"

    export.write(export.export_sft(conn(), export.Filters()), str(out),
                 scrub=True, bare=False)

    assert "ops@acme.co" not in out.read_text()
    assert "[email]" in out.read_text()


def test_bare_drops_the_metadata_for_strict_trainers(tmp_path):
    sample()
    verdict("run-1", "parse_dsl", True)
    score.score_all()
    out = tmp_path / "sft.jsonl"

    export.write(export.export_sft(conn(), export.Filters()), str(out),
                 scrub=False, bare=True)

    assert list(json.loads(out.read_text())) == ["messages"]


# ------------------------------------------------------------------------ cli

def test_the_cli_writes_a_file(tmp_path):
    sample()
    verdict("run-1", "run_tests", True, output={"total": 1, "passed": 1})
    out = tmp_path / "out.jsonl"

    assert export.main(["--format", "sft", "--out", str(out), "--min-quality", "1.0"]) == 0

    lines = out.read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["messages"][0]["role"] == "system"


def test_the_cli_reports_what_is_there(capsys):
    sample()
    export.main(["--format", "stats"])

    printed = capsys.readouterr().out
    assert "samples" in printed and "planner_node" in printed


def test_the_cli_refuses_when_capture_is_off(monkeypatch, capsys):
    monkeypatch.setattr(store, "settings",
                        store.settings.model_copy(update={"corpus_capture": "off"}))

    assert export.main(["--format", "sft"]) == 1
    assert "nothing to export" in capsys.readouterr().err.lower()


def test_the_stats_name_the_prompt_generations_you_can_filter_on():
    """`--prompt-hash` is unusable without somewhere to read the hashes from, and picking
    one generation of a 46KB instruction is the difference between a training set of one
    task and a training set of two."""
    sample(system="You design decision graphs.")
    sample(run="run-2", system="You design decision graphs. Prefer switches.")

    seen = score.summary()["prompts_seen"]

    assert len(seen) == 2
    assert all(len(row["hash"]) == 12 and row["samples"] == 1 for row in seen)
    assert {row["kind"] for row in seen} == {"planner"}
