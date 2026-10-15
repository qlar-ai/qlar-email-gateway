# Hosted worker

`qlar-email-hosted-worker` is the Qlar-run process that serves every hosted mailbox. It is not
for customer premises; there, use the [on-prem gateway](INSTALL.md). Image:
`ghcr.io/qlar-ai/email-hosted-worker`, built from `Dockerfile.hosted`. It listens on no port.

## Configuration

Five environment variables and nothing else. Which are needed depends on where Messenger-BE keeps the
mailbox credentials (`Messenger-BE/docs/email-hosted-runbook.md`): **Option A** Key Vault, **Option B**
Cosmos-encrypted (no Key Vault).

| Variable | Required | Meaning | Default |
|---|---|---|---|
| `QLAR_BASE_URL` | yes | Control-plane base URL, ends in `/api/email-gateway`; must be `https://` outside local testing | — |
| `QLAR_WORKER_API_KEY` | yes | Worker key, sent as `X-Internal-Api-Key` | — |
| `KEY_VAULT_URI` | only for Option A | Azure Key Vault holding the per-mailbox credential secrets. Required only if some registration's `credentialSource` is `keyvault` (absent counts as `keyvault`); a registration whose source needs it while it is unset is skipped with an error | empty |
| `STATE_DIR` | no | Root of the per-mailbox state | `/state` |
| `LOG_LEVEL` | no | Python log level | `INFO` |

With a required variable missing the worker prints `missing required environment: ...` and exits 1.
A worker for Option B needs only `QLAR_BASE_URL`, `QLAR_WORKER_API_KEY`, `STATE_DIR` and `LOG_LEVEL`:
registrations with `credentialSource` `control-plane` have their credentials fetched from
`GET /hosted/registrations/{gatewayId}/credentials` (worker key, plaintext over TLS, never cached).

## State layout

Mount a volume at `/state`. Each mailbox gets its own folder named by its gateway id:

```
/state/{gatewayId}/gateway-key.pem        identity key
/state/{gatewayId}/gateway-state.json     enrolment and poll state
/state/{gatewayId}/audit/mail.jsonl       local audit log
```

When a mailbox is removed (a `remove` command, or its registration is revoked or no longer
listed), the worker stops it and then deletes its whole `/state/{gatewayId}` folder, private key and
audit log included, once its threads are confirmed dead. If Qlar answers `unknown_gateway` for a
mailbox it still lists as live (its key was dropped, for example by a re-key), the worker deletes
that folder and enrols the mailbox again instead of halting it.

## Control plane

The worker reads the list of hosted registrations (`GET /hosted/registrations`), long-polls for
commands (`configure`, `refresh_credentials`, `remove`, `test_credentials`) with
`{"maxWaitSeconds": 25}`, and posts each credentials-test result back. A re-key is not a command:
when the worker finds a mailbox that Qlar lists as enrolled but that has no local key, it calls
`POST /hosted/registrations/{gatewayId}/rekey` itself and redeems the fresh enrolment code it gets
back; nothing prompts.

## Key Vault access (Option A only)

Option B needs no managed identity and no role. For Option A the worker's identity needs secret **get** only (for example the `Key Vault Secrets User` role
scoped to the vault). It never lists, sets or deletes secrets.

## Operating rules

- **One instance in v1.** Run exactly one worker. It serves at most 200 mailboxes: `MAX_MAILBOXES`
  is a constant in the code, not an environment variable; registrations beyond it are not served. Two instances against the same registrations
  would invalidate each other's enrolment codes.
- **A 401 or 403 from the control plane stops the worker with exit code 1.** Rotating
  `QLAR_WORKER_API_KEY` therefore needs an ordering: set the new key on the control plane side,
  then restart the worker with it. Until restarted, the old key makes the worker stop.
- **Stop timeout.** On SIGTERM/SIGINT the worker's shutdown is bounded by a shared deadline of
  ~25 s for all mailboxes, plus any in-flight SMTP send (up to 30 s). Set the orchestrator's stop
  grace period above that: `--stop-timeout 60` for Docker, `WEBSITES_CONTAINER_STOP_TIME_LIMIT=60`
  on App Service.

## Run

```
docker run -d --name email-hosted-worker -v email-hosted-state:/state \
  -e QLAR_BASE_URL=https://example.invalid/api/email-gateway \
  -e QLAR_WORKER_API_KEY=... -e KEY_VAULT_URI=https://example.vault.azure.net \
  --stop-timeout 60 ghcr.io/qlar-ai/email-hosted-worker:latest
```
