# Third-party components

The Node dependency names, versions and license identifiers are recorded in [package-lock.json](package-lock.json). The installation resolves packages from their registries. The source tree does not vendor the Node dependencies.

| Component | Upstream terms and role |
| --- | --- |
| Codex CLI | [Official upstream](https://github.com/openai/codex). Install it separately and configure your own access for generation. |
| Isaac Sim, Omniverse Kit and assets | [NVIDIA installation and licensing](https://developer.nvidia.com/isaac/sim/). Install the separate simulator and review the terms for Kit and each supplied asset. |
| Legacy cuRobo | [License at the pinned revision](https://github.com/NVlabs/curobo/blob/ebb71702f3f70e767f40fd8e050674af0288abe8/LICENSE). The optional numerical planner depends on this research/evaluation release. |
| NVIDIA AutoData | The Warp namespace compatibility change in `runtime/motion.py` and `tests/probe_motion_backend.py` is adapted from the [pinned upstream source](https://github.com/isaac-sim/AutoData/blob/953cd37778abd79f1c4621aa20183b0c5f9096a9/autodata_interfaces/motion_planners/curobo/curobo_planner.py#L35-L38), licensed under Apache 2.0. |
| Data format tools | The upstream projects linked in [delivery-requirements.txt](examples/delivery-requirements.txt) and the [delivery guide](skills/scene-generation/references/delivery.md) have separate terms. |
