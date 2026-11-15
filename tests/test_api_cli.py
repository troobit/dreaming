import io
import json

import pytest

from dreaming.cli import main


@pytest.mark.parametrize(
    "payload,code,exit_code",
    [
        ('{"dreaming_api":1,"op":"knowledge.recall"}', None, 0),
        ('{"dreaming_api":2,"op":"knowledge.recall"}', "incompatible_version", 2),
        ('{"dreaming_api":1,"op":"knowledge.get","id":"unknown"}', "not_found", 2),
        ('{"dreaming_api":1,"op":"unknown"}', "unknown_operation", 2),
        ("{bad", "invalid_request", 2),
        ('{"dreaming_api":1,"op":"knowledge.recall"}\n{}', "invalid_request", 2),
    ],
)
def test_api_one_json_response_and_no_read_side_effects(
    tmp_path, monkeypatch, capsys, payload, code, exit_code
):
    home = tmp_path / "absent-home"
    monkeypatch.setenv("DREAMING_HOME", str(home))
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert main(["api"]) == exit_code
    captured = capsys.readouterr()
    assert not captured.err
    assert len(captured.out.splitlines()) == 1
    response = json.loads(captured.out)
    assert response["dreaming_api"] == 1
    assert response.get("code") == code
    assert not home.exists()


def test_api_config_error_is_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DREAMING_HOME", str(tmp_path / "home"))
    config = tmp_path / "bad.toml"
    config.write_text("unexpected = true\n")
    monkeypatch.setattr("sys.stdin", io.StringIO('{"dreaming_api":1,"op":"knowledge.recall"}'))
    assert main(["--config", str(config), "api"]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "invalid_config"
