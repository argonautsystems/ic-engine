#!/usr/bin/env python3
"""
Phase 3c: Device Matrix Testing

Execute Tier 1 commands across all devices to validate:
- Device connectivity and SSH setup
- Timeout scaling per device (1x, 1.5x, 2.5x)
- Command execution on constrained hardware
- Performance baselines per device

Usage:
  python test_device_matrix.py --tier 1 --device STUDIO --mock-responses
  python test_device_matrix.py --all-devices --tier 1 --mock-responses
  python test_device_matrix.py --validate-devices (check SSH setup)
"""

import asyncio
import json
import logging
import subprocess
import sys
import time
from typing import Any, Dict, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

from harness.device_matrix import get_device
from harness.test_tier_execution import test_tier_execution


def check_ssh_configured(host: str, port: int = 22) -> bool:
    """Quick check if SSH to host works."""
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "ConnectTimeout=2",
                "-o",
                "BatchMode=yes",
                "-p",
                str(port),
                f"jasonperlow@{host}",
                "exit",
            ],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


async def test_device_tier(
    device_name: str, tier: int = 1, verbose: bool = False, mock_responses: bool = False
) -> Dict[str, Any]:
    """Test a specific tier on a specific device."""
    try:
        device_config = get_device(device_name)
    except ValueError as e:
        return {"error": str(e)}

    logger.info(f"\n{'#' * 70}")
    logger.info(f"# DEVICE: {device_name} | TIER: {tier}")
    logger.info(f"# Host: {device_config.host} | Timeout: {device_config.timeout_multiplier}x")
    logger.info(f"{'#' * 70}\n")

    # For non-STUDIO devices, check SSH setup
    if device_name != "STUDIO" and not mock_responses:
        if not check_ssh_configured(device_config.host, device_config.ssh_port):
            logger.warning(f"\n⚠️  SSH to {device_name} ({device_config.host}) not configured")
            logger.warning("Options to fix:")
            logger.warning(
                f"  1. Set up SSH key: ssh-copy-id -i ~/.ssh/id_rsa.pub jasonperlow@{device_config.host}"
            )
            logger.warning("  2. Or test with --mock-responses for framework validation")
            logger.warning("")
            return {
                "device": device_name,
                "status": "ssh_unconfigured",
                "commands_tested": 0,
                "commands_passed": 0,
                "error": f"SSH not configured for {device_name}",
            }

    # Run tier test
    result = await test_tier_execution(
        tier=tier,
        device=device_name,
        provider="gemma",
        verbose=verbose,
        mock_responses=mock_responses,
        skip_provider_check=mock_responses,
    )

    return result


async def test_all_devices_tier(
    tier: int = 1, verbose: bool = False, mock_responses: bool = False
) -> Dict[str, Any]:
    """Test a tier across all devices."""
    results = {"tier": tier, "timestamp": time.time(), "devices": {}}

    # Test STUDIO first, then Pis
    for device_name in ["STUDIO", "clawpi", "zeropi"]:
        result = await test_device_tier(
            device_name=device_name, tier=tier, verbose=verbose, mock_responses=mock_responses
        )
        results["devices"][device_name] = result

        # If SSH not configured, offer guidance
        if result.get("status") == "ssh_unconfigured":
            logger.info(f"Skipping {device_name}, SSH not configured")

    # Summary
    logger.info(f"\n{'=' * 70}")
    logger.info("TIER {0} DEVICE MATRIX SUMMARY".format(tier))
    logger.info(f"{'=' * 70}")

    for device_name, device_result in results["devices"].items():
        if device_result.get("status") == "ssh_unconfigured":
            logger.info(f"⚠️  {device_name}: SSH not configured")
        else:
            passed = device_result.get("commands_passed", 0)
            total = device_result.get("commands_tested", 0)
            status = "✓ PASS" if passed == total else "✗ FAIL"
            logger.info(f"  {device_name}: {passed}/{total} {status}")

    logger.info(f"{'=' * 70}\n")

    return results


async def run_phase3c_full(
    device_name: Optional[str] = None, verbose: bool = False, mock_responses: bool = False
) -> Dict[str, Any]:
    """
    Full Phase 3c: Test all tiers on specified device(s).
    """
    results = {"phase": "3c", "timestamp": time.time(), "tiers": {}, "summary": {}}

    if device_name:
        # Single device, all tiers
        for tier in [1, 2, 3]:
            logger.info(f"\n{'*' * 70}")
            logger.info(f"Testing {device_name}, Tier {tier}")
            logger.info(f"{'*' * 70}")

            tier_result = await test_device_tier(
                device_name=device_name, tier=tier, verbose=verbose, mock_responses=mock_responses
            )
            results["tiers"][f"tier_{tier}"] = {device_name: tier_result}
    else:
        # All devices, Tier 1 only (faster baseline)
        for tier in [1]:
            logger.info(f"\n{'*' * 70}")
            logger.info(f"Testing All Devices, Tier {tier}")
            logger.info(f"{'*' * 70}")

            tier_result = await test_all_devices_tier(
                tier=tier, verbose=verbose, mock_responses=mock_responses
            )
            results["tiers"][f"tier_{tier}"] = tier_result

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Phase 3c: Device Matrix Testing",
        epilog="""
Examples:
  # Validate device setup
  python test_device_matrix.py --validate-devices

  # Test STUDIO with mock mode (fast validation)
  python test_device_matrix.py --device STUDIO --tier 1 --mock-responses

  # Test all devices with mock (no SSH needed)
  python test_device_matrix.py --all-devices --tier 1 --mock-responses

  # Test all tiers on STUDIO (requires Ollama)
  python test_device_matrix.py --device STUDIO --all-tiers
        """,
    )
    parser.add_argument(
        "--device", default=None, help="Test specific device (STUDIO, clawpi, zeropi)"
    )
    parser.add_argument("--all-devices", action="store_true", help="Test all devices")
    parser.add_argument("--tier", type=int, default=1, help="Test specific tier (1, 2, 3)")
    parser.add_argument("--all-tiers", action="store_true", help="Test all tiers")
    parser.add_argument("--mock-responses", action="store_true", help="Use mock responses")
    parser.add_argument(
        "--validate-devices", action="store_true", help="Validate device connectivity"
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    try:
        if args.validate_devices:
            # Run device validation
            from harness.validate_devices import validate_all_devices

            result = asyncio.run(validate_all_devices())
        else:
            # Run tier testing
            result = asyncio.run(
                run_phase3c_full(
                    device_name=args.device if not args.all_devices else None,
                    verbose=args.verbose,
                    mock_responses=args.mock_responses,
                )
            )

        # Save results
        results_file = "device_matrix_results.json"
        with open(results_file, "w") as f:
            json.dump(result, f, indent=2, default=str)
        logger.info(f"Results saved to {results_file}")

        sys.exit(0)

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
