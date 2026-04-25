# Dual-Path Test Harness Design
## Capturing Real End-User Experience via Agents

**Version**: 1.0
**Date**: 2026-04-19
**Paradigm**: Two execution paths + unified metrics

---

## Problem with Simulation-Based Testing

Traditional test harnesses:
- Mock agent responses
- Test CLI/API in isolation
- Simulate model outputs
- **Miss**: Real agent interpretation, actual UX, system integration failures

**Solution**: Dual-path harness that:
- **Path A**: Direct CLI/API (fast validation, no agent)
- **Path B**: Agent-based (OpenClaw/ZeroClaw/Hermes) (real UX, real models)

Both paths test the same scenarios, capture different signals.

---

## Architecture: Dual-Path Harness

```
┌─────────────────────────────────────────────────────────────┐
│         UNIFIED HARNESS ORCHESTRATOR                        │
│  (harness/orchestrator.py)                                  │
└─────────────────────────────────────────────────────────────┘
        │
        ├─────────────────────┬──────────────────────┐
        │                     │                      │
    PATH A (CLI/API)     PATH B (AGENTS)      PATH C (COMPARISON)
    ────────────────     ───────────────      ──────────────────
        │                     │                      │
        ├─ Direct CLI         ├─ OpenClaw           ├─ Diff outputs
        ├─ Direct API         ├─ ZeroClaw (SSH)     ├─ UX divergence
        ├─ Holdings JSON      ├─ Hermes (if exists) ├─ Timing diff
        ├─ Exit codes         ├─ Agent prompts      ├─ Error handling
        │                     ├─ Agent responses    └─ Regression detection
        └─ METRICS:          │                         
          - Runtime           ├─ METRICS:
          - Output shape      │  - Agent latency
          - Data quality      │  - Model choice
          - Errors            │  - Narration fidelity
                              │  - Real user UX
                              │  - Agent state transitions
                              │  - Model performance
                              └─ RECORDINGS:
                                 - Prompts sent
                                 - Agent responses
                                 - Full conversation
                                 - Token usage
```

---

## Path A: Direct CLI/API Testing

**When**: Fast iteration, smoke tests, validation gates
**How**: Traditional CLI/API calls

```python
# Example: Direct API path
async def test_path_a_direct():
    # Direct command execution
    result = await holdings_stage.execute(context)
    
    # Metrics captured
    return {
        "path": "A",
        "stage": "holdings",
        "runtime_ms": 234,
        "exit_code": 0,
        "output_shape": {"positions": 28, "total_value": 100000000},
        "errors": None,
        "provider_used": "yfinance",
    }
```

**Outputs**:
- JSON result envelopes
- Exit codes
- Runtime metrics
- Data shape validation
- Provider routing

---

## Path B: Agent-Based Testing (Real UX)

**When**: Full integration, end-user experience, model validation
**How**: Send prompts directly to agents, capture responses

### B1: OpenClaw Testing
```python
async def test_path_b_openclaw():
    """Send prompt to OpenClaw agent, capture real UX"""
    
    prompt = "/portfolio holdings --accounts sample_portfolio.csv"
    
    # Direct agent call (WebSocket)
    response = await openclaw_agent.send_message(
        session_id="test-session-{uuid}",
        message=prompt,
        timeout=60,
    )
    
    # Capture what user actually sees
    return {
        "path": "B",
        "agent": "openclaw",
        "prompt": prompt,
        "response": response["content"],  # Raw agent output
        "model_used": response["metadata"]["model"],
        "agent_latency_ms": response["metadata"]["latency"],
        "narration_present": "stonkmode" in response["content"],
        "metadata": response["metadata"],  # Full agent metadata
        "full_conversation": response["conversation"],  # Complete history
    }
```

### B2: ZeroClaw Testing (Pi Devices)
```python
async def test_path_b_zeroclaw():
    """Send prompt to ZeroClaw on remote Pi via SSH"""
    
    # SSH tunnel to Pi
    pi_agent = SSHAgent(
        host="192.168.207.56",  # zeropi
        username="jasonperlow",
    )
    
    prompt = "/portfolio holdings --accounts"
    
    # Execute on remote device (captures real Pi UX)
    response = await pi_agent.send_message(
        message=prompt,
        timeout=30,
    )
    
    return {
        "path": "B",
        "agent": "zeroclaw",
        "device": "zeropi",
        "device_specs": {"memory": "2GB", "cpu": "Pi4 ARM"},
        "prompt": prompt,
        "response": response["content"],
        "memory_usage_mb": response["metadata"]["rss_mb"],
        "model_used": response["metadata"]["model"],
        "provider_routing": response["metadata"]["provider"],  # yfinance only on Pi
        "agent_latency_ms": response["metadata"]["latency"],
    }
```

