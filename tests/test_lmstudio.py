"""The direct LM Studio adapter against a local fake server (5.1, 5.8)."""

import json

import pytest
from lmstudio_fake import FakeLMStudio, model
from runkit import CallPhase, FakeClock, add_run, reads

from dreaming import config, run
from dreaming.backends import BackendError, lmstudio
from dreaming.errors import DreamingError

RUN = "dream-20261115-090000-abcd"
SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
    "additionalProperties": False,
}


@pytest.fixture
def server():
    fake = FakeLMStudio().start()
    fake.answer = {"value": 7}
    yield fake
    fake.stop()


def make_cfg(url, model_id="qwen3", **sections):
    return config.build_config(
        {
            "backends": {
                "local": {
                    "harness": "lmstudio",
                    "model": model_id,
                    "base_url": url,
                    "context_tokens": 32_768,
                }
            },
            "roles": {
                "reader": {"backends": ["local"]},
                "reviewer": {"backends": ["local"]},
            },
            **sections,
        }
    )


def backend(cfg, home, run_id=RUN):
    return lmstudio.LMStudioBackend(cfg, "local", home=home, run_id=run_id)


def test_a_call_returns_the_reply_usage_and_the_run_as_its_thread(server, home):
    result = backend(make_cfg(server.url), home).call("reader", "read this", SCHEMA, [])
    assert result.output == {"value": 7}
    assert (result.tokens_in, result.tokens_out) == (12, 4)
    assert result.session_ref == RUN
    assert result.model == "qwen3"
    (chat,) = server.chats()
    assert chat["model"] == "qwen3"
    assert chat["messages"] == [{"role": "user", "content": "read this"}]
    assert chat["response_format"]["type"] == "json_schema"
    assert chat["response_format"]["json_schema"]["schema"] == SCHEMA


def test_the_run_id_leads_the_body_so_a_truncated_log_entry_keeps_it(server, home):
    """LM Studio's log cuts long bodies; the run id must survive the cut (5.8)."""
    backend(make_cfg(server.url), home).call("reader", "x" * 50_000, SCHEMA, [])
    (body,) = server.bodies
    head = f'{{"client_metadata": {{"thread_id": "{RUN}"}}'.encode()
    assert body.startswith(head)
    assert RUN.encode() in body[: len(head)]


def test_the_context_is_read_from_the_loaded_model_on_every_call(server, home):
    local = backend(make_cfg(server.url), home)
    local.call("reader", "p", SCHEMA, [])
    assert local.context_tokens == 32_768
    server.models = [model("qwen3", context=8_192)]
    local.call("reader", "p", SCHEMA, [])
    assert local.context_tokens == 8_192


def test_an_input_larger_than_the_loaded_context_is_not_sent(server, home):
    server.models = [model("qwen3", context=1_000)]
    with pytest.raises(BackendError) as excinfo:
        backend(make_cfg(server.url), home).call("reader", "x" * 8_000, SCHEMA, [])
    assert "1000-token context" in excinfo.value.message
    assert server.bodies == []


@pytest.mark.parametrize(
    "models",
    [
        [model("qwen3", state="not-loaded")],
        [model("other")],
        [],
    ],
)
def test_a_model_that_is_not_loaded_is_a_failed_call(server, home, models):
    """Dreaming never loads a model, so nothing is sent for LM Studio to load
    on demand."""
    server.models = models
    with pytest.raises(BackendError) as excinfo:
        backend(make_cfg(server.url), home).call("reader", "p", SCHEMA, [])
    assert "not loaded" in excinfo.value.message
    assert server.bodies == []


def test_without_a_configured_model_the_loaded_chat_model_is_used(server, home):
    server.models = [
        model("nomic-embed", kind="embeddings"),
        model("idle", state="not-loaded"),
        model("gemma-4", context=16_384, kind="vlm"),
    ]
    local = backend(make_cfg(server.url, model_id=""), home)
    local.call("reader", "p", SCHEMA, [])
    assert server.chats()[0]["model"] == "gemma-4"
    assert local.context_tokens == 16_384


def test_tool_using_roles_are_refused(server, home):
    with pytest.raises(DreamingError) as excinfo:
        backend(make_cfg(server.url), home).call("researcher", "p", SCHEMA, ["read"])
    assert excinfo.value.code == "invalid_config"
    assert server.bodies == []
    with pytest.raises(DreamingError):
        make_cfg(server.url, roles={"researcher": {"backends": ["local"]}})


def test_errors_carry_the_raw_server_text(server, home):
    server.replies = [(400, '{"error": "Model unloaded mid-request"}')]
    with pytest.raises(BackendError) as excinfo:
        backend(make_cfg(server.url), home).call("reader", "p", SCHEMA, [])
    assert "HTTP 400" in excinfo.value.message
    assert "Model unloaded mid-request" in excinfo.value.message


def test_an_unreachable_server_is_a_failed_call(home):
    with pytest.raises(BackendError) as excinfo:
        backend(make_cfg("http://127.0.0.1:9"), home).call("reader", "p", SCHEMA, [])
    assert excinfo.value.message.startswith("local:")


def test_a_reply_that_is_not_json_is_returned_as_text(server, home):
    server.replies = [{"choices": [{"message": {"content": "sure, here it is"}}]}]
    result = backend(make_cfg(server.url), home).call("reader", "p", SCHEMA, [])
    assert result.output == "sure, here it is"
    assert (result.tokens_in, result.tokens_out) == (None, None)


def test_a_call_past_its_timeout_fails(server, home):
    server.delay = 3
    cfg = make_cfg(server.url, thresholds={"call_timeout_seconds": 1})
    with pytest.raises(BackendError):
        backend(cfg, home).call("reader", "p", SCHEMA, [])


def test_the_calls_row_names_the_run_as_the_session(server, db, home):
    """The calls row and the server log both carry the run id, which is how the
    dream's own LM Studio requests are excluded from selection (1.5)."""
    server.answer = {"value": 1}
    server.usage = None
    cfg = make_cfg(server.url)
    run_id = add_run(db, RUN, cfg=cfg)
    runner = run.Runner(
        db,
        home,
        run_id,
        cfg,
        phases={"read": CallPhase("reader")},
        backends={"local": backend(cfg, home, run_id)},
        planner=reads(1, review=False),
        clock=FakeClock(),
    )
    assert runner.execute() == "complete"
    (row,) = db.execute("SELECT * FROM calls").fetchall()
    assert row["session_ref"] == RUN
    assert row["outcome"] == "ok"
    # LM Studio reported no usage, so the call is counted by estimate (1.2).
    assert row["usage_estimated"] == 1
    assert json.loads(server.bodies[0])["client_metadata"]["thread_id"] == RUN
