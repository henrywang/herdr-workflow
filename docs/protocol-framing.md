# Herdr socket API: framing and transport

Findings from the Phase 0 spike. This is the artifact that makes the spike reusable —
read this instead of re-deriving it.

Sources: [herdr socket API docs](https://herdr.dev/docs/socket-api/), the bundled schema
(`herdr api schema --json`), and `~/.config/herdr/herdr-server.log`.

Status of each claim is marked **[docs]**, **[schema]**, **[log]**, or **[verified]**
(confirmed against a running daemon — herdr **0.7.5**, protocol **17**).

> **Read this first.** The single most important fact is not in the docs: **the server
> answers one request per connection and then closes it.** A long-lived client works for
> exactly one call and then fails everything after it. We shipped that bug and caught it
> the first time we ran against a real daemon.

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

## One request per connection

**[verified]** The server writes one response and closes the socket. Three probes, all
against herdr 0.7.5:

| Probe | Result |
|-------|--------|
| Send `ping`, read response, send `ping` again | second write raises **`BrokenPipeError`** |
| Pipeline two requests before reading | one response, then **close** |
| Read again after a response | immediate **EOF** |

So a request is a self-contained connect → write → read one line → close. **There is no id
correlation to do**: whatever comes back is the answer to the one thing you sent. This is
presumably also why the `herdr` CLI can reuse ids like `cli:agent:start`.

The only exception is `events.subscribe` — see below.

This makes the client much simpler than the docs imply, and it means anything wanting
concurrency just opens more connections.

---

## Framing

**[verified]** Newline-delimited JSON. Each request and each response occupies exactly one
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

**[verified] Errors from unparseable requests carry `"id": ""`, not your id.** Every
malformed-input probe came back with an empty id:

```json
{"id":"","error":{"code":"invalid_request","message":"invalid request: missing field `params` at line 1 column 30"}}
```

So the id is unusable for correlation exactly when you would most want it. Harmless here
— one request per connection — but fatal to any design that multiplexes.

**[verified]** An unknown method is `invalid_request`, **not** `unknown_method`, and the
message enumerates every valid method. Malformed JSON gets an error response too, then the
connection closes.

---

## Request ids

**[verified]** The id is echoed back on success and ignored otherwise. Since there is
nothing to correlate, uniqueness buys nothing — we send a descriptive `wq:<method>`,
which is worth more in herdr's server log than a counter would be. This matches what the
`herdr` CLI does (`cli:pane:rename`, `cli:agent:start`).

---

## Events: the one long-lived connection

**[verified]** `events.subscribe` acks and then **holds the connection open**:

```json
→ {"id":"d1","method":"events.subscribe","params":{"subscriptions":[{"type":"workspace.created"}]}}
← {"id":"d1","result":{"type":"subscription_started"}}
  … connection stays open, event lines follow …
```

Event lines carry `event` and `data` and **no `id`**.

### Two different event vocabularies — this will catch you

**[verified]** `events.subscribe` and `events.wait` do not name events the same way:

| Method | Schema type | Key | Naming |
|--------|-------------|-----|--------|
| `events.subscribe` | `Subscription` | `type` | **dotted**: `workspace.created` |
| `events.wait` | `EventMatch` | `event` | **underscored**: `workspace_created` |

Sending `{"event": "workspace_created"}` to `events.subscribe` fails with *missing field
`type`*. Sending `{"type": "workspace_created"}` fails with *unknown variant
`workspace_created`, expected one of `workspace.created`, …*.

**[verified]** Some subscriptions need extra keys: `{"type":
"pane.agent_status_changed"}` alone is rejected with *missing field `pane_id`*.

For the record: the docs site was right about `type` and we misread the schema by quoting
`EventMatch` where `Subscription` applies. Both are in the schema; they are different
definitions.

---

## Protocol version

**[verified]** `ping` returns `{type: "pong", version, protocol, capabilities}`. Against
herdr 0.7.5:

```json
{"id":"a1","result":{"type":"pong","version":"0.7.5","protocol":17,"capabilities":{"live_handoff":true,…}}}
```

This is how `wq doctor` compares the running server against our pinned protocol. The
bundled schema reports `protocol: 17, schema_version: 1`, with 89 request methods.

---

## Resolved

- [x] **Concurrent in-flight requests on one connection?** No — the server closes after
      one response. Concurrency means more connections.
- [x] **Malformed JSON?** Error response with `id: ""`, then close.
- [x] **Does `events.subscribe` keep the connection open?** Yes, and its ack is
      `{"type": "subscription_started"}`.
- [x] **`event` or `type` for subscriptions?** `type`, dotted — and `events.wait` uses
      `event`, underscored. Different vocabularies.
- [x] **Is `params` really required?** Yes, verified: omitting it returns *missing field
      `params`*.

## Still open

- [ ] Is there a maximum line length or message size? (`session.snapshot` on a big session
      is the realistic test.)
- [ ] Does the server close an idle *subscription* connection, and does it need keepalive?
- [ ] Do subscription connections survive `herdr server reload-config`?
