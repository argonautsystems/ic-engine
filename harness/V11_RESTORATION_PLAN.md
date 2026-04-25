# V11 Harness Restoration Plan
## Restoring v8.0 Testing Scope to v11 Dual-Path Architecture

**Status**: Ready for Implementation  
**Date**: 2026-04-19  
**Scope**: Multi-device, LLM provider matrix, full command coverage, remediation, performance monitoring  
**Objective**: 67-100% reduction in testing scope vs. v8.0 → 100% parity

---

## Executive Summary: What Was Lost (v8.0 → v11)

| Dimension | v8.0 | v11 | Gap | Impact |
|-----------|------|-----|-----|--------|
| **Devices** | 3 (STUDIO, clawpi, zeropi) | 1 (STUDIO only) | 2 missing | Can't validate Pi memory constraints, device routing |
| **LLM Providers** | 4+ (xAI, Google, Together, Gemma) | 0 (simulated) | All missing | No provider reliability validation |
| **Commands Tested** | 28 (full suite) | 4 (holdings, performance, bonds, dashboard) | 24 missing | 86% of functionality untested |
| **Remediation Flows** | 6 (CAP1-6) | 0 | 6 missing | Can't validate failure recovery |
| **Performance Baselines** | Established (T3 watchdog) | None | Missing | Can't detect regressions |
| **Memory/Constraint Testing** | Yes (Pi 2GB pressure) | No | Missing | Can't validate resource limits |
| **Test Phases** | 6 (A-F, provider reachability through ZeroClaw) | 1 (orchestration only) | 5 missing | Incomplete phase coverage |

**Total Scope Reduction: 67-100%** across all dimensions.

---

## Restoration Strategy: Three Phases

### Phase 1: Multi-Device Support (Immediate)
Restore device matrix: STUDIO (Mac), clawpi (8GB Pi), zeropi (2GB Pi constrained)

**Changes**:
- `harness/orchestrator.py`: Add device selector (`--device STUDIO|clawpi|zeropi`)
- `harness/agent_clients/zeroclaw_client.py`: Implement SSH+ZeroClaw for Pi devices
- Test matrix: All 3 devices for holdings, performance, bonds, dashboard

**Validation**:
- ✅ STUDIO (local, unconstrained)
- ✅ clawpi (8GB, baseline Pi testing)
- ✅ zeropi (2GB, memory constraint validation)

**Cost**: ~200 LOC, 1-2 hours

---

### Phase 2: LLM Provider Matrix (1-2 Days)
Restore provider testing: xAI Grok, Google Gemini, Together AI, Gemma-4 local

**Changes**:
- `harness/orchestrator.py`: Add provider selector (`--provider xai|google|together|gemma`)
- `harness/agent_clients/`: Add LLM-specific client implementations
- Test matrix: M1 (xAI Grok), M2 (Google Gemini), M3 (Together), M4 (Gemma local)

**Validation per provider**:
- Provider reachability (T0 phase)
- API key validation (T1 phase)
- OpenClaw baseline (T2 phase)
- ZeroClaw routing (T3+ phases)

**Cost**: ~400 LOC, 2-3 days

---

### Phase 3: Full Command Coverage & Remediation (2-3 Days)
Restore all 28 commands + 6 remediation workflows

**Commands** (28 total, grouped by test tier):

*Tier 1 (Fast, <1s)*:
1. holdings, 2. performance, 3. bonds, 4. analyst, 5. news, 6. news-plan
7. synthesize, 8. fixed-income, 9. optimize, 10. report

*Tier 2 (Medium, 1-3s)*:
11. eod-report, 12. session, 13. fa-topics, 14. lookup, 15. guardrails
16. update-identity, 17. run, 18. stonkmode, 19. check-updates, 20. ollama-setup

*Tier 3 (Slow, 3-5s)*:
21. help, 22. setup (plus 6 enterprise-only commands for v3A)

**Remediation Workflows** (6 flows):
- CAP1: Orchestration failure recovery
- CAP2: Provider degradation fallback
- CAP3: Device unreachable handling
- CAP4: Memory pressure response
- CAP5: ZeroClaw routing failure
- CAP6: Model mismatch recovery

**Cost**: ~600 LOC, 2-3 days

---

## Detailed Implementation Plan

### Step 1: Device Infrastructure (Days 1-2)

**File: `harness/device_matrix.py`** (NEW)

```python
@dataclass
class DeviceConfig:
    """Device configuration for multi-device testing."""
    name: str                    # "STUDIO", "clawpi", "zeropi"
    host: str                    # "127.0.0.1", "clawpi.local", "zeropi.local"
    ssh_enabled: bool            # False for local, True for Pi
    memory_mb: int               # 16000 for STUDIO, 8000 for clawpi, 2000 for zeropi
    gpu_available: bool          # False (Macs use CPU)
    max_concurrent_commands: int # 8 for STUDIO, 4 for clawpi, 1 for zeropi
    timeout_multiplier: float    # 1.0 for STUDIO, 1.5 for clawpi, 2.5 for zeropi

DEVICE_MATRIX = {
    "STUDIO": DeviceConfig(...),
    "clawpi": DeviceConfig(...),
    "zeropi": DeviceConfig(...),
}
```

