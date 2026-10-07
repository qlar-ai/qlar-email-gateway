# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). The wire protocol version is separate from the
release version and is listed per release.

## [0.1.0] — unreleased

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
