# Hosted worker — deployment runbook (DEV, then PROD)

> **Run these yourself; nothing here is run by the agent.** Placeholders are in `<angle brackets>`.
> Messenger-BE side first: `Messenger-BE/docs/email-hosted-runbook.md` (Key Vault, the two app
> settings, APIM). This document is the worker side. Reference for the worker itself: [HOSTED.md](HOSTED.md).

## Hard rules

1. **Exactly ONE worker instance in v1.** App Service scale-out count = 1, no autoscale rule, no
   deployment slot that runs alongside (swap-style deploys briefly run two). A second instance
   calls `GET hosted/registrations` too, and every call re-issues a one-time enrolment code for
   un-enrolled mailboxes, so two instances invalidate each other's codes and flap enrolments.
2. **Create the managed identity BEFORE the first worker start.** `DefaultAzureCredential` works on
   a laptop through developer sign-in but fails on App Service until the identity and its role exist.
3. **The worker calls Messenger-BE over the DIRECT App Service URL**, not APIM.
4. A `401`/`403` from the control plane stops the worker (exit code 1). See *Key rotation* below.
5. The Docker daemon was not available while this was implemented: **the image has never been built
   or smoke-tested locally**. Step 1 is therefore a pending step for you, and the first CI build is
   its first real build.

## 1. Build and publish the image

The image is built by `.github/workflows/release.yml` on a `v*` tag push, **not** by hand. The
workflow pushes, with `GITHUB_TOKEN`:

- `ghcr.io/qlar-ai/email-hosted-worker:<version>` and `:latest` (from `Dockerfile.hosted`)
- the existing on-prem image `ghcr.io/qlar-ai/email-gateway` (same workflow, same tag)

The tag must equal `__version__` in `src/qlar_email_gateway/__init__.py` (currently `0.1.3`, which
is already released for on-prem; bump it first, e.g. `0.1.4`, and add the matching `pyproject`/doc
changes your release process needs, see `docs/RELEASING.md`).

```bash
cd qlar-email-gateway
# after bumping __version__ and committing on main:
git tag v<version> && git push origin main v<version>
```

Watch the *Release* run on GitHub Actions. Then:

1. **Set the package Public**: GitHub → org `qlar-ai` → Packages → `email-hosted-worker` →
   Package settings → Change visibility → Public. A private package makes App Service fail the pull
   with `unauthorized` (or you must configure registry credentials instead).
