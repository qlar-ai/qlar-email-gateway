# Security model

What the Qlar Email Gateway protects, how, and — just as important — what it does not.

## The problem it solves

Connecting a mailbox to a cloud service normally means giving that service the mailbox password
or an OAuth grant, and letting it connect into your mail server from the internet. The gateway
removes both: the credentials stay on a machine you run, and that machine only ever makes
outbound connections.

## Trust boundaries

| Party | Trusted with |
|---|---|
| This machine | Mailbox credentials, the gateway's private key, the audit log |
| Your mail server | Everything it already holds |
| Qlar | The text and readable attachments of emails forwarded to it, and the decision of what to answer |
| Anything in between (proxies, TLS terminators) | Nothing — both directions are signed |

## Controls

### Network

The gateway listens on no port. It connects out to the Qlar API over HTTPS and to your mail
server over IMAP/SMTP. Qlar cannot open a connection to it.

### Identity

- On first run the gateway generates an ECDSA P-256 key pair. The private key is written with mode
  `0600` and the gateway refuses to start if the file is readable by group or others.
- Enrolment uses a one-time code (15 minutes, single use, stored by Qlar only as a hash) and is
  signed with the new key. Enrolment alone grants nothing: a person must compare the fingerprint in
  the CMS with the one the gateway printed and approve it.
- Every request carries a signature over method, path, timestamp, nonce and a hash of the body.
  Qlar refuses requests more than 120 seconds old and nonces it has seen.
- Every job carries Qlar's signature over all fields that change what the gateway does
  (recipients, subject, threading headers, body hashes, the description and SHA-256 of each
  attached picture, expiry). The gateway verifies it with the
  Qlar public key pinned at enrolment and silently discards anything that fails.

### What may be sent

The send guard runs on this machine and nothing in a job can change it:

- `From` is always the configured mailbox.
- One recipient per reply, given as one bare address (no display name, list or header tricks).
- At most `MAX_SENDS_PER_HOUR_PER_RECIPIENT` replies per recipient per hour.
- Attachments are only pictures (PNG, JPEG or WebP) that Qlar's signed job declares, each fetched
  from Qlar over the signed channel and checked against its SHA-256 (see "Attachments" below).
  Nothing else is ever attached.
- Expired jobs are refused.

Since 0.1.3 the gateway no longer limits replies to people who recently wrote in (the old
`RECIPIENT_ALLOWLIST` / `RECIPIENT_MEMORY_DAYS` settings are ignored): who the agent answers is set
in the Qlar CMS. A compromised Qlar could therefore send short text emails from your address to any
address, one recipient at a time and at most `MAX_SENDS_PER_HOUR_PER_RECIPIENT` per recipient per
hour, optionally with up to five PNG, JPEG or WebP pictures of at most 8 MB each (15 MB in total)
that pass the checks below. It cannot make the gateway send any other file type, read local files,
or send from another address.

### Attachments

**Inbound.** From 0.2.0 the gateway uploads the readable attachments of each email it forwards
(pictures, PDF, text, Word, Excel and CSV files) to Qlar over the same signed, outbound-only HTTPS
channel, before it forwards the email's text. Only the first five files of an email are uploaded,
at most 20 MB each. Files are sent only for emails the gateway has already decided to forward, after
its filters. Files of a kind it does not support, or that cannot be uploaded, are listed by name
only. Inline `cid:` pictures such as signature logos are never uploaded. In 0.2.0 there is no
setting to turn this off.

**Outbound.** A reply can carry pictures (PNG, JPEG or WebP) that Qlar's signed job declares. A job
that declares any other type is treated as failing the checks. The gateway fetches each one from
Qlar over the signed channel only, never from any other address, and checks its length and SHA-256
against the signed job. It accepts at most five pictures, 8 MB each and 15 MB in total. If
anything fails to verify, the reply is sent without the pictures. The gateway never attaches
anything from the local disk or from any other source. Any new attachment type needs a new release.

### Your mailbox

The gateway reads with `BODY.PEEK[]` and opens the folder read-only: it never marks mail as read,
moves or deletes anything. Mail that was already in the inbox at enrolment is never answered.

### Visibility

Every forwarded, filtered and sent email is recorded in `AUDIT_LOG_FILE` (one JSON object per
line: message id, from, to, subject, agent, conversation, outcome, reason, duration). The password
never appears in it, in the process log or in any request to Qlar.

## What this does **not** protect against

- **Email content leaves your network.** The text of each forwarded email (and, from 0.2.0, its
  readable attachments, which are uploaded to Qlar) is sent to Qlar and processed by the language
  model behind the agent. The gateway closes the credential and network-exposure gap, not data
  egress. Use a mailbox whose contents you are willing to share with the agent.
- **A compromised gateway host.** Anyone who controls this machine has the mailbox password and
  the gateway key.
- **What the agent says.** Replies are written by the agent; the guard limits who receives them,
  not their content.
- **Spoofed senders.** The gateway trusts your mail server's view of `From`. Use SPF/DKIM/DMARC
  filtering on the server.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md).
