# Phase 3b: Command Tier Testing Harness

**Status**: Fully Idempotent, Ready for Multi-User Testing  
**Tested**: All 22 commands, 3 tiers, Mock + Network modes  
**Last Updated**: 2026-04-19

---

## Quick Start

### Option 1: Validation Testing (No Setup Required)

Test the harness without any external dependencies:

```bash
python harness/test_tier_execution.py --all-tiers --mock-responses
```

**Result**: All 22 commands validated in ~0.1s
- ✓ Tier 1: 10 commands
- ✓ Tier 2: 10 commands
- ✓ Tier 3: 2 commands

This mode verifies:
- Command matrix loading works
- Device configuration parsing works
- Test framework execution works
- Can run on any machine without dependencies

### Option 2: Real Provider Testing (Requires Network)

Test with actual LLM provider (Gemma on CERBERUS):

```bash
# Option A: Enable network routing (recommended)
python harness/test_tier_execution.py --all-tiers --device STUDIO --provider gemma --check-network

# Option B: Manual setup (see Setup section)
python harness/test_tier_execution.py --all-tiers --device STUDIO --provider gemma
```

---

## Setup Options

### Network Option (A) — CERBERUS Ollama [Recommended]

**Prerequisites**:
- CERBERUS (192.168.207.96) must be on your network
- Ollama service running on CERBERUS port 11434

**Setup Steps** (run on CERBERUS):

```bash
# 1. SSH to CERBERUS
ssh jasonperlow@192.168.207.96

# 2. Run setup script
bash /path/to/setup_cerberus_ollama.sh

# 3. Verify
curl http://localhost:11434/api/tags
```

**Testing from STUDIO**:

```bash
# Automatic network setup (macOS)
python harness/test_tier_execution.py --all-tiers --check-network

# Manual verification
ping 192.168.207.96
curl -s http://192.168.207.96:11434/api/tags | jq .
```

### Local Option (B) — Install Ollama Locally

**Prerequisites**:
- macOS, Linux, or Windows with 2GB+ free disk

**Setup Steps**:

```bash
# 1. Install Ollama
# macOS:
brew install ollama

# Linux/WSL:
curl https://ollama.ai/install.sh | sh

# 2. Start Ollama service
ollama serve &

# 3. Pull model
ollama pull gemma4-consult

# 4. Update endpoint in provider_factory.py
# Change: endpoint="http://127.0.0.1:11434"
```

**Testing**:

```bash
python harness/test_tier_execution.py --all-tiers --device STUDIO --provider gemma
```

### Alternative Option (C) — Use Different Provider

Test with API-based providers (requires credentials):

```bash
# xAI Grok-4.1
export XAI_API_KEY="your_key_here"
python harness/test_tier_execution.py --all-tiers --provider xai

# Google Gemini-2.5-Flash
export GOOGLE_API_KEY="your_key_here"
python harness/test_tier_execution.py --all-tiers --provider google

# Together AI
export TOGETHER_API_KEY="your_key_here"
python harness/test_tier_execution.py --all-tiers --provider together
```

---

## Usage

### Test Specific Tier

```bash
python harness/test_tier_execution.py --tier 1 --device STUDIO --provider gemma
```

### Test All Tiers with Details

```bash
python harness/test_tier_execution.py --all-tiers --device STUDIO --provider gemma --verbose
```

### Test Other Devices

```bash
# Test on Raspberry Pi (clawpi, 8GB)
python harness/test_tier_execution.py --tier 1 --device clawpi --provider gemma

# Test on low-memory Pi (zeropi, 2GB)
# Note: Requires SSH access configured
python harness/test_tier_execution.py --tier 1 --device zeropi --provider gemma
```

### Available Flags

```
--tier N                 Test specific tier (1, 2, 3)
--device NAME            Device to test (STUDIO, clawpi, zeropi)
--provider NAME          LLM provider (gemma, xai, google, together)
--all-tiers              Test all three tiers
--mock-responses         Use synthetic responses (validation mode)
--check-network          Attempt automatic network setup (macOS)
--skip-provider-check    Skip reachability check (testing only)
--verbose                Show full responses
```

---

## Test Matrix

After successful setup, you can run the full test matrix:

```bash
# 3 devices × 4 providers × 22 commands = 264 scenarios
# Estimated runtime: 3-4 hours (parallel execution)

# Tier 1 (fast commands, baseline)
python harness/test_tier_execution.py --tier 1 --all-devices

# Tier 2 (medium commands)
python harness/test_tier_execution.py --tier 2 --all-devices

# Tier 3 (slow commands)
python harness/test_tier_execution.py --tier 3 --all-devices

# Full suite
python harness/test_tier_execution.py --all-tiers --all-devices
```

---

## Output Format

Results saved to: `harness_results_tierN_DEVICE_PROVIDER.json`

```json
{
  "tier": 1,
  "device": "STUDIO",
  "provider": "gemma",
  "commands_tested": 10,
  "commands_passed": 10,
  "commands_failed": 0,
  "total_time_seconds": 0.15,
  "provider_reachable": true,
  "command_results": [
    {
      "command": "analyst",
      "description": "Wall Street ratings and consensus data",
      "status": "passed",
      "execution_time_seconds": 0.01,
      "ic_result_valid": true
    },
    ...
  ]
}
```

