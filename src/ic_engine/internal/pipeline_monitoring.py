"""
Pipeline monitoring and observability utilities.
Provides detailed insights into execution flow, performance, and health.
"""

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ExecutionEvent:
    """A single event in pipeline execution."""

    timestamp: str
    stage_name: str
    event_type: str  # "start", "success", "failed", "skipped", "retry"
    duration_ms: Optional[float] = None
    error: Optional[str] = None
    metadata: Optional[Dict] = None

    def to_dict(self) -> Dict:
        return asdict(self)


class PipelineMonitor:
    """Monitor and analyze pipeline execution."""

    def __init__(self, log_file: Optional[Path] = None):
        """Initialize monitor with optional file logging."""
        self.events: List[ExecutionEvent] = []
        self.log_file = log_file

    def record_event(
        self,
        stage_name: str,
        event_type: str,
        duration_ms: Optional[float] = None,
        error: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> None:
        """Record an execution event."""
        event = ExecutionEvent(
            timestamp=datetime.now().isoformat(),
            stage_name=stage_name,
            event_type=event_type,
            duration_ms=duration_ms,
            error=error,
            metadata=metadata,
        )
        self.events.append(event)

        # Log to file if enabled
        if self.log_file:
            self._write_event_to_file(event)

    def get_stage_summary(self, stage_name: str) -> Dict[str, Any]:
        """Get summary stats for a stage."""
        stage_events = [e for e in self.events if e.stage_name == stage_name]

        if not stage_events:
            return {}

        successes = sum(1 for e in stage_events if e.event_type == "success")
        failures = sum(1 for e in stage_events if e.event_type == "failed")
        skipped = sum(1 for e in stage_events if e.event_type == "skipped")
        retries = sum(1 for e in stage_events if e.event_type == "retry")

        durations = [e.duration_ms for e in stage_events if e.duration_ms is not None]

        return {
            "stage_name": stage_name,
            "total_events": len(stage_events),
            "successes": successes,
            "failures": failures,
            "skipped": skipped,
            "retries": retries,
            "avg_duration_ms": sum(durations) / len(durations) if durations else 0,
            "min_duration_ms": min(durations) if durations else 0,
            "max_duration_ms": max(durations) if durations else 0,
        }

    def get_execution_timeline(self) -> List[Dict[str, Any]]:
        """Get chronological timeline of execution."""
        return [e.to_dict() for e in sorted(self.events, key=lambda x: x.timestamp)]

    def get_error_report(self) -> Dict[str, List[Dict]]:
        """Get report of all errors that occurred."""
        error_events = [e for e in self.events if e.error is not None]

        report = {}
        for event in error_events:
            if event.stage_name not in report:
                report[event.stage_name] = []
            report[event.stage_name].append(
                {
                    "timestamp": event.timestamp,
                    "event_type": event.event_type,
                    "error": event.error,
                }
            )

        return report

    def get_performance_report(self) -> Dict[str, Any]:
        """Get detailed performance report."""
        stage_names = set(e.stage_name for e in self.events)
        summaries = {name: self.get_stage_summary(name) for name in stage_names}

        total_duration = 0
        for event in self.events:
            if event.duration_ms:
                total_duration = max(total_duration, event.duration_ms)

        return {
            "total_pipeline_duration_ms": total_duration,
            "total_events": len(self.events),
            "stages": summaries,
            "timeline": self.get_execution_timeline(),
        }

    def save_report(self, output_file: Path) -> None:
        """Save full monitoring report to file."""
        report = {
            "generated_at": datetime.now().isoformat(),
            "performance": self.get_performance_report(),
            "errors": self.get_error_report(),
        }

        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w") as f:
            json.dump(report, f, indent=2)

        logger.info(f"Monitoring report saved to {output_file}")

    def _write_event_to_file(self, event: ExecutionEvent) -> None:
        """Write event to log file."""
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_file, "a") as f:
                f.write(json.dumps(event.to_dict()) + "\n")
        except Exception as e:
            logger.warning(f"Failed to write event to log file: {e}")

    def print_summary(self) -> None:
        """Print a summary of execution to console."""
        report = self.get_performance_report()

        print("\n" + "=" * 60)
        print("PIPELINE EXECUTION SUMMARY")
        print("=" * 60)
        print(f"Total Duration: {report['total_pipeline_duration_ms']:.0f}ms")
        print(f"Total Events: {report['total_events']}")
        print("\nStage Summary:")
        print("-" * 60)

        for stage_name, summary in report["stages"].items():
            if summary:
                print(f"\n{stage_name}:")
                print(f"  Successes: {summary.get('successes', 0)}")
                print(f"  Failures: {summary.get('failures', 0)}")
                print(f"  Skipped: {summary.get('skipped', 0)}")
                print(f"  Retries: {summary.get('retries', 0)}")
                print(f"  Avg Duration: {summary.get('avg_duration_ms', 0):.0f}ms")

        errors = self.get_error_report()
        if errors:
            print("\nErrors:")
            print("-" * 60)
            for stage, stage_errors in errors.items():
                print(f"\n{stage}:")
                for error in stage_errors:
                    print(f"  {error['timestamp']}: {error['error']}")

        print("\n" + "=" * 60)
