# Producing standard deliverables

Use this guide only when the current generation declares `deliveryContract='producer-exports-v1'`. Obtain the current request, identities, frozen revisions, episodes, and actual tool locations from `project_inspect`. Older jobs keep their original contracts. The author and independent reviewer remain responsible for requirements and quality.

## Deliver the requested outputs

### Explicit raw-video-only recording

The user's latest instruction and `project_inspect.recordOnlyVideos=true` override historical producer-exports-v1, review, and formal delivery requirements. This mode requires `videoOnly=true` and `requiredOutputs=["demonstrations"]`. `recordOnlyTaskIds` fixes the actual tasks. For each, retain one successful video of the real robot completing the full original task from its original initial state. Reuse existing successful recordings. Report only the original video reference, taskId, revision, and episodeId. Do not copy, re-encode, package, create a manifest, export training data, summon `scene_quality_review`, or run independent review, consumers, or `project_deliver` checks. Preserve the scene, physics, completion conditions, sources, executions, failures, and interruptions. Recording success does not imply review or format validation. The export and review rules below do not apply to this explicit mode. Setting only `videoOnly=true` retains the usual workflow.

### Complete execution videos only

With `project_inspect.videoOnly=true`, the demonstrations product comprises only requested complete robot execution videos. Omit LeRobot, HDF5, other training data, and unrequested products. Real control, evidence of completing the entire original task, and independent review still apply. A fixture, edited sequence, or correct final pose cannot establish success.
For new recording requests without `environmentRefs`, freeze the required environment in the project. Explicitly selected environments remain read-only. Keep raw environment and generation records without exporting an extra environment product or training trace.
When the new recording has no initial environment reference, bind the review and product manifest to the delivery interface's derived `environmentRefs=[{revision: currentDeliveryRevision}]` and `requiredOutputs=["demonstrations"]`. Do not propagate an undefined initial reference into the final manifest.

Add `videoOnly:true` at the top level of the existing product manifest and review binding as required by the interface. `products.robot` is a nonempty list. Each item contains exactly two relative paths, such as `{video:"T02.mp4",mapping:"T02-video.json"}`. Each mapping's `episodes` has one entry preserving the selected `{episodeId,taskId,revision?,use}` and adding `sourceVideo:"episodes/<episodeId>/<actual-original-relative-video-path>"`, relative to the remote project directory. Copy the complete original video. The consumer checks original bytes, full decoding, and execution provenance. Crops, re-encodes, substitutions, or other projects' files cannot represent that execution. The independent reviewer confirms framing and full task success.

Export under `delivery/`. List the video's and mapping's actual byte lengths and digests in `files`, bind the selected executions, and request `project_deliver`. Video names need not encode the task type. Reuse the episode for format repairs. Omitting `videoOnly` retains the dual training-format contract.

Read `requiredOutputs/environmentRefs`. For generation, export rawTrace, ATIF, and training JSONL. For demonstrations with videoOnly omitted, export same-source LeRobot and HDF5. For evaluation, export the frozen environment, tasks, and scoring. Omit unrequested keys from products. Do not invent directories or assert a pass. Keep the actual generation process in the robot stage without re-exporting the first product unless requested.

Under the scoped contract, manifest `binding` must match the host's `requiredOutputs/environmentRefs`. Include the actual `{episodeId,taskId,revision,use}` for every entry in `episodes`. Include the same revision in each robot group's `mapping.episodes`. Do not assign the latest revision to older episodes. Two formats, multiple cameras, or rerenders of a single physical episode count as one source trajectory. An environment package can hold multiple selected revisions from the same project within one data root. Include the manifest, resources, runtime, and entry point for every selected revision.

Retain the independent collection's behaviorRef and code closure with the robot data. When a robot installation exists, preserve each episode's robotSetupRef/robotSetupSha256, configuration and model dependency closure, combined I/O, and recording declarations. Use the same frozen installation for independent evaluation. Preserve relative paths under the data root without depending on the author's absolute project paths. A scene package may omit robot and expert entry points. The workspace sourceHash binds the unchanged workspace during export and review. Environment and independent behavior identities reference their respective frozen closures. Changing behavior or format does not require rebuilding an older environment.

## Responsibilities and workspace

