# EmbodiedStudio

[中文](README.zh-CN.md)

EmbodiedStudio is a browser workbench for building robot tasks in Isaac Sim. Describe a task, and Studio uses your Codex CLI to write the scene and the code that scores it. You can inspect the 3D scene, play back a recorded robot run, and see how that run was scored.

## Get started

Install [Node.js](https://nodejs.org/) 22 or later. From the repository directory, run:

```sh
npm ci
npm start
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). On a fresh install, Studio opens **Workspace settings**. The toolbar has a language picker and a **Settings** button to reopen the wizard.

You can open the workbench with just Node.js. To generate a scene, sign in to your [Codex CLI](https://developers.openai.com/codex/cli/) on the machine running Studio and connect a simulation host:

| Your setup | Where simulation runs |
| --- | --- |
| One x86-64 Linux PC with an NVIDIA RTX GPU | On the same PC. Choose **Workspace host** in Settings. |
| A Mac or a separate workstation | On your x86-64 Linux RTX server. Choose **SSH compute server** in Settings. |

If you already have Isaac Sim, enter its Python path in Settings. Otherwise, save a draft and select **Isaac Sim environment** in the installer. Once the environment is ready, check the compute host, choose a GPU, save and activate the draft, and restart Studio. Studio uses Isaac Sim `6.0.0.1` with Python 3.12. [Setup walkthrough](docs/compute.md)

## Try a tabletop task

Create a project and paste this into the prompt box:

> Set up a tabletop with a red block and a shallow tray. Define a task to put the block inside the tray.

Choose **Scene and evaluation environment** to start. Open the 3D view and scene tree after the build. For robot runs and recorded data, choose **Complete generation**. You can supply a robot package or name a model for Codex to look for. The robot also needs a controller or policy that can perform the task.

The source package contains the workbench and runtime. You can give Studio a public GLB link, bring a local USD or URDF package, or register a library you already use. [Asset setup](docs/assets.md)

## Use the outputs

| Output | What you can do with it |
| --- | --- |
| Construction history | Follow a scene change back to the source edits and build that produced it. |
| Robot execution data | Read recorded observations, actions, camera frames and task scores. |
| Evaluation environment | Run a compatible policy against the same frozen scene and scoring program. |

See the [authoring API](skills/scene-generation/references/api.md) and the [delivery guide](skills/scene-generation/references/delivery.md) for the file formats and interfaces. Both references are in Chinese.

## Develop

Run `npm test` for the workbench and tool tests.

<details>
<summary>Python tests</summary>

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-test.txt
python -m tests.run_suite tests.test_runtime tests.test_planned_motion tests.test_records
```

For the full suite, run `python -m tests.run_suite` in a Linux Isaac Sim environment with the test dependencies and the SDK's OpenUSD and PhysX plugins. Validate rendering and robot execution separately on the compute host.

</details>

## License

See the [source license](LICENSE) and [third-party terms](THIRD_PARTY.md). Asset, model and dataset licenses depend on their sources.
