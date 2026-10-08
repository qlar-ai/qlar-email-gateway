# Security policy

## Reporting a vulnerability

**Please do not open a public issue.** Email **security@pusaka.ai** with:

- what the problem is and which component it affects,
- the version (`qlar-email-gateway version`) and how it is deployed,
- the smallest reproduction you can manage.

You will get an acknowledgement within **2 business days** and an assessment with a fix
timeline within **10 business days**. We will keep you updated while a fix is prepared,
credit you in the release notes unless you prefer otherwise, and coordinate disclosure
timing with you.

Please do not include real credentials, mailbox passwords, `.env` contents or private
keys in your report — a redacted description is always enough to start.

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | yes |

While the project is pre-1.0, security fixes go to the latest minor release only.

## Scope

In scope: anything that lets a job send mail the send guard should have refused (another
recipient, another `From`, an attachment, past the rate limit), makes the gateway change the
mailbox (mark, move or delete mail), lets anyone impersonate a gateway or Qlar, bypasses
signature or replay protection, or extracts the private key or the mailbox password from the
process, its logs or its audit file.

Out of scope, because they are documented properties rather than defects (see
[docs/SECURITY-MODEL.md](docs/SECURITY-MODEL.md)): the text of forwarded emails being sent to Qlar and
into an AI model, which is the purpose of the software; an attacker who already has read
access to `gateway-key.pem` or `.env` on the host; and spoofed senders that the operator's own
mail server accepted.

## Verifying a release

Every release publishes SHA-256 checksums alongside the artifacts. Check them before
installing:

```bash
sha256sum -c qlar_email_gateway-0.1.0.sha256
```

Docker images are published to `ghcr.io/qlar-ai/email-gateway` and can be pinned by digest.
