# SPDX-License-Identifier: Apache-2.0
# InvestorClaw 4.4.6 ic-engine container — bridge image for the v4.1.18 application service
#
# Builds: mnemos-os/ic-engine:4.4.6-cpu
# Pairs with: mnemos-os/mnemos-rs:4.2 (over compose bridge network)
#
# What's in this container:
#   - Python 3.12 + uv-managed venv
#   - argonautsystems/ic-engine pinned to a specific SHA (set via build arg)
#   - FastMCP server at :8090
#   - Dashboard static files served at :8092
#   - MnemosClient (HTTP client to mnemos-rs at $MNEMOS_BASE)
#
# What's NOT in this container:
#   - Any agent runtime code
#   - Any user data (mounts /data volume from compose)
#   - Any raw API keys (mounts /data/keys.env at runtime)

# ============================================================================
# Stage 1: builder — fetch ic-engine source, install deps via uv
# ============================================================================
FROM python:3.12-slim AS builder

# Pinning ic-engine to a specific SHA (set via --build-arg).
# Default fills in at build time; production builds should pin explicitly.
# Repo migrated from perlowja/InvestorClaw → argonautsystems/ic-engine
# (org migration completed in v2.5.1; commit 729dd5d on master).
# Pinned to ic-engine@2d902d3 — v4.4.6 carries:
#   - optimize.py: OPT-01 equity pre-filter (top 30) + OPT-03 zero-price
#     symbol filter before DiscreteAllocation; argv preprocess fix
#   - All prior fixes through v4.1.38 narrator + runaway hardening
# Bump this SHA + the version label below for each ic-engine source bump
# so the build is deterministic; CI multi-arch buildx rebuilds both
# arches against the pinned commit.
ARG IC_ENGINE_REF=32713bb
ARG IC_ENGINE_REPO=https://gitlab.com/argonautsystems/ic-engine.git

# uv install (canonical Python toolchain per project policy)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

# Install uv from PyPI (the canonical Python toolchain per project policy).
# Using `pip install uv` rather than the upstream curl|sh installer for two
# reasons: (1) it goes through PyPI's signed package distribution, not a
# curl-piped shell script; (2) it's auditable by package scanners that
# treat shell-pipe installers as untrusted-source.
RUN python3 -m pip install --no-cache-dir --break-system-packages uv && uv --version

# Consolidated repo: the ic-engine source lives in THIS repo alongside the
# Dockerfile + bridge (no cross-repo git clone, no two-SHA lockstep). Copy the
# engine build inputs from the build context into /build/ic-engine. pyproject
# declares `readme = "README.md"`, so README.md must be present for uv sync.
WORKDIR /build/ic-engine
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY contract ./contract
# clio is vendored in-tree (git subtree of gitlab.com/argonautsystems/clio),
# wired via [tool.uv.sources] as a path dependency — no GITLAB_TOKEN needed.
COPY vendor ./vendor

# uv sync — produces a self-contained venv at /build/.venv
WORKDIR /build/ic-engine
RUN UV_PROJECT_ENVIRONMENT=/build/.venv uv sync --python 3.12 --frozen \
 || UV_PROJECT_ENVIRONMENT=/build/.venv uv sync --python 3.12

# uv sync installs the local project (`investorclaw`) editable by default,
# which writes a __editable___investorclaw_finder.py with MAPPING pointing
# at the build-stage path (/build/ic-engine/investorclaw). After we COPY
# the venv into the runtime stage, that path is gone, so `import investorclaw`
# fails with ModuleNotFoundError. Force a non-editable reinstall so the
# investorclaw module lands in site-packages and survives the stage hop.
RUN UV_PROJECT_ENVIRONMENT=/build/.venv uv pip install \
        --python /build/.venv/bin/python \
        --reinstall --no-deps /build/ic-engine

