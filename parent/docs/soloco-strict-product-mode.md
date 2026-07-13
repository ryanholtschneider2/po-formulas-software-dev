# SoloCo Strict Product Mode

Use strict product mode when a SoloCo build must leave reviewer-ready, live verification evidence even when the sizing model classifies the change as low-risk code or documentation. The mode changes proof requirements, not model judgment: sizing still decides what surfaces exist, while the formula validates only the required artifact structure and declared operator policy.

## Rig configuration

Add this to the SoloCo rig's `.po-env`:

```dotenv
PO_AGENTIC_PROOF_MODE=strict
PO_PREVIEW=local
PO_DEMO_VIDEO=1
```

- `PO_AGENTIC_PROOF_MODE=strict` always runs review-artifact assembly and the live verifier. The default, `adaptive`, preserves the pre-existing fast path for low-risk code/docs work.
- `PO_PREVIEW=local` opts into process, checkout, and served-SHA attestation when the worker publishes a localhost preview. Backend-only work may omit a preview.
- `PO_DEMO_VIDEO=1` retains the existing UI-only demo requirement. Strict mode does not silently override this compatibility knob.

Invalid proof-mode values fall back to `adaptive`, matching the preview knob's compatibility behavior. Treat configuration review as the policy gate; do not depend on a typo becoming strict.

## Dispatch

Stamp the complete runtime tuple on the seed, then dispatch normally:

```bash
po run software-dev-agentic \
  --backend codex-tmux \
  --account codex-personal \
  --account-class personal \
  --model gpt-5.4 \
  --effort xhigh \
  --issue-id <seed> \
  --rig soloco \
  --rig-path /absolute/path/to/soloco \
  --pack-path /absolute/path/to/po-formulas-software-dev
```

For a multi-child product build, use `agentic-epic` with the same rig and pack paths. Shared-branch children inherit `.po-env`; each child produces `verified-delivery.json`, and assembled acceptance rejects missing child evidence, wrong ancestry, or absent product surfaces before the one epic PR becomes ready.

## Evidence and recovery

Each selected phase must produce fresh evidence for its current iteration:

| Phase | Required evidence |
|---|---|
| Deploy smoke | Non-empty `smoke-test-output.txt` with no explicit failure marker |
| Review artifacts | Non-empty `review-artifacts/summary.md`; workflow/infrastructure work also requires `overview.md` |
| Live verifier | Non-empty `verification-report-iter-N.md` and an `approved` verdict |
| UI demo when enabled | Non-empty current `review-artifacts/demo.mp4` |

Phase-owned files are removed before the phase runs, so stopped/retried proof cannot pass on stale bytes. A failed phase returns a concrete revision note to the same worker and reruns the proof chain within the sizing-selected iteration budget. `PO_RESUME=1` preserves the run directory and role-session state for a true continuation; a fresh redispatch archives prior iteration state.

Inspect a run with `po logs <seed>`, `po artifacts <seed>`, and the canonical `<rig>/.planning/software-dev-agentic/<seed>/verified-delivery.json`. Never treat dispatch as delivery: completion requires the formula's terminal state plus its evidence package and PR.

## Dogfood gate

Before changing this integration contract, run:

```bash
uv run --project parent python \
  parent/evals/run_verified_delivery_dogfood.py /tmp/verified-delivery-dogfood
```

The retained report covers a formula-executed strict backend task, the full UI
rejection/retry chain, and a formula-executed shared child whose emitted
delivery contract is consumed by assembled epic acceptance. It also proves
formula-boundary rejection of red smoke and missing packaging, a real localhost
stale preview, wrong PR base, verifier rejection, and an injected stop followed
by `PO_RESUME=1` through terminal completion.
