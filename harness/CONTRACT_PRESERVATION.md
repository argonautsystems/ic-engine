# Contract Preservation in Dual-Path Harness
## Backward Compatibility & Watchdog Integration

**Version**: 1.0
**Date**: 2026-04-19
**Purpose**: Ensure dual-path harness extends (not replaces) v11 contracts and watchdogs

---

## Existing Contracts to Preserve

### 1. STATIC CONTRACT GATE (T0) - MANDATORY
Must be run FIRST, blocks all other tiers

**Contract**: `harness/contract_check.py`

```python
ENFORCES:
  ✅ Version consistency (openclaw.plugin.json, package.json, pyproject.toml, SKILL.toml, __init__.py)
  ✅ License: Apache 2.0 Dual in all source files
  ✅ Plugin manifest matches dist extension
  ✅ SKILL.toml uses `investorclaw` entry point (not `python3 investorclaw.py`)
  ✅ No developer-local paths (~/Projects/InvestorClaw)
  ✅ No stale v1.0.0 metadata
  ✅ No stale gemma4-consult config
  ✅ No credentials (ghp_, glpat_, api_key=, password=)
  ✅ Required public commands documented (22 minimum)
  ✅ SKILL.md and SKILL.toml in sync

REQUIRED PUBLIC COMMANDS (22):
  setup | holdings | performance | bonds | analyst | news | news-plan | synthesize |
  fixed-income | optimize | report | eod-report | session | fa-topics | lookup |
  guardrails | update-identity | run | stonkmode | check-updates | ollama-setup | help

FAILURE CLASS: CONTRACT_DRIFT_BLOCKER
BLOCKS: All other tiers (T1, T2, T3, T4, T5)
```

### 2. COMPACT OUTPUT CONTRACT (T1+) - MANDATORY
All commands return JSON ic_result envelope

```json
{
  "ic_result": {
    "script": "investorclaw holdings",
    "exit_code": 0,
    "duration_ms": 1234,
    "data": { /* command-specific output */ }
  }
}
```

### 3. FAILURE CLASSIFICATION (WATCHDOG)
Pre-defined failure categories across all tiers:

| Class | Tier | Action | Resolution |
|-------|------|--------|-----------|
| `CONTRACT_DRIFT_BLOCKER` | T0 | Stop (blocks T1+) | Fix source metadata |
| `ENVIRONMENTAL` | Any | Skip/Retry | Infrastructure issue, not code |
| `SKILL_CODE_DEFECT` | T1-T4 | Stop (P0 fix) | Fix source code |
| `PROVIDER_DEGRADATION_FAILURE` | T2-T3 | Stop (P1 fix) | Add fallback message |
| `STONKMODE_STATE_FAILURE` | T3 | Retry (activate state) | Activate ~/.openclaw/stonkmode |
| `INSTALL_BLOCKER` | T2 | Stop (P0 fix) | Fix install workflow |
| `MEMORY_CONSTRAINT_VIOLATION` | T4 (Pi) | Stop (optimization) | Reduce memory usage |
| `PROVIDER_ROUTING_FAILURE` | T4 (Pi) | Debug | Investigate ZeroClaw routing |
| `SSH_UNREACHABLE` | T4 | Skip/Retry | Device offline |
| `MODEL_MISMATCH` | T3 | Retry (bootstrap) | Re-bootstrap context |

### 4. EXECUTION SCHEDULE (GATING)

**Daily Regression** (T1+T2, ~30 min)
```
TRIGGER: Every commit to main | pre-release | local dev validation
SCOPE: CLI smoke (T1) + install validation (T2)
GATES: Must pass before merge
FAST PATH: ~30 min total
```

**Feature Validation** (T1+T3, ~90 min)
```
TRIGGER: New command or major feature added
SCOPE: Smoke tests + full command + model matrix (M1-M3 only)
GATES: Must pass before feature merge
```

