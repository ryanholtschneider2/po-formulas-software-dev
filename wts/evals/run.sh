#!/usr/bin/env bash
# Pack-level evals for po-formulas-software-dev-wts.
#
# Pattern (modeled after ~/Desktop/Code/personal/directive's dogfood-eval):
# bash runner exercises each registered formula end-to-end against a
# disposable bd-server rig, emits `PASS: <case>` / `FAIL: <case>` lines
# on stdout. The `dogfood-eval` formula in agent_engine counts those lines
# and writes a history entry — invoke that way for CI integration, or run
# this script directly during dev:
#
#   bash wts/evals/run.sh                          # all cases
#   bash wts/evals/run.sh --case epic-wts-shape    # one case
#   bash wts/evals/run.sh --no-cleanup             # keep tmp rig + bd db
#
# Each case sets up a scratch rig under /tmp/wts-eval-<case>-<utc>/,
# initializes bd against the local dolt-sql-server on port 3307, runs
# the formula with `--stub-backend`, asserts on the return-value JSON
# + verdict files, then tears down.
#
# Prereqs:
#   - dolt-sql-server running on 127.0.0.1:3307 (PO's standard dev backend)
#   - `po` on PATH, with po-formulas-software-dev{,-wts} editable-installed
#   - `bd` on PATH
#   - jq

set -uo pipefail
shopt -s expand_aliases

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
STATE_DIR="${REPO_ROOT}/evals/.state"
mkdir -p "${STATE_DIR}"

CASE_FILTER=""
KEEP_TMP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --case) CASE_FILTER="$2"; shift 2 ;;
    --no-cleanup) KEEP_TMP=1; shift ;;
    -h|--help)
      sed -n '1,30p' "$0"
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

PASS_COUNT=0
FAIL_COUNT=0

