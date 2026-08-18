# Kitesurf Evaluation Environment Deployment (fixture dual-source contract)

This branch (`kitesurf-eval`) evaluates Kitesurf in its remote-endpoint form. Task pages come from
two kinds of sources with different responsibilities and hosting models; this is the repository's
**formal decision**:

1. **Static source = this repository's GitHub Pages.** The `pages/` directory is committed on the
   branch and published by the Pages workflow
   to `https://lexmount.github.io/Lexbench-Headless-Browser`, under the versioned `v1/` path.
   The sha256 of every file is pinned in `config/kitesurf_static_fixture.json` and verified entry by entry before each run.
2. **Dynamic source = self-deployed by the user.** The 28 dynamic probes — WebSocket echo, SSE,
   redirect chains, the auth/cart session mini-app, the server-side grader, the slow document,
   the upload receipt, and others —
   require real server-side behavior, which static hosting such as GitHub Pages is structurally
   unable to provide. The project does **not host**
   the dynamic source: the user runs the FixtureServer bundled with the harness on their own machine
   and exposes it to the
   remote endpoint through an HTTPS tunnel. The tunnel URL is a **run parameter**, not part of the results.
3. **A deployment-contract preflight is mandatory before any Kitesurf run.**
   `config/kitesurf_dynamic_fixture.json` pins together the sha256 of the 127 static routes, the
   behavioral assertions of the 28 dynamic
   probes, and the FixtureServer implementation fingerprint (`contract_sha256`);
   if verification fails, the run is refused. The URL is not the identity of the content — the contract is.
4. **Expected evolution:** once Kitesurf releases a local binary, it joins the main-branch roster and this branch is retired.
5. **Evidence-model declaration:** the remote endpoint has no binary fingerprint, no resource
   measurement, shared infrastructure, and public-internet latency
   counted against the task budget; therefore `formal_score_eligible: false` is in effect throughout.
   Its results are not on the same tier as the four local
   engines, and reports must present them in a separate column with that caveat.

## Four commands to a working run

```bash
# 1. Start the dynamic source (local FixtureServer, stable port)
python3 -m runner.run fixture-serve --port 8907

# 2. Open an HTTPS tunnel (standard example: cloudflared quick tunnel; any equivalent HTTPS tunnel works)
cloudflared tunnel --url http://127.0.0.1:8907
#    Note the https://<random>.trycloudflare.com printed in the output

# 3. Verify the deployment contract (static Pages source + your dynamic source; both sources must pass)
python3 tools/kitesurf_static_fixture.py  --base-url https://lexmount.github.io/Lexbench-Headless-Browser \
    --output runs/kitesurf_static_verification.json
python3 tools/kitesurf_dynamic_fixture.py verify \
    --base-url https://<random>.trycloudflare.com \
    --output runs/kitesurf_dynamic_verification.json

# 4. Run the recipe (allowed only once the verification artifacts are in hand)
python3 tools/kitesurf_experiments.py run raw_full \
    --var fixture_base_url=https://<random>.trycloudflare.com \
    --var output=runs/kitesurf_raw_full
```

`tools/kitesurf_experiments.py list` shows all recipes; `check` validates the manifest;
`render` only prints the commands that would be executed.

## Boundary declarations

- **Latency budget.** Task timeouts are the same as for the local engines (30s for most
  tasks). Your entire path (local machine →
  tunnel → remote endpoint → tunnel → local machine) counts against the budget. A high-latency path
  will push borderline tasks into
  timeout — that is a property of the deployment, not of the engine; before drawing conclusions in a
  report, first examine the distribution of timeouts in
  `runs/<id>/results.jsonl`.
- **Shared endpoint.** A public Kitesurf endpoint may be used by others at the same time, and
  concurrent interference cannot be attributed.
  For formal readings, use k=1 plus the failure-rerun adjudication flow (class-B adjudication), and
  do not run multiple recipes concurrently against the same endpoint.
- **Security note.** The tunnel exposes your local FixtureServer to the public internet. The
  FixtureServer serves only the
  fixture tree and deterministic probes and does not read files outside the repository, but it is
  still recommended to: close the tunnel as soon as you are done;
  avoid reusing long-lived domains; and never put any private content in `fixtures/`. The
  credential probes (auth flow) use
  test credentials pinned in the contract, not secrets.

## Relationship to the main branch

The main branch contains only the four local fixed-binary engines and carries zero Kitesurf code.
This branch adds, on top of the main branch:
the runner's generic `remote_cdp` identity contract (three-field same-connection verification, see
`runner/scripts/adapters/PROTOCOL.md`), the recipe mechanism, the fixture dual-source contract, and this document.
The `base_main_at_consolidation` field in `config/kitesurf_experiments.json` records the main-branch
commit at which the fork occurred.
