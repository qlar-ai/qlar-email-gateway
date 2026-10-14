# Hosted mode — DEV acceptance walk (real Gmail mailbox)

Maps to hosted PRD §9 (`docs/email-channel-hosted-prd.md`). Walk it in order in **DEV** after
`Messenger-BE/docs/email-hosted-runbook.md` and [DEPLOY-HOSTED.md](DEPLOY-HOSTED.md) are done.
Companion to the on-prem [ACCEPTANCE.md](ACCEPTANCE.md).

## Preconditions

- [ ] Runbook smoke (a) passed: direct `GET hosted/registrations` with the worker key returned `200 {"registrations":[]}`.
- [ ] Messenger-BE log has **no** `kept in Redis` warning since its last restart (Key Vault store active).
- [ ] Worker running (single instance), no `the control plane refused the worker key` in its log.
- [ ] CMS-BE/CMS-Web `feat/email-gateway` deployed to DEV (CMS-BE drift resolved).
- [ ] A throwaway Gmail account with 2-step verification and an **app password** (Google shows it as
      four groups with spaces). Not a real customer mailbox. A real DEV agent with a published knowledge setup.
- [ ] A second mailbox/address you can send *from*, outside the test account.

Evidence column: paste the log line, screenshot name or command output. Mark each row PASS / FAIL / PENDING.

| # | Check (PRD §9) | Steps | Expected | Evidence | Result |
|---|---|---|---|---|---|
| 1 | **Connect without a terminal** (criterion 1) | CMS DEV → agent → Email channel → choose **Hosted (Qlar-managed)** → Gmail preset → address + app password pasted **exactly as Google shows it (with spaces)** → Test connection → Save. Start a stopwatch at Save | Test connection OK; channel becomes `active` and `mailboxStatus=ok` within 60 s; nobody touched a terminal. Messenger log: `Credentials stored in Key Vault`; worker log: `mailbox egw_… started`; `az keyvault secret show --vault-name <vault-dev> --name email-hosted-egw-<guid>` finds the secret (do not print the value) | | |
| 2 | **Answers in thread** (criterion 2) | From the outside address, email the mailbox with a question the agent can answer. Reply to the agent's answer with a follow-up | Answer arrives in the **same thread**; the second reply shows it kept context; both exchanges appear in the CMS console rows | | |
| 3 | **Credential failure and recovery** (criterion 3) | At Google, revoke/replace the app password. Wait for the next poll | CMS shows `auth_failed` with the instruction hint (not a generic error). Then CMS **Update credentials** with the new app password: Test passes, status returns to `ok`, **no new channel** is created, same gateway id | | |
| 4 | **Disconnect and reuse** (criterion 4) | CMS → Disconnect. Then: `az keyvault secret show --vault-name <vault-dev> --name email-hosted-egw-<guid>`. Then connect the same mailbox to a **different** agent | Worker log: `mailbox egw_… is gone or revoked; stopping it`, then `mailbox egw_…: deleted its local key and state` (the `/state/egw_…` folder is gone); secret show → not found (`SecretNotFound`); the second agent connects the same mailbox (duplicate-mailbox guard no longer blocks) | | |
| 5 | **Restart under load, no duplicates** (Review Focus 3) | With the mailbox active, start sending 3 distinct emails from the outside address one by one, and **restart the worker App Service** (`az webapp restart -g RG_DEV -n <worker-app-dev>`) while they are in flight | After recovery each of the 3 emails is answered **exactly once** (no missed, no duplicate), nothing re-answered from before the restart. Compare Azure Files `gateway-state.json` is intact and the worker log shows `mailbox … started` for the mailbox again | | |
| 6 | **On-prem unaffected** (criterion 6) | The DEV on-prem gateway from the previous acceptance (`ACCEPTANCE.md`) keeps running through this release. Send it an email | Still polls and answers, unchanged; its registration shows `mode` on-prem, status active; no re-enrolment was needed | | |
| 7 | **Load and isolation** (criterion 5), may be deferred | 20 hosted mailboxes on the one worker, deliberately break one password (change it at the provider). Measure reply latency of the other 19 before and during the failure | The broken mailbox goes `auth_failed` with escalating backoff; the other 19 show no latency increase | | PENDING (needs 20 test mailboxes) |

## Cross-checks during the walk

- [ ] **No credential in any log** (worker, Messenger-BE, CMS-BE): search each for the app password with and
      without spaces; zero hits.
- [ ] Single worker instance throughout (App Service plan instance count = 1); no enrolment flapping in the worker log.
- [ ] CMS GET responses never show a password or the secret name's value.

## Sign-off

Rows 1-6 PASS ⇒ DEV accepted. Row 7 stays PENDING until 20 test mailboxes exist. PROD smoke
(PRD §7 phase H4) repeats rows 1, 2 and 4 on an internal organization after the PROD runbooks are run.
