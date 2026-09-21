# Threat model

What agent-parity handles and where it could go wrong, written from what the code and docs do today. It is not a
security assessment. To report a problem, see [SECURITY.md](../SECURITY.md).

## Assets and data

- **Vendor API credentials** (SentinelOne token, Carbon Black API ID / key / org key, BitDefender key), **object-storage
  keys** and the optional **Splunk HEC token**. They are read from environment variables that `config.yaml` references
  as `${VAR}`. The code does not read `.env` itself (there is no dotenv dependency). Docker Compose takes it via
  `--env-file`, and local runs work the same way: `uv run --env-file .env agent-parity ...`. A variable that isn't in
  the process environment resolves to nothing, and its connector silently falls back to fixture mode.
- **AD inventory metadata** exported by `Export-ADDevices.ps1`: name, DNS hostname, OS and version, last logon, enabled
  flag, distinguished name. No password or hash fields are requested.
- **No domain credentials.** agent-parity never binds to LDAP; the script runs on an already domain-joined,
  vendor-managed endpoint ([ADR 0001](decisions/0001-collect-ad-data-via-vendor-remote-scripting.md)).

## Trust boundaries

```
agent-parity host ──vendor token──▶ EDR vendor ──runs script──▶ AD-joined endpoint
   │  (holds all credentials)                                        │
   │◀── GET / DELETE (own storage keys) ── object storage ◀── PUT ───┘
        presigned PUT URL travels to the endpoint as a script argument
```

The boundaries are the operator host, the vendor API and remote-scripting channel, the endpoint, the object store and,
if enabled, Splunk. The endpoint receives a URL, never a storage credential.

## The presigned PUT URL

- **Can:** write exactly one object, at one key (`ad-exports/<vendor>/<uuid>.csv`), until it expires (default 900
  seconds).
- **Can't:** read, list or delete anything, write any other key, or work after expiry
  ([ADR 0002](decisions/0002-return-ad-export-via-presigned-put-url.md)).
- **Not restricted:** only bucket, key and expiry are signed; content and size are not limited, and `Content-Type` is
  deliberately not bound.
- **Travels** through the vendor's channel as `UploadUrl` (SentinelOne `inputParams`; appended to the PowerShell command
  line for Carbon Black). Whether either vendor keeps those arguments in its console history has not been verified. A
  search of the Windows PowerShell event logs on the machines the original tool ran the script on found no record of the
  parameters, which says nothing about the vendor side.
- **Cleanup:** the object is deleted after download, best-effort; a failed delete is only logged.

## What is stored or logged

- **Credentials:** never written by the package.
- **`run` and `compare`:** write CSVs to `output/` (gitignored) and persist nothing else.
- **`sync` and the Celery tasks:** persist a local SQLite file (`agent_parity.db` by default, `*.db` gitignored) with
  hostnames, OS, coverage status and per-vendor status text, which includes error messages from failed calls.
- **Logs:** the CLI logs at WARNING; failures include exception text. Debug logging (not enabled by the CLI) records
  request URL, params and body, but not headers. For script runs the body includes the presigned `UploadUrl`, so a debug
  log holds a live write capability until it expires.
- **Credentials in error text:** vendor API credentials and the Splunk token are sent only in request headers, never in
  the URL, params or body. HTTP error text includes the URL but not headers, so it does not contain them. Vendor error
  bodies are echoed as received (for example BitDefender's RPC error) and have not been checked for embedded secrets.
- **Splunk (opt-in):** forwards coverage deltas to the configured HEC URL.
- **Keeping secrets out of the repo:** `.env` and `.env.*` are gitignored (`.env.example` has empty values),
  `config.yaml` holds only `${VAR}` references, and with nothing set every connector runs against synthetic fixtures. CI
  runs gitleaks and pip-audit, and Actions are pinned to commit SHAs.

## Residual risks

- Anyone who obtains the presigned URL within its window can write that one key.
- If a delete fails, an export (AD inventory metadata) stays in the bucket.
- Credentials are plain environment variables or a `.env` file on the operator host; there is no secrets-manager
  integration, and the CSV and SQLite outputs are unencrypted plain files.
- `api_url` and `STORAGE_ENDPOINT_URL` are used as given: TLS verification is on by default, but nothing requires
  `https://`.
- Error text is logged and stored as described above, and is not scrubbed.
- The script runs as the SYSTEM account on the endpoint, the highest local privilege, although all it does is read
  computer objects and upload one file. Anything that could alter the script before it is pushed would run with the
  same privileges.
- The Docker Compose stack is a dev/demo setup: MinIO uses default root credentials when `.env` is unset, the override
  file publishes ports 9000 and 9001, and the MinIO and `uv` images use `latest` tags.

## Non-goals

- Not an EDR or vulnerability scanner: it reports coverage gaps and remediates nothing.
- No web UI, authentication layer or multi-user access control
  ([ADR 0009](decisions/0009-standalone-package-owning-scheduling-and-persistence.md)).
- No claim about any live deployment: this repository runs on synthetic data.

Sources: [`storage.py`](../src/agent_parity/storage.py), [
`script_runner.py`](../src/agent_parity/script_runner.py), [
`rest_adapter.py`](../src/agent_parity/shared/rest_adapter.py), [
`Export-ADDevices.ps1`](../src/agent_parity/scripts/Export-ADDevices.ps1), [
`db.py`](../src/agent_parity/scheduling/db.py), [`docker-compose.yml`](../docker/docker-compose.yml).
