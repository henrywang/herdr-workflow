# Herdr socket API: framing and transport

Findings from the Phase 0 spike. This is the artifact that makes the spike reusable —
read this instead of re-deriving it.

Sources: [herdr socket API docs](https://herdr.dev/docs/socket-api/), the bundled schema
(`herdr api schema --json`), and `~/.config/herdr/herdr-server.log`.

Status of each claim is marked **[docs]**, **[schema]**, **[log]**, or **[verified]**
(confirmed against a running daemon).

---

## There are two sockets. Use the right one.

**[log]** `herdr-server.log` on startup:

```
herdr::api::server: api server listening path=~/.config/herdr/herdr.sock
herdr::server::headless: client protocol socket listening path=~/.config/herdr/herdr-client.sock
```

| Socket | Purpose | Encoding |
|--------|---------|----------|
| `herdr.sock` | **The API. This is ours.** | newline-delimited JSON |
| `herdr-client.sock` | The TUI client protocol | `SemanticFrame`, binary |

**[log]** The `handshake succeeded version=17 encoding=SemanticFrame` line in
`herdr-client.log` belongs to the **client** socket. It does not apply to the API socket
and there is no such handshake there. Do not be misled by it — this cost time once
already.

---

## Socket path resolution

**[docs]** In order:

1. `--session <name>` → `~/.config/herdr/sessions/<name>/herdr.sock`
2. `HERDR_SOCKET_PATH` environment variable
3. `HERDR_SESSION` environment variable → the named-session path
4. Default: `~/.config/herdr/herdr.sock`

This is what `[herdr] socket = "auto"` in the config resolves through.

---

## Framing

**[docs]** Newline-delimited JSON. Each request and each response occupies exactly one
line. There is **no handshake** — clients send requests immediately on connect.

### Request

```json
{"id":"req_1","method":"ping","params":{}}
```

**[schema]** `id`, `method`, and `params` are all required. `params` is required even when
the method takes none — send `{}`, do not omit the key. Methods using `EmptyParams`
(e.g. `session.snapshot`) still require it.

### Success response

```json
{"id":"req_1","result":{"type":"pong","version":"...","protocol":17}}
```

**[schema]** `result` is an internally tagged union on `type`.

### Error response

```json
{"id":"req_1","error":{"code":"not_found","message":"pane not found"}}
```

A response carries **either** `result` or `error`, never both. Discriminate on key
presence.

---

## Request ids and correlation

**[docs]** `id` correlates responses to requests.

**[log]** The `herdr` CLI uses human-readable, **non-unique** ids — `cli:pane:rename`,
`cli:agent:start`, and `cli:agent:start` again minutes later. That is only safe because
the CLI opens one connection per request. It tells us nothing about whether the server
requires uniqueness.

**We generate unique ids anyway.** We hold one long-lived connection, so uniqueness is
ours to guarantee regardless of what the server tolerates.

**[docs]** Whether multiple requests may be in flight on one connection is *not*
documented. See "Open questions" below.

---

## Events arrive on the same connection, without ids

**[docs]** After `events.subscribe`:

> "The first response acknowledges the subscription. Later lines are pushed events."
> "Events arrive as additional JSON lines on the same connection without request IDs."

**This is the fact that determines the client's shape.** Because unsolicited lines can
arrive between a request and its response, the client cannot be a
write-then-read-one-line loop. It needs:

- a background read task consuming lines continuously
- a pending-futures map keyed on request `id`
- an event dispatch path for lines with no `id`

That structure also gives concurrent in-flight requests for free, whether or not the
server needs it.

### Docs/schema discrepancy on subscription shape

**[docs]** shows `{"type": "pane.agent_status_changed", "pane_id": ...}`.
**[schema]** `EventMatch` uses `{"event": "workspace_created", "workspace_id": ...}`.

**Trust the shipped schema** — it is versioned with the binary (protocol 17); the docs
site is not. Confirm against a live daemon before relying on either.

---

## Protocol version

**[schema]** `ping` returns `{type: "pong", version: <string>, protocol: <uint32>,
capabilities?: ...}`. `version` and `protocol` are required.

This is how `wq doctor` compares the running server against our pinned protocol. The
bundled schema at the time of writing reports `protocol: 17, schema_version: 1`, with 89
request methods.

---

## Open questions — resolve against a live daemon

- [ ] Are concurrent in-flight requests on one connection actually accepted, and do ids
      come back out of order? Test: send two requests before reading either response.
      *(Our client works either way; this only tells us how hard to lean on it.)*
- [ ] Is there a maximum line length or message size?
- [ ] What happens on malformed JSON — error response, or connection close?
- [ ] Does the server close idle connections?
- [ ] Does `events.subscribe` echo the subscription in its ack?
- [ ] Confirm `EventMatch` uses `event` (schema) rather than `type` (docs).
