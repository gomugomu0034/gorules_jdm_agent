"""What a failing test tells you.

The old report was one wide markdown table with a Details column. It carried the
mismatches and nothing else - not the input that produced them, not what actually came
back - and JSON in a markdown cell wraps at about forty characters, so anything with more
than one field stopped lining up.

Four things are needed to decide whether the policy is wrong or the test is: the input,
what was expected, what came back, and which field disagreed. All four are now there.
"""

from __future__ import annotations

import json

import pytest

from backend.lang_graph_agent import _format_test_report_markdown
from backend.tools.zen_evaluator import run_test_suite

GRAPH = json.dumps({
    "contentType": "application/vnd.gorules.decision",
    "nodes": [
        {"id": "i", "name": "Request", "type": "inputNode",
         "content": {"schema": ""}, "position": {"x": 0, "y": 0}},
        {"id": "f", "name": "Fee", "type": "expressionNode", "position": {"x": 1, "y": 0},
         "content": {"expressions": [
             {"id": "e", "key": "shippingFee", "value": "orderTotal > 500 ? 0 : 6"},
             {"id": "e2", "key": "band", "value": "orderTotal > 500 ? 'big' : 'small'"}]}},
        {"id": "o", "name": "Response", "type": "outputNode",
         "content": {"schema": ""}, "position": {"x": 2, "y": 0}},
    ],
    "edges": [{"id": "a", "sourceId": "i", "targetId": "f", "type": "edge"},
              {"id": "b", "sourceId": "f", "targetId": "o", "type": "edge"}],
})

CASES = [
    {"name": "free over 50", "input": {"orderTotal": 80},
     "expectedOutput": {"shippingFee": 0, "band": "big"}},
    {"name": "paid under 50", "input": {"orderTotal": 20},
     "expectedOutput": {"shippingFee": 6}},
    {"name": "no expectation", "input": {"orderTotal": 1}},
]


@pytest.fixture
def report() -> str:
    return _format_test_report_markdown(run_test_suite(GRAPH, CASES, trace=False))


def test_a_failure_carries_the_input_that_produced_it(report):
    """Without it the reader cannot reproduce the failure, which is the first thing anyone
    does with one."""
    assert '| Input | `{"orderTotal": 80}` |' in report


def test_a_failure_shows_expected_and_actual_side_by_side(report):
    assert '| Expected | `{"shippingFee": 0, "band": "big"}` |' in report
    assert '| Actual | `{"shippingFee": 6, "band": "small"}` |' in report


def test_a_failure_says_which_field_disagreed_and_how(report):
    """Reading two JSON blobs and diffing them by eye is what this replaces."""
    assert "`shippingFee` should be 0 but was 6" in report
    assert '`band` should be "big" but was "small"' in report


def test_a_passing_test_stays_one_line(report):
    """The only thing worth saying about a passing case is that it passed. Giving it the
    same block as a failure buries the failures."""
    assert "- ✅ paid under 50" in report
    assert "| Input |" not in report.split("**❌")[0], "no block before the first failure"


def test_a_skipped_case_says_that_nothing_was_proven(report):
    """`skipped` reads like a minor status. It means the case asserted nothing at all, and
    a suite of them reports zero failures while checking nothing."""
    assert "- ➖ no expectation" in report
    assert "nothing about them was proven" in report


def test_an_errored_case_reports_the_error_not_a_comparison():
    """Nothing came back, so there is no actual to set beside the expectation."""
    broken = json.dumps({"contentType": "application/vnd.gorules.decision",
                         "nodes": [], "edges": []})
    text = _format_test_report_markdown(
        run_test_suite(broken, [{"name": "anything", "input": {},
                                 "expectedOutput": {"x": 1}}], trace=False))

    assert "does not compile" in text or "| Error |" in text
    assert "| Expected |" not in text


def test_a_clean_run_says_so_without_any_blocks():
    text = _format_test_report_markdown(
        run_test_suite(GRAPH, [{"name": "under", "input": {"orderTotal": 20},
                                "expectedOutput": {"shippingFee": 6}}], trace=False))

    assert text.startswith("### ✅ 1/1 tests passed")
    assert "| Input |" not in text


def test_a_long_value_is_truncated_rather_than_flooding_the_chat():
    """One oversized field should not push the rest of the report out of view."""
    text = _format_test_report_markdown({
        "summary": {"total": 1, "passed": 0, "failed": 1, "errored": 0, "skipped": 0,
                    "duration_ms": 1},
        "results": [{"test_id": None, "name": "big", "status": "failed",
                     "input": {"blob": "x" * 5000}, "expected": {"a": 1},
                     "actual": {"a": 2}, "mismatches": [{"path": "a", "expected": 1,
                                                         "actual": 2}],
                     "performance": None, "trace": {}, "error": None}],
    })

    assert "…`" in text
    assert len(text) < 2000


def test_many_mismatches_are_summarised_not_listed_forever():
    text = _format_test_report_markdown({
        "summary": {"total": 1, "passed": 0, "failed": 1, "errored": 0, "skipped": 0,
                    "duration_ms": 1},
        "results": [{"test_id": None, "name": "wide", "status": "failed",
                     "input": {}, "expected": {}, "actual": {},
                     "mismatches": [{"path": f"f{i}", "expected": i, "actual": 0}
                                    for i in range(9)],
                     "performance": None, "trace": {}, "error": None}],
    })

    assert "(+5 more)" in text
