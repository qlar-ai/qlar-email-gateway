# Qlar Email Gateway protocol, version 1

This is the complete wire contract between the on-premise email gateway and Qlar. It is
published so that anyone can audit it, or write their own gateway in another language
without reading the Python.

Two rules shape everything below:

1. **The gateway only ever makes outbound requests.** Qlar never connects to the customer's
   network or mail server. There is no listening socket and no inbound firewall rule.
   Mailbox credentials stay on the gateway machine and are never transmitted to Qlar.
2. **Both directions are signed.** HTTPS protects the channel, but on-premise deployments
   routinely terminate TLS at a proxy, so authenticity is established by signatures over
   the payloads themselves.

---

## 1. Transport

All calls are `POST`, JSON in and JSON out, to paths under the base URL the operator
configures as `QLAR_BASE_URL` (the API endpoint, ending in `/api/email-gateway` — the CMS
email panel prints the exact value for that deployment).

| Path | Purpose |
|---|---|
| `/enroll` | one-time registration |
| `/jobs/poll` | long-poll for work (reply emails to send, connection tests) |
| `/jobs/{jobId}/result` | report what happened to a job |
| `/inbound` | forward one email received in the connected mailbox |

### Long polling

The gateway posts to `/jobs/poll` and Qlar holds the request open until a job appears or
`maxWaitSeconds` elapses, answering `200` with a job or `204 No Content`. Either way the
gateway immediately polls again.

`maxWaitSeconds` must stay at or below **25** — under the 30-second idle timeout most
corporate proxies and load balancers impose. Qlar checks its queue every **150 ms** while
holding the request. Queued jobs live for **10 minutes**; a gateway that does not poll in
that time never sees them.

---

## 2. Authentication

### Keys

The gateway generates an **ECDSA P-256** key pair on first run and keeps the private key
on-premise, mode `0600`. Only the public half is ever transmitted.

Signatures are ECDSA with SHA-256, **DER-encoded (RFC 3279)**, then base64.

The **fingerprint** of a public key is SHA-256 over its DER `SubjectPublicKeyInfo`, rendered
as uppercase hex bytes separated by `:` (`3A:7F:…:C2`).

### Signing a request

Every request carries five headers:

| Header | Value |
|---|---|
| `X-Qlar-Gateway-Id` | the id Qlar issued at enrolment (`egw_…`; absent on `/enroll`) |
| `X-Qlar-Timestamp` | Unix seconds, as a decimal string |
| `X-Qlar-Nonce` | a fresh random value, single use |
| `X-Qlar-Signature` | base64( DER( ECDSA-SHA256( canonical string ))) |
| `X-Qlar-Protocol` | `1` |

The canonical string is five fields joined by `\n`:

```
METHOD \n PATH \n TIMESTAMP \n NONCE \n base64(SHA-256(body))
```

for example

```
POST
/inbound
1791360000
n0nce-fixture
<base64 sha256 of the exact request body bytes>
```

`METHOD` is uppercase. **`PATH` is the endpoint path relative to the base URL** — `/inbound`,
*not* `/api/email-gateway/inbound`. Qlar sits behind an API gateway that may rewrite the
prefix. When a request has a query string, `PATH` includes it exactly as sent.

### Verification on Qlar's side

Qlar rejects a request with `401` when the signature does not verify against the registered
public key (`{"reason":"unauthorized"}`), when the timestamp is more than **120 seconds** from
its own clock (`{"reason":"clock_skew"}`), or when the nonce has been seen within the last
**5 minutes** (`{"reason":"unauthorized"}`). A gateway not in `active` state gets `403` (see
§4). A clock more than two minutes out is the most common cause of `401` — check NTP first.

### Signing a job

Each job carries its own `signature` field: base64(DER(ECDSA-SHA256(canonical job bytes)))
made with **Qlar's** private key. The gateway verifies it against the Qlar public key it
pinned at enrolment and silently discards a job that fails.

The canonical job bytes are **named fields in a fixed order joined by `\n`** — not canonical
JSON:

```
jobId \n type \n protocol \n to \n subject \n inReplyTo \n references \n
b64sha256(textBody) \n b64sha256(htmlBody) \n issuedAt \n expiresAt \n
agentId \n userId \n conversationId
```

(one line in reality; wrapped here for reading.)

| Field | Rule |
|---|---|
| `to` | every address **lowercased**, joined by `,` |
| `references` | joined by `,`, exactly as given (no case change) |
| `textBody`, `htmlBody` | base64(SHA-256(UTF-8 bytes)); a missing body hashes the empty byte string: `47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=` |
| `issuedAt`, `expiresAt` | `yyyy-MM-ddTHH:mm:ssZ`, UTC, whole seconds — exactly as on the wire |
| any missing string | contributes `""` |