Studio preserves public events, source snapshots, frozen scenes, per-cycle physics measurements, and provenance, and supplies official format tools and the delivery interface. Author and run export, validation, launch, and packaging scripts in the project's `delivery/` and the remote `projects/<projectId>/delivery/<run>/`. These are repairable project products. Treat raw records and tool environments as read-only.

Establish one mapping from selected inputs to intended uses and derive both robot formats from it. Read them back with standard readers. The independent reviewer checks semantics, permissions, and the original request. Repair export scripts, field mappings, paths, and formats within the project, then re-export from the same real episode. Use `generation_wait_system` only for an actual failure in trusted capture, the official reader environment, or system publication.

## Three products

| Product | Output and use |
| --- | --- |
| LLM trajectory | Original public events and references, `ATIF-v1.7`, and training JSONL. Recover actual tool and code decisions, including failures and gaps. |
| Robot trajectory | Same-source LeRobot v3 and HDF5 readable by robomimic's `SequenceDataset`. Keep the original episode as the reference. |
| Simulation evaluation environment | Same-revision scene, assets, robot where applicable, tasks, rubrics, cases, observation/action contract, obtainable runtime dependencies, unmodified evaluation entry point, and instructions. |

Format readability does not establish scene quality, task success, or learning performance. If the request requires robot completion or successful demonstrations, obtain real successful execution and internal review. Preserve failure and truncation labels.

### LLM trajectory

Derive the product from a complete event prefix of the original public trace. Lock the prefix's byte length and SHA256 in the manifest. Retain the original regardless of training length. Each training record needs nonempty `sourceEventIds` identifying actual events in that prefix, and either `messages` or `prompt`/`completion`.

`project_inspect.recording` locates the workbench trace, its mirror path relative to the simulation data root, the `.trace/sources` and `.trace/inputs` directories, and the latest mirror status. This provides discovery only. `project_inspect` still records its normal source snapshot and call. Copy and package original read-only archives under `delivery/`. Do not reconstruct history from today's files or excerpts printed by a tool.

References are part of the generation product. Scan structured `snapshotRef/inputRef/beforeSourceRef/afterSourceRef` fields in the locked raw prefix, including references from other generation turns. Include historical sources named by `source_snapshot.previousSourceHash`. Follow input-archive references such as `instructionsRef` transitively. Use actual path fields only. Do not infer references from code, commands, prose, or diffs. Copy the original JSON bytes under `products.generation.archiveRoot`, preserving `.trace/sources/<hash>.json` and `.trace/inputs/<hash>.json`, and list each file in `files`. Resolve same-project `projects/<projectId>/.trace/...` references to the same layout. Do not cross projects, use symlinks, or guess aliases.

A sources filename and `sourceHash` identify the compact JSON digest of the `sourceFiles` mapping. Preserve key order, Unicode including Chinese text, and source whitespace. This differs from the archive file's byte digest. An inputs filename and `inputHash` use SHA256 of the original archive bytes. Separately, `files[].sha256/bytes` locks the actual bytes, including JSON whitespace, for each archived file. At freeze, the consumer compares against the original project byte for byte. After freeze it revalidates from the archived originals. A reserialized equivalent document cannot replace the original.

Require `archiveRoot` only when the raw prefix references source or input archives. Omit it otherwise. `archiveClosure.complete` certifies closure of source, input, and transitively referenced instruction archives for that prefix only. It does not cover hidden context, the complete native session, or every media or environment reference. Later events and archives belong to later exports. Include every archive already referenced at the cutoff. A robot delivery without generation need not add the first product. Older bindings omitting `requiredOutputs` still default to all three.