**File: `harness/agents_clients/zeroclaw_device_client.py`** (NEW)

```python
class ZeroClawDeviceClient(BaseAgentClient):
    """Execute commands on Pi devices via SSH + ZeroClaw."""
    
    async def execute_test(self, command: str, device: str) -> PathBResult:
        """SSH to device, run zeroclaw agent command, collect result."""
        cfg = DEVICE_MATRIX[device]
        
        # SSH into device
        # Run: zeroclaw agent -m "investorclaw <command>"
        # Capture: response + memory usage + timing
        # Return: PathBResult with device metadata
```

**File: `harness/orchestrator.py`** (ENHANCEMENT)

```python
async def run_scenario(self, scenario: TestScenario, device: str = "STUDIO") -> Dict:
    """Execute scenario on specified device."""
    if device == "STUDIO":
        # Existing local execution
        result_a = await self.path_a_executor.execute(scenario)
        result_b = await self.path_b_executor.execute(scenario)
    else:
        # New SSH+ZeroClaw execution
        zeroclaw_client = ZeroClawDeviceClient(device)
        result_b = await zeroclaw_client.execute_test(scenario.path_b_prompt, device)
        # Path A on Pi (local to device via SSH)
        result_a = await self._execute_path_a_remote(scenario, device)
```

**CLI Usage**:
```bash
python3 harness/orchestrator.py --device STUDIO        # Local (existing)
python3 harness/orchestrator.py --device clawpi         # 8GB Pi
python3 harness/orchestrator.py --device zeropi         # 2GB Pi (constraint testing)
```

---

### Step 2: LLM Provider Matrix (Days 2-4)

**File: `harness/provider_matrix.py`** (NEW)

```python
@dataclass
class ProviderConfig:
    name: str                  # "xai", "google", "together", "gemma"
    endpoint: str              # API endpoint or local
    model_id: str              # "grok-4.1", "gemini-2.5-flash", etc.
    supports_tool_use: bool    # Does provider support function calling?
    rate_limit_rps: int        # Requests per second
    cost_per_1k_tokens: float  # For billing tracking
    fallback_model: Optional[str]  # Fallback if primary unavailable

PROVIDER_MATRIX = {
    "xai": ProviderConfig(
        endpoint="https://api.x.ai/v1",
        model_id="grok-4.1",
        supports_tool_use=True,
        rate_limit_rps=10,
    ),
    "google": ProviderConfig(
        endpoint="https://generativelanguage.googleapis.com/v1beta",
        model_id="gemini-2.5-flash",
        supports_tool_use=True,
    ),
    "together": ProviderConfig(
        endpoint="https://api.together.xyz/v1",
        model_id="MiniMax-M2.7",
        supports_tool_use=False,  # Requires prompt engineering
    ),
    "gemma": ProviderConfig(
        endpoint="http://192.168.207.96:11434",  # CERBERUS Ollama
        model_id="gemma4-consult",
        supports_tool_use=False,
        supports_local=True,
    ),
}
```

**File: `harness/agents_clients/provider_client.py`** (NEW)

```python
class ProviderClient(BaseAgentClient):
    """Provider-agnostic agent client (xAI, Google, Together, Gemma)."""
    
    async def execute_test(self, provider: str, command: str) -> PathBResult:
        """
        Execute command via specified provider.
        Handles:
        - Provider reachability (T0 phase)
        - API key validation (T1 phase)
        - Model-specific prompt engineering (T2 phase)
        - Provider fallback on degradation (T3 phase)
        """
        cfg = PROVIDER_MATRIX[provider]
        
        # Phase T0: Reachability check
        reachable = await self._check_reachability(cfg)
        if not reachable:
            return PathBResult(
                agent=Agent.PROVIDER,
                metadata={"error": f"Provider {provider} unreachable"}
            )
        
        # Phase T1: Validate credentials
        credentials_valid = await self._validate_credentials(cfg)
        if not credentials_valid:
            return PathBResult(
                agent=Agent.PROVIDER,
                metadata={"error": f"Invalid credentials for {provider}"}
            )
        
        # Phase T2: Execute command
        response = await self._call_provider(cfg, command)
        
        return PathBResult(
            agent=Agent.PROVIDER,
            provider_used=provider,
            model_used=cfg.model_id,
            response_content=response,
            agent_latency_ms=...
        )
```