### B3: Hermes Testing (If Multi-Agent System)
```python
async def test_path_b_hermes():
    """Send prompt to Hermes agent orchestrator"""
    
    # Hermes routes to best agent based on task
    response = await hermes_agent.send_message(
        message="/portfolio synthesize",
        timeout=45,
    )
    
    return {
        "path": "B",
        "agent": "hermes",
        "routing_decision": response["metadata"]["routed_to"],  # Which agent handled it
        "prompt": "/portfolio synthesize",
        "response": response["content"],
        "agent_chain": response["metadata"]["agent_chain"],  # Full chain of agents
    }
```

---

## Path C: Comparison Analysis

Compare Path A vs Path B to detect UX divergence:

```python
async def test_path_c_comparison():
    """Compare direct CLI (A) vs agent-based (B) outputs"""
    
    result_a = await test_path_a_direct()
    result_b = await test_path_b_openclaw()
    
    # Both should solve same problem
    comparison = {
        "path": "C",
        "test_id": "test-holdings-001",
        
        # Output comparison
        "outputs_match": result_a["output_shape"] == extract_shape(result_b["response"]),
        "shape_a": result_a["output_shape"],
        "shape_b": extract_shape(result_b["response"]),
        
        # UX comparison
        "narration_in_agent": "stonkmode" in result_b["response"],
        "narration_in_direct": False,  # Direct path has no narration
        
        # Timing comparison
        "direct_runtime_ms": result_a["runtime_ms"],
        "agent_latency_ms": result_b["agent_latency_ms"],
        "agent_overhead_ms": result_b["agent_latency_ms"] - result_a["runtime_ms"],
        
        # Error comparison
        "both_successful": result_a["exit_code"] == 0 and result_b["exit_code"] == 0,
        "error_divergence": (result_a["errors"] is None) != (result_b["errors"] is None),
        
        # Verdict
        "ux_fidelity": "OK" if not divergence else "REGRESSION",
    }
    
    return comparison
```

---

## Test Scenarios (Both Paths)

Each scenario runs in both Path A (direct) and Path B (agents):

### Scenario 1: Holdings Analysis
```yaml
input: sample_portfolio.csv (28 positions, $100M)
path_a: investorclaw holdings --portfolio sample_portfolio.csv
path_b: "/portfolio holdings" → OpenClaw agent

captures:
  - Direct: runtime, data quality, provider routing
  - Agent: agent interpretation, narration, model choice, real latency
  - Compare: output consistency, UX divergence
```

### Scenario 2: Performance Analysis
```yaml
input: sample_portfolio.csv
path_a: investorclaw performance --portfolio sample_portfolio.csv
path_b: "/portfolio performance" → OpenClaw agent

captures:
  - Direct: metric calculations, timing breakdown
  - Agent: model-generated insights, stonkmode narration quality
  - Compare: analysis consistency, narrative accuracy
```

### Scenario 3: Synthesis & Narration
```yaml
input: sample_portfolio.csv
path_a: investorclaw synthesize --portfolio sample_portfolio.csv
path_b: "/portfolio synthesize" → OpenClaw agent

captures:
  - Direct: synthesis logic, fallback handling
  - Agent: narration fidelity, model quality, user-facing text
  - Compare: consistency in recommendations, narrative quality
```

### Scenario 4: Degradation (Zero Keys)
```yaml
input: sample_portfolio.csv, K0 (no API keys)
path_a: investorclaw holdings (yfinance-only fallback)
path_b: "/portfolio holdings" → OpenClaw with K0 config

captures:
  - Direct: fallback logic, graceful degradation
  - Agent: agent understanding of constraints, user-friendly error messages
  - Compare: error messaging consistency, graceful vs crash
```

### Scenario 5: Device Constraints (Pi 2GB)
```yaml
input: sample_portfolio.csv
device: zeropi (2GB memory)
path_a: SSH to Pi, run investorclaw holdings
path_b: Send "/portfolio holdings" to ZeroClaw on Pi

captures:
  - Direct: memory usage, feasibility
  - Agent: real Pi experience, model choice, latency from device
  - Compare: performance on constrained device, user experience
```

