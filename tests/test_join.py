"""The join flow: redeem once, set up with the pull destination, write the env file 0600."""
from __future__ import annotations

import os
import stat

import httpx

from fake_huddles import (
    CONNECTOR_TOKEN,
    FakeHuddles,
    INVITE_URL,
    MCP_URL,
    PARTICIPATION,
    SETUP_TOKEN,
)
from huddles_client import HuddlesClient
from join import join, read_env


def _patch_fake(join_module, monkeypatch, fake) -> None:
    """Point join at the fake: the redeem POST and the MCP client both hit the MockTransport."""
    monkeypatch.setattr(
        join_module,
        "redeem_invite",
        lambda invite_url, **kwargs: {
            "mcp_url": MCP_URL,
            "setup_token": SETUP_TOKEN,
            "participation_id": PARTICIPATION,
            "mention": "hermes",
        },
    )
    monkeypatch.setattr(
        join_module,
        "HuddlesClient",
        lambda mcp_url, token, **kwargs: HuddlesClient(MCP_URL, token, handler=fake.fetcher()),
    )


def test_join_redeems_sets_up_pull_and_writes_the_env_file(tmp_path, monkeypatch, capsys) -> None:
    fake = FakeHuddles()
    env_file = tmp_path / ".hermes" / ".env"
    env_file.parent.mkdir(parents=True)

    import join as join_module

    _patch_fake(join_module, monkeypatch, fake)

    code = join(INVITE_URL, env_path=env_file)
    assert code == 0
    # The token went to the file, mode 0600, and never to stdout.
    values = read_env(env_file)
    assert values["HUDDLES_CONNECTOR_TOKEN"] == CONNECTOR_TOKEN
    assert values["HUDDLES_MCP_URL"] == MCP_URL
    assert values["HUDDLES_PARTICIPATION_ID"] == PARTICIPATION
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert CONNECTOR_TOKEN not in out
    assert "Joined as @hermes" in out
    assert "Run hermes gateway restart." in out
    # The setup call carried the pull webhook; no reply was sent on join's behalf.
    assert fake.calls_by_name("huddles_setup")[0].arguments["webhook"] == {"kind": "pull"}


def test_join_merges_into_an_existing_env_file_and_keeps_other_lines(tmp_path, monkeypatch) -> None:
    fake = FakeHuddles()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# hermes config\nOTHER_PROFILE_KEY=keep\nHUDDLES_CONNECTOR_TOKEN=stale\n",
        encoding="utf-8",
    )

    import join as join_module

    _patch_fake(join_module, monkeypatch, fake)

    assert join(INVITE_URL, env_path=env_file) == 0
    values = read_env(env_file)
    assert values["HUDDLES_CONNECTOR_TOKEN"] == CONNECTOR_TOKEN
    assert values["OTHER_PROFILE_KEY"] == "keep"
    assert "# hermes config" in env_file.read_text(encoding="utf-8")


def test_a_lost_setup_answer_is_retried_with_the_same_idempotency_key(
    tmp_path, monkeypatch, capsys
) -> None:
    fake = FakeHuddles()
    env_file = tmp_path / ".env"

    import join as join_module

    attempts = {"n": 0}
    inner = fake.fetcher()

    def lost_answer(request: httpx.Request) -> httpx.Response:
        """The first setup reaches Huddles but its answer is lost in transit."""
        if b"huddles_setup" in request.content:
            attempts["n"] += 1
            if attempts["n"] == 1:
                inner(request)  # Huddles processed it; the answer never came back
                raise httpx.ConnectError("answer lost")
        return inner(request)

    monkeypatch.setattr(
        join_module,
        "redeem_invite",
        lambda invite_url, **kwargs: {
            "mcp_url": MCP_URL,
            "setup_token": SETUP_TOKEN,
            "participation_id": PARTICIPATION,
            "mention": "hermes",
        },
    )
    monkeypatch.setattr(
        join_module,
        "HuddlesClient",
        lambda mcp_url, token, **kwargs: HuddlesClient(
            MCP_URL, token, handler=lost_answer
        ),
    )

    assert join(INVITE_URL, env_path=env_file) == 0
    setups = fake.calls_by_name("huddles_setup")
    assert len(setups) == 2
    assert {call.arguments["idempotency_key"] for call in setups} == {"setup-1"}
    assert read_env(env_file)["HUDDLES_CONNECTOR_TOKEN"] == CONNECTOR_TOKEN