`test_connection` jobs use the same field list with the email fields empty.

Everything that changes what the gateway will *do* — who receives mail, what it says, which
thread it joins — is in that list. A field outside it is informational and not covered by
the signature.

#### Worked example (interop vector)

Job:

```json
{
  "jobId": "job_1",
  "type": "send_email",
  "protocol": 1,
  "to": ["Budi@Example.com"],
  "subject": "Re: Harga paket",
  "inReplyTo": "<m1@example.com>",
  "references": ["<root@example.com>", "<m1@example.com>"],
  "textBody": "Halo Budi",
  "htmlBody": "<p>Halo Budi</p>",
  "issuedAt": "2026-10-07T08:00:00Z",
  "expiresAt": "2026-10-07T08:10:00Z",
  "agentId": "agt_1",
  "userId": "budi@example.com",
  "conversationId": "root@example.com"
}
```

Canonical bytes (UTF-8, `\n` separators, no trailing newline):

```
job_1
send_email
1
budi@example.com
Re: Harga paket
<m1@example.com>
<root@example.com>,<m1@example.com>
vR+7278oCiP11GZx5vF5k1UCifUxfyC4bS7kmMoP7wI=
Tla78pZSiLIcYnzFn0/c4Z+dC/yOiazDVjBrFnI5/VA=
2026-10-07T08:00:00Z
2026-10-07T08:10:00Z
agt_1
budi@example.com
root@example.com
```

---

## 3. Enrolment

`POST /enroll` — authenticated by a one-time code generated in the Qlar CMS. The request is
still signed with the key being registered, which proves the caller holds the private half.

```json
{
  "enrollmentCode": "K7P4-9WQX-2MTD",
  "publicKeyPem": "-----BEGIN PUBLIC KEY-----\n...",
  "fingerprint": "3A:7F:...:C2",
  "name": "support mailbox gateway",
  "hostname": "srv-mail-01",
  "platform": "Linux 5.15.0",
  "version": "0.1.0",
  "protocol": 1,
  "mailboxAddress": "ask@pelanggan.com",
  "idleSupported": true
}
```

Response:

```json
{
  "gatewayId": "egw_8fc21a...",
  "qlarPublicKeyPem": "-----BEGIN PUBLIC KEY-----\n...",
  "status": "pending_approval",
  "fingerprint": "3A:7F:...:C2"
}
```

Enrolment codes have the form `XXXX-XXXX-XXXX` over the alphabet
`ABCDEFGHJKLMNPQRSTVWXYZ23456789`, are **single use** and expire after **15 minutes**. Qlar
stores only the SHA-256 (uppercase hex) of the trimmed, uppercased code. `mailboxAddress` is
stored lowercased.

**Enrolment does not make the gateway usable.** It lands in `pending_approval` until a human
in the CMS compares the fingerprint shown there with the one printed by
`qlar-email-gateway enroll` and approves it. One mailbox address can be active on only one
gateway at a time.

---

## 4. Polling

`POST /jobs/poll`

```json
{
  "version": "0.1.0",
  "protocol": 1,
  "mailboxStatus": "ok",
  "lastUid": 4182,
  "maxWaitSeconds": 25
}
```

`mailboxStatus` is `ok`, `auth_failed`, `unreachable` or `idle_unsupported`; `lastUid` is the
highest IMAP UID already forwarded. Every poll refreshes the gateway's `lastSeenAt`; a gateway
is shown **online** when it polled within the last **90 seconds**.

| Status | Meaning |
|---|---|
| `200` | body is `{"job": {…}}` — the job object below, wrapped in a `job` property |
| `204` | nothing to do; poll again immediately |
| `401` with `{"reason":"clock_skew"}` or `{"reason":"unauthorized"}` | timestamp, signature or nonce rejected |
| `403` with `{"reason":"pending_approval"}` | enrolled but not yet approved — keep polling, report it as a wait |
| `403` with `{"reason":"revoked"}` | stop; a human must re-enrol this gateway |

### The job

The `200` body wraps it: `{"job": <the object below>}`.

```json
{
  "jobId": "job_01J9...",
  "type": "send_email",
  "protocol": 1,
  "to": ["budi@example.com"],
  "subject": "Re: Harga paket",
  "inReplyTo": "<m1@example.com>",
  "references": ["<root@example.com>", "<m1@example.com>"],
  "textBody": "Halo Budi, ...",
  "htmlBody": "<p>Halo Budi, ...</p>",
  "issuedAt": "2026-10-07T08:00:00Z",
  "expiresAt": "2026-10-07T08:10:00Z",
  "agentId": "agt_...",
  "userId": "budi@example.com",
  "conversationId": "root@example.com",
  "signature": "MEUCIQ..."
}
```

