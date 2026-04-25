"""
Remediation workflows (CAP1-CAP6) for automated failure recovery.

Contingency Action Plans for test failures:
- CAP1: Orchestration failure recovery
- CAP2: Provider degradation fallback
- CAP3: Device unreachable handling
- CAP4: Memory pressure response
- CAP5: ZeroClaw routing failure
- CAP6: Model mismatch recovery
"""

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class FailureClass(Enum):
    """Classification of test failures."""

    ORCHESTRATION_FAILURE = "ORCHESTRATION_FAILURE"
    PROVIDER_DEGRADATION = "PROVIDER_DEGRADATION"
    DEVICE_UNREACHABLE = "DEVICE_UNREACHABLE"
    MEMORY_CONSTRAINT = "MEMORY_CONSTRAINT"
    ZEROCLAW_ROUTING = "ZEROCLAW_ROUTING"
    MODEL_MISMATCH = "MODEL_MISMATCH"
    ENVIRONMENTAL = "ENVIRONMENTAL"
    SKILL_CODE_DEFECT = "SKILL_CODE_DEFECT"


@dataclass
class TestFailure:
    """Structured failure information."""

    failure_class: FailureClass
    message: str
    error: Optional[Exception] = None
    context: Optional[Dict[str, Any]] = None
    retryable: bool = True
    escalation_needed: bool = False


