# Copyright 2026 clio Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""clio.drift.alarm — surface drift events for human review.

When clio.drift.detect emits events that aren't auto-resolved by
clio.drift.remap, the operational pattern is to surface them somewhere a
human (or a downstream alerting system) can see them. This module is the
surfacing layer.

v0.1 supports two targets:

    * "log" — write events to a configured Python logger at a level matched
      to event severity (info / warning / error). This is the default and
      doesn't require any external infrastructure.
    * "file" — append a JSON-Lines record per event to an alarm log file.
      Default path is `logs/clio-drift-alarms.jsonl` overridable per call.

Future targets (slack, email, webhook) hang off the same surface() entry
point — implementations are left to v0.2+.

Severity aggregation across a batch is the maximum of individual severities,
with the implicit ordering critical > error > warn > info. That's what
severity_of() returns.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path
from typing import Iterable, Optional

from clio.drift.detect import DriftEvent

logger = logging.getLogger(__name__)


_SEVERITY_RANK: dict[str, int] = {
    "info": 0,
    "warn": 1,
    "error": 2,
    "critical": 3,
}

_SEVERITY_TO_LOG_LEVEL: dict[str, int] = {
    "info": logging.INFO,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


DEFAULT_ALARM_LOG_PATH = Path("logs/clio-drift-alarms.jsonl")


def severity_of(events: Iterable[DriftEvent]) -> str:
    """Return the maximum severity across a batch of events.

    Empty input returns "info" (no events = no drift = informational state).
    """
    items = list(events)
    if not items:
        return "info"
    max_rank = max(_SEVERITY_RANK.get(e.severity, 1) for e in items)
    for severity, rank in _SEVERITY_RANK.items():
        if rank == max_rank:
            return severity
    return "info"  # unreachable, satisfies type checker


def _event_to_json_record(event: DriftEvent) -> str:
    """Serialize a DriftEvent to a single-line JSON string."""
    record = dataclasses.asdict(event)
    # detection_date is a datetime; isoformat for JSON.
    record["detection_date"] = event.detection_date.isoformat()
    return json.dumps(record, sort_keys=True, default=str)


def _surface_to_log(events: list[DriftEvent]) -> None:
    """Surface events to the Python logger with severity-mapped levels."""
    for event in events:
        level = _SEVERITY_TO_LOG_LEVEL.get(event.severity, logging.WARNING)
        logger.log(
            level,
            "drift detected: %s severity=%s prior=%s current=%s auto_resolved=%s metadata=%s",
            event.event_type,
            event.severity,
            event.prior_fingerprint_id[:12],
            event.current_fingerprint_id[:12],
            event.auto_resolved,
            event.metadata,
        )


def _surface_to_file(events: list[DriftEvent], path: Path) -> None:
    """Append events as JSON-Lines to a file path. Creates parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for event in events:
            fh.write(_event_to_json_record(event))
            fh.write("\n")


def surface(
    events: Iterable[DriftEvent],
    target: str = "log",
    *,
    file_path: Optional[Path] = None,
) -> None:
    """Surface a batch of drift events to a configured target.

    Args:
        events: Events from clio.drift.detect (optionally post-clio.drift.remap).
        target: "log" (default) | "file".
        file_path: For target="file", override the alarm log path. Default
            is DEFAULT_ALARM_LOG_PATH ("logs/clio-drift-alarms.jsonl").

    Raises:
        ValueError: if target is not a recognized value.
    """
    items = list(events)
    if not items:
        return

    if target == "log":
        _surface_to_log(items)
    elif target == "file":
        _surface_to_file(items, file_path or DEFAULT_ALARM_LOG_PATH)
    else:
        raise ValueError(f"unsupported drift-alarm target: {target!r} (expected 'log' or 'file')")
