# Qlar Email Gateway

Connect a mailbox to a Qlar agent without giving Qlar the mailbox password.

The gateway runs on a machine inside your network, next to your mail server. It watches one
mailbox over IMAP, forwards each email a person sends to it to Qlar, and sends the agent's
reply over SMTP from the same mailbox. Every connection it makes is **outbound**: it listens on
no port, needs no firewall rule, no certificate and no DNS entry.

```
 your network                                  |   Qlar
                                               |
 mail server <--IMAP/SMTP-- qlar-email-gateway ---HTTPS (outbound)--> Messenger API --> agent
```

- **Credentials stay here.** `MAIL_PASSWORD` is used for the IMAP and SMTP login on this
  machine and is never sent to Qlar, logged, printed or audited.
- **Both directions are signed.** The gateway signs every request with its own key; Qlar signs
  every reply job, and the gateway discards a job whose signature does not verify.
- **The gateway decides who may be emailed.** A reply only goes to someone who wrote to this
  mailbox in the last `RECIPIENT_MEMORY_DAYS` days (or is on `RECIPIENT_ALLOWLIST`), at most
  `MAX_SENDS_PER_HOUR_PER_RECIPIENT` times an hour, with no attachments, and always from your own
  address. Nothing Qlar sends can loosen this.
- **Your mailbox is left alone.** The gateway never marks mail as read, never moves or deletes it.
- **You keep a record.** Every email forwarded, filtered and sent is written to a local JSONL
  audit file (`AUDIT_LOG_FILE`).

## What it forwards and what it does not

Forwarded: every email a person sends to the mailbox, after the gateway is enrolled.

Never forwarded: auto-replies (`Auto-Submitted`, `X-Auto-Response-Suppress`), bulk and list mail
(`Precedence`, `List-Id`, `List-Unsubscribe`), mail from the mailbox itself, from
`mailer-daemon`/`postmaster`/`noreply`, mail with no `Message-ID`, and mail dated before enrolment.
Existing mail in the inbox is never answered.

**Privacy.** The text of each forwarded email (and, from 0.2.0, readable attachments) goes to Qlar
and to the language model behind the agent. The gateway removes the need to hand over mailbox
credentials or open your network; it does not keep email content inside your network. See
[docs/SECURITY-MODEL.md](docs/SECURITY-MODEL.md).

## Requirements

- A mailbox with IMAP and SMTP access by username and password (or an app password).
- Docker, or Python 3.11+ on Linux or Windows.
- Outbound HTTPS to the Qlar API, and outbound IMAP/SMTP to your mail server.

## Install and connect

The Qlar CMS (agent → Channels → Email) prints these lines with your own URL and a one-time code.

**Docker**

```bash
echo QLAR_BASE_URL=https://api.qlar.ai/messenger/api/email-gateway >> .env
echo QLAR_ENROLLMENT_CODE=K7P4-9WQX-2MTD >> .env
docker run -d --name qlar-email-gateway --restart unless-stopped \
  --env-file .env -v "$PWD/state:/state" ghcr.io/qlar-ai/email-gateway:0.1.2 enroll
docker logs -f qlar-email-gateway
```

**Python**

```bash
pip install --upgrade "qlar-email-gateway @ https://github.com/qlar-ai/qlar-email-gateway/releases/download/v0.1.2/qlar_email_gateway-0.1.2-py3-none-any.whl"
qlar-email-gateway version
qlar-email-gateway enroll --base-url https://api.qlar.ai/messenger/api/email-gateway --code K7P4-9WQX-2MTD
```

With Docker, the mailbox settings (`IMAP_HOST`, `SMTP_HOST`, `MAIL_USER`, `MAIL_PASSWORD`, …) must be
in `.env` too, or answered once interactively — see [docs/INSTALL.md](docs/INSTALL.md). With Python,
`enroll` asks for the mailbox details once (IMAP and SMTP server, user, password), tests the
login, registers with Qlar and prints a **fingerprint**. Compare it with the one the CMS shows and
click **Approve**. The gateway starts working by itself a few seconds later.

## Commands

| Command | What it does |
|---|---|
| `enroll [--base-url URL] [--code CODE]` | Asks for missing settings, enrols if not yet enrolled, then serves. Safe to run again; use it as the container command |
| `run [--init]` | Serves; fails if not enrolled. `--init` asks the setup questions again |
| `test-mailbox` | Logs in to IMAP and SMTP and reports banners, IDLE support and inbox size. Does not contact Qlar |
| `fingerprint` | Prints this gateway's key fingerprint |
| `version` | Prints the release and protocol version |

## Configuration

All settings live in `.env` (see [.env.example](.env.example)); real environment variables win.

| Key | Meaning | Default |
|---|---|---|
| `QLAR_BASE_URL` | Qlar API endpoint, ends in `/api/email-gateway` | required |
| `QLAR_ENROLLMENT_CODE` | One-time code, only for enrolment | — |
| `QLAR_GATEWAY_NAME` | Label in the CMS | hostname |
| `IMAP_HOST`, `IMAP_PORT`, `IMAP_SECURITY` | IMAP server; `ssl` or `starttls` | —, 993, ssl |
| `IMAP_FOLDER` | Folder watched | INBOX |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY` | SMTP server; `ssl` or `starttls` | —, 587, starttls |
| `MAIL_USER`, `MAIL_PASSWORD` | Mailbox login. Never sent to Qlar | required |
| `MAIL_ADDRESS` | Mailbox address when it differs from the user | `MAIL_USER` |
| `MAIL_FROM_NAME` | Sender name on replies | — |
| `POLL_INTERVAL_SECONDS` | Check interval when the server has no IDLE | 60 |
| `RECIPIENT_ALLOWLIST` | Addresses or domains that may always be emailed (`a@x.com, @corp.com, y.org`) | — |
| `RECIPIENT_MEMORY_DAYS` | How long someone who wrote in may be answered | 30 |
| `MAX_SENDS_PER_HOUR_PER_RECIPIENT` | Send rate limit per recipient | 20 |
| `MAX_INBOUND_TEXT_CHARS` | Longest text forwarded per email | 20000 |
| `AUDIT_LOG_FILE` | Local JSONL audit | `./audit/mail.jsonl` (`/state/audit/mail.jsonl` in Docker) |
| `GATEWAY_KEY_FILE`, `GATEWAY_STATE_FILE` | Identity and state | `./gateway-key.pem`, `./gateway-state.json` (`/state/` in Docker) |
| `QLAR_INSECURE_SKIP_TLS_VERIFY` | Local testing only | false |

## Documentation

- [docs/INSTALL.md](docs/INSTALL.md) — Docker, systemd, troubleshooting
- [docs/SECURITY-MODEL.md](docs/SECURITY-MODEL.md) — what the gateway protects and what it does not
- [docs/PROTOCOL.md](docs/PROTOCOL.md) — the wire contract, for auditors and other implementations
- [docs/RELEASING.md](docs/RELEASING.md)

## Licence

MIT — see [LICENSE](LICENSE).
