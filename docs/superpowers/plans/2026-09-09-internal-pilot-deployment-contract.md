# Internal Pilot Deployment Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a reproducible, secret-safe Compose deployment contract for the internal-pilot knowledge-management workflow.

**Architecture:** Keep `app` loopback-only and make its host port configurable. Compose app health reflects liveness while `/api/ready` remains strict knowledge-query readiness. A separate backup profile preserves the existing one-off `backup-all.sh` workflow, while documentation, a Caddy template, and a read-only verifier define the sole deployment entrypoint.

**Tech Stack:** Docker Compose v2, FastAPI, pytest, PyYAML, PowerShell 7, Caddy.

---

## File structure

- Modify `compose.yml`: app port and healthcheck; backup profile and safe default command.
- Modify `.env.example`: declare the non-secret deployment variables.
- Modify `tests/test_compose_auth.py`: assert the Compose deployment contract.
- Modify `tests/test_auth_api.py`: assert liveness and strict readiness coexist before first publication.
- Modify `tests/test_deployment_docs.py`: assert the public runbook, verifier, Caddy template, and acceptance form document the contract.
- Create `deploy/proxy/Caddyfile.example`: HTTPS reverse-proxy template for a host-local Caddy process.
- Create `deploy/operations/verify-deployment.ps1`: read-only verifier that redacts values.
- Modify `README.md`: single `.env`, fixed project name, HTTPS flow, profiles, readiness semantics, and verifier usage.
- Modify `docs/operations/coordinated-backup-restore.md`: explicit backup invocation and profile boundary.
- Modify `docs/operations/internal-pilot-auth-acceptance.md`: fillable knowledge-management pilot record.

### Task 1: Define failing Compose contract tests

**Files:**
- Modify: `tests/test_compose_auth.py:62-90,269-276`
- Modify: `tests/test_knowledge_e2e_contract.py:464-482`

- [ ] **Step 1: Write failing tests for the variable-backed loopback port and liveness healthcheck.**

  Add assertions that use the literal Compose interpolation contract, so the default and override are both rendered by Compose rather than guessed by Python:

  ```python
  assert app["ports"] == ["127.0.0.1:${APP_HOST_PORT:-8000}:8000"]
  assert "api/health" in " ".join(app["healthcheck"]["test"])
  assert "api/ready" not in " ".join(app["healthcheck"]["test"])
  for service_name in ("mysql", "postgres", "redis", "chroma", "knowledge-publisher", "knowledge-read-proxy", "knowledge-indexer", "knowledge-backup"):
      assert "ports" not in services[service_name]
  ```

- [ ] **Step 2: Write failing tests for backup isolation and its safe default.**

  Assert `knowledge-backup` is only in `knowledge-backup`, has `restart: "no"`, retains its two read-only knowledge volumes and has a non-empty command whose joined text references `backup-all.sh` and exits non-zero when launched directly. Keep the existing backup archive test so the script still overrides that default command with the actual archive operation.

- [ ] **Step 3: Run the targeted Compose tests and verify they fail because the old contract is still present.**

  Run: `pytest tests/test_compose_auth.py tests/test_knowledge_e2e_contract.py -q -p no:cacheprovider -p no:tmpdir`

  Expected: failures identify the hard-coded `127.0.0.1:8000:8000`, `/api/ready` healthcheck and `knowledge-admin` backup profile.

- [ ] **Step 4: Implement the minimal Compose and env-example changes.**

  In `compose.yml`, replace only the app binding and healthcheck endpoint:

  ```yaml
  ports:
    - "127.0.0.1:${APP_HOST_PORT:-8000}:8000"
  healthcheck:
    test:
      - CMD
      - python
      - -c
      - "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"
  ```

  For `knowledge-backup`, use `profiles: ["knowledge-backup"]` and an explicit guard command such as:

  ```yaml
  command:
    - python
    - -c
    - "import sys; sys.stderr.write('Use deploy/operations/backup-all.sh for a governed backup.\\n'); raise SystemExit(64)"
  ```

  Preserve its image, networks, read-only volumes, dependencies, and `restart: "no"`. Add `APP_HOST_PORT=8000`, `COMPOSE_PROJECT_NAME=metro-agent-pilot`, and a non-secret `PILOT_FQDN=` explanation to `.env.example`; do not add secret values.

