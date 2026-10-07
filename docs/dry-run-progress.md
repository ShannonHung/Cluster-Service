# Dry-Run Mode — progress and working notes

**Last updated:** 2026-10-07

Living status note for the dry-run work across `deploy-service` and `cluster-service`.
It records what is done, what is next, and the things learned while building it that are
not captured anywhere else. Update it as tickets land; delete it once T11 closes and its
durable content has moved into `CLAUDE.md`.

The design itself lives elsewhere and is **not** repeated here:

- **`../deploy-service/docs/arch/dry-run-mode.md`** — the authoritative spec for *both*
  services: problem statement, seam selection, user stories, out-of-scope.
- **`CLAUDE.md`** (this repo) and **`../deploy-service/CLAUDE.md`** — the as-built docs.
- Per-ticket issues carry acceptance criteria; merged commits carry each decision's rationale.

---

## Status

### deploy-service — complete

T1–T7 merged, every issue closed including the parent. `develop` at `a992d5f`.
562 tests pass (`make test`: 494 passed / 71 deselected).

### cluster-service — T9 is next

| Ticket | Issue | PR | State |
|---|---|---|---|
| T8 scaffolding + prod guard + response marker | [#21](https://github.com/ShannonHung/Cluster-Service/issues/21) | [#25](https://github.com/ShannonHung/Cluster-Service/pull/25) | merged, closed |
| T10 fake `CoreV1Api` | [#23](https://github.com/ShannonHung/Cluster-Service/issues/23) | [#26](https://github.com/ShannonHung/Cluster-Service/pull/26) | **PR open** |
| **T9 dry-run deploy-service client** | **[#22](https://github.com/ShannonHung/Cluster-Service/issues/22)** | — | **next, 0 blockers** |
| T11 deny-path coverage + docs | [#24](https://github.com/ShannonHung/Cluster-Service/issues/24) | — | blocked by #22, #23 |

Parent: [#20](https://github.com/ShannonHung/Cluster-Service/issues/20).

T9 and T10 touch different seams and never conflict, so T9 can start regardless of #26.
Check the dependency frontier with:

```bash
gh api repos/ShannonHung/Cluster-Service/issues/22 \
  --jq '.issue_dependencies_summary.blocked_by'
```

---

## What T9 involves

The seam is **`DeployServiceClient`**, consumed by `PipelineService` (deploy proxy) and
`CommandService` (command proxy). Both are thin orchestration layers, which is what makes
the client the right substitution point — the services keep running.

Two things make T9 fiddlier than T10, and are why it was sequenced second:

1. **`DeployServiceTokenManager` is a module-level singleton** in `app/api/v1/deploy.py`
   (`_deploy_token_manager`), shared with the command proxy. A dry-run instance must hold
   no deploy-service credentials, so the token fetch has to be bypassed — without
   instantiating a second manager per request.
2. **`_request_with_retry` retries once on 401** after forcing a token refresh, then maps
   any non-2xx to `DeployServiceError`. Whatever the fake returns must keep that mapping
   reachable, or the deny paths T11 needs disappear.

`DeployServiceError` adapts the upstream error body via `_DEPLOY_CODE_MAP` (body
`error.code`) with `_DEPLOY_STATUS_MAP` (HTTP status) as fallback. A dry-run client that
returns a plausible error shape keeps that adapter honest.

**Update the start-up banner when T9 lands.** It names what is still real, and a test in
`tests/unit/test_dry_run_mode.py` pins that wording (`test_the_warning_*`) — it will go red,
which is deliberate rather than a nuisance: the banner must never claim more safety than
exists, since an operator could otherwise believe a drain was safe to run. Update the
banner, that test, the Dry-Run section of `CLAUDE.md`, and the note in `.env` together.

---

## Working agreements

- **Branch off `develop` per ticket** (`feat/<slug>`), PR into `develop`, never commit to
  `main`.
- **Close issues manually.** GitHub's `Closes #N` only fires when a PR merges into the
  *default* branch (`main`); both repos merge into `develop`. Closing also refreshes the
  dependency frontier — leaving one open keeps its dependents looking blocked.
- **`gh issue list` does not expose `issueDependenciesSummary`** — use `gh api` as above.
- **Use `--body-file -` with a heredoc for `gh` bodies.** A `$(cat <<'EOF' …)` wrapper lets
  the shell run backtick content as command substitution; it silently ate branch names once.
- **The full suite takes ~2 minutes**, past some default command timeouts. `make test` is
  ~30s because it deselects the real-cluster `e2e` tests.

---

## Conventions this work follows

**Mutation-test every ticket.** Every PR so far proved its tests fail when the wiring is
removed. This matters more than usual here: the premise of the whole spec is that a naive
dry-run produces tests that can never fail. The canonical mutation is *simulating the
"simpler" refactor* — short-circuiting in the router — which must break the deny-path tests.

**Back mutations up by copying files, never `git checkout`.** A `git checkout` during T10
reverted the ticket's own wiring along with the mutation and produced 21 phantom failures.
`cp` the file aside, mutate, `cp` back, confirm with `git status`.

**Deny paths are the point, not an afterthought.** Assert the *reason*, not just the status
code. A deploy-service scope test passed for the wrong reason — the user also lacked a
whitelist file, so a bypassed scope check still produced 403 — and had quietly stopped
testing scopes at all.

**Keep the `e2e` marker meaningful.** Here `e2e` means *needs a real cluster* (10 tests,
`E2E_DRAIN_NODE`, excluded from `make test`). Dry-run tests need no cluster, so they must
**not** carry it. In deploy-service the same marker means *needs Redis/SSH/docker*.

**Synthetic values are deliberately implausible** — `.invalid` hostnames, RFC 5737
TEST-NET-1 IPs (`192.0.2.0/24`), pipeline id `999001`, kubelet `v9.99.0-dry-run`. A leaked
value should fail loudly, never collide with a real record.

---

## Things learned while building this

Not in the spec, and the kind of thing that bites twice. Some of these describe code
introduced by T10, which is still on `feat/dry-run-fake-kube-client` until
[#26](https://github.com/ShannonHung/Cluster-Service/pull/26) merges — the notes are kept
here so they are not lost, but check the branch before looking for the symbols.

**Two `dry_run` concepts coexist in this service.** The node-drain endpoint has a
per-request `drain.dry_run` body field (router short-circuit, caller-controlled, one
endpoint). `DRY_RUN_MODE` is deployment-level. They are documented as explicitly **not to be
unified**, and a test asserts both work independently. Do not "tidy" this.

**The dry-run cluster repository must be swapped *before* the client factory.** The real
repositories read kubeconfig files from `KUBECONFIG_BASE_PATH`, so stubbing only
`KubeClientFactory` would still demand credentials on disk.

**Fake cluster state is mutable and shared per cluster name**, cleared by
`reset_dry_run_clusters()`. Per-request state was the first attempt and was wrong: a cordon
has to still be in effect on the next request. Call the reset in any test that mutates
state, the way you would call `get_settings.cache_clear()`.

**A fake must honour semantics, not just shapes.** Three concrete cases:

- Eviction has to actually remove the pod. `_wait_for_pods_gone` polls for 25s otherwise —
  a *slow false failure*, not an obvious error, which is why there is an explicit timing
  assertion. The mutation proving it takes the dry-run suite from ~13s to ~116s.
- `patch_node` has to convert taint dicts to `V1Taint`, because the service reads `.key`
  off them and a real read returns objects.
- In deploy-service, `kill -0` has to report non-zero for a dead process group.

**Use `default_factory=`, never `default=`, for the response marker.** `default=` binds at
import, so the marker then ignores every later settings change — including every test that
toggles the flag. Easy to write, invisible by inspection; a named test pins it.

**Test fixtures differ between the two repos.** This repo has `test_admin` and
`test_operator`, both holding `cluster_api` — so no account can be denied a node route.
deploy-service has `test_admin` / `test_deployer` / `test_command`. Check before writing a
scope-denial test; widening the shared users fixture for one assertion is the worse trade.

**A missing token answers FastAPI's own `{"detail": …}`**, not the app's `{"error": {…}}`
envelope, because `OAuth2PasswordBearer` raises before the app handler runs. Use a
*malformed* token to exercise the app's error contract. Pre-existing in both services and
unrelated to dry-run.

---

## Settled scope boundary

Dry-run proves *our services* behave correctly; it cannot prove the GitLab pipeline itself
runs. Confirmed acceptable on 2026-10-06 — the requesting team cares that our service
behaves correctly. Recorded in the spec's Out of Scope section. **Do not reopen.**