**Full Release Matrix** (T0+T1+T2+T3+T4, ~3 hours)
```
TRIGGER: Before release tag | dependency updates | ZeroClaw validation
SCOPE: All tiers, all 6 model combos, all phases, all devices
GATES: Must pass before release
```

---

## Dual-Path Harness Extension (NOT Replacement)

The new dual-path harness extends v11 by adding:

```
EXISTING CONTRACTS (Preserved)        NEW DUAL-PATH EXTENSIONS
────────────────────────────────      ─────────────────────────
T0: Static contract gate      ───────→ T0: Static + Path A validation
                                       (verify contract in both direct & agent)

T1: CLI smoke tests           ───────→ T1A: Direct CLI
                                       T1B: Agent prompts
                                       T1C: UX fidelity comparison

T2: Install & config          ───────→ T2A: Direct setup
                                       T2B: Agent setup validation
                                       T2C: Agent config switching

T3: Command + model matrix    ───────→ T3A: Direct command
                                       T3B: Agent command + narration
                                       T3C: Model consistency check
                                       (M1-M3, HB1-HB3)

T4: Device matrix             ───────→ T4A: Direct on Pi
                                       T4B: Agent on Pi
                                       T4C: Device UX fidelity

T5: Release candidate         ───────→ T5: All paths pass + UX fidelity OK
```

---

## Contract Validation in Dual-Path

### Contract Checks (Both Paths)

```python
# T0: Static Contract (must pass FIRST)
class ContractValidator:
    def validate(self) -> ContractResult:
        """
        Validates both Path A (direct) and Path B (agents) comply with:
        1. Version consistency
        2. License headers
        3. Plugin manifest
        4. Required commands exposed
        5. No credentials
        """
        return {
            "path_a_contract": self.check_path_a(),  # CLI contract
            "path_b_contract": self.check_path_b(),  # Agent contract
            "status": "PASS" if both_pass else "CONTRACT_DRIFT_BLOCKER",
        }

# T1+: Output Contract (both paths must return ic_result)
class OutputContractValidator:
    def validate_path_a(self, output: str) -> bool:
        """Path A must return JSON ic_result envelope"""
        return self._has_ic_result_envelope(output)
    
    def validate_path_b(self, output: str) -> bool:
        """Path B agent must return JSON ic_result in response"""
        return self._has_ic_result_envelope(output)
```

### UX Fidelity as Contract Extension

```python
# NEW: Path C validates both paths maintain contract consistency
class UXContractValidator:
    """
    Extends contract system to validate end-user experience consistency
    between Path A (direct) and Path B (agent execution)
    """
    
    def validate_fidelity(self, path_a: PathAResult, path_b: PathBResult):
        """
        Contract: Path B must NOT violate outputs from Path A
        
        VIOLATIONS:
        - Output shape divergence → REGRESSION
        - Error handling divergence → REGRESSION
        - Missing provider messages → PROVIDER_DEGRADATION_FAILURE
        - Agent narration incomplete → UX_FIDELITY_FAILURE
        """
        
        violations = []
        
        # Contract: ic_result envelope in both paths
        if not self._has_ic_result(path_a.output):
            violations.append("CONTRACT_VIOLATION: Path A missing ic_result")
        if not self._has_ic_result(path_b.response):
            violations.append("CONTRACT_VIOLATION: Path B missing ic_result")
        
        # Contract: Exit code consistency
        if path_a.exit_code != path_b.exit_code:
            violations.append("CONTRACT_VIOLATION: Exit code divergence")
        
        # Contract: Error message consistency
        if (path_a.has_error) != (path_b.has_error):
            violations.append("PROVIDER_DEGRADATION_FAILURE: Error handling differs")
        
        # Contract: Narration present in agent (where applicable)
        if path_b.agent and not self._has_narration(path_b.response):
            violations.append("UX_FIDELITY_FAILURE: Agent narration missing")
        
        return {
            "status": "PASS" if not violations else "CONTRACT_VIOLATION",
            "violations": violations,
        }
```

---

## Failure Classification in Dual-Path

