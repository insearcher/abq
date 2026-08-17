import json

from abq.registry import Agent, Registry


def make_agent(alias="api", provider="claude", session="s-1", cwd="/tmp"):
    return Agent(alias=alias, provider=provider, cwd=cwd, address={"session_id": session})


def test_join_and_get(tmp_path):
    registry = Registry(str(tmp_path / "registry.json"))
    registry.join(make_agent())

    reloaded = Registry(str(tmp_path / "registry.json"))
    agent = reloaded.get("api")
    assert agent is not None
    assert agent.provider == "claude"
    assert agent.address["session_id"] == "s-1"


def test_rejoin_under_new_alias_drops_the_old_one(tmp_path):
    registry = Registry(str(tmp_path / "registry.json"))
    registry.join(make_agent(alias="api"))
    replaced = registry.join(make_agent(alias="backend"))

    assert replaced is not None and replaced.alias == "api"
    assert registry.get("api") is None
    assert registry.get("backend") is not None
    assert len(registry) == 1


def test_two_sessions_coexist(tmp_path):
    registry = Registry(str(tmp_path / "registry.json"))
    registry.join(make_agent(alias="api", session="s-1"))
    registry.join(make_agent(alias="web", provider="codex", session="s-2"))

    assert len(registry) == 2
    assert [a.alias for a in registry] == ["api", "web"]


def test_by_session_ignores_empty_key(tmp_path):
    registry = Registry(str(tmp_path / "registry.json"))
    registry.join(Agent(alias="ghost", provider="claude", cwd="/tmp", address={}))

    assert registry.by_session("") is None


def test_codex_agents_are_addressed_by_thread_id(tmp_path):
    registry = Registry(str(tmp_path / "registry.json"))
    registry.join(
        Agent(alias="cx", provider="codex", cwd="/tmp", address={"thread_id": "t-9"})
    )

    assert registry.by_session("t-9").alias == "cx"


def test_leave(tmp_path):
    registry = Registry(str(tmp_path / "registry.json"))
    registry.join(make_agent())

    assert registry.leave("api") is True
    assert registry.leave("api") is False
    assert len(Registry(str(tmp_path / "registry.json"))) == 0


def test_saved_file_is_valid_json_with_version(tmp_path):
    path = tmp_path / "registry.json"
    registry = Registry(str(path))
    registry.join(make_agent())

    payload = json.loads(path.read_text())
    assert payload["version"] == 1
    assert "alias" not in payload["agents"]["api"]


def test_missing_file_yields_empty_registry(tmp_path):
    assert len(Registry(str(tmp_path / "nope.json"))) == 0


def test_corrupt_file_yields_empty_registry(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text("{not json")

    assert len(Registry(str(path))) == 0


def test_entries_from_another_tool_version_are_skipped_not_fatal(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps(
            {
                "agents": {
                    "legacy": {"session_id": "old", "team": "session-old", "cwd": "/x"},
                    "future": {"provider": "claude", "cwd": "/y", "address": {},
                               "unknown_field": 1},
                    "junk": "not-a-dict",
                }
            }
        )
    )

    registry = Registry(str(path))
    assert [a.alias for a in registry] == ["future"]


def test_non_object_file_is_treated_as_empty(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text("[1, 2, 3]")

    assert len(Registry(str(path))) == 0