- [ ] **Step 5: Re-run targeted Compose tests and verify they pass.**

  Run: `pytest tests/test_compose_auth.py tests/test_knowledge_e2e_contract.py -q -p no:cacheprovider -p no:tmpdir`

  Expected: all selected tests pass and the isolation assertions remain unchanged.

- [ ] **Step 6: Commit the isolated contract change.**

  ```powershell
  git add compose.yml .env.example tests/test_compose_auth.py tests/test_knowledge_e2e_contract.py
  git commit -m "fix: make app loopback port configurable"
  ```

### Task 2: Prove layered liveness and readiness semantics

**Files:**
- Modify: `tests/test_auth_api.py:994-1064`

- [ ] **Step 1: Write failing API tests for the cold-start, first-publication, published, and restart-equivalent states.**

  Add a test with a mutable `published` flag. Its injected readiness checker raises `ReleaseValidationError("published release pointer is unavailable")` while the flag is false and returns normally when true. Request both endpoints before and after flipping the flag, then construct a fresh app with the same ready checker to represent process restart:

  ```python
  with TestClient(app, raise_server_exceptions=False) as client:
      assert client.get("/api/health").status_code == 200
      assert client.get("/api/ready").status_code == 503
      published = True
      assert client.get("/api/ready").status_code == 200

  with TestClient(restarted_app, raise_server_exceptions=False) as client:
      assert client.get("/api/health").status_code == 200
      assert client.get("/api/ready").status_code == 200
  ```

  Construct `app` with the same fake graph, history, monitoring and auth factories as `test_liveness_does_not_depend_on_readiness`; this makes the test exercise the real routes rather than mocks.

- [ ] **Step 2: Run the focused API test and verify it initially fails only if the test exposes an unrepresented state.**

  Run: `pytest tests/test_auth_api.py -k "first_publication or liveness_does_not_depend_on_readiness or readiness_returns" -q -p no:cacheprovider -p no:tmpdir`

  Expected: the new test is a regression test for existing FastAPI behavior and may pass immediately; retain it because the production behavior change is Compose's switch from strict readiness to liveness. Separately, `test_admin_uploader_self_publishes_only_through_controlled_job` remains the automated first-upload and publish transition evidence.

- [ ] **Step 3: Make only the minimal production change required by the failing test.**

  Do not relax `KnowledgeReadinessChecker` or change `/api/ready`. The intended implementation is no application-code change: Compose's move to `/api/health` is the behavior change, while `api.py` continues to return `503` from `/api/ready` when strict readiness fails.

- [ ] **Step 4: Run the focused API and readiness suites.**

  Run: `pytest tests/test_auth_api.py tests/test_readiness.py tests/test_knowledge_readiness.py -q -p no:cacheprovider -p no:tmpdir`

  Expected: all tests pass, proving the four states are represented by liveness (cold start), the existing governed first-upload/publish E2E flow, strict readiness before publication, strict readiness after a valid release, and strict readiness after fresh app construction.

- [ ] **Step 5: Commit the readiness regression coverage.**

  ```powershell
  git add tests/test_auth_api.py
  git commit -m "test: define deployment readiness contract"
  ```

### Task 3: Add a read-only verifier and HTTPS proxy template

**Files:**
- Create: `deploy/operations/verify-deployment.ps1`
- Create: `deploy/proxy/Caddyfile.example`
- Modify: `tests/test_deployment_docs.py`

- [ ] **Step 1: Write failing tests for the operational artifacts.**

  Assert both new files exist. Assert the verifier uses `docker compose --env-file`, `config --quiet`, `ps --format json`, checks the names `POSTGRES_PASSWORD`, `AUTH_MYSQL_PASSWORD`, `MYSQL_ROOT_PASSWORD`, `AUTH_SESSION_PEPPER`, `MODEL_DIR`, `KNOWLEDGE_SOURCE_DIR`, `KNOWLEDGE_PUBLISHER_INTERNAL_BEARER_SECRET`, and `APP_HOST_PORT`, and contains no `up`, `down`, `start`, `stop`, `rm`, `volume rm`, or output of `$value`. Assert the Caddy template references `{$PILOT_FQDN}` and `reverse_proxy 127.0.0.1:{$APP_HOST_PORT:8000}` and sets the forwarded proto/host headers.

