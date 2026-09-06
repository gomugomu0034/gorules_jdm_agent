"""What to offer the user next, once a turn has finished.

A turn that ends with an answer and nothing else leaves the reader to guess what else the
assistant can do. That guessing is how the linter stayed invisible: it has been reachable
in conversation since Phase 3, and nothing anywhere ever mentioned it.

Two rules hold this together.

Every chip is worded so the intent router lands on the node the chip advertises, using the
rules-first path rather than the LLM fallback - a suggestion that costs a model call just
to work out what it meant is a poor suggestion. `test_suggested_chips_route_where_they_say`
holds that, and will fail if either the wording or the router's patterns drift.

A chip that cannot be a complete request on its own does not send. "Change this policy" is
not an instruction, and answering it costs a round trip to ask the obvious question, so
that one loads the composer and waits for the user to finish the sentence.
"""

from __future__ import annotations

import json


def _chip(label: str, prompt: str, *, send: bool = True) -> dict:
    return {"label": label, "prompt": prompt, "send": send}


RUN_TESTS = _chip("Run the tests", "Run the test suite")
CHECK_PROBLEMS = _chip("Check for problems", "Check this policy for problems")
EXPLAIN = _chip("Explain it", "Explain what this policy does")
# Deliberately unfinished, and deliberately not "Add a rule": removing and rewriting are
# just as common, and a prefill that assumes one of them is a prefill people delete.
CHANGE = _chip("Make a change", "Change this policy so that ", send=False)
FIX_FAILURES = _chip("Fix the failures", "Fix the cases that failed")
FIX_FINDINGS = _chip("Fix the findings", "Fix the problems you found")


def follow_ups(values: dict) -> list[dict]:
    """The moves worth offering after a turn that ended in an answer.

    `values` is the settled agent state. Nothing is offered when there is no policy to act
    on: every chip here is a thing to do *to* one.
    """
    if not _has_policy(values):
        return []

    intent = str(values.get("intent") or "").upper()

    if intent == "TEST":
        # Failures first. Anything else - explain it, change it - is a worse next move than
        # dealing with what the run just found.
        head = [FIX_FAILURES] if _tests_failed(values) else []
        return head + [EXPLAIN, CHANGE]

    if intent == "LINT":
        head = [FIX_FINDINGS] if values.get("lint_findings") else []
        return head + [RUN_TESTS, CHANGE]

    # EXPLAIN, CREATE and MODIFY all leave a policy the user has just read about or just
    # received, and has not yet put under any kind of check.
    return [RUN_TESTS, CHECK_PROBLEMS, CHANGE]


def _has_policy(values: dict) -> bool:
    for key in ("jdm_json", "existing_jdm_json", "canvas_jdm_json"):
        raw = values.get(key) or ""
        if raw.strip() and raw.strip() not in ("{}", "null"):
            return True
    return False


def _tests_failed(values: dict) -> bool:
    """Did the suite this turn ran come back with something wrong?

    `evaluation_feedback` carries the run summary as JSON. A turn that could not run the
    suite at all leaves prose or nothing there, and neither is a failure count - so an
    unreadable summary offers the ordinary chips rather than claiming a failure.
    """
    try:
        summary = json.loads(values.get("evaluation_feedback") or "")
    except (TypeError, ValueError):
        return False
    if not isinstance(summary, dict):
        return False
    return bool(summary.get("failed")) or bool(summary.get("errored"))
