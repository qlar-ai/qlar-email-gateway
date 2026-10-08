# DEV acceptance — 0.1.0 (protocol 1)

PRD §10 criteria 1–9, walked against DEV with a real test mailbox. Fill in **Result** and
**Evidence** as each row is run; a failing row is fixed in the owning repo and run again.

## Before starting

| Needed | Who |
|---|---|
| Messenger-BE (plan 1) and Dialog-BE / CMS-BE / CMS-Web (plan 2) deployed to DEV | user |
| `EmailGatewayRegistration` container and `Messenger_EmailGateway_SigningPrivateKey` in DEV (`Messenger-BE/docs/email-gateway-runbook.md`) | user |
| Release `v0.1.0` tagged; GHCR package `qlar-ai/email-gateway` set **Public** | user |
| A test mailbox with IMAP + SMTP basic auth or an app password (e.g. `qlar-test@…`) | user |
| A second, external address to send from (the "customer") | user |
| A test agent in DEV CMS with a published knowledge base | user |
| A machine with Docker or Python 3.11+ for the gateway | user |

Where to look:

- **CMS** — agent → Channels → Email: gateway status, mailbox status, console.
- **Gateway terminal** — `docker logs -f qlar-email-gateway` or the `run` output.
- **Local audit** — `state/audit/mail.jsonl` (Docker) or `audit/mail.jsonl`.
- **Messenger-BE log** — `az webapp log tail -g RG_DEV -n wap-pusaka-messenger-dev | grep "\[EmailGateway"`.

## Checklist

| # | Action | Expected | Where | Result | Evidence |
|---|---|---|---|---|---|
| 1 | In CMS generate an enrolment code; paste the Docker (or Python) lines from the panel on the gateway machine; answer the mailbox prompts | Terminal prints a fingerprint; CMS shows the gateway `pending_approval` with the **same** fingerprint. Click Approve: within 10 s CMS shows **online** and mailbox status **ok**; terminal says `approved` | CMS, terminal | | |
| 2 | From the external address, email the test mailbox a question the agent can answer | Reply arrives within 2 minutes **in the same thread** in the mail client; subject `Re: <original>`; raw headers show `Auto-Submitted: auto-replied` and `X-Qlar-Agent`; audit has `forwarded` then `sent`; CMS console shows IN received, OUT queued, OUT sent | Mail client, audit, CMS console | | |
| 3 | Reply to the agent's answer in the same thread with a follow-up that depends on it; then send a **new** email (different subject, no reply) | Follow-up answered with the earlier context; the new email starts a fresh conversation (CMS conversation list shows two threads for this sender) | Mail client, CMS conversations | | |
| 4 | Send one email with header `Auto-Submitted: auto-replied` (e.g. trigger an out-of-office) and one from a `noreply@` address | Neither reaches Qlar (no CMS console IN line); audit has `filtered` with reasons `auto_submitted` and `system_sender` | Audit, CMS console | | |
| 5 | CMS → Email → **Test send** to an address that never wrote to the mailbox and is not on `RECIPIENT_ALLOWLIST` | Gateway refuses: CMS console OUT `rejected` (category `rejected`, code `recipient_not_known`); audit `send_rejected`; nothing in the external inbox; not retried | CMS console, audit | | |
| 6 | Stop the gateway (`docker stop qlar-email-gateway`); send three emails; start it again | Nothing processed while stopped; after start the three are forwarded once each, in order; three replies, no duplicates; `lastUid` in `state/gateway-state.json` = last UID | Mail client, audit, state file | | |
| 7 | (a) CMS → Revoke. (b) Re-approve, then delete the test agent | (a) Terminal logs `revoked … stopping` on the next poll. (b) Messenger-BE shows the registration revoked with reason `agent_deleted`; after 5 minutes the row is gone; the agent's email channel is gone from CMS | Terminal, CMS, `[EmailGateway admin]` logs | | |
| 8 | Move the gateway machine's clock 5 minutes ahead (with NTP off); wait one poll; restore the clock | Terminal shows `401` with `clock_skew`; after restoring, polling recovers by itself with no restart | Terminal | | |
| 9 | Enrol a second gateway for the **same mailbox** under another agent B and approve it | CMS refuses with a message naming the mailbox as already connected; the second gateway stays `pending_approval` | CMS | | |

Criterion 10 (the same scenarios with the TypeScript fake gateway) is covered by
`CMS-Web/e2e/email-gateway/email-gateway-protocol.spec.ts` and plan 2's UI E2E.

## Sign-off

| | |
|---|---|
| Date | |
| Messenger-BE build | |
| Gateway version | 0.1.0 (protocol 1) |
| Run by | |