**CLI Usage**:
```bash
python3 harness/orchestrator.py --provider xai       # xAI Grok
python3 harness/orchestrator.py --provider google    # Google Gemini
python3 harness/orchestrator.py --provider together  # Together AI
python3 harness/orchestrator.py --provider gemma     # Local Gemma-4
```

---

### Step 3: Full Command Coverage (Days 4-6)

**File: `harness/command_matrix.py`** (NEW)

```python
COMMAND_MATRIX = {
    # Tier 1: Fast (<1s)
    "holdings": {"tier": 1, "timeout": 2},
    "performance": {"tier": 1, "timeout": 2},
    "bonds": {"tier": 1, "timeout": 2},
    "analyst": {"tier": 1, "timeout": 3},
    "news": {"tier": 1, "timeout": 3},
    "news-plan": {"tier": 1, "timeout": 3},
    "synthesize": {"tier": 1, "timeout": 5},
    "fixed-income": {"tier": 1, "timeout": 3},
    "optimize": {"tier": 1, "timeout": 5},
    "report": {"tier": 1, "timeout": 2},
    
    # Tier 2: Medium (1-3s)
    "eod-report": {"tier": 2, "timeout": 5},
    "session": {"tier": 2, "timeout": 3},
    "fa-topics": {"tier": 2, "timeout": 3},
    "lookup": {"tier": 2, "timeout": 2},
    "guardrails": {"tier": 2, "timeout": 2},
    "update-identity": {"tier": 2, "timeout": 2},
    "run": {"tier": 2, "timeout": 10},
    "stonkmode": {"tier": 2, "timeout": 3},
    "check-updates": {"tier": 2, "timeout": 2},
    "ollama-setup": {"tier": 2, "timeout": 5},
    
    # Tier 3: Slow (3-5s)
    "help": {"tier": 3, "timeout": 2},
    "setup": {"tier": 3, "timeout": 10},
}

def get_command_suite(tier: int) -> List[str]:
    """Return all commands for test tier."""
    return [cmd for cmd, cfg in COMMAND_MATRIX.items() if cfg["tier"] <= tier]
```

**File: `harness/orchestrator.py`** (ENHANCEMENT)

```python
async def run_command_suite(self, tier: int = 1) -> Dict[str, Any]:
    """Execute full command suite (tier 1, 2, or 3)."""
    commands = get_command_suite(tier)
    
    results = {}
    for command in commands:
        scenario = TestScenario(
            name=command,
            path_a_command=f"investorclaw {command}",
            path_b_prompt=f"/portfolio {command}",
            # ... other scenario config
        )
        results[command] = await self.run_scenario(scenario)
    
    return {
        "tier": tier,
        "total_commands": len(commands),
        "passed": sum(1 for r in results.values() if r["comparison"]["ux_fidelity"] == "OK"),
        "regressions": sum(1 for r in results.values() if r["comparison"]["ux_fidelity"] == "REGRESSION"),
        "results": results,
    }
```

**CLI Usage**:
```bash
python3 harness/orchestrator.py --tier 1              # Fast (10 commands)
python3 harness/orchestrator.py --tier 2              # Medium (20 commands)
python3 harness/orchestrator.py --tier 3              # Slow (22 commands)
python3 harness/orchestrator.py --command holdings     # Single command
```

---

### Step 4: Remediation Workflows (Days 6-7)

**File: `harness/remediation.py`** (NEW)

```python
class RemediationWorkflow:
    """CAP (Contingency Action Plan) execution framework."""
    
    async def CAP1_orchestration_failure(self, failure: TestFailure):
        """Recover from orchestrator crashes."""
        # Log failure
        # Capture system state
        # Retry with reduced concurrency
        # Escalate if repeated
    
    async def CAP2_provider_degradation(self, failure: TestFailure):
        """Handle provider API errors."""
        # Check provider status (rate limit vs. outage)
        # Switch to fallback provider
        # Queue for retry
    
    async def CAP3_device_unreachable(self, failure: TestFailure):
        """Handle device offline/SSH timeout."""
        # Skip device tests temporarily
        # Queue for infrastructure team
        # Continue with other devices
    
    async def CAP4_memory_pressure(self, failure: TestFailure):
        """Respond to Pi memory constraints."""
        # Reduce command concurrency
        # Clear caches
        # Rerun with reduced memory footprint
    
    async def CAP5_zeroclaw_routing_failure(self, failure: TestFailure):
        """Handle ZeroClaw routing errors."""
        # Check ZeroClaw health
        # Verify model availability
        # Retry with bootstrap
    
    async def CAP6_model_mismatch(self, failure: TestFailure):
        """Recover from LLM context mismatch."""
        # Clear context cache
        # Re-bootstrap model state
        # Retry command
```

