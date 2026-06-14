"""ic-engine — Python portfolio-analysis library powering the InvestorClaw skill ecosystem.

This package is consumed by two adapters:
  - InvestorClaw (claws-runtime: OpenClaw / Hermes / ZeroClaw / standalone)
  - InvestorClaude (Claude Code plugin)

The canonical agent-skill contract template lives in `contract/` (sibling to src/).
"""

__version__ = "4.7.7"

# Install yfinance disk-cache before any analyzer imports yfinance.
# Without this, every subprocess invocation re-fetches prices from Yahoo
# Finance, and after 1-2 bursts Yahoo IP-rate-limits us. With it, the
# first call writes /data/reports/.yf-cache/<key>.parquet, and subsequent
# subprocess calls in the same day read parquet locally with no network.
# Disable via INVESTORCLAW_YF_CACHE=disabled.
try:
    from .providers import yf_disk_cache as _yf_disk_cache

    _yf_disk_cache.install()
except Exception:
    # Never fail import on cache-install issues; analyzer code still works
    # with raw yfinance, just hits the rate-limit issue.
    pass
