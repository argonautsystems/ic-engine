"""
Simple in-memory cache for expensive stage operations.
Reduces redundant API calls when running stages multiple times.
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class StageCache:
    """Thread-safe cache for stage results with TTL support."""

    def __init__(self, cache_dir: Optional[Path] = None, ttl_hours: int = 24):
        """Initialize cache with optional disk persistence."""
        self.cache_dir = cache_dir
        self.ttl_hours = ttl_hours
        self.in_memory: Dict[str, Dict[str, Any]] = {}

    def _make_key(self, stage_name: str, input_hash: str) -> str:
        """Generate cache key from stage name and input hash."""
        return f"{stage_name}:{input_hash}"

    def _hash_input(self, holdings_file: str, params: Optional[Dict] = None) -> str:
        """Create a hash of the input for cache key."""
        content = f"{holdings_file}"
        if params:
            content += json.dumps(params, sort_keys=True)
        return hashlib.md5(content.encode()).hexdigest()[:8]

    def get(
        self,
        stage_name: str,
        holdings_file: str,
        params: Optional[Dict] = None,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve cached result if valid."""
        key = self._make_key(stage_name, self._hash_input(holdings_file, params))

        # Check in-memory cache first
        if key in self.in_memory:
            entry = self.in_memory[key]
            if self._is_valid(entry):
                logger.debug(f"Cache hit for {stage_name}")
                return entry["data"]
            else:
                del self.in_memory[key]

        # Check disk cache if enabled
        if self.cache_dir:
            disk_entry = self._load_from_disk(key)
            if disk_entry and self._is_valid(disk_entry):
                logger.debug(f"Disk cache hit for {stage_name}")
                self.in_memory[key] = disk_entry
                return disk_entry["data"]

        return None

    def set(
        self,
        stage_name: str,
        holdings_file: str,
        data: Dict[str, Any],
        params: Optional[Dict] = None,
    ) -> None:
        """Store result in cache."""
        key = self._make_key(stage_name, self._hash_input(holdings_file, params))
        entry = {
            "data": data,
            "timestamp": datetime.now().isoformat(),
            "ttl_hours": self.ttl_hours,
        }

        self.in_memory[key] = entry
        logger.debug(f"Cached result for {stage_name}")

        if self.cache_dir:
            self._save_to_disk(key, entry)

    def clear(self) -> None:
        """Clear all caches."""
        self.in_memory.clear()
        if self.cache_dir:
            for f in self.cache_dir.glob("*.cache.json"):
                try:
                    f.unlink()
                except Exception as e:
                    logger.warning(f"Failed to delete cache file: {e}")

    @staticmethod
    def _is_valid(entry: Dict) -> bool:
        """Check if cache entry is still valid."""
        if "timestamp" not in entry or "ttl_hours" not in entry:
            return False

        timestamp = datetime.fromisoformat(entry["timestamp"])
        ttl = timedelta(hours=entry["ttl_hours"])
        return datetime.now() < timestamp + ttl

    def _load_from_disk(self, key: str) -> Optional[Dict]:
        """Load cache entry from disk."""
        if not self.cache_dir:
            return None

        cache_file = self.cache_dir / f"{key}.cache.json"
        if cache_file.exists():
            try:
                with open(cache_file) as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load cache: {e}")
        return None

    def _save_to_disk(self, key: str, entry: Dict) -> None:
        """Save cache entry to disk."""
        if not self.cache_dir:
            return

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = self.cache_dir / f"{key}.cache.json"
        try:
            with open(cache_file, "w") as f:
                json.dump(entry, f)
        except Exception as e:
            logger.warning(f"Failed to save cache: {e}")


def cacheable_stage(cache: Optional[StageCache] = None, ttl_hours: int = 24):
    """Decorator to add caching to stage functions."""

    def decorator(func: Callable) -> Callable:
        async def wrapper(
            self,
            holdings_file: str,
            *args,
            **kwargs,
        ):
            if not cache:
                return await func(self, holdings_file, *args, **kwargs)

            # Try to get from cache
            params = {k: str(v) for k, v in kwargs.items()}
            cached_result = cache.get(
                func.__self__.stage_name if hasattr(func, "__self__") else func.__name__,
                holdings_file,
                params,
            )
            if cached_result:
                return cached_result

            # Execute function and cache result
            result = await func(self, holdings_file, *args, **kwargs)
            if result and isinstance(result, dict):
                cache.set(
                    func.__self__.stage_name if hasattr(func, "__self__") else func.__name__,
                    holdings_file,
                    result,
                    params,
                )
            return result

        return wrapper

    return decorator
