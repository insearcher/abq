# Compatibility

Neither vendor documents a way for an outside process to talk to a running
session, so `abq` uses interfaces that can change without notice. This file
records what was verified, and what to check when something breaks.

| Component | Verified against | Date |
|---|---|---|
| Claude Code | 2.1.227, 2.1.229 (macOS) | 2026-08-17 |
| Codex CLI | 0.145.0-alpha.29 (macOS, npm install) | 2026-08-17 |

## Claude Code

**What abq relies on.** Each session listens on a Unix socket at
`$CLAUDE_CODE_MESSAGING_SOCKET` (`<tmpdir>/cc-socks/<pid>.sock`, mode 0600) and
accepts newline-delimited JSON. A message is one frame:

```json
{"type": "user", "message": {"role": "user", "content": "..."}}
```

Optional fields abq sets: `msg_id`, `session_id` (the receiver drops the frame
when it does not match, which protects against a recycled pid), and `from` set
to `uds:<reply-socket>` so the receiver can report a delivery status back.

There is no token in this handshake — the 0600 socket is the authorisation, so
only the same local user can send.

**The delivery gate.** A session applies its `crossSessionInbound` setting to
anything arriving this way:

- `accept` — reaches the agent immediately.
- `hold` — reaches the session but waits for the user to approve it; the sender
  gets a `held` receipt, which `abq send` reports.
- unset — a normal session accepts, a `--dangerously-skip-permissions` session
  holds.

To let peer messages through without touching your settings file, start the
session with `claude --settings '{"crossSessionInbound":"accept"}'`.

**Known dead end.** Writing to the Agent Teams mailbox
(`~/.claude/teams/<team>/inboxes/<agent>.json`) worked on 2.1.201 but does
nothing on 2.1.227+: a session no longer polls that file for its own inbox.
abq does not use it.

**If delivery stops working**, check in this order: the socket still exists for
the session's pid; the frame is still accepted (the bundle carries a
`[uds-messaging] Inject messages:` log line showing the current shape); the
`crossSessionInbound` values still mean the same thing.

## Codex CLI

**What abq relies on.** The app-server protocol, JSON-RPC 2.0 carried over a
WebSocket. `initialize` requires `clientInfo` and returns immediately; a
message becomes a real user turn with:

```json
{"method": "turn/start",
 "params": {"threadId": "...", "input": [{"type": "text", "text": "..."}]}}
```

`thread/loaded/list` gives the set of threads a given app-server can inject
into. Inside a session, `$CODEX_THREAD_ID` names its own thread, which is what
`abq join` records.

**Non-obvious details.**

- `unix://` is WebSocket-framed, not raw JSONL. Writing bare JSON gets the
  connection dropped with no error frame — the server logs the reason, the
  client sees only EOF.
- `initialize` has no protocol-version field. Older third-party clients that
  send `{"version": "r9"}` still work only because unknown fields are ignored.
- The server may send requests *to* the client mid-turn (`currentTime/read`,
  approval prompts) and blocks the turn until answered. abq answers the time
  and **declines every approval** — it is a courier, not the human.
- A plain `codex` TUI runs its agent in-process and is invisible to every
  app-server, so it cannot be joined retroactively. Sessions must start as
  `codex --remote <endpoint>`.
- Threads are per-machine state, not per-process: `thread/list` reads the shared
  store under `~/.codex`, while `thread/loaded/list` is only what *this*
  app-server holds in memory. abq checks the loaded set before delivering,
  because a thread can be listed everywhere yet injectable nowhere.

### Watching a bridged thread in the desktop app

A thread started with `codex --remote` appears in the Codex desktop app's thread
list, since both read the same store — verified by both app-servers holding the
same rollout file open. That makes the desktop app a comfortable way to watch
two agents talk while abq drives them from the terminal.

Watch, but don't drive it from there. Typing into that thread in the desktop app
puts a second app-server on the same conversation, and which one owns the turn is
not defined by anything documented. Send through the session abq knows about.

abq cannot deliver into a thread the desktop app started on its own: that
app-server runs on stdio with no listening socket, and `~/.codex/ipc/ipc.sock`
speaks a different protocol (it refuses the WebSocket upgrade).

**Verification schema.** The authoritative shapes come from the CLI itself:

```bash
codex app-server generate-json-schema --experimental --out ./schema
```

Compare `v2/TurnStartParams.json` and `v1/InitializeParams.json` against
`src/abq/adapters/codex.py` when a Codex update breaks delivery.
