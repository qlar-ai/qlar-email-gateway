# Contributing

This gateway runs inside other organisations' networks, next to their mail server, holding a
mailbox password. The bar for changes here is deliberately higher than for a typical project:
every change is something a customer's security team may have to re-review.

## Principles

1. **Outbound only.** Nothing may listen on a port.
2. **Credentials stay here.** `MAIL_PASSWORD` is used for the IMAP and SMTP login and nowhere else:
   not in a log line, an exception message, the audit file or any request to Qlar.
3. **The send guard is local and cannot be loosened by Qlar.** Nothing in a job may change `From`,
   the recipient rule, the rate limit, or add attachments.
4. **The mailbox is read, never changed.** Read-only `SELECT`, `BODY.PEEK[]`, no flags, no moves.
5. **Few dependencies.** `cryptography`, `httpx`, `imapclient` and `beautifulsoup4`; adding one
   needs a good argument and a licence that is not copyleft.

## Development

```bash
git clone https://github.com/qlar-ai/qlar-email-gateway.git
cd qlar-email-gateway
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check .
pytest -q
```

The suite needs no mail server and no Qlar: IMAP, SMTP and Qlar are faked, and sleeps and the clock
are injected, so scenarios that take minutes in real life (an hour of backlog, IDLE dropping every
few minutes) run instantly. Keep it that way.

When a test fakes a library, fake what the library really does. The IMAP fakes in
`tests/test_mailbox.py` follow imapclient's behaviour — for example, `idle_check` returning an empty
list at once when the server closes the connection, rather than raising.

## Protocol changes

`docs/PROTOCOL.md` is the contract with Qlar. Any change to what is signed, sent or expected is a
protocol change: bump `PROTOCOL_VERSION`, update the document and the canonical-bytes tests on both
sides (here and in Messenger-BE), and keep accepting the previous version on the server.

## Releases

See [docs/RELEASING.md](docs/RELEASING.md).
