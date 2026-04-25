# Cross-runtime NL pilot — TYPHON operations runbook

Per RFC §6.3 the v2.2 acceptance gate requires running the 10-prompt NL
pilot against three live agent runtimes. STUDIO does not host the agents;
TYPHON (192.168.207.61) is the canonical agent box per
`~/.claude/CLAUDE.md` fleet reference.

This document captures the steps to execute the live pilot.

## Pre-flight checklist (on TYPHON)

```bash
ssh jasonperlow@192.168.207.61
hostname    # expect: typhon
```

Verify the three runtimes are up and reachable:

```bash
# OpenClaw — gateway should be on a known port
ps aux | grep -i 'openclaw' | grep -v grep
nc -z -w 1 localhost 18789 && echo "OpenClaw WS port open" || echo "DOWN"

# ZeroClaw — typically containerized
docker ps | grep -i zeroclaw
# Or, if running natively: which zeroclaw && zeroclaw --version

# Hermes — often launched on demand; check the running PIDs
ps aux | grep -i 'hermes' | grep -v grep
```

If any are down, bring them up before running the pilot. Reference the
project-specific quickstart docs for each runtime.

## Provider key

The pilot uses Gemini (`gemini-flash-latest`) by default. Set the key
either in shell env or via the agent runtimes' own configs:

```bash
export GEMINI_API_KEY="<your key>"
# or, if the runtime reads its own .env, no shell export needed
```

## Pull + sync the v2.2 branch on TYPHON

```bash
cd ~/Projects/InvestorClaw   # or wherever the working clone lives
git fetch origin              # origin = ARGONAS via fleet convention
git checkout feat/v2.2-consolidate
git pull origin feat/v2.2-consolidate

# If origin isn't argonas yet on TYPHON, fix the remotes first per
# git_remote_convention.md memory:
#   git remote rename origin gitlab
#   git remote add origin root@192.168.207.101:/mnt/datapool/git/InvestorClaw.git

# Sync deps
uv sync --frozen --group dev
```

## Run the pilot

```bash
mkdir -p reports
uv run python harness/run_cross_runtime_pilot.py \
    --output reports/v2.2-cross-runtime-pilot.json \
    --timeout 60 \
    --verbose

# Exit codes:
#   0 = all gates passed (OpenClaw ≥10/10, ZeroClaw ≥8/10, Hermes ≥6/10)
#   1 = at least one gate failed (read the report for failure breakdown)
#   2 = no runtime reachable (the pilot couldn't get a response from any agent)
echo "Pilot exit: $?"
```

The script prints a summary table and writes a full JSON report at the
output path. To run a subset (e.g. only OpenClaw because the others are
down):

```bash
uv run python harness/run_cross_runtime_pilot.py \
    --runtimes openclaw \
    --output reports/v2.2-pilot-openclaw-only.json
```

## After running

If `exit 0`:

1. Copy the report back to STUDIO (or commit it):
   ```bash
   # Commit the report so it lives in the repo as v2.2 evidence
   git add reports/v2.2-cross-runtime-pilot.json
   git commit -m "docs(v2.2): cross-runtime NL pilot results"
   git push gitlab feat/v2.2-consolidate
   git push github feat/v2.2-consolidate
   GIT_SSH_COMMAND='sshpass -p "Gumbo@Kona1b" ssh -o PubkeyAuthentication=no' \
     git push origin feat/v2.2-consolidate
   ```

2. Tag `v2.2.0` (drop the rc suffix) at the same commit and push the tag.

3. Merge `feat/v2.2-consolidate` → `main` (cherry-pick or fast-forward
   per repo convention).

If `exit 1` (gate failure):

1. Inspect `reports/v2.2-cross-runtime-pilot.json` — `scores.<runtime>.failures`
   lists which scenarios routed wrong, with `expected_tools` vs
   `invoked_tools`.
2. Iterate on `SKILL.toml` description copy (or, for Hermes, file an
   upstream fix request) before re-running.
3. Stay on `v2.2.0-rc2` until the pilot passes.

If `exit 2` (no runtime reachable):

The script can't reach any of the agent endpoints. Re-check the
pre-flight checklist; the agents need to be running BEFORE the script
is invoked.

## Quick smoke (no live runtimes)

If you just want to confirm the pilot harness is sane without involving
the live agents, run the unit tests:

```bash
uv run pytest tests/test_cross_runtime_pilot.py -v
```

These tests verify the scenario catalog, scoring helpers, and gate
aggregation without needing the agents up — they're the same tests that
GitLab CI runs.
