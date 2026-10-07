# Installation

## Before you start

You need:

1. **A mailbox** reachable over IMAP and SMTP with a username and password. Microsoft 365 and
   Google Workspace usually need an *app password* or SMTP AUTH enabled for that mailbox.
2. **The CMS panel open** at agent → Channels → Email, where you generate a one-time code
   (valid 15 minutes) and later approve the gateway.
3. **Outbound network access** from this machine to the Qlar API (HTTPS, 443) and to your mail
   server (IMAP 993 or 143, SMTP 587 or 465). Nothing inbound.
4. **An accurate clock.** Requests are signed with a timestamp; a clock more than two minutes off
   is refused. Make sure NTP is running.

Use a dedicated mailbox (for example `ask@yourcompany.com`): the gateway answers **every** human
email that arrives in it.

## Option A — Docker

```bash
mkdir qlar-email-gateway && cd qlar-email-gateway
echo QLAR_BASE_URL=<the URL the CMS shows> >> .env
echo QLAR_ENROLLMENT_CODE=<the code the CMS shows> >> .env
docker run -it --rm --env-file .env -v "$PWD/state:/state" ghcr.io/pusakaai/email-gateway:0.1.0 enroll
```

The first run asks the mailbox questions, writes them to `.env`, tests the login, enrols and
prints a fingerprint. After approving it in the CMS, stop it (Ctrl+C) and start it detached:

```bash
docker run -d --name qlar-email-gateway --restart unless-stopped \
  --env-file .env -v "$PWD/state:/state" ghcr.io/pusakaai/email-gateway:0.1.0 enroll
docker logs -f qlar-email-gateway
```

`state/` holds the private key, the enrolment state and the audit log. Back it up; losing it
means enrolling again. `docker-compose.example.yml` shows the same setup with a read-only root
filesystem.

To set everything without prompts, put the keys from `.env.example` in `.env` before the first run.

## Option B — Python and systemd

```bash
python3 -m venv /opt/qlar-email-gateway
/opt/qlar-email-gateway/bin/pip install "qlar-email-gateway @ https://github.com/qlar-ai/qlar-email-gateway/releases/download/v0.1.0/qlar_email_gateway-0.1.0-py3-none-any.whl"
cd /etc/qlar-email-gateway
/opt/qlar-email-gateway/bin/qlar-email-gateway enroll --base-url <URL> --code <CODE>
```

Then a unit, running as an unprivileged user that owns `/etc/qlar-email-gateway`:

```ini
[Unit]
Description=Qlar Email Gateway
After=network-online.target
Wants=network-online.target

[Service]
User=qlar
WorkingDirectory=/etc/qlar-email-gateway
ExecStart=/opt/qlar-email-gateway/bin/qlar-email-gateway run
Restart=always
RestartSec=5
# The gateway needs to read its own directory and reach the network. Nothing else.
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/etc/qlar-email-gateway
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

On Windows, run the same `pip install` in a virtual environment and start
`qlar-email-gateway run` with Task Scheduler or a service wrapper.

## Verifying

- `qlar-email-gateway test-mailbox` logs in to IMAP and SMTP and prints the server banners, IDLE
  support and the number of messages in the folder, without contacting Qlar.
- In the CMS the gateway shows **online** and mailbox status **ok**.
- Send an email to the mailbox from another address: the reply arrives in the same thread, and
  `audit/mail.jsonl` has a `forwarded` and a `sent` line.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `enrolment code is not valid` | Codes are single use and expire after 15 minutes. Generate a new one |
| Enrolment says the URL is not Qlar's API | `QLAR_BASE_URL` must be the API URL from the CMS panel, ending in `/api/email-gateway` |
| `401 clock_skew` | This machine's clock is more than two minutes off. Fix NTP |
| `waiting for approval` | Expected. Compare the fingerprint and click Approve in the CMS |
| Mailbox status `auth_failed` | Wrong `MAIL_USER`/`MAIL_PASSWORD`, or the provider needs an app password / SMTP AUTH |
| Mailbox status `unreachable` | Host, port or `IMAP_SECURITY` wrong, or a firewall blocks the mail ports |
| Mailbox status `idle_unsupported` | The server dropped IDLE repeatedly; the gateway checks every `POLL_INTERVAL_SECONDS` instead. Replies may take up to that much longer |
| A reply is `rejected` in the CMS console | The send guard refused it: the recipient never wrote in (or longer ago than `RECIPIENT_MEMORY_DAYS`), or the hourly limit was reached. Add the address or domain to `RECIPIENT_ALLOWLIST` if it should always be allowed |
| `this gateway has been revoked` | Someone revoked it in the CMS. Delete `gateway-state.json` and enrol again |