Dual-path adds new failure classes while preserving existing ones:

```python
WATCHDOG_CLASSIFICATIONS = {
    # PRESERVED (from v11)
    "CONTRACT_DRIFT_BLOCKER": {
        "tier": "T0",
        "severity": "BLOCKER",
        "action": "Stop all tiers",
    },
    "ENVIRONMENTAL": {
        "tier": "Any",
        "severity": "SKIP",
        "action": "Retry or skip (infrastructure issue)",
    },
    "SKILL_CODE_DEFECT": {
        "tier": "T1-T4",
        "severity": "P0",
        "action": "Stop and fix source",
    },
    
    # NEW (dual-path specific)
    "PATH_A_PATH_B_DIVERGENCE": {
        "tier": "T1-T4",
        "severity": "P1",
        "action": "Investigate UX difference, document or fix",
        "details": "Path A and Path B produce different outputs",
    },
    "NARRATION_QUALITY_FAILURE": {
        "tier": "T3",
        "severity": "P2",
        "action": "Review model response, may need model update",
        "details": "Agent narration quality below threshold",
    },
    "AGENT_OVERHEAD_EXCESSIVE": {
        "tier": "T3",
        "severity": "P2",
        "action": "Optimize agent latency",
        "details": "Path B latency >5s for command (agent adds >3s overhead)",
    },
}
```

---

## Backward Compatibility Guarantees

The dual-path harness MUST:

1. **Preserve T0 Contract Gate**
   - Run `harness/contract_check.py` FIRST (before any dual-path tests)
   - Block all tiers on CONTRACT_DRIFT_BLOCKER
   - Report contract status for both Path A and Path B

2. **Preserve T1-T4 Output Contracts**
   - All outputs must contain ic_result envelope
   - Exit codes must be consistent
   - Error messages must be predictable

3. **Preserve Failure Classification**
   - Use same failure classes (ENVIRONMENTAL, SKILL_CODE_DEFECT, etc.)
   - Add new classes only for dual-path specific issues
   - Maintain same severity/action mappings

4. **Preserve Execution Schedule**
   - Daily regression (T1+T2) stays ~30 min
   - Feature validation (T1+T3) stays ~90 min
   - Full matrix (T0+T1+T2+T3+T4) stays ~3 hours
   - Dual-path overhead <10% per tier

5. **Preserve Gating Behavior**
   - T0 blocks T1+ (contract gate)
   - T1 blocks feature merge
   - T2 blocks feature merge
   - T3 blocks feature merge
   - T4 blocks release
   - T5 = all pass + UX fidelity OK

---

## Implementation Checklist

When implementing dual-path harness:

- [ ] T0 runs first, validates contract in both paths
- [ ] T1-T4 return ic_result envelope in both paths
- [ ] Failure classification uses existing + new classes
- [ ] Execution times stay within schedule
- [ ] Gating behavior preserved (T0 blocks, etc.)
- [ ] Backward compatible with v11 test results
- [ ] Can run Path A and Path B independently
- [ ] Can compare Path A vs Path B results
- [ ] Recordings compatible with v11 format
- [ ] Dashboard metrics compatible with existing

---

## Example: T1 Daily Regression (Preserved + Enhanced)

**V11 Specification**:
```
T1: CLI smoke tests
  investorclaw holdings --portfolio sample.csv → exit_code=0
  investorclaw performance --portfolio sample.csv → exit_code=0
  ...
```

**Dual-Path Enhancement**:
```
T1A: Direct CLI (preserved)
  investorclaw holdings --portfolio sample.csv → exit_code=0

T1B: Agent execution (new)
  Agent("/portfolio holdings") → exit_code=0 + narration

T1C: Comparison (new)
  Validate outputs match, narration present in agent only

CONTRACT: All outputs have ic_result envelope
GATING: Must pass before merge
WATCHDOG: CONTRACT_DRIFT_BLOCKER if envelope missing
```

Both paths preserve the original contract while adding real UX validation.
