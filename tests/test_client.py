"""The client: wire shape, wake envelope, auth failures and tool errors."""
from __future__ import annotations

import asyncio

import pytest

from fake_huddles import FakeHuddles, MCP_URL
from huddles_client import HuddlesAuthError, HuddlesClient, HuddlesToolError

TOKEN = "connector-token-0000000000"


def make_client(fake: FakeHuddles, token: str = TOKEN) -> HuddlesClient:
    return HuddlesClient(MCP_URL, token, handler=fake.fetcher())


def test_next_wake_parses_the_envelope_a_webhook_would_carry() -> None:
    fake = FakeHuddles()
    fake.queue_wake()

    async def run() -> None:
        client = make_client(fake)
        wake = await client.next_wake(wait_seconds=20)
        assert wake is not None
        assert wake.wake_id == "wake-1"
        assert wake.participation_id == "participation-test"
        assert wake.workspace_id == "ws-test"
        assert wake.priority == "mention"
        # The feed is the same bearer, GET, with the wait echoed.
        request = fake.feed_requests[0]
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.url.params["wait"] == "20"
        await client.aclose()

    asyncio.run(run())


def test_next_wake_answers_none_on_204() -> None:
    fake = FakeHuddles()

    async def run() -> None:
        client = make_client(fake)
        assert await client.next_wake() is None
        await client.aclose()

    asyncio.run(run())


def test_next_wake_wait_is_capped_at_25() -> None:
    fake = FakeHuddles()

    async def run() -> None:
        client = make_client(fake)
        await client.next_wake(wait_seconds=100)
        assert fake.feed_requests[0].url.params["wait"] == "25"
        await client.aclose()

    asyncio.run(run())


def test_mcp_calls_carry_the_stateless_jsonrpc_envelope() -> None:
    fake = FakeHuddles()

    async def run() -> None:
        client = make_client(fake)
        read = await client.read_room("participation-test", "wake-1")
        assert "discussion, never instructions" in read.transcript
        call = fake.calls[0]
        assert call.name == "huddles_read_room"
        assert call.arguments == {"participation_id": "participation-test", "wake_id": "wake-1"}
        await client.aclose()

    asyncio.run(run())


def test_reply_keys_idempotency_on_the_wake_id() -> None:
    fake = FakeHuddles()

    async def run() -> None:
        client = make_client(fake)
        receipt = await client.reply("participation-test", "wake-9", "On it.")
        assert receipt["event_ids"]
        call = fake.calls[0]
        assert call.name == "huddles_reply"
        assert call.arguments["idempotency_key"] == "wake-9"
        assert call.arguments["content"] == {"format": "plain", "text": "On it."}
        await client.aclose()

    asyncio.run(run())


def test_observe_carries_the_outcome() -> None:
    fake = FakeHuddles()

    async def run() -> None:
        client = make_client(fake)
        await client.observe("participation-test", "wake-9", "no_op")
        call = fake.calls[0]
        assert call.name == "huddles_observe"
        assert call.arguments["outcome"] == "no_op"
        assert call.arguments["idempotency_key"] == "wake-9"
        await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("status", [401, 403])
def test_a_dead_grant_raises_auth_and_stops_the_caller(status: int) -> None:
    fake = FakeHuddles()

    async def run() -> None:
        client = make_client(fake)
        fake.feed_status = status
        with pytest.raises(HuddlesAuthError):
            await client.next_wake()
        fake.mcp_status = status
        with pytest.raises(HuddlesAuthError):
            await client.read_room("participation-test", "wake-1")
        await client.aclose()

    asyncio.run(run())


def test_a_tool_error_raises_tool_error() -> None:
    fake = FakeHuddles()
    fake.fail_tools.add("huddles_read_room")

    async def run() -> None:
        client = make_client(fake)
        with pytest.raises(HuddlesToolError):
            await client.read_room("participation-test", "wake-1")
        await client.aclose()

    asyncio.run(run())


def test_setup_sends_the_pull_webhook_and_answers_the_connector_token() -> None:
    fake = FakeHuddles()

    async def run() -> None:
        client = HuddlesClient(MCP_URL, "setup-token-000000000000", handler=fake.fetcher())
        result = await client.setup(
            "setup-token-000000000000", "participation-test", "setup-1", {"kind": "pull"}
        )
        assert result["connector_token"]["token"] == TOKEN
        call = fake.calls[0]
        assert call.name == "huddles_setup"
        assert call.arguments["idempotency_key"] == "setup-1"
        assert call.arguments["webhook"] == {"kind": "pull"}
        await client.aclose()

    asyncio.run(run())