- [ ] **Step 2: Run the deployment-document tests and verify they fail because the artifacts do not exist.**

  Run: `pytest tests/test_deployment_docs.py -q -p no:cacheprovider -p no:tmpdir`

  Expected: failures for the missing verifier and Caddy template.

- [ ] **Step 3: Implement the minimal verifier and proxy template.**

  Implement the verifier with parameters and a non-mutating command wrapper:

  ```powershell
  param(
      [Parameter(Mandatory = $true)][string]$EnvFile,
      [string]$ProjectName = "metro-agent-pilot"
  )
  $required = @("POSTGRES_PASSWORD", "AUTH_MYSQL_PASSWORD", "MYSQL_ROOT_PASSWORD", "AUTH_SESSION_PEPPER", "MODEL_DIR", "KNOWLEDGE_SOURCE_DIR", "KNOWLEDGE_PUBLISHER_INTERNAL_BEARER_SECRET", "APP_HOST_PORT")
  $missing = @($required | Where-Object { -not $envMap.ContainsKey($_) -or [string]::IsNullOrWhiteSpace($envMap[$_]) })
  if ($missing) { throw "Missing required deployment variables: $($missing -join ', ')" }
  & docker compose --env-file $EnvFile --project-name $ProjectName config --quiet
  ```

  Parse the env file into `$envMap` without printing values. Parse `docker compose ... config --format json`, assert `services.app.ports` equals `127.0.0.1:$($envMap.APP_HOST_PORT):8000`, and inspect `ps --format json` only when the project exists; accept `running`/`healthy` for long-lived app, publisher and proxy services, and `exited` code zero for migrations. Return non-zero through `throw` for any mismatch.

  Create this host-Caddy template:

  ```caddyfile
  {$PILOT_FQDN} {
      encode zstd gzip
      reverse_proxy 127.0.0.1:{$APP_HOST_PORT:8000} {
          header_up Host {host}
          header_up X-Forwarded-Host {host}
          header_up X-Forwarded-Proto {scheme}
          header_up X-Forwarded-For {remote_host}
      }
  }
  ```

- [ ] **Step 4: Re-run the operational-artifact tests.**

  Run: `pytest tests/test_deployment_docs.py -q -p no:cacheprovider -p no:tmpdir`

  Expected: all pass, including the redaction and no-mutation assertions.

- [ ] **Step 5: Commit the artifacts and contract tests.**

  ```powershell
  git add deploy/operations/verify-deployment.ps1 deploy/proxy/Caddyfile.example tests/test_deployment_docs.py
  git commit -m "docs: add internal pilot deployment verifier"
  ```

### Task 4: Publish the runbook, backup boundary, and acceptance record

**Files:**
- Modify: `README.md:11-48,81-104`
- Modify: `docs/operations/coordinated-backup-restore.md`
- Modify: `docs/operations/internal-pilot-auth-acceptance.md:5-71`
- Modify: `deploy/operations/backup-all.sh`
- Modify: `tests/test_deployment_docs.py`

- [ ] **Step 1: Add failing documentation assertions.**

  Extend `test_deployment_docs.py` to require these exact contract strings across the three documents: `--env-file`, `--project-name metro-agent-pilot`, `APP_HOST_PORT`, `KNOWLEDGE_SOURCE_DIR`, `KNOWLEDGE_PUBLISHER_INTERNAL_BEARER_SECRET`, `deploy/operations/verify-deployment.ps1`, `deploy/proxy/Caddyfile.example`, `--profile knowledge-backup`, `backup-all.sh`, `首次发布前`, `PILOT_FQDN`, `版本 ID`, `包 SHA-256`, `操作者`, `镜像 digest`, and `证据位置`.

- [ ] **Step 2: Run documentation tests and verify the new assertions fail.**

  Run: `pytest tests/test_deployment_docs.py -q -p no:cacheprovider -p no:tmpdir`

  Expected: failures identify the absent single-env/project-name, verifier, proxy, backup-profile, readiness, and traceability instructions.