- Pair tool calls and returns by real call ID. Keep `tool_call_id` in training messages or an equivalent association in another template's mapping. Merge streamed deltas for the same native item and distinguish a then-truncated return from the full background report. If a native item records name, arguments, and result without a separate model call_id, document an association using its stable item identity. Retain actual tool actions without claiming unknown model fields. Include public commands, edits, and consequences in ATIF and training projections. Preserve unrecognized public edits and disclose gaps. A diff or snapshot without the call records a change only. Do not infer the assistant's call or intent.
- Retain sourced inputs and histories separately for parent and child sessions. Link them only through actual delegation. Mark missing delegation or message input unknown. Do not substitute the root request for a child's input as complete SFT context or merge concurrent messages into one agent's history.
- Sourced public assistant messages and actual tool calls can be assistant targets. Tool returns are observations. Activity notifications, wait statuses, and system/tool observations must not become fabricated assistant speech. Inputs contain only facts available before the action. Exclude subsequent code and outcomes, private reasoning, and assistant analysis from public ATIF and training views. Leave model, complete context, token/logprob, and finish time unknown when unsupported.
- Preserve failures in the raw trace. Keep the relationships between failures, consequences, repairs, and validation in training. Distinguish targets, observations, and weights by use. Waits and failures are not automatically equal-weight positive examples. Retain genuine failing reproducers and useful unsuccessful attempts.
- Split at complete decisions or failure–repair windows under the actual target model's tokenizer budget. Record coverage and truncation. Fixed step counts cannot replace token budgeting. Preserve JSON, paired calls/returns, and message structure. Keep a project's branches and repair fragments in the same data split. Separate loss masks for assistant targets from prior assistant/user/tool observations. When the target model is unspecified, deliver reusable messages and disclose the absence of tokenizer-specific budgeting and token masks. If a whole call/return still exceeds budget, retain the original and disclose omitted coverage.
- The reviewer traces valuable decisions or failure–repair chains from raw events through edits, tool returns, consequences, and applicable child work to their coverage in ATIF and training. Disclose necessary gaps. JSON parsing and official readers cannot substitute for semantic verification. Never invent calls, child inputs, or successes to complete a chain.

The eight Harbor task-authoring fields are optional method summaries. They do not replace the interaction history or impose fixed durations, two models, fixed rounds, or a system-assigned score.

