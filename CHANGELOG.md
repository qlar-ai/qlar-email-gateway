# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). The wire protocol version is separate from the
release version and is listed per release.

## [Unreleased]

## [0.1.8] — 2026-10-26

Protocol 1. Hosted worker and on-prem gateway share the change; the server needs no change.

### Fixed

- A forwarded email is no longer cut away. Forwards (a `Fwd:`/`Fw:` subject, or a "Forwarded message" /
  "Begin forwarded message:" banner ahead of any reply header) used to be treated as quoted history, so
  the agent saw only what the forwarder typed above the block — or just the banner line when they typed
  nothing. The forwarded message is now sent as the text, with inline `>` quote marks removed. Replies in
  a thread keep their quoted history cut, even when the subject still says `Fwd:` after a `Re:`.

## [0.1.7] — 2026-10-23

Protocol 1. Hosted worker and on-prem gateway share the change; on a normal disk behaviour is identical.

### Fixed

- The private-key permission check no longer stops a mailbox from starting on a filesystem that cannot
  hold permission bits. An SMB share (App Service's persistent `/home`, an Azure Files mount) reports
  every file as `0777` and ignores `chmod`, so 0.1.6 refused to start any mailbox whose state lived
  there (`KeyFilePermissionError`). The worker now probes the directory with a throw-away `0600` file;
  if that reads back loose too, it logs a warning and continues. A loose key on a disk that does keep
  permissions is still refused.

## [0.1.6] — 2026-10-17

Protocol 1. Hosted worker only; the on-prem gateway is unchanged.

### Fixed

- The hosted worker now answers `200 ok` on `WEBSITES_PORT` (or `PORT`) when the host sets one.
  Azure App Service for Containers kills a container that answers no HTTP ping at start, so 0.1.5
  could not run there. Nothing listens when neither variable is set, and the answer carries no
  state.

## [0.1.5] — 2026-10-17

Protocol 1. The first release that publishes the hosted worker image. Tag `v0.1.4` was pushed while
`__version__` was still `0.1.3`, so the release workflow's version check failed and nothing was
published under it; use 0.1.5.

### Added

- Hosted worker mode: `qlar-email-hosted-worker`, run by Qlar to serve many mailboxes from one
  process (install with the `hosted` extra), and the image `ghcr.io/qlar-ai/email-hosted-worker`
  built from `Dockerfile.hosted`. Mailbox credentials come from Azure Key Vault and registrations
  from the Qlar control plane. See [docs/HOSTED.md](docs/HOSTED.md). The on-prem gateway is
  unchanged.

## [0.1.3] — 2026-10-11

Protocol 1. No Messenger-BE change needed.

### Removed

- The recent-senders rule of the send guard. A reply no longer has to go to someone who wrote to
  the mailbox in the last 30 days, and `RECIPIENT_ALLOWLIST` / `RECIPIENT_MEMORY_DAYS` are ignored
  (an existing `.env` that sets them still loads). Who the agent answers is decided in the Qlar CMS
  — the channel's public / private mode and contact lists — and test sends now reach any address.
  The guard still allows one recipient per reply, as one bare address, and at most
  `MAX_SENDS_PER_HOUR_PER_RECIPIENT` replies per recipient per hour.

### Fixed

- `enroll` against an unreachable Qlar (an expired TLS certificate, no network) crashed with a
  traceback. It now says what failed, that a certificate problem is the server's to fix, and
  that the enrolment code was not used.

## [0.1.2] — 2026-10-09

Protocol 1. Needs Messenger-BE with the `unknown_gateway` refusal for the deletion part.

### Changed

- `enroll --code <code>` on a machine that is already enrolled enrols it again. The old
  `gateway-state.json` is moved to `gateway-state.json.old` once Qlar accepts the new code; a
  refused code (yesterday's command re-run from history) keeps the existing enrolment and serves.
  It used to stop with "already enrolled ... delete gateway-state.json".
- Enrolled against a different endpoint (direct App Service URL vs. `api-dev.qlar.ai`) now
  enrols against the new one instead of refusing.

### Added

- A gateway deleted in the CMS stops serving instead of retrying a `401` forever: Qlar answers
  `403 {"reason":"unknown_gateway"}`, the state file is set aside, and at a terminal the gateway
  asks for a new code and carries on. Without a terminal it exits with instructions.

## [0.1.1] — 2026-10-08

Protocol 1.

### Fixed

- SMTP over `starttls` or `ssl` failed before login with `check_hostname requires
  server_hostname`: the connection was opened without telling smtplib the host name it verifies
  the certificate against. Every real mail server hit this; the unit tests' fake SMTP did not.

## [0.1.0] — 2026-10-08

First release, protocol 1. Started as a copy of `qlar-data-gateway` 0.2.0 (CLI, setup prompts,
enrolment, signing, polling, audit, Docker and release pipeline) with the database half replaced
by a mailbox.

### Added
- IMAP watcher: IDLE with polling fallback, UID tracking per `UIDVALIDITY`, read-only and
  `BODY.PEEK[]` so mail is never marked read, moved or deleted.
- Inbound filters for auto-replies, bulk/list mail, the mailbox itself, system senders, mail
  without a `Message-ID` and mail dated before enrolment.
- Text extraction (plain text preferred, HTML converted with BeautifulSoup), quoted-reply
  stripping and a length limit.
- `send_email` jobs sent over SMTP with threading headers and `Auto-Submitted: auto-replied`.
- Send guard: own `From` only, one recipient who wrote in recently or is allow-listed, hourly rate
  limit, no attachments.
- `test_connection` jobs and the `test-mailbox` command.
- Local JSONL audit of every forwarded, filtered and sent email.