# Drop CUDA stack and replace with CPU-only torch.
# clio (transitive dep of ic-engine) pulls full GPU torch by default,
# which drags 2.7 GB of nvidia/* + 639 MB triton + 1.1 GB GPU torch.
# ic-engine does not use CUDA at runtime, so we strip the whole stack
# and reinstall CPU-only torch (~200 MB).
# Expected image-size win: ~4 GB.
#
# Note: uv-built venvs do not include pip, so we use `uv pip` (uv's
# pip-compatible CLI) for both list and uninstall. The earlier attempt
# using `python -m pip` failed silently inside xargs because pip isn't
# in the venv.
# hadolint ignore=SC2086,DL4006
RUN set -exo pipefail; \
    PKGS=$(UV_PROJECT_ENVIRONMENT=/build/.venv uv pip list --python /build/.venv/bin/python --format=json \
       | /usr/local/bin/python3 -c "import json, sys; print(' '.join(p['name'] for p in json.load(sys.stdin) if p['name'].lower().startswith('nvidia') or p['name'].lower() in ('triton','torch')))"); \
    echo "uninstalling: $PKGS"; \
    UV_PROJECT_ENVIRONMENT=/build/.venv uv pip uninstall --python /build/.venv/bin/python $PKGS; \
    UV_PROJECT_ENVIRONMENT=/build/.venv uv pip install \
        --python /build/.venv/bin/python \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        --index-strategy unsafe-best-match \
        torch; \
    echo "verifying torch is CPU-only..."; \
    /build/.venv/bin/python -c "import torch; print('torch:', torch.__version__, 'cuda:', torch.cuda.is_available())"

# Strip GPL/LGPL packages to keep the runtime image redistribution-clean
# under Apache-2.0. The four targets we care about:
#
#   PyMuPDF (AGPL-3.0)  — pulled by clio for vision PDF extraction.
#                         clio guards `import fitz` in a try/except inside
#                         clio.extract.vision; uninstalling produces a
#                         graceful runtime error on vision-only paths.
#                         Non-vision paths are unaffected.
#   premailer (LGPL via cssutils) — used in ic_engine.rendering.template_engine
#                         for inlining CSS into HTML reports. The module
#                         already wraps `from premailer import Premailer` in
#                         try/except and sets PREMAILER_AVAILABLE=False on
#                         missing import; reports render without inlined CSS.
#   cssutils (LGPL-3.0)  — only used as premailer's dep.
#   encutils (LGPL-3.0)  — transitive of cssutils.
#   frozendict (LGPL-3.0) — yfinance imports `from frozendict import frozendict`
#                         unconditionally, so we replace with a tiny pure-Python
#                         Apache-2.0 shim. yfinance's use is hash-stability for
#                         dict cache keys; a subclass-of-dict shim suffices.
# Copy the bridge code (MnemosClient, MCP server wrappers, dashboard
# static files, frozendict shim) before any post-uv-sync surgery that
# references files in /build/bridge/.
COPY bridge/ /build/bridge/
COPY dashboard/ /build/dashboard/
# Non-editable install: bridge code lands in venv site-packages, survives
# the COPY --from=builder /build/.venv → /opt/ic-engine/.venv hop. Editable
# (-e) would leave a venv .pth pointing at /build/bridge, which doesn't
# exist in the runtime stage.
RUN UV_PROJECT_ENVIRONMENT=/build/.venv uv pip install --python /build/.venv/bin/python /build/bridge

RUN set -ex; \
    UV_PROJECT_ENVIRONMENT=/build/.venv uv pip uninstall \
        --python /build/.venv/bin/python \
        pymupdf premailer cssutils encutils frozendict || true; \
    cp -r /build/bridge/frozendict_shim /build/.venv/lib/python3.12/site-packages/frozendict; \
    /build/.venv/bin/python -c "from frozendict import frozendict; d=frozendict(a=1); h=hash(d); assert d['a']==1; print('frozendict shim ok, hash:', h)"; \
    echo "verifying GPL/LGPL strip..."; \
    /build/.venv/bin/python -c "import importlib.metadata as md; banned={'pymupdf','premailer','cssutils','encutils'}; found=[d.metadata['Name'] for d in md.distributions() if (d.metadata['Name'] or '').lower() in banned]; assert not found, f'still installed: {found}'; print('GPL/LGPL packages absent: pymupdf premailer cssutils encutils')"

