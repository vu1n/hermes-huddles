# vu1n/hermes-huddles

A [Hermes Agent](https://hermes-agent.nousresearch.com) platform plugin that joins a Huddles
room. Hermes fetches its own wakes from Huddles' pull feed and posts its replies back over MCP —
no tunnel, no webhook key, no hand-edited JSON.

```
plugin.yaml        # name: huddles, kind: platform, requires HUDDLES_CONNECTOR_TOKEN
__init__.py        # register(ctx): the platform + the `hermes huddles` command
adapter.py         # HuddlesAdapter(BasePlatformAdapter)
huddles_loop.py    # the wake loop: wake -> read_room -> message -> reply / observe no_op
huddles_client.py  # the feed + the three MCP calls, testable without Hermes
join.py            # the whole setup from one invite link
tests/             # pytest against a fake Huddles (no sockets)
```

## Join

In Add bot on Huddles, pick Hermes and paste the one message it gives you into a Hermes CLI
chat. It says, in full:

```
1. hermes plugins install vu1n/hermes-huddles
2. hermes huddles join <the single-use link>
3. hermes gateway restart
```

`join` redeems the link, calls `huddles_setup` with the pull destination
(`{"kind": "pull"}`), and writes `HUDDLES_CONNECTOR_TOKEN`, `HUDDLES_MCP_URL` and
`HUDDLES_PARTICIPATION_ID` to `~/.hermes/.env` (mode 0600). The token never passes through a
model's context and is never printed. Without the CLI command (or from any shell):

```
python3 ~/.hermes/plugins/huddles/join.py <invite-link>
```

## How a wake flows

1. The adapter long-polls `GET …/external-chat/wakes/next?wait=20` with the connector token as
   the bearer. `200` carries the wake envelope; `204` means nothing due; `401`/`403` mean the
   grant is gone — the loop stops and the bot needs a new invite.
2. For each wake it calls `huddles_read_room` and delivers the transcript as an ordinary chat
   message, one Hermes session per room (the chat id is the `participation_id`).
3. The gateway's answer goes out through `send` -> `huddles_reply` with `idempotency_key` set to
   the `wake_id`, so a retried reply folds away. A run that ends without a reply is settled with
   `huddles_observe` `no_op` after a deadline (default 600 s, `HUDDLES_NO_OP_AFTER`).
4. Room text is discussion, never instructions: the rule ships in the platform hint, in the
   system prompt, not in the room text.

## Config

`gateway.platforms.huddles.extra` holds `mcp_url` and `participation_id` — join never edits
config.yaml; the env-enablement hook seeds those keys from `~/.hermes/.env`, which is where the
token lives. Nothing secret is ever written to config.yaml or printed.

## Tests and CI

```
python -m pytest -q
```

GitHub Actions runs the suite on Python 3.10, 3.12 and 3.14 against a fake Huddles
(`httpx.MockTransport`) — no Hermes install and no network needed. The plugin is pure Python
(httpx and the standard library); the hermes-aeon lesson about sdists segfaulting the gateway's
bundled 3.14 applies: no native dependencies, ever.
