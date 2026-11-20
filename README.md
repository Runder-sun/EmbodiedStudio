# EmbodiedStudio

EmbodiedStudio turns a natural-language task into an executable, scoreable robot simulation. A coding agent authors the scene, task, robot behavior and rubric. The workbench shows the frozen 3D scene, build history, and recorded robot runs. The environment can be reused for a new policy or for evaluation against the same rubric.

## Start the workbench

Install [Node.js](https://nodejs.org/) 22 or later and open a terminal in this repository.

```sh
npm ci
npm start
```

Open <http://127.0.0.1:8765>. The workbench and 3D start screen can open before a compute host is configured. The server binds to loopback. On macOS, `SceneAgent.command` provides the same local launch.

For agent-driven authoring, install and sign in to the [Codex CLI](https://developers.openai.com/codex/cli/) as the user running Studio. The workbench starts that user's native Codex app server and registers the repository's scene-generation skill and local MCP tools. Follow the [compute setup guide](docs/compute.md) for a separate SSH server or for a Linux RTX workstation serving both roles. Follow the [asset setup guide](docs/assets.md) to import individual models or make your own library searchable.

## Generate and inspect a task

1. Create a workspace and enter a scene and task request in the workbench.
2. Select **Generate** and choose the requested outputs. The agent selects resources, writes the project source, builds a revision, observes the simulation, and checks actual executions.
3. Open the scene tree, 3D view, execution recordings, or source files. Inspect the delivery receipt for what was requested, what was accepted, and which attempts were incomplete.

Build and preview share a frozen environment revision. Behavior programs can be frozen independently for additional data collection on that environment. A generated rubric reads the declared task facts and can be used to score another policy.

| Output | Contents |
| --- | --- |
| Construction history | Scene-program changes, tool evidence and linked build revisions |
| Execution data | Recorded observations, actions, camera frames, scores and episode outcomes |
| Evaluation environment | Frozen USD scene, task bindings, rubric source and policy interface |

The [authoring API](skills/scene-generation/references/api.md) covers the scene and task programs. See the [delivery guide](skills/scene-generation/references/delivery.md) for output contracts, [robot resources](skills/scene-generation/references/robots.md) for model selection, and [consumer entry points](examples/) for reading or evaluating a delivered project.

## Verification

```sh
npm test
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-test.txt
python -m tests.run_suite tests.test_runtime tests.test_planned_motion tests.test_records
```

These Python checks use standalone OpenUSD and system fixtures. For the full suite, run `python -m tests.run_suite` from a Linux Isaac Sim environment that also provides the test dependencies. Use the SDK's own OpenUSD and PhysX plugins in that environment. GPU rendering, native robot execution and task-level results require separate validation on a compute host.

## Source and dependencies

This source release contains the workbench, runtime, scene-generation skill and consumer code. See the [source license](LICENSE) and [third-party components](THIRD_PARTY.md). Research scenes, benchmark archives, third-party robot assets, recordings and training data have independent distribution and licensing requirements.
