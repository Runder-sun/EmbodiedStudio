# Configure the simulation host

The workbench runs on your workstation. It can dispatch simulation locally on an x86-64 Linux RTX machine or over SSH to one. The runtime targets Isaac Sim `6.0.0.1`, Python 3.12 and PhysX. Set up the NVIDIA driver and Linux bubblewrap first. See the [NVIDIA installation guide](https://docs.isaacsim.omniverse.nvidia.com/6.0.0/installation/install_python.html) and [driver requirements](https://docs.isaacsim.omniverse.nvidia.com/6.0.0/installation/requirements.html).

## Choose the deployment

| Setup | Where each part runs |
| --- | --- |
| Workstation and your own GPU server | Run the workbench and your signed-in Codex CLI on the workstation. Run Isaac Sim, the asset library and jobs on your Linux RTX server. |
| One Linux RTX workstation | Install both parts on that workstation and select **Workspace host**. |
| macOS workstation | Run the workbench and Codex on the Mac. Use a Linux RTX host for simulation, rendering and robot execution. |

## Set up from the browser

1. Run `npm ci` and `npm start`. The first launch opens **Workspace settings** automatically. Open it later through **Settings** in the toolbar.
2. Choose a workspace and compute location. For SSH, add your host alias to `~/.ssh/config`, check its host key, and enable key login. Use **Check SSH connection** and **Check workspace and Codex**.
3. Fill in an existing compute Python path and run **Check compute and GPUs**. Select the intended GPU from the measured list. Save a draft. To install a missing Isaac Sim environment, select the draft, choose **Isaac Sim environment**, supply an unused directory and the host's Python 3.12 path, and acknowledge the upstream license. Take the new interpreter from the completed receipt and run the compute check again.
4. To install a separate Codex CLI, select **Codex CLI** and enter a version and a new destination. Take the installed entry from the receipt and sign in with that CLI on the Studio host.
5. Save the final draft, activate it, and restart Studio when idle. Read the current port in the settings if you changed it. The installation receipts persist across restarts.

The checks read connections, versions, GPU memory and catalogs. The **Verify new environment** action uses a selected GPU, a peak memory reservation and an existing frozen Franka package for the numerical and Isaac Sim checks. Run a separate representative task to verify the robots and task conditions you intend to use.

## Configure manually in the terminal

1. Set up a host alias in `~/.ssh/config` and verify `ssh -o BatchMode=yes simulation-host true`. Use the SSH account that will own the data and jobs.
2. Install Isaac Sim in an isolated Python environment on that host. Choose a writable project data root with room for immutable scene revisions and video. Optional pre-existing asset catalogs can be referenced through a read-only `legacyRoot`. An unset `legacyRoot` resolves to `asset-library` inside the project data root.
3. Copy `studio.config.example.json` to `studio.config.json` and edit the SSH alias and absolute paths for `remoteRoot` and `remotePython`. Set `codexBinary` if the executable is outside the workstation's command search path. Set `codexModel` only if your account uses an explicit model.
4. Query the compute GPU with `nvidia-smi --query-gpu=index,uuid,memory.free --format=csv`. For rendering and execution, set `remoteGpu` to an object with the measured integer `index` and the corresponding `uuid`. Use a GPU that has been assigned to your workload. On shared hosts, check free memory and coordinate reservations with other users before adding jobs.
5. Restart the workbench and run `npm run scene -- health`. The health command verifies the SSH, Python and worker dispatch path. Build a scene and run a recorded robot task to verify the simulator and your robot resources.

For a single Linux workstation, set `runtimeMode` to `local` and use local absolute paths as `remoteRoot` and `remotePython`. For a separate compute host, set `runtimeMode` to `ssh` and use absolute paths on the SSH host.

Install and sign in to Codex on the machine that starts Studio. Use the same OS account for both. Run `codex login status` and `codex app-server --help` to check the installation. Studio launches the CLI through `codexBinary`, defaulting to `codex` on the command search path. An omitted `codexModel` uses the CLI's default. The released workbench connects to the native app server and uses its skill, local tool and subagent capabilities.

## Optional runtime components

- **Robot and scene assets.** Follow the [asset setup guide](assets.md) for individual imports, a searchable library and robot packages.
- **Numerical planning.** The included cuRobo integration expects the exact revision in [requirements-motion.txt](../requirements-motion.txt), the SDK Warp distribution, and a verified local `runtime/motion-dependencies.json`. This file identifies the interpreter, actual loaded library paths, dependency byte hashes and ABI. The workbench includes it in the frozen runtime packet when present. Check planning capability in `health` before selecting this backend. The lab's machine-specific profile is excluded. The [native compatibility probe](../tests/probe_motion_backend.py) can characterize a prepared local profile. It records the compatibility checks and source identity on the target compute host.
- **Training format readers.** [examples/delivery-requirements.txt](../examples/delivery-requirements.txt) documents the separate Linux CPython 3.12 reader environment for LeRobot and HDF5. Follow the [delivery contract](../skills/scene-generation/references/delivery.md) for the required formats and consumer checks. Preserve existing frozen runtime snapshots when reusing old artifacts.