# Strip the torch / transformers / OpenCV stack.
#
# clio declares sentence-transformers as a hard dep, which transitively
# pulls torch + transformers + safetensors + tokenizers + huggingface-hub +
# sympy + (in some configs) opencv-python-headless. After our CUDA strip
# above, that's still ~1 GB of binary weight in the venv. None of it is
# load-bearing for the deterministic-engine path:
#
#   - clio/runtime/hardware.py:816 wraps `import torch` in try/except;
#     falls through to HardwareProfile-based "cuda"/"mps"/"cpu" detection.
#   - clio/extract/schema_map.py:170 has an unguarded `import torch`, but
#     that method (.map_columns) only fires when a user uploads a broker
#     CSV with non-canonical column names. For known broker formats
#     (UBS xls, Schwab CSV, etc.) the path never executes.
#   - The full author-claude fix is in
#     docs/handoff-2026-05-01-clio-torch-trim-for-author-claude.md
#     (swap sentence-transformers -> fastembed, ONNX-based, ~10-20 MB,
#     proven in prod mnemos at PYTHIA :5002).
#
# Until the upstream clio swap lands we strip the stack at the runtime
# layer. Unknown-broker schema mapping fails loudly on use — that's the
# documented runtime limitation.
# hadolint ignore=SC2086,DL4006
RUN set -exo pipefail; \
    PKGS=$(UV_PROJECT_ENVIRONMENT=/build/.venv uv pip list --python /build/.venv/bin/python --format=json \
       | /usr/local/bin/python3 -c "import json, sys; ml={'torch','torchvision','torchaudio','functorch','torchgen','transformers','sentence-transformers','safetensors','tokenizers','huggingface-hub','accelerate','sympy','opencv-python','opencv-python-headless','onnxruntime-gpu'}; print(' '.join(p['name'] for p in json.load(sys.stdin) if p['name'].lower() in ml))"); \
    echo "stripping ML stack: $PKGS"; \
    UV_PROJECT_ENVIRONMENT=/build/.venv uv pip uninstall --python /build/.venv/bin/python $PKGS || true; \
    echo "verifying ML stack strip..."; \
    /build/.venv/bin/python -c "import importlib.metadata as md; banned={'torch','transformers','sentence-transformers','safetensors','tokenizers','huggingface-hub','sympy','opencv-python-headless'}; found=[d.metadata['Name'] for d in md.distributions() if (d.metadata['Name'] or '').lower() in banned]; assert not found, f'still installed: {found}'; print('ML stack absent: torch transformers sentence-transformers safetensors tokenizers huggingface-hub sympy opencv-python-headless')"; \
    echo "verifying clio still importable (its torch usage is lazy)..."; \
    /build/.venv/bin/python -c "import clio; print('clio import ok:', getattr(clio, '__version__', 'unknown'))"; \
    /build/.venv/bin/python -c "from clio.runtime.hardware import detect_device; print('clio device detection (no torch):', detect_device())"