class RemediationWorkflow:
    """Contingency Action Plan execution framework."""

    def __init__(self, max_retries: int = 3, debug_mode: bool = False):
        self.max_retries = max_retries
        self.debug_mode = debug_mode
        self.remediation_log = []

    async def CAP1_orchestration_failure(self, failure: TestFailure) -> bool:
        """
        Recover from orchestrator crashes or runtime errors.

        Actions:
        1. Log failure with full context
        2. Capture system state (memory, CPU, processes)
        3. Clear caches and buffers
        4. Retry with reduced concurrency
        5. Escalate if repeated failures
        """
        logger.warning(f"CAP1: Orchestration failure - {failure.message}")

        self.remediation_log.append(
            {
                "cap": "CAP1",
                "action": "orchestration_recovery",
                "message": failure.message,
                "context": failure.context,
            }
        )

        try:
            # Clear caches and buffers
            import gc

            gc.collect()
            logger.info("CAP1: Cleared garbage collector")

            # Log system state
            await self._capture_system_state()
            logger.info("CAP1: System state captured")

            # Indicate retry should be attempted
            return True

        except Exception as e:
            logger.error(f"CAP1: Recovery failed - {e}")
            failure.escalation_needed = True
            return False

    async def CAP2_provider_degradation(self, failure: TestFailure) -> bool:
        """
        Handle provider API errors and timeouts.

        Actions:
        1. Check provider status/rate limits
        2. Determine if transient or permanent failure
        3. Switch to fallback provider if available
        4. Queue for retry after backoff
        5. Alert if provider is down
        """
        logger.warning(f"CAP2: Provider degradation - {failure.message}")

        self.remediation_log.append(
            {
                "cap": "CAP2",
                "action": "provider_recovery",
                "message": failure.message,
                "context": failure.context,
            }
        )

        try:
            # Check if this is rate limiting vs outage
            error_msg = str(failure.error or "")

            if "rate" in error_msg.lower() or "429" in error_msg:
                logger.info("CAP2: Rate limit detected, queuing for retry with backoff")
                await asyncio.sleep(5)  # Backoff
                return True

            elif "timeout" in error_msg.lower() or "503" in error_msg:
                logger.info("CAP2: Service unavailable, checking fallback")
                # Would check for fallback provider here
                return False

            else:
                logger.warning(f"CAP2: Unknown provider error - {error_msg}")
                return False

        except Exception as e:
            logger.error(f"CAP2: Recovery failed - {e}")
            failure.escalation_needed = True
            return False

    async def CAP3_device_unreachable(self, failure: TestFailure) -> bool:
        """
        Handle device offline or SSH timeout.

        Actions:
        1. Verify device is actually unreachable (not just timing out)
        2. Check network connectivity
        3. Skip device tests temporarily
        4. Queue for infrastructure team
        5. Continue with other devices
        """
        device = failure.context.get("device") if failure.context else None
        logger.warning(f"CAP3: Device unreachable - {failure.message} (device={device})")

        self.remediation_log.append(
            {
                "cap": "CAP3",
                "action": "device_skip",
                "message": failure.message,
                "device": device,
            }
        )

        try:
            # Attempt a simple ping/connectivity check
            if device:
                result = await self._test_connectivity(device)
                if result:
                    logger.info(f"CAP3: Device {device} is reachable, retry")
                    return True
                else:
                    logger.warning(f"CAP3: Device {device} is offline, skipping")
                    failure.retryable = False
                    return False
            else:
                return False

        except Exception as e:
            logger.error(f"CAP3: Connectivity check failed - {e}")
            return False

    async def CAP4_memory_pressure(self, failure: TestFailure) -> bool:
        """
        Respond to memory constraints on Pi devices.

        Actions:
        1. Measure current memory usage
        2. If near limit (>90%), reduce concurrency
        3. Clear caches and temporary files
        4. Retry with reduced memory footprint
        5. Consider command consolidation
        """
        device = failure.context.get("device") if failure.context else None
        logger.warning(f"CAP4: Memory pressure - {failure.message} (device={device})")

        self.remediation_log.append(
            {
                "cap": "CAP4",
                "action": "memory_optimization",
                "message": failure.message,
                "device": device,
            }
        )

        try:
            # Clear caches
            import gc

            gc.collect()
            logger.info("CAP4: Garbage collection completed")

            # For Pi devices, reduce concurrency
            if device and device.startswith("zero"):
                logger.info("CAP4: Detected zeropi, reducing concurrency to 1")
                # Signal to reduce concurrent command execution
                failure.context["reduced_concurrency"] = 1
                return True
            else:
                return False

        except Exception as e:
            logger.error(f"CAP4: Memory optimization failed - {e}")
            return False

    async def CAP5_zeroclaw_routing_failure(self, failure: TestFailure) -> bool:
        """
        Handle ZeroClaw routing errors or model unavailability.

        Actions:
        1. Check ZeroClaw health
        2. Verify model is available
        3. Re-bootstrap model context if needed
        4. Check network connectivity to device
        5. Retry with fresh context
        """
        logger.warning(f"CAP5: ZeroClaw routing failure - {failure.message}")

        self.remediation_log.append(
            {
                "cap": "CAP5",
                "action": "zeroclaw_recovery",
                "message": failure.message,
            }
        )

        try:
            # Check if this is a model context issue
            error_msg = str(failure.error or "")

            if "model" in error_msg.lower() or "context" in error_msg.lower():
                logger.info("CAP5: Model context issue, clearing and retrying")
                # Would signal to re-bootstrap context
                failure.context["force_rebootstrap"] = True
                return True
            else:
                logger.warning("CAP5: Unknown routing error")
                return False

        except Exception as e:
            logger.error(f"CAP5: Recovery failed - {e}")
            return False

    async def CAP6_model_mismatch(self, failure: TestFailure) -> bool:
        """
        Recover from LLM context or model mismatch errors.

        Actions:
        1. Clear model context cache
        2. Re-bootstrap model state
        3. Verify model availability
        4. Retry with fresh initialization
        5. Check for model version conflicts
        """
        logger.warning(f"CAP6: Model mismatch - {failure.message}")

        self.remediation_log.append(
            {
                "cap": "CAP6",
                "action": "model_recovery",
                "message": failure.message,
            }
        )

        try:
            # Clear caches
            import gc

            gc.collect()

            # Force model reinitialization
            failure.context["force_rebootstrap"] = True
            failure.context["clear_cache"] = True

            logger.info("CAP6: Model context cleared, retry with fresh state")
            return True

        except Exception as e:
            logger.error(f"CAP6: Recovery failed - {e}")
            return False

    async def _test_connectivity(self, device: str) -> bool:
        """Test basic connectivity to device (ping, SSH handshake)."""

        try:
            # Simple ping test
            result = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    "ping",
                    "-c",
                    "1",
                    device,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=5,
            )
            await result.wait()
            return result.returncode == 0

        except (asyncio.TimeoutError, FileNotFoundError, Exception):
            return False

    async def _capture_system_state(self) -> Dict[str, Any]:
        """Capture system state for debugging (memory, CPU, processes)."""
        try:
            import os

            import psutil

            state = {
                "memory": {
                    "used_percent": psutil.virtual_memory().percent,
                    "available_mb": psutil.virtual_memory().available / (1024 * 1024),
                },
                "cpu": {
                    "percent": psutil.cpu_percent(interval=1),
                    "count": os.cpu_count(),
                },
            }
            logger.debug(f"System state: {state}")
            return state

        except ImportError:
            logger.debug("psutil not available for system state capture")
            return {}

    async def execute_with_remediation(self, test_fn, max_retries: int = None) -> bool:
        """
        Execute a test function with automatic remediation on failure.

        Usage:
            remediation = RemediationWorkflow()
            success = await remediation.execute_with_remediation(test_function)
        """
        max_retries = max_retries or self.max_retries
        retry_count = 0

        while retry_count < max_retries:
            try:
                await test_fn()
                return True

            except TestFailure as failure:
                retry_count += 1
                logger.warning(f"Remediation attempt {retry_count}/{max_retries}")

                # Route to appropriate CAP
                recovery_successful = False

                if failure.failure_class == FailureClass.ORCHESTRATION_FAILURE:
                    recovery_successful = await self.CAP1_orchestration_failure(failure)

                elif failure.failure_class == FailureClass.PROVIDER_DEGRADATION:
                    recovery_successful = await self.CAP2_provider_degradation(failure)

                elif failure.failure_class == FailureClass.DEVICE_UNREACHABLE:
                    recovery_successful = await self.CAP3_device_unreachable(failure)

                elif failure.failure_class == FailureClass.MEMORY_CONSTRAINT:
                    recovery_successful = await self.CAP4_memory_pressure(failure)

                elif failure.failure_class == FailureClass.ZEROCLAW_ROUTING:
                    recovery_successful = await self.CAP5_zeroclaw_routing_failure(failure)

                elif failure.failure_class == FailureClass.MODEL_MISMATCH:
                    recovery_successful = await self.CAP6_model_mismatch(failure)

                if not recovery_successful or not failure.retryable:
                    if failure.escalation_needed:
                        logger.error(f"ESCALATION REQUIRED: {failure.message}")
                    return False

            except Exception as e:
                logger.error(f"Unhandled exception during test execution: {e}", exc_info=True)
                return False

        logger.error(f"All {max_retries} remediation attempts exhausted")
        return False

    def get_remediation_log(self) -> list:
        """Get log of all remediation actions taken."""
        return self.remediation_log
