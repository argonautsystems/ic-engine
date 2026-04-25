#!/usr/bin/env python3
"""Granular performance timing for data provider operations."""

import json
import logging
import time
from contextlib import contextmanager
from typing import Dict, List

logger = logging.getLogger(__name__)


class PerformanceTimer:
    """Track operation timing for data providers and analysis tasks."""

    def __init__(self):
        self.timings: Dict[str, List[float]] = {}
        self.stack: List[str] = []

    @contextmanager
    def measure(self, operation: str):
        """Context manager to measure operation timing."""
        start = time.perf_counter()
        self.stack.append(operation)
        try:
            yield
        finally:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            if operation not in self.timings:
                self.timings[operation] = []
            self.timings[operation].append(elapsed_ms)
            self.stack.pop()
            logger.debug(f"{operation}: {elapsed_ms}ms")

    def emit(self) -> Dict:
        """Emit timing summary as JSON."""
        if not self.timings:
            return {}

        summary = {}
        for op, times in self.timings.items():
            summary[op] = {
                "count": len(times),
                "total_ms": sum(times),
                "avg_ms": sum(times) // len(times) if times else 0,
                "min_ms": min(times) if times else 0,
                "max_ms": max(times) if times else 0,
            }
        return {"provider_timing": summary}

    def log(self) -> None:
        """Log timing summary."""
        timing = self.emit()
        if timing:
            logger.info(f"Performance timing: {json.dumps(timing)}")


# Global timer instance
_timer = PerformanceTimer()


def time_operation(operation: str):
    """Decorator to time a function."""

    def decorator(func):
        def wrapper(*args, **kwargs):
            with _timer.measure(operation):
                return func(*args, **kwargs)

        return wrapper

    return decorator


def get_timer() -> PerformanceTimer:
    """Get the global timer instance."""
    return _timer