pass() { echo "PASS: $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "FAIL: $1 — $2"; FAIL_COUNT=$((FAIL_COUNT + 1)); }

# ─────────────────────── shared scaffold ──────────────────────────────

scaffold_rig() {
  local name="$1"
  local rig="/tmp/wts-eval-${name}-$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "${rig}"
  (cd "${rig}" && git init -q && git commit --allow-empty -m "init" -q)
  (cd "${rig}" && bd init \
    --server --server-host=127.0.0.1 --server-port=3307 \
    --server-user=root --database="wts_eval_${name}_$(date +%s)" \
    >/dev/null 2>&1) || { echo "${rig}::bd-init-failed"; return 1; }
  echo "${rig}"
}

cleanup_rig() {
  local rig="$1"
  [ "${KEEP_TMP}" = "1" ] && { echo "kept: ${rig}" >&2; return 0; }
  rm -rf "${rig}"
}

case_active() {
  [ -z "${CASE_FILTER}" ] && return 0
  [ "${CASE_FILTER}" = "$1" ] && return 0
  return 1
}

# ─────────────────────── cases ────────────────────────────────────────

# CASE 1: po list shows all wts formulas + epic-wts is registered.
case_po_list_shape() {
  case_active "po-list-shape" || return 0
  local out
  out=$(po list 2>&1)
  local missing=()
  for f in epic-wts epic-finalize-wts pre-pr-review-wts pr-writer \
           software-dev-full-wts software-dev-fast-wts software-dev-edit-wts; do
    grep -qE "^formula\s+${f}\b" <<<"${out}" || missing+=("${f}")
  done
  if [ ${#missing[@]} -eq 0 ]; then
    pass "po-list-shape"
  else
    fail "po-list-shape" "missing formulas: ${missing[*]}"
  fi
}

# CASE 2: epic-wts dispatches both children, advances chain, returns dict.
# Validates the shape we hand-tested in this session.
case_epic_wts_shape() {
  case_active "epic-wts-shape" || return 0
  local rig epic c1 c2 rc out
  rig=$(scaffold_rig "epic-wts") || { fail "epic-wts-shape" "scaffold failed"; return; }
  epic=$(cd "${rig}" && bd create "epic-wts eval epic" --type epic --priority 1 \
    -d "synthetic 2-child epic for shape validation" 2>&1 | \
    sed -nE 's/^[^:]*Created issue:[[:space:]]+([^[:space:]]+).*/\1/p' | head -1)
  c1=$(cd "${rig}" && bd create "child 1" --type task -p 2 \
    --deps "parent-child:${epic}" -d "stub child 1" 2>&1 | \
    sed -nE 's/^[^:]*Created issue:[[:space:]]+([^[:space:]]+).*/\1/p' | head -1)
  c2=$(cd "${rig}" && bd create "child 2" --type task -p 2 \
    --deps "parent-child:${epic}" -d "stub child 2" 2>&1 | \
    sed -nE 's/^[^:]*Created issue:[[:space:]]+([^[:space:]]+).*/\1/p' | head -1)
  (cd "${rig}" && bd update "${c1}" --set-metadata po.formula=software-dev-fast-wts >/dev/null 2>&1)
  (cd "${rig}" && bd update "${c2}" --set-metadata po.formula=software-dev-fast-wts >/dev/null 2>&1)
  out=$(po run epic-wts --epic-id "${epic}" --rig wts-eval --rig-path "${rig}" \
        --dry_run true --stub-backend 2>&1)
  rc=$?
  # Shape assertions: chain reached pre-pr-review and returned a verdict.
  local errors=()
  [ ${rc} -eq 0 ] || errors+=("non-zero rc=${rc}")
  grep -q "submitting 2 node(s)" <<<"${out}" || errors+=("did not submit 2 nodes")
  grep -q "epic_wts: pre-pr-review" <<<"${out}" || errors+=("did not reach pre-pr-review step")
  grep -qE "'verdict': '(passed|blocked|partial|failed)'" <<<"${out}" || errors+=("missing top-level verdict")
  grep -q "'epic_run'" <<<"${out}" || errors+=("missing epic_run in return dict")
  grep -q "'pre_pr_review'" <<<"${out}" || errors+=("missing pre_pr_review in return dict")
  if [ ${#errors[@]} -eq 0 ]; then
    pass "epic-wts-shape"
  else
    fail "epic-wts-shape" "${errors[*]}"
    echo "--- last 30 lines of output ---" >&2
    echo "${out}" | tail -30 >&2
  fi
  cleanup_rig "${rig}"
}

# CASE 3: pr-writer dry-run writes a verdict file with the expected shape.
case_pr_writer_dry_run() {
  case_active "pr-writer-dry-run" || return 0
  local rig bead out rc errors=()
  rig=$(scaffold_rig "pr-writer") || { fail "pr-writer-dry-run" "scaffold failed"; return; }
  bead=$(cd "${rig}" && bd create "pr-writer dry-run target" --type task -p 2 \
    -d "stub bead" 2>&1 | grep -oE "[a-z_]+-[a-z0-9]+" | head -1)
  out=$(po run pr-writer --bead_id "${bead}" --rig wts-eval --rig-path "${rig}" \
        --dry_run true --stub-backend 2>&1)
  rc=$?
  [ ${rc} -eq 0 ] || errors+=("non-zero rc=${rc}")
  local verdict_path="${rig}/.planning/pr-writer/${bead}/verdicts/pr-writer.json"
  if [ -f "${verdict_path}" ]; then
    jq -e '.verdict == "PASS"' "${verdict_path}" >/dev/null 2>&1 \
      || errors+=("verdict file present but verdict != PASS")
    jq -e '.dry_run == true' "${verdict_path}" >/dev/null 2>&1 \
      || errors+=("verdict file missing dry_run=true marker")
  else
    errors+=("verdict file not written at ${verdict_path}")
  fi
  if [ ${#errors[@]} -eq 0 ]; then
    pass "pr-writer-dry-run"
  else
    fail "pr-writer-dry-run" "${errors[*]}"
  fi
  cleanup_rig "${rig}"
}

# CASE 4: epic-finalize dry-run emits a post-flight artifact + Gates section.
case_epic_finalize_post_flight() {
  case_active "epic-finalize-post-flight" || return 0
  local rig epic out rc errors=()
  rig=$(scaffold_rig "epic-finalize") || { fail "epic-finalize-post-flight" "scaffold failed"; return; }
  epic=$(cd "${rig}" && bd create "epic-finalize eval target" --type epic -p 1 \
    -d "stub epic" 2>&1 | grep -oE "[a-z_]+-[a-z0-9]+" | head -1)
  out=$(po run epic-finalize-wts --epic-id "${epic}" --rig wts-eval \
        --rig-path "${rig}" --dry_run true 2>&1)
  rc=$?
  [ ${rc} -eq 0 ] || errors+=("non-zero rc=${rc}")
  local post_flight="${rig}/.planning/epics/${epic}/post-flight.md"
  if [ -f "${post_flight}" ]; then
    grep -q "## Gates" "${post_flight}" || errors+=("post-flight missing ## Gates section")
    grep -q "smoke walkthrough:" "${post_flight}" || errors+=("missing smoke line in Gates")
    grep -q "demo video:" "${post_flight}" || errors+=("missing demo video line in Gates")
    grep -q "remote CI:" "${post_flight}" || errors+=("missing remote CI line in Gates")
  else
    errors+=("post-flight artifact not written at ${post_flight}")
  fi
  if [ ${#errors[@]} -eq 0 ]; then
    pass "epic-finalize-post-flight"
  else
    fail "epic-finalize-post-flight" "${errors[*]}"
  fi
  cleanup_rig "${rig}"
}

# ─────────────────────── run all ──────────────────────────────────────

case_po_list_shape
case_epic_wts_shape
case_pr_writer_dry_run
case_epic_finalize_post_flight

echo
echo "evals: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"
[ ${FAIL_COUNT} -eq 0 ]