2. **Confirm the name.** The workflow actually pushes `ghcr.io/qlar-ai/…` (the design spec text says
   `pusakaai`; GITHUB_TOKEN cannot push to another org's registry, so `qlar-ai` is what exists). The
   on-prem CMS panel also references `ghcr.io/qlar-ai/email-gateway`; make sure the org name there and
   the one you use below are the one you see on GitHub.
3. Optional local smoke before relying on CI (needs Docker):

   ```bash
   docker build -f Dockerfile.hosted -t email-hosted-worker:local .
   docker run --rm email-hosted-worker:local   # must print "missing required environment: ..." and exit 1
   ```

## 2. App Service for Containers (DEV; PROD by substituting names)

Names below: DEV `RG_DEV`, `<worker-app-dev>`, plan `<plan-dev>` (Linux; B2/S2 is the sizing the
design assumes for ~200 mailboxes); PROD `RG_PROD`, `<worker-app-prod>`.

```bash
az appservice plan create -g RG_DEV -n <plan-dev> --is-linux --sku B2
az webapp create -g RG_DEV -p <plan-dev> -n <worker-app-dev> \
  --container-image-name ghcr.io/qlar-ai/email-hosted-worker:<version>

# (1) Identity FIRST, before any start with real settings
az webapp identity assign -g RG_DEV -n <worker-app-dev>

# (2) Pin to one instance, no autoscale
az appservice plan update -g RG_DEV -n <plan-dev> --number-of-workers 1
az webapp config set -g RG_DEV -n <worker-app-dev> --always-on true

# (3) Persistent state at /state (Azure Files)
az storage account create -g RG_DEV -n <storageaccountdev> -l <region> --sku Standard_LRS
az storage share-rm create --storage-account <storageaccountdev> -n email-hosted-state
az webapp config storage-account add -g RG_DEV -n <worker-app-dev> \
  --custom-id emailstate --storage-type AzureFiles --account-name <storageaccountdev> \
  --share-name email-hosted-state --access-key "<storage-key>" --mount-path /state

# (4) Do not let App Service mount its own storage over the container, and give shutdown time
az webapp config appsettings set -g RG_DEV -n <worker-app-dev> --settings \
  WEBSITES_ENABLE_APP_SERVICE_STORAGE=false WEBSITES_CONTAINER_STOP_TIME_LIMIT=60
```

If the plan is shared with other apps, scale-out applies to all of them: use a dedicated plan.
`WEBSITES_CONTAINER_STOP_TIME_LIMIT` must stay above the worker's ~25 s shutdown. The worker
listens on no port; if App Service's startup probe reports the container as unhealthy, disable the health
check rather than adding a listener (unverified: first run on App Service is yours to observe).

Then grant the Key Vault role to the identity just created
(`Messenger-BE/docs/email-hosted-runbook.md` §1b, role **Key Vault Secrets User**, get only).

## 3. App settings (the five worker variables)

Generate the key once per environment and use the **same** value on Messenger-BE
(`Messenger_EmailGateway_HostedWorkerApiKey`).

```bash
az webapp config appsettings set -g RG_DEV -n <worker-app-dev> --settings \
  QLAR_BASE_URL="https://wap-pusaka-messenger-dev.azurewebsites.net/api/email-gateway" \
  QLAR_WORKER_API_KEY="<worker-key>" \
  KEY_VAULT_URI="https://<vault-dev>.vault.azure.net/" \
  STATE_DIR="/state" \
  LOG_LEVEL="INFO"
az webapp restart -g RG_DEV -n <worker-app-dev>
```

PROD: `QLAR_BASE_URL=https://wap-pusaka-messenger-prod.azurewebsites.net/api/email-gateway`,
`<vault-prod>`, a different key. `QLAR_BASE_URL` must be `https://` and **not** the APIM host. The
worker refuses to start with a missing variable (`missing required environment: ...`, exit 1).
`LOG_LEVEL` stays `INFO`; do not run at `DEBUG` in PROD.

## 4. Verify

```bash
az webapp log tail -g RG_DEV -n <worker-app-dev>
```

Expect, with no hosted mailboxes yet: a quiet INFO log. **There is no "reconcile ... 0 mailboxes"
summary line**: the worker only logs per mailbox (`mailbox <id> started`, `mailbox <id> is gone or
revoked; stopping it`). Healthy is therefore *absence of the failure lines* plus a live control plane:

- no `the control plane refused the worker key` (critical; key mismatch or Messenger setting missing),
- no repeated `control plane unreachable` (wrong `QLAR_BASE_URL`, Messenger down),
- no `missing required environment` / container restart loop,
- on Messenger-BE, `az webapp log tail ... | grep "EmailGateway hosted"` shows no
  `Refused a worker call without a valid worker key`.

Once the first hosted mailbox is created from CMS (ACCEPTANCE-HOSTED.md row 1) the worker log shows
`mailbox egw_<guid> started`. That is the real "worker is wired" proof. A `Forbidden` while reading the
secret means the Secrets User role has not propagated or went to the wrong principal.

**No credential string in logs.** After the first mailbox has run, search the log for the password
and the app password (without spaces):

```bash
az webapp log download -g RG_DEV -n <worker-app-dev> --log-file worker-log.zip
# unzip and search for the app password you used in acceptance; there must be zero hits
```

## Key rotation (worker key)

Order matters, because a refused key stops the worker:

1. Set the **new** key on Messenger-BE (`Messenger_EmailGateway_HostedWorkerApiKey`).
   From this moment the running worker is refused and exits 1 on its next call.
2. Set the same new key on the worker (`QLAR_WORKER_API_KEY`).
3. Restart the worker (`az webapp restart`). Mailboxes are down between steps 1 and 3; do it quickly.

## Update the worker image

```bash
az webapp config container set -g RG_DEV -n <worker-app-dev> \
  --container-image-name ghcr.io/qlar-ai/email-hosted-worker:<new-version>
az webapp restart -g RG_DEV -n <worker-app-dev>
```

Do not use deployment slots (a slot swap runs two instances for a moment, rule 1). Mailbox state
survives on the Azure Files share.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `ImagePullFailure` / `unauthorized` | GHCR package is still Private, or the name/org differs from what CI pushed |
| Container restarts immediately, `missing required environment` | one of `QLAR_BASE_URL`, `QLAR_WORKER_API_KEY`, `KEY_VAULT_URI` unset |
| `the control plane refused the worker key` then exit 1 | key differs from Messenger-BE's, or Messenger's setting is unset (403) |
| mailbox `skipping`/errors reading the secret, `Forbidden` | Secrets User not assigned or not yet propagated; identity created after assignment target |
| Mailboxes lose enrolment after a state wipe | worker re-keys itself via `POST hosted/registrations/{id}/rekey`; nothing to do, but the share must be mounted at `/state` |