# Post-strip cleanup: orphans + leftovers + optional bits.
#
#   scikit-learn (31 MB)   was a transitive of transformers; now orphaned.
#                          ic-engine doesn't import sklearn directly.
#   cuda-bindings/         NVIDIA's newer CUDA Python packages (cuda_*
#   cuda-pathfinder/       naming, missed by the earlier nvidia-* glob).
#   cuda-toolkit (~23 MB)  Orphaned now that torch is gone.
#   matplotlib + deps      Only used in ic_engine/commands/optimize.py:41
#   (~100 MB)              at module level. We hot-patch optimize.py to
#                          make the matplotlib import lazy at runtime,
#                          then strip the package. Optimize results are
#                          still computable (numpy/scipy/cvxpy do the
#                          math); only the inline chart rendering is lost.
#                          The dashboard renders charts client-side anyway.
#   litellm (61 MB)        REINSTATED 2026-05-03. Comment "only used by
#                          stonkmode" was wrong: ic_engine/runtime/narrator.py
#                          imports it for the user-facing narrative synthesis
#                          path. Stripping it caused EVERY `ask` call to fall
#                          through to the heuristic catalog blurb (silently
#                          swallowed ImportError), which in turn caused
#                          months of "30/30 PASS" to be a false positive on
#                          a too-lenient verdict. NEVER strip litellm again
#                          without first auditing every `from litellm` import
#                          in the engine source — narrator wraps it in a
#                          bare try/except and the bug is silent. See
#                          MNEMOS feedback_v4_0_30_30_was_false_positive.md.
# hadolint ignore=SC2086,DL4006
RUN set -exo pipefail; \
    /usr/local/bin/python3 /build/bridge/patches/lazy_matplotlib_optimize.py \
        /build/.venv/lib/python3.12/site-packages; \
    PKGS=$(UV_PROJECT_ENVIRONMENT=/build/.venv uv pip list --python /build/.venv/bin/python --format=json \
       | /usr/local/bin/python3 -c "import json, sys; orphans={'scikit-learn','cuda-bindings','cuda-pathfinder','cuda-toolkit','matplotlib','fonttools','contourpy','cycler','kiwisolver','pillow'}; print(' '.join(p['name'] for p in json.load(sys.stdin) if p['name'].lower() in orphans))"); \
    echo "stripping orphans + optional: $PKGS"; \
    UV_PROJECT_ENVIRONMENT=/build/.venv uv pip uninstall --python /build/.venv/bin/python $PKGS || true; \
    echo "ensuring litellm IS installed (required by narrator)..."; \
    UV_PROJECT_ENVIRONMENT=/build/.venv uv pip install --python /build/.venv/bin/python litellm; \
    echo "verifying ic-engine still importable..."; \
    /build/.venv/bin/python -c "import ic_engine.commands.optimize; print('optimize import ok (matplotlib lazy)')"; \
    /build/.venv/bin/python -c "import ic_engine; print('ic_engine ok')"; \
    /build/.venv/bin/python -c "import litellm; print('litellm ok (narrator dep)')"

# Pass 5 (post v4.1.x trim): orphans-not-caught-earlier + tests/ directories.
#
#   sqlalchemy (14 MB)  Not used by ic_engine or bridge anywhere
#                       (greppable: zero hits on `sqlalchemy` and `sqlite3`
#                       in ic_engine/ and bridge/). Must have been pulled
#                       in by an earlier dep that's since been stripped
#                       (likely litellm or transformers). Drop.
#   networkx (8 MB)     Same — zero ic_engine import hits, zero reverse-
#                       deps still installed after the litellm/torch strip.
#   {pandas,pyarrow,scipy,numpy}/tests/  Test fixtures shipped inside
#                       the wheels. Never invoked at runtime. Adds up to
#                       ~43 MB across the four packages. Removing them
#                       only breaks `pytest path/to/wheel`, not the libs.
#
# Note: uvloop (16 MB) is intentionally NOT stripped — uvicorn (FastMCP's
# HTTP transport) declares it as a soft dep and prefers it when present.
# Falling back to asyncio's default loop works but is a measurable perf
# hit on event-loop-heavy workloads. Revisit if/when we move FastMCP to
# a different transport.
# hadolint ignore=SC2086,DL4006
RUN set -exo pipefail; \
    PKGS=$(UV_PROJECT_ENVIRONMENT=/build/.venv uv pip list --python /build/.venv/bin/python --format=json \
       | /usr/local/bin/python3 -c "import json, sys; dead={'sqlalchemy','networkx'}; print(' '.join(p['name'] for p in json.load(sys.stdin) if p['name'].lower() in dead))"); \
    echo "stripping orphan deps: $PKGS"; \
    UV_PROJECT_ENVIRONMENT=/build/.venv uv pip uninstall --python /build/.venv/bin/python $PKGS || true; \
    echo "stripping tests/ from heavy packages..."; \
    for pkg in pandas pyarrow scipy numpy; do \
        find /build/.venv/lib/python3.12/site-packages/$pkg \
            -type d -name tests -prune -exec rm -rf {} + 2>/dev/null || true; \
    done; \
    echo "verifying ic-engine + heavy deps still importable..."; \
    /build/.venv/bin/python -c "import ic_engine; import polars; import pandas; import scipy; import numpy; import pyarrow; print('imports ok: ic_engine + polars + pandas + scipy + numpy + pyarrow')"

