import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError, validate

from dreaming.config import ROLES, BackendConfig, load_config, load_lenses
from dreaming.roles import RoleSet

INSTRUCTIONS = Path(__file__).resolve().parents[1] / "instructions"


@pytest.mark.parametrize(
    "role,payload",
    [
        ("reader", {"observations": []}),
        ("reviewer", {"operations": []}),
        ("researcher", {"findings": [], "unavailable_sources": []}),
        (
            "author",
            {
                "disposition": "defer",
                "reason": "insufficient evidence",
                "evidence_ids": [],
                "edits": [],
                "learning": None,
                "external_input": False,
                "depends_on": [],
                "skill": None,
            },
        ),
        ("replayer", {"response": "A bounded replay response"}),
    ],
)
def test_role_schemas_validate_good_and_reject_bad_outputs(role, payload):
    schema = json.loads((INSTRUCTIONS / "schemas" / f"{role}.json").read_text())
    Draft202012Validator.check_schema(schema)
    validate(payload, schema)
    with pytest.raises(ValidationError):
        validate({"unexpected": "content"}, schema)
    for key in payload:
        bad = {**payload, key: 42}
        with pytest.raises(ValidationError):
            validate(bad, schema)


def test_observation_requires_provenance_and_preserves_modality():
    schema = json.loads((INSTRUCTIONS / "schemas" / "reader.json").read_text())
    observation = dict(
        source="claude",
        session_id="s",
        entry_id="e",
        source_location="entry:0",
        representation_version=1,
        quote="Perhaps use Make",
        kind="preference",
        modality="hedged",
        outcome="unknown",
        outcome_evidence=[],
        lens_id="preferences",
        lens_version="1",
        pattern_key="make",
        subject="user",
    )
    validate({"observations": [observation]}, schema)
    for key in ("quote", "modality", "session_id", "source_location"):
        bad = dict(observation)
        del bad[key]
        with pytest.raises(ValidationError):
            validate({"observations": [bad]}, schema)


def test_initial_lenses_cover_required_topics_and_load():
    lenses = load_lenses(INSTRUCTIONS / "lenses.toml")
    assert {lens.id for lens in lenses} == {
        "preferences",
        "successful_approaches",
        "explanations",
        "mcp_tools",
        "skills",
        "xcode",
        "learning_stalls",
        "repeated_steps",
        "deliverables",
        "context_weight",
        "references",
    }
    assert all(lens.version and lens.look_for and lens.changes for lens in lenses)


def test_reviewer_has_only_bounded_summaries_and_evidence_ids(tmp_path):
    config = load_config(tmp_path / "config.toml")
    config = replace(
        config,
        backends={"a": BackendConfig(context_tokens=4096), "b": BackendConfig(context_tokens=3072)},
        roles={**config.roles, "reviewer": ("a", "b")},
    )
    roles = RoleSet(INSTRUCTIONS, config)
    request = roles.build(
        "reviewer", summaries=[{"summary": "x" * 1000, "evidence_ids": ["e"]} for _ in range(20)]
    )
    assert request.context_tokens == 3072
    assert request.estimated_tokens <= 3072 - request.output_tokens
    assert request.omitted_count > 0
    assert "evidence_ids" in request.prompt
    with pytest.raises(ValueError, match="transcript"):
        roles.build("reviewer", transcripts=["full session"])
    with pytest.raises(ValueError, match="transcript"):
        roles.build("reviewer", summaries=[{"transcript": "hidden full session"}])


def test_replayer_sees_only_opening_prompt_and_instruction(tmp_path):
    roles = RoleSet(INSTRUCTIONS, load_config(tmp_path / "config.toml"))
    request = roles.build(
        "replayer", opening_prompt="explain this", instruction="Use simple language"
    )
    assert not request.tools
    assert "Use simple language" in request.prompt
    with pytest.raises(ValueError):
        roles.build("replayer", opening_prompt="hi", instruction="one", knowledge=["unrelated"])


def test_no_role_exposes_agent_tools_and_reader_input_is_never_cut(tmp_path):
    config = load_config(tmp_path / "config.toml")
    roles = RoleSet(INSTRUCTIONS, config)
    assert all("Agent" not in config.tools[role] for role in ROLES)
    bad = replace(config, tools={**config.tools, "author": ("spawn_agent",)})
    with pytest.raises(ValueError, match="agent"):
        RoleSet(INSTRUCTIONS, bad).build("author", candidate={})
    with pytest.raises(ValueError, match="split"):
        roles.build("reader", sessions=["x" * 900000], lenses=[])
