#!/usr/bin/env python3
"""
ZeroClaw/Nemoclaw Orchestrator Testing — Phase 3c Extended

Test actual ZeroClaw and Nemoclaw orchestrator implementations:
- clawpi: Nemoclaw+ZeroClaw (nclawzero) — 8GB Pi testing
- zeropi: Pure ZeroClaw — 2GB Pi constrained testing

This validates the orchestrator implementations can execute:
- PathA (direct CLI commands)
- PathB (agent-based routing)
- Multi-provider execution
- Device-specific constraints and timeouts

Usage:
  python test_zeroclaw_orchestrator.py --validate-deployment
  python test_zeroclaw_orchestrator.py --device clawpi --test-path-a
  python test_zeroclaw_orchestrator.py --device zeropi --test-path-b
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


def check_orchestrator_deployment(host: str, port: int = 22) -> Optional[Dict[str, Any]]:
    """Check if orchestrator (ZeroClaw/Nemoclaw) is deployed and running."""
    checks = {
        "host": host,
        "orchestrator_installed": False,
        "zeroclaw_present": False,
        "nemoclaw_present": False,
        "harness_present": False,
        "details": {},
    }

    try:
        # Check for zeroclaw
        cmd = "[ -d ~/zeroclaw ] && echo 'zeroclaw_found' || echo 'not_found'"
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
                cmd,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        checks["zeroclaw_present"] = "zeroclaw_found" in result.stdout

        # Check for nemoclaw
        cmd = "[ -d ~/nemoclaw ] && echo 'nemoclaw_found' || echo 'not_found'"
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
                cmd,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        checks["nemoclaw_present"] = "nemoclaw_found" in result.stdout

        # Check for harness
        cmd = "[ -d ~/InvestorClaw/harness ] && echo 'harness_found' || echo 'not_found'"
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
                cmd,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        checks["harness_present"] = "harness_found" in result.stdout

        # Get Python version
        cmd = "python3 --version 2>&1"
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
                cmd,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            checks["details"]["python_version"] = result.stdout.strip()

        checks["orchestrator_installed"] = checks["zeroclaw_present"] or checks["nemoclaw_present"]

        return checks

    except Exception as e:
        logger.debug(f"Orchestrator check failed for {host}: {e}")
        checks["error"] = str(e)
        return checks


def test_path_a_execution(host: str, port: int = 22) -> Optional[Dict[str, Any]]:
    """Test PathA (direct CLI) execution on remote device."""
    try:
        # Try to run a simple command
        cmd = "cd ~/InvestorClaw && python investorclaw.py holdings 2>&1 | head -20"
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=3", "-p", str(port), f"jasonperlow@{host}", cmd],
            capture_output=True,
            text=True,
            timeout=15,
        )

        if result.returncode == 0:
            return {
                "host": host,
                "path": "PathA",
                "status": "success",
                "output": result.stdout[:200],
            }
        else:
            return {"host": host, "path": "PathA", "status": "failed", "error": result.stderr[:200]}

    except subprocess.TimeoutExpired:
        return {"host": host, "path": "PathA", "status": "timeout", "error": "SSH command timeout"}
    except Exception as e:
        return {"host": host, "path": "PathA", "status": "error", "error": str(e)}


def test_path_b_execution(host: str, port: int = 22) -> Optional[Dict[str, Any]]:
    """Test PathB (agent-based) execution on remote device."""
    try:
        # Try to run via agent
        cmd = "cd ~/InvestorClaw && openclaw agent --agent main -m 'get holdings' 2>&1 | head -20"
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=3", "-p", str(port), f"jasonperlow@{host}", cmd],
            capture_output=True,
            text=True,
            timeout=15,
        )

        if result.returncode == 0:
            return {
                "host": host,
                "path": "PathB",
                "status": "success",
                "output": result.stdout[:200],
            }
        else:
            return {"host": host, "path": "PathB", "status": "failed", "error": result.stderr[:200]}

    except subprocess.TimeoutExpired:
        return {"host": host, "path": "PathB", "status": "timeout", "error": "SSH command timeout"}
    except Exception as e:
        return {"host": host, "path": "PathB", "status": "error", "error": str(e)}


async def validate_zeroclaw_deployment() -> Dict[str, Any]:
    """Validate ZeroClaw/Nemoclaw deployment across all orchestrator devices."""
    results = {"timestamp": time.time(), "devices": {}}

    for device_name in ["clawpi", "zeropi"]:
        try:
            device_config = get_device(device_name)
        except ValueError:
            continue

        logger.info(f"\n{'=' * 70}")
        logger.info(f"Validating {device_name} ({device_config.description})")
        logger.info(f"{'=' * 70}")

        device_checks = check_orchestrator_deployment(device_config.host, device_config.ssh_port)

        if device_checks:
            logger.info(f"Host: {device_config.host}")
            logger.info(f"ZeroClaw present: {'✓' if device_checks['zeroclaw_present'] else '✗'}")
            logger.info(f"Nemoclaw present: {'✓' if device_checks['nemoclaw_present'] else '✗'}")
            logger.info(f"Harness present: {'✓' if device_checks['harness_present'] else '✗'}")
            logger.info(
                f"Orchestrator installed: {'✓' if device_checks['orchestrator_installed'] else '✗'}"
            )

            results["devices"][device_name] = device_checks

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ZeroClaw/Nemoclaw Orchestrator Testing")
    parser.add_argument(
        "--validate-deployment", action="store_true", help="Validate orchestrator deployment"
    )
    parser.add_argument("--device", choices=["clawpi", "zeropi"], help="Test specific device")
    parser.add_argument("--test-path-a", action="store_true", help="Test PathA (direct CLI)")
    parser.add_argument("--test-path-b", action="store_true", help="Test PathB (agent-based)")

    args = parser.parse_args()

    try:
        if args.validate_deployment:
            result = asyncio.run(validate_zeroclaw_deployment())
        elif args.device:
            try:
                device_config = get_device(args.device)
            except ValueError as e:
                logger.error(f"Device error: {e}")
                sys.exit(1)

            if args.test_path_a:
                result = test_path_a_execution(device_config.host, device_config.ssh_port)
            elif args.test_path_b:
                result = test_path_b_execution(device_config.host, device_config.ssh_port)
            else:
                logger.error("Specify --test-path-a or --test-path-b")
                sys.exit(1)
        else:
            parser.print_help()
            sys.exit(0)

        # Save results
        results_file = "zeroclaw_orchestrator_results.json"
        with open(results_file, "w") as f:
            json.dump(result, f, indent=2, default=str)
        logger.info(f"\nResults saved to {results_file}")

        sys.exit(0)

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
