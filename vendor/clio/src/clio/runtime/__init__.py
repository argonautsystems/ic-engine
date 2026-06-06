# Copyright 2026 clio Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""clio.runtime — AI-aware execution: hardware detection, model cache, GPU memory.

Subsystems:

    hardware      CPU/GPU/memory probing across darwin + linux + WSL2. Used by
                  extraction subsystems to pick a device for sentence-transformer
                  encoding, vision-LLM rasterization, and similar AI workloads.
                  Lifted Phase 1.5a from ic-engine setup/hardware.py.

    model_cache   sentence-transformers + vision-model load + cache wrapper.
                  (Phase 1.5b/c.)

    gpu_memory    GPU memory budgeter for concurrent model loads. (Phase 1.5b/c,
                  modeled on the CERBERUS_GPU_STRATEGY pattern.)
"""