---

## Execution Framework

### Test Harness Orchestrator
```python
class DualPathHarness:
    """Runs scenarios in both Path A (direct) and Path B (agent)"""
    
    async def run_scenario(self, scenario: Scenario):
        """Execute scenario in both paths, compare results"""
        
        # Path A: Direct CLI/API
        result_a = await self.run_path_a_direct(scenario)
        
        # Path B: Agent-based (OpenClaw/ZeroClaw)
        result_b = await self.run_path_b_agent(scenario)
        
        # Path C: Comparison
        comparison = await self.run_path_c_comparison(result_a, result_b)
        
        return {
            "scenario": scenario.name,
            "path_a": result_a,
            "path_b": result_b,
            "comparison": comparison,
            "ux_fidelity": comparison["ux_fidelity"],
            "timestamp": datetime.now().isoformat(),
        }
    
    async def run_path_a_direct(self, scenario):
        """Execute via direct CLI/API (no agent)"""
        # Subprocess call, capture output
        pass
    
    async def run_path_b_agent(self, scenario):
        """Execute via agent (OpenClaw/ZeroClaw/Hermes)"""
        # WebSocket or SSH to agent, capture response
        pass
    
    async def run_path_c_comparison(self, result_a, result_b):
        """Compare outputs, detect UX divergence"""
        # Diff, validate consistency, check narration quality
        pass
```

---

## Metrics Captured (Both Paths)

### Path A (Direct)
- Runtime (ms)
- Output shape (JSON structure)
- Exit code
- Errors/exceptions
- Provider routing
- Data quality metrics

### Path B (Agent)
- **Agent latency** (ms)
- **Model used** (M1/M2/M3/etc)
- **Narration fidelity** (stonkmode quality)
- **Agent state** (conversation history)
- **Token usage** (prompt + completion)
- **User-facing output** (what user actually sees)
- **Agent interpretation** (how agent understood request)
- **UX quality** (formatting, clarity, helpfulness)

### Path C (Comparison)
- Output consistency (data matches)
- UX divergence (differences in user experience)
- Timing difference (agent overhead)
- Error divergence (different error handling)
- Regression detection (was this test passing before?)

---

## Recording & Playback

Each test automatically records:

```json
{
  "test_id": "test-holdings-001-openclaw",
  "timestamp": "2026-04-19T12:34:56Z",
  "scenario": "Holdings Analysis",
  "path": "B",
  "agent": "openclaw",
  
  "prompt_sent": "/portfolio holdings --accounts sample_portfolio.csv",
  
  "agent_response": {
    "content": "Your portfolio contains 28 positions...",
    "metadata": {
      "model": "google/gemini-2.5-flash",
      "latency_ms": 2341,
      "tokens_prompt": 1234,
      "tokens_completion": 567,
    }
  },
  
  "full_conversation": [
    {"role": "user", "content": "/portfolio holdings..."},
    {"role": "assistant", "content": "Your portfolio..."}
  ],
  
  "ux_signals": {
    "narration_present": true,
    "narration_quality": 4.2,  # Out of 5
    "user_clarity": 4.5,
    "accuracy": 5.0,
  }
}
```

Can replay real sessions:
```bash
# Replay user session from recording
harness replay test-holdings-001-openclaw.json
# Shows exactly what user saw, same model responses, same timing
```

---

## Benefits of Dual-Path Harness

| Aspect | Path A (Direct) | Path B (Agent) |
|--------|-----------------|----------------|
| Speed | Fast (ms) | Slower (includes agent) |
| Real UX | ❌ No | ✅ Yes (what users see) |
| Model Quality | ❌ N/A | ✅ Captures |
| Narration | ❌ No | ✅ Real narration |
| Agent Behavior | ❌ No | ✅ Real agent decisions |
| Regression Detection | ✅ Yes | ✅ Yes |
| Debugging | ✅ Direct logic | ✅ Full agent trace |
| User Confidence | Medium | **High** (captured real UX) |

---

## Next Steps

1. **Implement OpenClaw client** - Send prompts via WebSocket
2. **Implement ZeroClaw client** - SSH agent prompts
3. **Define all scenarios** - Map harness v11 test cases to dual-path
4. **Build comparison logic** - Detect UX divergence
5. **Integrate with CI/CD** - Report on both paths
6. **Record sessions** - Build replay library of real user interactions
