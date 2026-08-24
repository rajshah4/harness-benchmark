import copy

import pytest

import run_suite


def skill(name):
    return {
        "name": name,
        "content": f"instructions for {name}",
        "trigger": None,
        "source": "public",
        "description": None,
        "is_agentskills_format": True,
    }


def test_curated_context_is_identical_across_native_and_acp(monkeypatch):
    catalog = [skill(name) for name in run_suite.CURATED_CANVAS_SKILLS]
    catalog.append(skill("add-javadoc"))
    materialized = {
        "GLM": {
            "agent_kind": "openhands",
            "llm": {"api_key": "redacted"},
            "agent_context": {"skills": catalog, "load_public_skills": False},
        },
        "ODSC-Pi-GLM52": {
            "agent_kind": "acp",
            "agent_context": {"skills": []},
        },
    }

    monkeypatch.setattr(
        run_suite,
        "materialize_profile",
        lambda name: copy.deepcopy(materialized[name]),
    )
    monkeypatch.setattr(run_suite, "api_key", lambda: "session-key")
    monkeypatch.setattr(
        run_suite,
        "secret_value",
        lambda name, url, key: "provider-key",
    )

    result = run_suite.resolve_curated_agent_settings(("openhands", "pi"))
    expected = list(run_suite.CURATED_CANVAS_SKILLS)
    for settings in result.values():
        context = settings["agent_context"]
        assert [item["name"] for item in context["skills"]] == expected
        assert "add-javadoc" not in expected
        assert context["load_public_skills"] is False
        assert context["load_user_skills"] is True
        assert context["load_project_skills"] is True
        assert context["disabled_skills"] == []
    assert result["openhands"]["llm"]["api_key"] == "provider-key"


def test_curated_context_fails_closed_when_catalog_is_incomplete(monkeypatch):
    monkeypatch.setattr(
        run_suite,
        "materialize_profile",
        lambda _name: {"agent_context": {"skills": [skill("github")]}},
    )

    with pytest.raises(RuntimeError, match="missing curated defaults"):
        run_suite.resolve_curated_agent_settings(("openhands",))


def test_launch_requires_exactly_one_agent_source(tmp_path):
    with pytest.raises(ValueError, match="exactly one agent source"):
        run_suite.launch("run", "task", "openhands", None, None, tmp_path, False)