- [ ] **Step 3: Update the three documents with the exact operational path.**

  In `README.md`, replace ambiguous `docker compose` examples with a PowerShell preamble that sets only the env-file path and invokes:

  ```powershell
  $envFile = "D:\\MetroAgent\\pilot\\.env"
  docker compose --env-file $envFile --project-name metro-agent-pilot config --quiet
  docker compose --env-file $envFile --project-name metro-agent-pilot --profile knowledge-admin up --build -d
  pwsh -File deploy/operations/verify-deployment.ps1 -EnvFile $envFile -ProjectName metro-agent-pilot
  ```

  State that no worktree `.env` is authoritative, and that data volumes must never be reused with a different password set. Document `APP_HOST_PORT`, `MODEL_DIR`, `KNOWLEDGE_SOURCE_DIR`, and `KNOWLEDGE_PUBLISHER_INTERNAL_BEARER_SECRET` without values. Explain Caddy's `PILOT_FQDN` and `APP_HOST_PORT` environment variables, HTTPS certificate provisioning, and `AUTH_COOKIE_SECURE=true`.

  In `backup-all.sh`, require `METRO_AGENT_ENV_FILE` and `METRO_AGENT_COMPOSE_PROJECT_NAME`, define a `compose()` wrapper that calls `docker compose --env-file "$METRO_AGENT_ENV_FILE" --project-name "$METRO_AGENT_COMPOSE_PROJECT_NAME"`, and route every existing Compose invocation through it. In the backup guide, make `backup-all.sh` the only documented normal backup entrypoint and show `--profile knowledge-backup` only as its implementation dependency; explicitly say `--profile knowledge-admin up -d` must not start `knowledge-backup`.

  Add a `## 知识管理专项验收` section to the acceptance record containing a table with columns for item, command/action, expected result, evidence location and observed value. Include administrator login, real sanitized ZIP upload, validation, publish, traceable-source query, non-admin `403`, failed publish retains current version, rollback proves the old version, plus fields for version ID, package SHA-256, operator and image digest. Add separate rows for `/api/health=200`, pre-publication `/api/ready=503`, post-publication/restart `/api/ready=200`, and Cookie attributes over HTTPS.

- [ ] **Step 4: Re-run documentation tests.**

  Run: `pytest tests/test_deployment_docs.py -q -p no:cacheprovider -p no:tmpdir`

  Expected: all pass and no assertion requires a secret value.

- [ ] **Step 5: Commit the runbook and acceptance material.**

  ```powershell
  git add README.md docs/operations/coordinated-backup-restore.md docs/operations/internal-pilot-auth-acceptance.md deploy/operations/backup-all.sh tests/test_deployment_docs.py
  git commit -m "docs: add reproducible internal pilot deployment runbook"
  ```

### Task 5: Verify the integrated deployment contract

**Files:**
- Verify only: `compose.yml`, `.env.example`, deployment artifacts, tests, and documents above.

- [ ] **Step 1: Run the required focused test suite.**

  Run: `pytest tests/test_compose_auth.py tests/test_knowledge_admin_compose.py tests/test_knowledge_readiness.py tests/test_knowledge_e2e_contract.py tests/test_auth_api.py tests/test_deployment_docs.py -q -p no:cacheprovider -p no:tmpdir`

  Expected: zero failures.

- [ ] **Step 2: Run static analysis.**

  Run: `ruff check src tests`

  Expected: exit code `0`.

- [ ] **Step 3: Render the admin Compose profile using an explicitly supplied non-secret test env file.**

  Run: `docker compose --env-file D:\\MetroAgent\\pilot-contract\\.env --project-name metro-agent-pilot-contract --profile knowledge-admin config --quiet`

  Expected: exit code `0`; inspect the JSON render to confirm app maps only `127.0.0.1:18000:8000` when the isolated env file sets `APP_HOST_PORT=18000`, and no `knowledge-backup` service is selected.

- [ ] **Step 4: Render the backup profile separately.**

  Run: `docker compose --env-file D:\\MetroAgent\\pilot-contract\\.env --project-name metro-agent-pilot-contract --profile knowledge-backup config --quiet`

  Expected: exit code `0`; use `docker compose ... --profile knowledge-backup run --rm knowledge-backup` only to confirm its guard command exits non-zero with the documented `backup-all.sh` instruction, never with the normal admin startup.

- [ ] **Step 5: Review the final diff and commits.**

  Run: `git diff --check HEAD` and `git status --short`

  Expected: no whitespace errors; only the user's pre-existing untracked `docs/superpowers/plans/2026-09-07-knowledge-web-admin.md` and `uv.lock` remain outside this work.