# Strip any git metadata before it crosses into the runtime stage.
# Runtime needs the source files, never the git metadata.
RUN rm -rf /build/ic-engine/.git

# ============================================================================
# Stage 2: runtime — minimal image with venv + bridge + dashboard
# ============================================================================
FROM python:3.12-slim AS runtime

# Runtime dependencies (libgomp for numpy/scipy on Debian slim, etc.)
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Non-root user
ARG USER_UID=1000
ARG USER_GID=1000
RUN groupadd -g ${USER_GID} ic && \
    useradd -l -u ${USER_UID} -g ${USER_GID} -m -s /bin/bash ic

# Copy venv + bridge + ic-engine + dashboard from builder
COPY --from=builder --chown=ic:ic /build/.venv /opt/ic-engine/.venv
COPY --from=builder --chown=ic:ic /build/ic-engine /opt/ic-engine/source
COPY --from=builder --chown=ic:ic /build/bridge /opt/ic-engine/bridge
COPY --from=builder --chown=ic:ic /build/dashboard /opt/ic-engine/dashboard

# Rewrite venv shebangs from /build/.venv → /opt/ic-engine/.venv so console
# scripts (investorclaw, investorclaw-bridge, etc.) execve cleanly. uv-built
# venvs hardcode absolute shebangs at install time; the COPY across stages
# leaves them pointing at the build-stage path that no longer exists in the
# runtime image. Without this, `exec investorclaw` fails with ENOENT despite
# the binary itself being present.
RUN find /opt/ic-engine/.venv/bin -type f -exec \
        sed -i '1s|^#!/build/.venv/bin/python.*|#!/opt/ic-engine/.venv/bin/python|' {} \; \
 && /opt/ic-engine/.venv/bin/python -c "import sys; print('venv ok:', sys.executable)"

# /data is the canonical mount point for compose volume.
# chown here sets ownership in the IMAGE layer — applies when Docker
# initializes a NEW named volume from the image (first-run population).
# Pre-existing volumes retain their ownership; see compose.yml ic-data-init
# service which runs as root to fix permissions on existing volumes.
RUN mkdir -p /data/portfolios /data/reports /data/.ic-init \
 && chown -R ic:ic /data \
 && chmod 755 /data/portfolios /data/reports

USER ic
WORKDIR /opt/ic-engine

# Environment defaults — overridable in compose env: block
ENV PATH="/opt/ic-engine/.venv/bin:${PATH}"

# Bridge-side env (read by investorclaw_bridge.serve / mcp_server)
ENV IC_PORTFOLIO_DIR=/data/portfolios
ENV IC_REPORTS_DIR=/data/reports
ENV IC_KEYS_FILE=/data/keys.env
ENV IC_MCP_BIND=0.0.0.0:8090
ENV IC_DASHBOARD_BIND=0.0.0.0:8092
# IC_ENGINE_VERSION is read by /api/version + portfolio_version_check
# so the bridge can self-report its image version (the OCI label below
# isn't readable from inside the container without docker socket access).
# Set from the VERSION build-arg (CI and tools/publish-arm64.sh pass it);
# local builds without the arg report "dev".
ARG VERSION=dev
ENV IC_ENGINE_VERSION=${VERSION}

# ic-engine reads its own canonical env-var names (INVESTOR_CLAW_*).
# Set them to the same values so subprocess'd analyzers honor /data/.
# Without these, ic-engine path_resolver.get_portfolio_dir() falls back
# to ~/portfolios (then to <skill_dir>/portfolios in site-packages).
ENV INVESTOR_CLAW_PORTFOLIO_DIR=/data/portfolios
ENV INVESTOR_CLAW_REPORTS_DIR=/data/reports
ENV INVESTOR_CLAW_DATED_REPORTS=false
ENV INVESTORCLAW_PORTFOLIO_DIR=/data/portfolios

