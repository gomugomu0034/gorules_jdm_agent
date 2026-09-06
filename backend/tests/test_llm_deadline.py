"""A model call has to be able to run out of time.

`LLM_TIMEOUT` was unenforceable on the OpenRouter path, and not by a little. `requests`
bounds the gap *between* socket reads, not the call; OpenRouter pads a non-streaming
response with whitespace while the upstream thinks - confirmed on the wire, a body that
opens with hundreds of blank lines - so bytes kept arriving and the read timeout could
never fire. One explain request spent ten minutes on the rail saying "Reading the graph"
and produced nothing at all.

The builder's attempt arithmetic (`_attempt_budget`) divides the whole-run budget by
`LLM_TIMEOUT_SECONDS` to decide how many repairs it can afford. That sum is only true if a
call really does end when the timeout says so.
"""

from __future__ import annotations

import time

import pytest

from backend import lang_graph_agent as agent


class Padded:
    """A response that keeps sending keepalive whitespace and never finishes."""

    def __init__(self, forever: bool = True):
        self.forever = forever
        self.chunks_read = 0

    def iter_content(self, chunk_size: int = 8192):
        while True:
            self.chunks_read += 1
            time.sleep(0.01)
            yield b"\n         \n"
            if not self.forever and self.chunks_read > 3:
                yield b'{"ok": true}'
                return


def test_a_padded_response_still_runs_out_of_time():
    deadline = time.monotonic() + 0.05
    with pytest.raises(agent.LLMTimeout) as excinfo:
        agent._read_within(Padded(), deadline)

    # `_error_code` reads the message to tell a timeout from an agent bug; without the
    # word the chat would blame the agent for the provider's silence.
    assert "timed out" in str(excinfo.value).lower()
    from backend.services import chat_runner
    assert chat_runner._error_code(excinfo.value) == "LLM_TIMEOUT"


def test_a_response_that_arrives_in_time_is_returned_whole():
    body = agent._read_within(Padded(forever=False), time.monotonic() + 30)
    assert body.endswith(b'{"ok": true}')
    # The padding is part of the body and `json.loads` skips it, which is what makes the
    # non-streaming parse work unchanged.
    import json
    assert json.loads(body) == {"ok": True}


def test_the_deadline_is_the_configured_timeout_not_the_socket_gap():
    """A regression guard on the shape of the call itself.

    The bug was a single argument: `timeout=LLM_TIMEOUT_SECONDS` reads as a total and is
    not one. Both halves have to be present - a connect/read pair *and* a wall clock.
    """
    import inspect

    source = inspect.getsource(agent._call_openrouter)
    assert "_read_within" in source, "nothing bounds the total time this call can take"
    assert "stream=True" in source, "the body cannot be read under a deadline unless it is streamed"
