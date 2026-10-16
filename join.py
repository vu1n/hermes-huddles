"""Join a Huddles room from its single-use invite link — the whole setup in one command.

    python3 join.py <invite-link>          # standalone
    hermes huddles join <invite-link>      # the plugin's CLI subcommand

Steps: redeem the link once, call `huddles_setup` with the pull destination (retried with the
same idempotency key, which answers the same connector token), then write the token to
`~/.hermes/.env` (mode 0600) with the MCP URL and participation the env-enablement hook seeds
into `gateway.platforms.huddles.extra`. The token goes from Huddles to the env file without
passing through any model's context, and nothing secret is printed.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import stat
import sys
from pathlib import Path

import httpx

try:
    from .huddles_client import (
        HuddlesAuthError,
        HuddlesClient,
        HuddlesToolError,
        redeem_invite,
        structured,
    )
except ImportError:  # run as a script: the plugin directory is on sys.path
    from huddles_client import (
        HuddlesAuthError,
        HuddlesClient,
        HuddlesToolError,
        redeem_invite,
        structured,
    )

ENV_TOKEN = "HUDDLES_CONNECTOR_TOKEN"
ENV_MCP_URL = "HUDDLES_MCP_URL"
ENV_PARTICIPATION = "HUDDLES_PARTICIPATION_ID"

DEFAULT_ENV_PATH = Path.home() / ".hermes" / ".env"


def join(invite_link: str, env_path: str | Path | None = None) -> int:
    """Run the whole setup. Returns a process exit code; prints nothing secret."""
    path = Path(env_path) if env_path is not None else DEFAULT_ENV_PATH
    print("Redeeming the invite link (it works once) ...")
    try:
        invite = redeem_invite(invite_link)
    except (httpx.HTTPError, HuddlesToolError) as cause:
        print(f"The invite link could not be redeemed: {cause}")
        return 1
    mcp_url = str(invite.get("mcp_url", "")).strip()
    participation = str(invite.get("participation_id", "")).strip()
    mention = str(invite.get("mention", "")).strip() or "you"
    setup_token = str(invite.get("setup_token", ""))
    if not (mcp_url and participation and setup_token):
        print("The invite answer was incomplete; ask for a new link.")
        return 1

    print("Finishing setup with the pull destination (no webhook needed) ...")

    async def run() -> tuple[dict, str, object]:
        client = HuddlesClient(mcp_url, setup_token)
        try:
            answer = await _setup_with_retry(client, setup_token, participation)
            result = structured(answer)
            connector = result.get("connector_token") or {}
            return result, str(connector.get("token", "")).strip(), result.get("destination")
        finally:
            await client.aclose()

    try:
        result, token, destination = asyncio.run(run())
    except (httpx.HTTPError, HuddlesAuthError, HuddlesToolError) as cause:
        print(f"huddles_setup failed: {cause}")
        return 1
    if destination not in (None, "set"):
        print(f"huddles_setup answered destination {destination!r}; ask for a new link.")
        return 1
    if not token:
        print("huddles_setup answered no connector_token; ask for a new link.")
        return 1

    write_env(path, {ENV_TOKEN: token, ENV_MCP_URL: mcp_url, ENV_PARTICIPATION: participation})
    print(f"Joined as @{mention}. Wakes are fetched from Huddles; nothing of yours is exposed.")
    print("Run hermes gateway restart.")
    return 0


async def _setup_with_retry(
    client: HuddlesClient, setup_token: str, participation: str, idempotency_key: str = "setup-1"
):
    """`huddles_setup` with the pull webhook. A lost answer is retried with the same
    idempotency key, which answers the same connector token — never a second bot."""
    webhook = {"kind": "pull"}
    try:
        return await client.setup(setup_token, participation, idempotency_key, webhook)
    except httpx.HTTPError:
        print("The answer was lost; retrying with the same idempotency key ...")
        return await client.setup(setup_token, participation, idempotency_key, webhook)


def write_env(path: Path, values: dict[str, str]) -> None:
    """Merge `values` into the env file, creating it 0600. Existing keys are replaced; every
    other line, comment included, is kept. Values never reach stdout."""
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    remaining = dict(values)
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip() if "=" in line else None
        if key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}"
    for key, value in remaining.items():
        lines.append(f"{key}={value}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines).rstrip("\n") + "\n")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def print_status(env_path: str | Path | None = None) -> None:
    """`hermes huddles status`: whether the token and its coordinates are present, never values."""
    path = Path(env_path) if env_path is not None else DEFAULT_ENV_PATH
    values = read_env(path)
    for key, label in (
        (ENV_TOKEN, "connector token"),
        (ENV_MCP_URL, "MCP URL"),
        (ENV_PARTICIPATION, "participation"),
    ):
        state = "set" if values.get(key, "").strip() else "missing"
        print(f"{label}: {state}")
    if all(values.get(k, "").strip() for k in (ENV_TOKEN, ENV_MCP_URL, ENV_PARTICIPATION)):
        print("Run hermes gateway restart if you have not yet.")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="huddles-join", description="Join a Huddles room")
    parser.add_argument("invite_link", help="The single-use invite link from Add bot")
    parser.add_argument(
        "--env-file", default=None, help="Write the token here instead of ~/.hermes/.env"
    )
    args = parser.parse_args(argv)
    return join(args.invite_link, env_path=args.env_file)


if __name__ == "__main__":
    sys.exit(main())