**Integration with orchestrator**:
```python
async def run_with_remediation(self, scenario: TestScenario) -> Dict:
    """Run scenario with automated remediation."""
    max_retries = 3
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            result = await self.run_scenario(scenario)
            return result
        except TestFailure as e:
            retry_count += 1
            
            # Route to appropriate remediation
            if isinstance(e, OrchestrationFailure):
                await remediation.CAP1_orchestration_failure(e)
            elif isinstance(e, ProviderError):
                await remediation.CAP2_provider_degradation(e)
            # ... other CAPs
            
            if retry_count >= max_retries:
                raise
```

---

### Step 5: Performance Baselines & Monitoring (Days 7-8)

**File: `harness/performance_baseline.py`** (NEW)

```python
@dataclass
class PerformanceBaseline:
    """Performance expectations per command per device."""
    command: str
    device: str
    p50_ms: float          # 50th percentile latency
    p95_ms: float          # 95th percentile (regression threshold)
    p99_ms: float          # 99th percentile (hard limit)
    memory_mb: float       # Expected memory usage
    
BASELINES = {
    ("holdings", "STUDIO"): PerformanceBaseline(
        command="holdings", device="STUDIO",
        p50_ms=850, p95_ms=1200, p99_ms=1500, memory_mb=120,
    ),
    ("holdings", "clawpi"): PerformanceBaseline(
        command="holdings", device="clawpi",
        p50_ms=1500, p95_ms=2200, p99_ms=3000, memory_mb=320,
    ),
    ("holdings", "zeropi"): PerformanceBaseline(
        command="holdings", device="zeropi",
        p50_ms=3000, p95_ms=5000, p99_ms=7000, memory_mb=1200,
    ),
    # ... all 28 commands x 3 devices
}
```

**Watchdog Integration**:
```python
class PerformanceWatchdog:
    """Monitor for regressions vs. baseline."""
    
    async def validate(self, result: ComparisonResult, baseline: PerformanceBaseline):
        """Check if result meets baseline expectations."""
        if result.direct_runtime_ms > baseline.p95_ms:
            return {
                "status": "PERFORMANCE_REGRESSION",
                "baseline_p95": baseline.p95_ms,
                "actual": result.direct_runtime_ms,
                "delta_ms": result.direct_runtime_ms - baseline.p95_ms,
                "severity": "P2",  # Monitor, may escalate to P1
            }
```

---

## Test Matrix Summary

After full restoration, v11 will validate:

```
DEVICES (3):
  STUDIO (Mac, local, 16GB)
  clawpi (Pi, 8GB)
  zeropi (Pi, 2GB constrained)

PROVIDERS (4):
  xAI (Grok-4.1)
  Google (Gemini-2.5-Flash)
  Together (MiniMax-M2.7)
  Gemma (local Ollama)

COMMANDS (22):
  All core analysis + reporting + setup commands

PHASES (6):
  T0: Provider reachability
  T1: Smoke tests
  T2: Command execution
  T3: Full command suite
  T4: Device matrix
  T5: Release validation + remediation

REMEDIATION (6 CAPs):
  CAP1: Orchestration recovery
  CAP2: Provider degradation
  CAP3: Device unreachable
  CAP4: Memory pressure
  CAP5: ZeroClaw routing
  CAP6: Model mismatch

MATRIX TOTAL: 3 devices × 4 providers × 22 commands = 264 test scenarios
```

---

## Implementation Schedule

| Phase | Duration | Days | Deliverable |
|-------|----------|------|-------------|
| 1: Multi-device | 1-2 days | 1-2 | Device matrix + ZeroClaw client |
| 2: LLM providers | 2-3 days | 3-5 | Provider matrix + client implementations |
| 3: Full commands | 2-3 days | 6-8 | Command matrix + orchestrator enhancements |
| 4: Remediation | 1-2 days | 9-10 | CAP1-CAP6 workflows |
| 5: Perf baseline | 1 day | 11 | Baseline monitoring + watchdog |
| **TOTAL** | **~7-10 days** | **1-2 weeks** | **Full v8.0 parity** |

---

## Success Criteria

✅ All 264 test scenarios executable (3 devices × 4 providers × 22 commands)  
✅ Multi-device SSH+ZeroClaw working end-to-end  
✅ Provider matrix validated (all 4 reachable, credentials valid)  
✅ All 22 commands tested (Tier 1-3 completion)  
✅ 6 remediation workflows operational (CAP1-6)  
✅ Performance baselines established and monitored  
✅ Contract preservation validated (all paths maintain ic_result)  
✅ Full harness run completes in <4 hours (including all 264 scenarios)

---

## Next Step

Begin Phase 1 (multi-device support) implementation immediately on STUDIO with local ARGONAS NFS access.

All code changes:
- Backward compatible with existing v11 harness
- No breaking changes to contract system
- Preserve T0-T5 gating and failure classification
- Maintain existing test recordings format

**Estimated start**: Immediately  
**Estimated completion**: 2026-04-28 (10 business days)