# MNEMOS_BASE intentionally NOT baked at image level — it is a compose-time
# wiring that varies by deployment (compose service name, Tailscale node,
# external HTTPS endpoint). Operators set it via compose environment or
# .env at runtime. See compose.yml for the canonical compose-internal value.
ENV PYTHONUNBUFFERED=1

# Narrative-synthesis defaults — Together AI serverless.
# Together is the fleet default per CLAUDE.md primary directive 5
# ("Anthropic forbidden as LLM provider for the nclawzero/zeroclaw stack").
# Model: google/gemma-4-31B-it (cheapest serverless model with strong
# narrative reasoning as of 2026-05). MiniMaxAI/MiniMax-M2.7 was removed
# from Together's serverless tier in 2026-05 and now requires a paid
# dedicated endpoint. Cheaper than Gemini Pro by a wide margin. If the
# operator wants to point at Google instead, they should set
# INVESTORCLAW_NARRATIVE_MODEL to a *Flash* model — Pro is the expensive
# one (Pro: $1.25/M input, $5/M output; Flash: $0.10/$0.40).
# Last cost incident: $70/night API bill from accidental Gemini Pro use,
# 2026-04-30; documented in feedback_llm_provider_cost_policy.md.
#
# The bridge resolves $TOGETHER_API_KEY (read from /data/keys.env) into
# INVESTORCLAW_NARRATIVE_API_KEY at subprocess-spawn time, so operators
# only need to set the per-provider key file once.
ENV INVESTORCLAW_NARRATIVE_PROVIDER=openai_compat
ENV INVESTORCLAW_NARRATIVE_ENDPOINT=https://api.together.xyz/v1
ENV INVESTORCLAW_NARRATIVE_MODEL=google/gemma-4-31B-it
# Consultation tier (deeper analysis, optional — used by select code paths).
# Disabled by default; falls back to the narrative endpoint when invoked.
# Operators with their own local-LLM endpoint set
#   INVESTORCLAW_CONSULTATION_ENABLED=true
#   INVESTORCLAW_CONSULTATION_ENDPOINT=<their endpoint>
#   INVESTORCLAW_CONSULTATION_MODEL=<their model>
# at runtime (env / compose / .env). NOT baked into the image — defaulting
# the endpoint to a private LAN address would leak fleet topology to public
# users and never resolve in their network.
ENV INVESTORCLAW_CONSULTATION_ENABLED=false
# Hybrid / local-first override examples (NOT defaults):
#   INVESTORCLAW_NARRATIVE_ENDPOINT=http://localhost:11434/v1     # local Ollama
#   INVESTORCLAW_NARRATIVE_MODEL=gemma4:e4b
#   INVESTORCLAW_CONSULTATION_ENDPOINT=http://localhost:8080      # local vLLM

EXPOSE 8090 8092

# Healthcheck — overridden by compose for finer control
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=5 \
    CMD curl -sf http://127.0.0.1:8090/healthz || exit 1

# Entry point: bridge serves both MCP-HTTP (port 8090) and the dashboard (8092)
# in one process. Bridge code reads /data/bundle.json + /data/keys.env at start.
ENTRYPOINT ["/opt/ic-engine/.venv/bin/python", "-m", "investorclaw_bridge.serve"]

# Build-time labels (OCI image-spec)
LABEL org.opencontainers.image.title="InvestorClaw ic-engine"
LABEL org.opencontainers.image.description="Portfolio analysis service exposing MCP-HTTP at :8090 and a dashboard at :8092. Pairs with mnemos-os/mnemos-rs over compose."
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.source="https://github.com/argonautsystems/ic-engine"
LABEL org.opencontainers.image.documentation="https://investorclaw.app"
LABEL org.opencontainers.image.version="${VERSION}"