| `type` | What the gateway does | Expiry |
|---|---|---|
| `send_email` | send one reply over SMTP from the connected mailbox, after its own send guard | 10 min |
| `test_connection` | log in to IMAP and SMTP and report banners | 45 s |

`conversationId` is the thread key (the normalized root `Message-ID`). `agentId` / `userId` /
`conversationId` are for the customer's audit log only.

A job past `expiresAt` is not executed; the gateway reports it with category `expired`. A job
whose `protocol` is higher than the gateway speaks is refused with `rejected`.

---

## 5. Results

`POST /jobs/{jobId}/result`. Idempotent — keyed by job id — so the gateway retries on a
network failure. Qlar answers `200 {"accepted": true}` (or `401`/`403` as in §4).

Success:

```json
{ "status": "ok", "sentMessageId": "<qlar-1@pelanggan.com>", "durationMs": 812 }
```

For `test_connection`, success adds:

```json
{ "status": "ok", "durationMs": 640, "imapBanner": "...", "smtpBanner": "...", "idleSupported": true, "inboxCount": 1204 }
```

Failure:

```json
{ "status": "error", "error": { "category": "smtp_rejected", "code": "550", "messageText": "mailbox unavailable" } }
```

| `category` | Meaning |
|---|---|
| `smtp_rejected` | the SMTP server refused the message |
| `connection` | IMAP/SMTP server unreachable |
| `auth` | login refused |
| `timeout` | gave up waiting on the mail server |
| `expired` | the job was already past `expiresAt` |
| `rejected` | the **gateway's** send guard refused it (unknown recipient, rate limit, protocol too new); never retried by Qlar |

---

## 6. Timeouts

| Stage | Budget |
|---|---|
| Long-poll hold | ≤ 25 s |
| Job queue lifetime | 10 min |
| `send_email` `expiresAt` | 10 min after issue |
| `test_connection` `expiresAt` | 45 s after issue |
| Qlar's wait for a `test_connection` result | 30 s |
| Clock skew tolerated | 120 s |
| Nonce memory | 5 min |

Nobody waits synchronously for a `send_email` result: Qlar records it when it arrives.

---

## 7. Versioning

`X-Qlar-Protocol` and the job's `protocol` field carry the **contract** version (`1`); the
release version (`0.1.0`) moves independently. Qlar accepts protocol `1` and above
(`MinimumVersion = 1`). Adding an endpoint that older gateways never call does not bump the
protocol.

---

## 8. Inbound mail

`POST /inbound`, signed like every other call. The gateway sends every human email that
arrives in the connected mailbox, one request per email, in IMAP UID order.

```json
{
  "messageId": "<m2@example.com>",
  "uid": 4183,
  "from": { "address": "budi@example.com", "name": "Budi" },
  "to": [{ "address": "ask@pelanggan.com", "name": "Support" }],
  "cc": [],
  "replyTo": null,
  "subject": "Re: Harga paket",
  "date": "2026-10-07T07:58:12Z",
  "inReplyTo": "<qlar-1@pelanggan.com>",
  "references": ["<root@example.com>", "<qlar-1@pelanggan.com>"],
  "text": "Kalau paket B berapa?",
  "textTruncated": false,
  "attachments": [],
  "headers": { "autoSubmitted": null, "precedence": null, "listId": null }
}
```

`attachments[]` items are `{fileName, contentType, sizeBytes, url}`. `replyTo` is a single bare
address (`team@corp.com`, no display name) or `null`; Qlar falls back to `from.address` when it is
not exactly one valid address. Header values must not contain CR or LF.

| Status | Meaning |
|---|---|
| `202 {"accepted": true}` | accepted (or already accepted — a repeated `messageId` is answered `202` and processed once). Advance `lastUid` now |
| `400 {"message": "..."}` | `messageId` or `from.address` empty. Do not retry this email; advance `lastUid` and audit it |
| `401` / `403` | as in §4 |

Qlar answers `202` before processing: the email is queued durably and answered in the
background, so a reply arrives later as a `send_email` job. The gateway saves `lastUid` only
after `202`, and retries with backoff when Qlar is unreachable.

### Threading

Qlar derives the **thread key** from the first element of `references`, else `inReplyTo`, else
the email's own `messageId` — trimmed, without `<>`, lowercased. An email with neither header
joins an earlier thread from the same sender whose normalized subject (without `Re:`, `Fwd:`,
`Bls:` …) matches, within 30 days.
