"""The Huddles client: the wake feed and the three MCP tool calls, testable without Hermes.

The Huddles MCP endpoint is stateless JSON-RPC (no session or initialize handshake), so every
call is one POST. The wake feed is one GET that long-polls server-side. Bearer is always the
participation's connector token — the same one for the feed and for MCP. Every method is async:
a 20-second long-poll must never block the gateway's event loop.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

MCP_PROTOCOL_VERSION = "2025-11-25"

WAKES_PATH = "/external-chat/wakes/next"
MCP_PATH = "/external-chat/mcp"


class HuddlesAuthError(Exception):
    """The connector token is gone (401/403): the grant was revoked or expired.

    The loop stops and the bot needs a new invite; retrying forever cannot help.
    """


class HuddlesToolError(Exception):
    """Huddles answered, but the tool call failed."""


@dataclass
class Wake:
    """One pulled wake: the identifiers Huddles names, nothing more."""

    workspace_id: str
    wake_id: str
    participation_id: str
    thread: dict[str, Any] = field(default_factory=dict)
    priority: str = "ambient"

    @classmethod
    def from_envelope(cls, envelope: dict[str, Any]) -> "Wake":
        try:
            body = envelope["huddles_wake"]
            return cls(
                workspace_id=body["workspace_id"],
                wake_id=body["wake_id"],
                participation_id=body["participation_id"],
                thread=body.get("thread", {}),
                priority=body.get("priority", "ambient"),
            )
        except (KeyError, TypeError) as cause:
            raise HuddlesToolError("malformed wake envelope") from cause


@dataclass
class RoomRead:
    """A wake's sealed room context: the transcript to show, plus the raw result."""

    transcript: str
    result: dict[str, Any]


Json = dict[str, Any]
Handler = Any  # sync httpx.MockTransport handler


def redeem_invite(invite_url: str, *, timeout: float = 40.0) -> Json:
    """POST the single-use invite link once and return its answer (mcp_url, setup_token,
    participation_id, mention). A plain GET explains the link without consuming it; a POST
    redeems it. The link is the only credential the invite ever needs. Sync: join runs as a
    CLI command, where a blocking POST is fine."""
    with httpx.Client(timeout=timeout) as client:
        response = client.post(invite_url, content=b"", headers={"accept": "application/json"})
    if response.status_code != 200:
        raise HuddlesToolError(f"the invite link answered {response.status_code}")
    return response.json()


class HuddlesClient:
    """One participation's client: wake feed plus read/reply/observe over MCP."""

    def __init__(
        self,
        mcp_url: str,
        token: str,
        *,
        handler: Handler | None = None,
        timeout: float = 40.0,
    ) -> None:
        # `mcp_url` is the full MCP endpoint the invite names (…/external-chat/mcp); the feed
        # sits beside it. Strip the suffix so a doubled path can never be requested.
        base = mcp_url.rstrip("/")
        if base.endswith(MCP_PATH):
            base = base[: -len(MCP_PATH)]
        self._mcp_url = base
        self._token = token
        # A real run uses one connection pool for feed and MCP; a test injects a handler.
        if handler is not None:
            self._client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler), timeout=timeout
            )
        else:
            self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._token}",
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            "mcp-protocol-version": MCP_PROTOCOL_VERSION,
        }

    async def next_wake(self, wait_seconds: int = 20) -> Wake | None:
        """Fetch the next due wake, waiting up to `wait_seconds` (at most 25) server-side.

        Answers `None` when Huddles has nothing due (204). Raises `HuddlesAuthError` when the
        token's grant is gone: the caller must stop, not retry.
        """
        wait = max(0, min(int(wait_seconds), 25))
        response = await self._client.get(
            f"{self._mcp_url}{WAKES_PATH}",
            params={"wait": wait},
            headers={"authorization": f"Bearer {self._token}"},
        )
        self._raise_for_auth(response)
        if response.status_code == 204:
            return None
        if response.status_code != 200:
            raise HuddlesToolError(f"wake feed answered {response.status_code}")
        return Wake.from_envelope(response.json())

    async def read_room(self, participation_id: str, wake_id: str) -> RoomRead:
        result = await self._call(
            "huddles_read_room", {"participation_id": participation_id, "wake_id": wake_id}
        )
        transcript = ""
        for part in result.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "text":
                transcript = part.get("text", "")
                break
        return RoomRead(transcript=transcript, result=result)

    async def reply(self, participation_id: str, wake_id: str, text: str) -> Json:
        """Post the reply for one wake. `idempotency_key` is the `wake_id`, so a retry of the
        same reply folds away instead of posting twice."""
        return await self._call(
            "huddles_reply",
            {
                "participation_id": participation_id,
                "wake_id": wake_id,
                "idempotency_key": wake_id,
                "content": {"format": "plain", "text": text},
            },
        )

    async def observe(self, participation_id: str, wake_id: str, outcome: str = "no_op") -> Json:
        """Record that the wake was seen without a reply, so viewers are not left waiting."""
        return await self._call(
            "huddles_observe",
            {
                "participation_id": participation_id,
                "wake_id": wake_id,
                "idempotency_key": wake_id,
                "outcome": outcome,
            },
        )

    async def setup(
        self,
        setup_token: str,
        participation_id: str,
        idempotency_key: str,
        webhook: Json,
    ) -> Json:
        """Finish setup with the invite's setup token: register the wake destination and
        subscribe. The answer carries the long-lived `connector_token`."""
        bearer = self._token
        self._token = setup_token
        try:
            return await self._call(
                "huddles_setup",
                {
                    "participation_id": participation_id,
                    "idempotency_key": idempotency_key,
                    "webhook": webhook,
                },
            )
        finally:
            self._token = bearer

    async def _call(self, name: str, arguments: Json) -> Json:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        response = await self._client.post(
            f"{self._mcp_url}{MCP_PATH}", headers=self._headers(), content=json.dumps(body)
        )
        self._raise_for_auth(response)
        if response.status_code != 200:
            raise HuddlesToolError(f"{name} answered {response.status_code}")
        payload = response.json()
        if "error" in payload:
            raise HuddlesToolError(f"{name}: {payload['error'].get('message', 'tool error')}")
        result = payload.get("result") or {}
        # The modern MCP Events shape wraps complete results; both carry the same fields.
        if isinstance(result, dict) and result.get("resultType") == "complete":
            result = {key: value for key, value in result.items() if key != "resultType"}
        if result.get("isError"):
            text = ""
            for part in result.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text", "")
                    break
            raise HuddlesToolError(f"{name}: {text or 'tool error'}")
        return result

    @staticmethod
    def _raise_for_auth(response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            raise HuddlesAuthError(
                f"Huddles answered {response.status_code}: the connector token's grant is gone;"
                " ask for a new invite."
            )


def structured(result: Json) -> Json:
    """A tool result's structured content, or the result itself when it has none."""
    nested = result.get("structuredContent")
    return nested if isinstance(nested, dict) else result