Use the [official Harbor ATIF models and readers](https://docs.harborframework.com/agents/atif). Explicitly set `schema_version='ATIF-v1.7'` independently of library defaults. Number steps consecutively from 1. Match each `observation.results[].source_call_id` to that step's actual `tool_call_id`. Use official `Trajectory.model_validate()` and `TrajectoryValidator.validate(Path(...))` and check local image references. Store custom provenance and gaps in `extra`.

### Dual robot formats

Read the frozen I/O, Task, episode, and media frame tables first. Full qualification with `examples/read_demonstrations.py` requires a matching SDK environment. CPU format scripts read the same verified raw files and digests. They cannot revalidate physics by skipping SDK checks.

Document source episodes and uses, feature provenance, port and joint order, shape/dtype, units, frames, command semantics, control clock, camera permissions, source frame times, and annotation provenance. Group different dimensions or semantics separately. Avoid brand whitelists and implicit zero padding.

- `executedAction` is the drive target actually issued and read back. Store measured q/dq separately. Resolve omitted persistent ports from the episode's `nativeInitialTargets` and preceding commands. If an older record lacks initial values, leave them unknown or report the profile gap. Never substitute build probes or zero. Do not silently fill transient effort.
- Each row represents the real control cycle `observation_t → command_t → observation_next`. Store higher-rate physics samples separately. When holding the latest available lower-rate image, retain its source frame ID, actual sample time, and age. Use the same mapping in both formats. No future frames or fabricated black images.
- Distinguish policy-permitted streams and `policyCameraIds` from observer cameras and scoring truth. Training a new policy on additional views needs an explicit new observation contract. Preserve per-frame intrinsics, extrinsics, and simulated-sensor or external-observer provenance.
- Task text and generator phase annotations may accompany data. Keep rubric labels as `generated_interpretation` with cycle and source. They are neither human ground truth nor sensor events. Report unsupported depth or pixel semantics when not captured.
- Keep `terminated`, `truncated`, and `success` separate. Never fill unknown reward with zero. Explicitly omit unknown supervision unused for behavior cloning. Mark uses requiring it incomplete. Preserve full natural episodes, keep training windows within episodes, mask boundary padding, and compute statistics from the training split only.

Use the pinned official LeRobot methods `LeRobotDataset.create(...)`, `add_frame(...)`, `save_episode()`, and `finalize()`. After `finalize()`, open a fresh local reader and read actual numbers and video. Derive feature shapes from the actual robot and include real features and `task` in every frame. The writer makes `timestamp/frame_index` from fps. Store actual capture times in source-clock features or mappings and distinguish them from container time. See the [official v3 guide](https://huggingface.co/docs/lerobot/lerobot-dataset-v3).

Derive `features`, `fps`, and frames in this example from one mapping of the actual data. Studio does not prescribe a fixed robot vector.

```python
from lerobot.configs import RGBEncoderConfig

writer = LeRobotDataset.create(
    repo_id, root=lerobot_root, fps=fps, features=features,
    robot_type=actual_robot_type, use_videos=True, video_backend='pyav',
    encoder_threads=1, rgb_encoder=RGBEncoderConfig(vcodec='h264'),
)
try:
    for episode in canonical_episodes:
        for frame in episode:
            writer.add_frame(frame.copy())  # Include actual features and task.
        writer.save_episode(parallel_encoding=False)
finally:
    writer.finalize()
loaded = LeRobotDataset(repo_id, root=lerobot_root,
                       download_videos=False, video_backend='pyav')
sample = loaded[0]  # Read numbers and pixels and compare against the source mapping.
```

Choose encoding parameters for the intended access pattern. The single thread illustrates a small example and does not limit other workloads.

Check dtype and shape after readback. Default conversions may reduce floating-point time precision or return scalar tensors for singleton features. Preserve original integer ticks, timeOrigin, and rate when possible. State the precision of derived integer timestamps. Restore a singleton to its declared shape only when the reader squeezed it. Do not invent values.

Use `h5py` to write `data/demo_i/obs/<key>`, `next_obs/<key>`, `actions`, and `num_samples`. Store actual uint8 pixels, not video paths. Read with the pinned official `robomimic.utils.dataset.SequenceDataset`. Initialize low_dim/RGB with `ObsUtils.initialize_obs_modality_mapping_from_dict()` and pass actual `obs_keys`, `action_keys`, and `action_config`. Set `lang=None` to avoid loading another language model. Task text and provenance may be extension metadata. Claim only support for its offline training layout. Never fabricate MuJoCo `states/model_file` or an environment type. See the [official Dataset API](https://robomimic.github.io/docs/modules/datasets.html).

```python
import robomimic.utils.obs_utils as ObsUtils

ObsUtils.initialize_obs_utils_with_obs_specs({'obs': modality_keys})
reader = SequenceDataset(
    hdf5_path=str(hdf5_path), obs_keys=obs_keys,
    action_keys=['actions'], dataset_keys=[],
    action_config={'actions': {'normalization': None}},
    seq_length=window_length, get_pad_mask=True,
    hdf5_cache_mode='low_dim', load_next_obs=True, lang=None,
)
try:
    sample = reader[0]
finally:
    reader.close_and_delete_hdf5_handle()
```

Compare both readers against the same episode, actions, states, source clock, frames, uses, and labels. Report pixel differences between lossy decoded MP4 and original PNG. Their compressed file digests need not match. Use one mapping and count both exports as one collection.

### Simulation evaluation directory

Preserve the existing relative reference layout under the environment root. Copy only necessary public files into the manifest.

```text
<environment.root>/
  projects/<projectId>/revisions/<revision>/manifest.json
  projects/<projectId>/revisions/<revision>/...same-revision sources, stage, and resource closure
  projects/<projectId>/episodes/<selectedEpisode>/...required records and media
  runtime/<runtimeCodeHash>/runtime/...frozen kernel
  jobs/<publicationJob>/{status,result}.json
```

Include the required terminal publication files to preserve signed provenance. Do not archive entire projects or jobs. Exclude PIDs, process-control files, private scoring state, isolation probes, credentials, caches, and other projects. Provide exact acquisition and installation instructions for resources whose licenses prohibit redistribution. Never extract assets from encrypted BEHAVIOR data.

Copy Studio's `examples/evaluate.py` unchanged as `entrypoint`. Document `--root <environment.root>`, the manifest, compatible candidate policy, and explicit GPU selection in the README. Preserve the frozen worker, actual dependencies, and interpreter/ABI checks. Claim only the obtainable Linux/NVIDIA runtime. Document dependency installation or image/prefix acquisition, versions, and licenses. Changing an absolute path or removing checks does not establish portability.

Document the task, entity bindings, permitted observations/actions, initial state/seed, duration, termination/truncation, rubrics, cases, and result meanings in the protocol. Declare a fixed instance when the seed is fixed. Do not invent randomization or generalization splits. The formal consumer loads from the new root and checks the interface for at most one control cycle. It does not certify repeated success on the whole task.

## Official CPU tools

Get the data root from the host project instructions. This deployment uses `envs/delivery-readers-v1/bin/python` under that root and pins official sources under `tools/delivery-readers-v1/`. `project_inspect.runtimeExecution` identifies the simulation SDK. Use the separate CPU interpreter for formats. `environment.json` records dependencies and provenance. Keep the shared tools and SDK unchanged.

LeRobot and h5py are installed libraries. The official Harbor wheel and pinned robomimic source supply only the required models/readers, without the full Harbor scheduler or robomimic simulation/training dependencies. Add the two pinned paths for the actual data root in your exporter and use the official APIs.

```python
import sys
from pathlib import Path

tools = Path(data_root) / 'tools/delivery-readers-v1'
sys.path.insert(0, str(tools / 'harbor-0.24.0-py3-none-any.whl'))
sys.path.insert(0, str(tools / 'robomimic-d309eaecc18acf4152a830a895a6984b8ac71b05'))
from harbor.models.trajectories import Trajectory
from harbor.utils.trajectory_validator import TrajectoryValidator
from lerobot.datasets.lerobot_dataset import LeRobotDataset
from robomimic.utils.dataset import SequenceDataset
import h5py
```

Use `root=`, an explicit video backend, and offline files. Do not upload to the Hub. Diagnose errors against the pinned API and original error, then fix the project script. Report a system defect only for corrupt tool sources or a pinned dependency that itself cannot be imported.

## Delivery manifest

Write the remote manifest at `projects/<projectId>/delivery/<run>/manifest.json`. Product paths are relative to its directory. List every real regular file's `path/bytes/sha256` in `files`. Cover complete directories through their files. Omit the manifest itself. `project_deliver` locks its digest separately to avoid self-reference.

```json
{
  "schema": "sceneagent-produced-artifacts-v1",
  "binding": {
    "projectId": "current-project", "generationId": "current-generation",
    "requirementVersion": 1, "requestHash": "actual-request-hash",
    "sourceHash": "reviewed-workspace-source-hash", "revision": "frozen-revision",
    "requiredOutputs": ["generation", "evaluation"],
    "environmentRefs": [{"revision": "frozen-revision"}]
  },
  "episodes": [],
  "trace": {"bytes": 123, "sha256": "actual-public-trace-prefix-digest"},
  "files": [{"path": "actual-relative-file", "bytes": 123, "sha256": "actual-file-digest"}],
  "products": {
    "generation": {"rawTrace": "generation/raw.jsonl", "archiveRoot": "generation/originals", "atif": "generation/trajectory.json", "training": "generation/train.jsonl"},
    "environment": {"root": "environment/data", "entrypoint": "environment/evaluate.py", "readme": "environment/README.md", "runtime": "environment/runtime.json"}
  }
}
```

This scene-only example requests generation and evaluation. Compute identities, counts, and digests from the inputs. Never submit example values. For demonstrations, use the actual `binding.requiredOutputs/environmentRefs`, add the real dual-format `products.robot` entry, and populate `episodes` with `{episodeId,taskId,revision,use}` matching the review selection. Keep verification distinct from demonstrations. A `use` value does not establish success.

Each group's `mapping.json` contains `episodes=[{episodeId,revision,lerobotEpisodeIndex,hdf5Demo,steps}]` and `hdf5Obs={state:'low_dim',front:'rgb',...}`. Document actual actions, clock, permissions, cameras, annotations, and splits. Groups must be disjoint and collectively cover exactly the selected episodes.

Read back the outputs and compute the manifest digest before review. The reviewer includes the identical `producedArtifacts={manifestRef,sha256}` at the top level of the final v2 receipt. After current-revision re-review passes, submit the same pointer to the existing `project_deliver` with a new requestId. Changed exports or manifests require review of the new digest. Missing formats or read failures remain incomplete. Repair the export and reapply, retaining original executions and previous failures. Announce completion only after the authoritative receipt returns accepted.
