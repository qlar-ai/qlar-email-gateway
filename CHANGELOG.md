# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). The wire protocol version is separate from the
release version and is listed per release.

## [Unreleased]

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