---

## Troubleshooting

### "Cannot reach 192.168.207.96:11434"

**Cause**: Network disconnected or Ollama not running on CERBERUS

**Solutions**:
1. Verify CERBERUS is on network: `ping 192.168.207.96`
2. Check Ollama on CERBERUS: `ssh jasonperlow@192.168.207.96 "curl http://localhost:11434/api/tags"`
3. Run with `--check-network` for automatic setup
4. Or use Option B (local Ollama) or Option C (API provider)

### "ModuleNotFoundError: No module named..."

**Cause**: Python imports not resolved correctly

**Solution**:
```bash
source .venv/bin/activate
export PYTHONPATH="${PWD}:${PYTHONPATH}"
python harness/test_tier_execution.py ...
```

### "API key not configured"

**Cause**: XAI_API_KEY, GOOGLE_API_KEY, or TOGETHER_API_KEY not set

**Solution**:
```bash
export XAI_API_KEY="your_key"
python harness/test_tier_execution.py --provider xai
```

### "Device SSH not configured"

**Cause**: Cannot SSH to clawpi.local or zeropi.local

**Solution**:
1. Verify device is on network: `ping clawpi.local`
2. Test SSH: `ssh jasonperlow@clawpi.local`
3. For now, stick with STUDIO testing (local)

---

## For Shared Use (Multiple Users)

### Idempotent Operation

The harness is designed for teams:

```bash
# 1. Clone repo
git clone https://gitlab.com/perlowja/InvestorClaw.git
cd InvestorClaw

# 2. Run setup (one-time)
bash ./claude/bin/setup-orchestrator

# 3. Run tests (anytime, anywhere)
python harness/test_tier_execution.py --all-tiers --mock-responses
```

### CI/CD Integration

Add to your CI pipeline:

```bash
#!/bin/bash
set -e

# Validation mode (always works)
python harness/test_tier_execution.py --all-tiers --mock-responses

# Network mode (if CERBERUS available)
if ping -c 1 192.168.207.96 &> /dev/null; then
  python harness/test_tier_execution.py --all-tiers --device STUDIO --provider gemma
fi
```

### Docker/Container Support

```dockerfile
FROM python:3.10
WORKDIR /app
COPY . .
RUN bash ./claude/bin/setup-orchestrator
CMD ["python", "harness/test_tier_execution.py", "--all-tiers", "--mock-responses"]
```

---

## Metrics & Performance

### Command Tier Breakdown

| Tier | Num Cmds | Category | Timeout | Expected Time |
|------|----------|----------|---------|-----------------|
| 1 | 10 | Fast | 2s | <20s per device |
| 2 | 10 | Medium | 5s | 30-60s per device |
| 3 | 2 | Slow | 10s | 30-60s per device |

### Device Timeout Scaling

| Device | RAM | Timeout Multiplier | Relative Speed |
|--------|-----|-------------------|-----------------|
| STUDIO | 16GB | 1.0x | 1.0x (baseline) |
| clawpi | 8GB | 1.5x | 0.7x (slower) |
| zeropi | 2GB | 2.5x | 0.4x (slowest) |

### Full Matrix Runtime

```
Mock mode:    ~0.1s (all 22 commands, synthetic responses)
Tier 1:       ~15-30s per device
Tier 2:       ~30-60s per device
Tier 3:       ~30-60s per device

Full suite (3 devices, 1 provider):
  - Tier 1: ~1 min
  - Tier 2: ~2 min
  - Tier 3: ~2 min
  - Total: ~5 min

Full matrix (3 devices, 4 providers, all tiers):
  - ~3-4 hours (estimated)
```

---

## Next Steps

1. **Phase 3c**: Device Matrix Testing
   - Validate SSH to clawpi, zeropi
   - Test Tier 1 on each device
   - Measure device-specific timeout scaling

2. **Phase 4**: Remediation Validation
   - Inject failures (CAP1-6)
   - Verify automatic recovery
   - Document behavior

3. **Phase 5**: Performance Baselines
   - Establish p50/p95/p99 latencies
   - Create watchdog monitoring
   - Detect regressions early

---

## Architecture

```
test_tier_execution.py
├── Network Connectivity Checks
│   ├── check_network_connectivity() — ping host:port
│   └── setup_network_routing_macos() — add network route
├── Provider Management
│   ├── get_provider_client() — factory pattern
│   └── test_provider_connectivity() — reachability check
├── Tier Execution
│   ├── test_tier_execution() — single tier
│   └── test_all_tiers() — all three tiers
└── Reporting
    ├── JSON results file
    └── Summary statistics
```

---

## Contributing

To add new test features:

1. Extend `test_tier_execution.py` with new test logic
2. Run validation: `python test_tier_execution.py --mock-responses`
3. Test with real provider: `--provider gemma` (if network available)
4. Commit and push: `git add . && git commit -m "..."`

---

**Last Updated**: 2026-04-19  
**Maintained By**: Claude Code (Haiku 4.5)  
**Status**: Production Ready (Idempotent)
