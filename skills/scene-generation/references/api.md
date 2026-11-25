# SceneAgent Studio project API

This reference describes the native extension interface. The actual contract is the current `project_inspect.io`, frozen manifest, and running kernel. Older v4/v5 packages use their own frozen workers and interpreters. Do not rebuild them from this reference. Verify import, field validation, numerical planning, and actual task success separately.

## Project and scene tree

Depending on scope, the project contains `scene.py/tasks.py/rubrics.py` plus required frozen helpers and verification sources. The robot-data stage also needs behavior and public `*.robot.json` configuration as applicable. `World()` defaults to native mode with no robot. Older packages retain their workers. Building, controlling, and scoring run in separate processes. Production runs retain no build-time Python callbacks. `scene_build` requires a nonempty Task list and generated rubric. There is no early task-free preview mode. Never fabricate a task or an always-true score. Express new models, policies, and physics already available in the SDK through project configuration, sources, and resources without new system categories, adapters, or task tools.

```python
# scene.py: world.stage is the only stage for this native build.
def build(world):
    from pxr import UsdGeom
    world.space('room')
    UsdGeom.Xform.Define(world.stage, '/World/Entity')
    world.bind('entity', '/World/Entity', owner='room', purpose='Entity requested by the user')
    # Use the pinned SDK for actual geometry, physics, and materials, then declare actual I/O.

# tasks.py: return a list of Task instances.
def build(world):
    ...

# expert.py: robot data only. Receives a controlled message proxy, never a live SDK handle.
def run(env, task):
    contract = env.open()
    observation = env.observe()
    # Compute named actions from permitted observations and execute them through env.step.

# rubrics.py: private generated scoring. Do not write actions or host results.
def evaluate(task, facts, state):
    ...
```

`world.stage` starts with `/World`, metric units, and Z up. `world.resources[resourceRef]` returns a frozen entry's relative path. The build's working directory is the output package root, suitable for native USD references and official importers. Prepare all meshes, materials, configurations, weights, and their dependencies, and declare them in `scene_build.resourceRefs`. Do not depend on live absolute paths or downloads at execution time.

World does not create the physics scene automatically. Native build verifies actual I/O. The final metric, Z-up stage must contain one actual `UsdPhysics.Scene` whose `PhysxSchema.PhysxSceneAPI` step frequency matches `world.io.clock.physicsRateHz`. Inspect the composed stage before creating or reusing the scene. Do not add a scene per imported asset. This requirement does not require loading a robot.

World retains stable IDs, semantic hierarchy, requirements, and native prim bindings. Frozen stage geometry, joints, drives, and physical attributes are authoritative. `owner` describes membership and `frame` the coordinate reference. Declare support, containment, and connections independently. Changing membership alone neither moves nor welds an object. `world.bind(id, prim_path, owner=..., frame=..., purpose=...)` binds an actual prim under `/World/...`. Read its pose from the native reader. Do not infer a dynamic pose from World.position or duplicate physical state.

Convenience functions `space/box/cylinder/asset/light` are available alongside the native SDK. Box and cylinder take metric sizes, at, and wxyz quaternions. Assets use the prepared path/upAxis and uniform scaling. `collision_mode=box|mesh|convex_decomposition` applies to these convenience geometries. Verify actual accuracy for static mesh or dynamic convex decomposition. Native builds can use other schemas from the pinned SDK. Load robot configurations with `world.robot` below. Reserve `role='enclosure'` for fixed architectural elements. Preserve moving objects, walls, and collisions during observation.

`box/cylinder.at` is the geometric center in its chosen frame. Parent transforms affect the final world position. With default `mass` of 0 and no joint, geometry is static and will not fall when suspended. `supported_by` records a relationship without aligning, welding, or verifying contact. Ownership, frame, labels, and structural validation cannot establish stable placement. Inspect the actual stage for a referenced model's origin and support geometry.

Use native UsdLux or `world.light(id,kind,intensity,at=...,frame=...,orientation=...,color=...,radius=...,width=...,height=...)`. For sphere lights set radius, and for rect lights set width/height. Explicit lighting uses source declarations only. `lights=[]` disables automatic lights. Intensity is an authored setting. Web GLB and RTX share a stage but need not match pixel for pixel. `rendering.webPreview.available=false` in a build receipt means web conversion is unsupported. Read `reason/limitations`. The system withholds incomplete GLB, and the native scene remains available to `scene_observe` and `robot_run`. Use actual native images to inspect required appearance and presentation. A missing preview neither invalidates the model nor establishes task success.

After build isolation and before the production SDK opens the stage, a passive check inspects external dependencies, animation, executable scripts, and graphs. Later consumers verify the frozen report and byte digests without reparsing physics attribute lists. Unwired scripts, Script/Graph nodes, and startup extensions cannot gain privileges through stage reload. Do not encode control in build callbacks, timeSamples, or direct pose writes. Robot demonstrations come from actual drive execution.

Keep the stage metric and Z up. MDL can retain original sources and bindings. The official isolated SDK audit recursively checks imports, actual resources, and effective parameters during build. Verify digests after freeze. Anchor paths to the owning USD layer. The pinned SDK resolves bare references such as `OmniPBR.mdl`. A package-local module with the same filename can have a distinct verified identity. Keep that identity. Apply the current audit even when a historical catalog record notes an MDL limitation. Unsupported connected input graphs, time-sampled inputs, arrays of resource parameters, and non-texture asset parameters fail explicitly. File freezing does not qualify arbitrary material graphs. Distinguish optional SDK thumbnails from required render resources.

Existing activation limits for USDZ, MDLE/MDR containers, MaterialX, and OSL still apply independently. Web GLB supports only qualified conversions. If native material works but the web conversion cannot represent it, retain `webPreview.available=false` and the original error, and inspect same-revision RTX images through `scene_observe`. `sourceResolution` revalidates official sources and dependencies. `rendererMapping` of `unknown` means the internal mapping was not observed. It proves neither failure nor BSDF equivalence. For derived materials, keep original inputs, conversion source, original/derived digests, and structural differences. Preserve the original library and disclose lossy PBR mappings. Legacy `World.when` execution declarations are explicitly rejected. Use qualified native physics components instead of restoring host task macros or silently ignoring declarations.

Before SDK import, resolve the entry to an existing absolute filename with `Path(entry).resolve(strict=True)`. This protects relative mesh references when the importer changes directory. The build's `/output` is temporary. Once the importer returns its filename, author stage references relative to the owning USD layer and verify dependency layers. Never freeze absolute `/output` or `/inputs` aliases. Keep source resources read-only and convert in independent copies.

## Assets and materials

Search preferred simulation libraries, the broad local library, online existing assets, then custom generation. Even for ordinary objects, begin with `assets_lookup(libraryTier='preferred')`. If no suitable candidate exists, try broad, then search online, and finally use project sources, LLM assistance, or an asset-generation model. Default `all` retains whole-library search compatibility. Establish category, purpose, complete geometry, dimensions, and required interactions before comparing appearance. Do not use a component for an entire object. Cache status or extra keyword hits should not displace a suitable preferred candidate. Caching reuses the current preparation of a selected asset. Keywords such as robot, liquid, or soft body do not invoke a task recipe.

Pass a known object class separately in `category`. Use `query` for purpose, material, and style. If the category is uncertain, start with natural language and refine from the results. Try English synonyms if Chinese or library-specific terms miss. One empty result does not establish absence.

```python
assets_lookup(query='wardrobe', category='cabinet', libraryTier='preferred', collection='artvip', requiresJoints=True, limit=6)
assets_lookup(query='dining chair', category='chair', libraryTier='preferred', limit=6)
```

The preferred catalog is `assets/collections/catalog.json` under the current data root. It shares a rebuildable index with other sources. ArtVIP, RoboCasa365-Lightwheel, RoboDojo, selected NVIDIA SimReady items, and later additions offer complementary candidates. Use the actual result's item, collection ID, and version. Downloads not yet indexed are not searchable. `collection` matches the exact ID. `qualityEvidence` distinguishes library sampling from observation of this item. `importRequirements` describes source format and current barriers. Rigid-body or joint fields, preferred origin, and frozen files do not establish usable interaction. Encrypted BEHAVIOR scene/object assets stay in OmniGibson. Do not extract or convert them into standalone Isaac assets. Check the separate unencrypted MIT robot package against the [robot guide](robots.md) and its actual license.

For articulation, start with `collection='artvip'` in the preferred tier, then check other preferred libraries. `requiresJoints=true` matches only `jointEvidence.status='source_inspected'` with a positive integer `movableJointCount`. This counts inspected internal nonfixed movable joints, excluding fixed joints and root connections. Evidence retains method/sourceRef/sourceRevision/sourceDigest. It attests to the source structure only. `requiresJoints=false` or omission imposes no joint requirement. Preserve unknown values. If strict filtering finds nothing, remove the flag and inspect the source structure of a few preferred candidates before drawing conclusions.

Search metadata first. Use `assets_lookup(assetId=actualId)` on a short list to see locally available hash-locked item images without preparing the whole package. Reuse existing verified item previews, including official single-item images. `publisher_render` identifies a publisher's image. Compare actual contours, proportions, materials, detail, and style. A library sample or texture map does not show the complete item. Preserve missing images and unknown dimensions or joints. Avoid requiring `textured=true` on the initial USD/MJCF/SDF search because evidence may be unknown. `textured` indicates texture evidence, and `visualReady` indicates availability of a preparation thumbnail. The generator selects candidates and reviews final scene quality.

Use `assets_prepare(assetId=actualId)` on a native catalog item to preserve its full package layout, collision, and joints. Reference its preparation report through `resourceRefs` in the build. Never silently reduce the visual to a static bounding box. MDL uses the official closure audit above. USDZ retains its existing limits. Verify MJCF/SDF import support against the actual SDK and resources. Catalog preparation proves only that the declared files were frozen. The generator inspects fitness and the final scene. Reuse one preparation for multiple instances.

For a native package, false `rigidReady/simulationReady/visualReady` flags indicate that the respective qualification has not been obtained through this path. Consider `preparationStatus/importRequirements` and actual observations. A publisher image can guide selection without qualifying as a Studio render. If a source limitation blocks integration, consider a traceable supported derivative preserving the original input or another available candidate. A missing preview, unknown field, or empty search does not justify replacing it with a crude box. Record why candidates were unsuitable before custom authoring. Apply the same geometry, material, and physics requirements.

After placing a native asset, verify joint `body0/body1` targets and the reference frames for `localPos0/1` and `localRot0/1`. With a target, coordinates are relative to that target. A side without a target attaches to the static world. Moving the top-level instance Xform need not move a world anchor. For transform, collision, or mounting issues in an otherwise suitable asset, check whether project-source calibration can preserve geometry and materials, then verify in physics and images. Preserve necessary mobility and the read-only original. Continue selection if the asset cannot be integrated.

Choose one input mode for `assets_prepare`.

| Source | Arguments and scope |
| --- | --- |
| Local index | `assetId=<actual lookup ID>` |
| Online visual model | `url=<public self-contained HTTPS GLB>,sourcePage,name,license`. The workbench host downloads it. Retain actual provenance and unknown license status. |
| Native package | `resourceFile=<absolute simulation-host entry>,dependencyFiles=[all other dependencies],name,license`. Optional `mdlSearchRoots=[absolute MDL module directories]`. Use one common permitted source root under current data assets, read-only legacy assets/data, or the pinned SDK prefix. Exclude other projects and arbitrary host files. |
| Generated source | `sourceFile,expectedSourceHash,materialIds`. Freeze `build_asset(materials)`, helpers, and explicitly declared materials. The builder has no network access. |

Native packages preserve layout relative to the source root. The `kind='native_files'` report returns entry/path/reportPath/digest/fileCount, with the full manifest and provenance at report artifactRef. Large files stay out of source JSON and model messages. Declare dependencies explicitly and check digests. Do not infer arbitrary configuration/weight dependencies or execute weights. `filesFrozen` proves file preparation only. These packages do not receive rigidReady/simulationReady/visualReady qualification.

Supply `mdlSearchRoots` only when absolute MDL imports need additional module roots. Strict file references and built-in SDK materials do not require manually supplied roots. Each root must be in the same permitted source tree and contain explicitly declared MDL files. Declaring a directory does not copy or scan all its contents. List declared internal/external modules, textures, and tiles in `dependencyFiles`. Revalidate built-in SDK module sources separately through runtime identity and actual source digests without copying the entire SDK. Reuse a collection's `packageRoot` for existing MDL files. Collection metadata may specify special subdirectories. Preparation records `inputs.mdlSearchRoots` relative to `files/...` so they move with the frozen package. Do not depend on the original absolute directory in future runs. Freezing files and declaring roots does not prove official material resolution or RTX appearance.

`project_inspect.runtimeExecution` gives the actual interpreter and prefix. From the native terminal, use the configured remoteHost over SSH to inspect known remote resource paths read-only, or consult official public sources for the pinned version. Freeze required examples/configurations with `resourceFile`. Do not execute scripts distributed with a model or incorporate example code that directly changes live state into the runner.

Generated sources can return a Trimesh/Scene or write a complete native package under `/output` and return exactly `{'entry':'relative-entry','files':['all-output-files',...]}`. Optional `mdlSearchRoots=['relative-module-root']` belongs in that return value, with `.` meaning the output root. It is not a top-level tool argument for `sourceFile` mode. List every actual regular file in the directory, with no undeclared files or links. The preparer uses a separate channel for the return declaration and reserves no package filename. The report preserves source, material, and output digests. Different module roots produce different frozen identities. Direct cache reuse requires one valid same-source match. With multiple declarations, select the package corresponding to the actual builder output.

The static_mesh preparation and World.asset convenience path require positive dimensions on all three axes. Check native USD separately for zero-thickness planes. Use actual model/material reports and search `kind='material'` for materials. Models retain UVs, multiple materials, and textures. Box/cylinder supports materials. `texture_period_m` specifies the metric length of one texture period and cannot be combined with uv_repeat. For online models, inspect the source page before passing a public self-contained GLB's url/sourcePage/name/license to `assets_prepare`. Retain search and download provenance. If authentication is required or no direct download exists, seek another resource. Observe native boundaries for payment, login, or missing information. `currentPreparation` requires the current version, complete frozen files, and valid local source dependencies. Do not treat a stale report or changed file as current evidence. Preserve unknown license status. Preview does not establish scene quality or successful operation.

## Robot configuration

The official importer may give URDF links different USD names. Studio uses the official names/displayName mapping. When automatic matching is ambiguous, use linkMap to select the actual prim explicitly. Ambiguous matches are rejected. Do not infer identity by character replacement.

Follow the [robot model and resource guide](robots.md) to select a real model and verify its structure. The configuration describes integration of that model. It neither certifies capabilities nor permits redesigning mechanisms for a task.

Add a project-local `*.robot.json` referencing the prepared native model and planning resources. The system does not select control code by robot name or consult a model whitelist. The profile describes the model and actual I/O. Generated programs define task goals, constraints, recovery, scoring, and semantic labels. Multiple robots may share one profile while using separate instance IDs, mount poses, and port namespaces.

Prepare the complete package, inspect actual structure, and write the profile. Set the single `world.io.clock` before `world.robot`. After build, read frozen I/O, create the planner from the same profile, compare FK for the required poses, and verify a short real execution and its records. Report mapping, dependency, or planning failures. Do not substitute direct object motion or change scoring.

The following shows the configuration shape. Replace `resourceRef` with actual preparation reports and obtain prim paths, joint names, and planning fields from the selected model.

```json
{
  "schema": "sceneagent-robot-profile-v1",
  "model": {"resourceRef": "actual-model-package-report", "file": "files/model.usd"},
  "ports": {
    "arm": {"source": {"primitive": "joint_position", "path": ".", "joints": ["joint_a", "joint_b"]}}
  },
  "streams": {
    "q": {"source": {"reader": "joint_positions", "path": ".", "joints": ["joint_a", "joint_b"]}, "visibleToPolicy": true, "required": true},
    "dq": {"source": {"reader": "joint_velocities", "path": ".", "joints": ["joint_a", "joint_b"]}, "visibleToPolicy": true, "required": true}
  },
  "planning": {"resourceRef": "actual-planning-package-report", "file": "files/robot.yml", "assetRoot": "files", "ports": ["arm"]}
}
```

- Require `schema/model/ports/streams`. Optional fields are `planning/initialJoints/drives`. Reject unknown fields, duplicate keys, nonfinite numbers, and invalid paths. No task, scoring, callback, or SDK method fields are supported.
- Include `model.resourceRef` in `scene_build.resourceRefs`. It identifies a `native_files` package. Omit `file` to use its entry, or give a package-relative path listed in nativeFiles. Supported formats are USD/USDA/USDC and URDF. Do not fill dependencies from SDK example directories.
- USD uses defaultPrim or an explicit absolute source prim in `model.prim`. `model.variants` maps real variantSet names to selection strings. Require metric, Z-up sources with no root resetXformStack. The system does not rescale implicitly. Reference the read-only source in the output stage.
- URDF uses the pinned SDK's official importer. In `model.importOptions`, `fix_base` is bool or null. `merge_fixed_joints/merge_mesh/allow_self_collision/collision_from_visuals/run_asset_transformer/run_multi_physics_conversion` are bool. `collision_type` accepts Convex Hull, Convex Decomposition, Bounding Sphere, or Bounding Cube. `joint_drive_type` accepts force, acceleration, or null. `joint_target_type` accepts none, position, velocity, or null. `override_joint_stiffness/override_joint_damping` are nonnegative finite numbers or null. `link_density` is positive finite or null. The system supplies input/output paths. Explicitly package meshes and textures. Unresolved package URIs do not trigger ROS directory searches. URDF does not accept prim/variants and USD does not accept importOptions.
- `ports` and `streams` use native I/O source fields. Specify actual prim paths relative to the model root, with `.` for the root. Paths must remain inside the model. Local names are simple identifiers, qualified as `instanceId.localName`. The SDK derives shape, units, limits, and origin. Streams accept `rateHz/visibleToPolicy/required` and default to the actual contract's cadence. Declare cameras and global contact_report separately in the scene.
- `initialJoints` maps explicit joint paths within the model to `{position,velocity}`. `drives` similarly maps to `{stiffness,damping,targetPosition,targetVelocity}`. Specify only fields to set. Use finite SI values and nonnegative gains. These settings apply only at build time to real revolute/prismatic schemas. Angular quantities and angular drive gains convert to USD units. The fields cannot change state at runtime.

```python
# scene.py: the scene owns the installation pose. The profile is reusable.
def build(world):
    world.io['clock'] = {'physicsRateHz': 200, 'controlRateHz': 50}
    robot = world.robot('arm_1', 'robots/model.robot.json', owner='room',
                        at=[0, 0, 0], orientation=[1, 0, 0, 0])
    # robot contains data fields id/primPath/mountPath/ports/streams/frames.
```

`owner` must exist. `frame` can be an existing static node or `{'nativePath':'actual-prim-path'}`. The mount pose applies only at build time. `primPath` identifies the model instance root. `ports/streams` map local to qualified names. Do not guess encoded prim names. Freeze needed mappings in task.parameters for an expert rather than depending on live build-process objects.

`planning` includes `resourceRef/ports`. For file-based configuration, use package-relative `file` or omit it for the entry. Safely read the native cuRobo YAML robot_cfg or its inner dictionary. `assetRoot` defaults to the YAML directory, or explicitly names a path relative to the report package root. Alternatively, set `planning.config` directly to the native inner cuRobo dictionary with kinematics accepted by `create_planner(dict)`. It is mutually exclusive with file. This mode does not interpret entry as YAML and defaults assetRoot to the planning package root. Both paths verify dependency closure, joints, and limits. The actual URDF/USD, meshes, asset_root, and collision_spheres must be listed in the frozen package. No fallback to SDK assets. Preserve native booleans and never substitute bool for a number.

`planning.ports` selects local position ports controlled by the solver. By default require a complete, unambiguous bijection by real joint name. For different names, explicitly supply `jointMap={planningJointName:{'port':localPortName,'joint':actualJointName}}`. Do not infer mapping from length or order. Use `planning.linkMap` for planning base/tool names mapped to actual relative prim paths. Otherwise require unique same-name prims. A locked joint is a numerical assumption. The expert must check actual q. It does not impose a physical lock.

Resolve base/tool from the actual model. `frames[link]` returns `stream/bodyPath` for actual body pose and a fixed 4×4 column-vector `bodyFromLink`. Explicitly declare one qualified pose stream of matching bodyPath, source, and cadence in the project I/O or profile. Planning does not publish aliases or alter `visibleToPolicy/required`. A hidden stream can bind a frame, but the policy can read it only when authorized. Missing, ambiguous, or absent rigid ancestors report the actual path. Compute the tool pose with `worldFromBody @ bodyFromLink`. This transforms a measured body stream into a fixed frame. Verify FK and actual execution error separately from a direct TCP measurement.

Treat `*.robot.json` as public model data. Editing it changes sourceHash. Build and expert include all matching project profiles automatically. Exclude private rubric/case data, scoring state, and future observations. `sourceRef` must be Python. helperRefs can explicitly include configuration files. Ordinary JSON is not frozen automatically. `world.robot` resolves paths only from the frozen source root. In the expert use `Path(__file__).parent / 'robots/model.robot.json'` or an explicit source_root, independent of cwd.

Configuration extends existing USD/URDF models, named ports, and existing raw readers to real robot models. Missing algorithms, drivers, or unknown sensor readers require separate implementation. Humanoid and mobile models need compatible control policies. If existing SDK controls/readers suffice, extend through project configuration, resources, and policy. Report missing lower-level primitives explicitly.

## Tasks, control, and observation

`Task` includes `id/instruction/bindings/parameters/rubric/measurements/evaluation/recording/execution`. Bind generated roles to actual nodes and express task semantics in parameters. `rubric={'sourceRef':'rubrics.py','helperRefs':[...]}` selects the independent scoring closure. Each applicable `evaluation.cases` entry supplies `id/sourceRef/helperRefs/expected/requirementRefs`. Design discriminative actual cases and independently review them. Run robot_run first, then pass same-revision episodeIds to task_check. A program error cannot serve as an ordinary negative example.

`expected` selects a nonempty subset of `success/terminated/reward/details`. Compare each entire selected JSON field exactly. success is bool or null, terminated bool, reward a finite number or null, and details finite JSON. No `rewardMin/rewardMax` or cross-episode ordering operators are supported. See `runtime.scene.Task.validate_evaluation` and `runtime.worker._case_matches`. Derive expectations independently of the scoring under test.

`caseId` selects a frozen verification case and binds the episode to its expected result. It does not inject policy parameters such as `env.case`. For different control behaviors, select corresponding frozen sources. Sources may share helpers. Multiple cases may use one source to check different output fields.

Declare native numerical measurements only in io.streams. Task.measurements retains task camera permissions via `camera={'cameraIds': [...], 'visibleToPolicy': True|False}`. Unknown legacy measurement channels are rejected.

### Native I/O and controlled actions

Declare `world.io={'clock':...,'ports':...,'streams':...}`. Build checks joints, drive modes and targets, limits, dimensions, units, and origins against actual SDK objects and freezes the single `sceneagent-native-io-v1` contract. Do not maintain a parallel Franka action slice or another actionSpec/observationSpec.

```python
def declare_io(world, articulation_path, actual_joint_names):
    source = {'path': articulation_path, 'joints': actual_joint_names}
    world.io = {
        'clock': {'physicsRateHz': 200, 'controlRateHz': 50},
        'ports': {'arm': {'source': {**source, 'primitive': 'joint_position'}}},
        'streams': {
            'q': {'source': {**source, 'reader': 'joint_positions'},
                  'rateHz': 50, 'visibleToPolicy': True, 'required': True},
            'dq': {'source': {**source, 'reader': 'joint_velocities'},
                   'rateHz': 50, 'visibleToPolicy': True, 'required': True},
        },
    }
```

The example names and rates show structure only. Use actual model paths, joints, and drives. `joint_position/joint_velocity/joint_effort` require real PD, velocity, and zero-gain effort drives respectively. Renaming a mode cannot bypass checks. Ports may span native Articulations, but each actual actuator can bind only once. Freeze port modes with the revision. Initialized, genuinely persistent position/velocity targets may be omitted. Explicitly provide effort in every request. The runner reissues it at physics rate. Unknown targets, conflicting aliases, out-of-envelope commands, and invalid shapes fail before stepping. Never fill HOME/0 or infer a previous target from current q.

For position ports, `limits=[[null,null],...]` expresses the SDK's unbounded drive reference domain. `physicalJointLimits` records actual joint stops separately. A policy reference may exceed a stop. Do not incorrectly reject or silently clamp it. Plan within the physical model's actual range and measure resulting motion separately. Velocity/effort limits are conservative command envelopes from native configuration, without claiming a hard limit on the SDK target buffer. Use `nativeDofIndices/nativeDofPaths` for actual ordering.

`env.open()` returns `io/resources/compute/resetSpec/sensorSpec/observation`. Resource keys remain preparation report references and values identify read-only `/resources/...` entries inside the isolated process. cuda:0 in compute denotes the assigned GPU's local index. Pure computation helpers may reuse pinned libraries and weights. They have no live stage, network, other projects, or shared writable JIT cache.

`env.observe()` returns the clock and `streams/streamStatus/cameraFrames` without advancing physics. `visibleToPolicy` grants per-stream access without publishing scoring facts. After the single commit boundary, large arrays use `{'ref':'arrays/<sha>.npy','sha256':...,'dtype':...,'shape':...}`. Use `runtime.records.read_array(contract['sensorSpec']['arrayRoot'], ref)` to verify and load arrays read-only. Never read the entire episode or future data. arrayRoot is currently `/sensors`. Do not fill missing references.

`env.step({'portName': numericArray}, seconds=None)` is the sole physical action entry and defaults to one frozen control cycle. Duration must be a positive finite non-bool real number covering whole control cycles, at most `max(0.1, 1/controlRateHz)` seconds. Strings are rejected and the episode's remaining physics budget applies. After full validation, write native drives and record the request, actual targets read back, and raw state. Effort readback is the actuation buffer, not measured torque. Stop remaining cycles on termination or truncation and retain the actual prefix on failure. A reset after recording requires a new episode.

`runtimeExecution.controllerOutputContract='separate-logs-v1'` indicates ordinary Python/C stdout during controller import and execution is routed to diagnostics separately from the action protocol. `env.open/reset/observe/step` and real action records retain their existing semantics. Older frozen runners without this flag retain their implementation. For this general fix, freeze a new same-source revision in a project authorized to build and preserve prior scenes and executions. The capability does not certify robot success.

executedAction records only ports sent and read back in this request. Omitting a persistent port creates no synthetic write. nativeInitialTargets is the actual initialization readback. active_targets is derived from it and later commands. For measured effective targets every cycle, explicitly declare streams such as joint_position_targets/joint_velocity_targets.

`Task.execution` declares only `physicalSeconds`, a finite positive non-bool number chosen for the task with no universal maximum. Omitting `Task.execution` defaults to 30 seconds. The internal controller and each scoring invocation have guards of at most 120 and 30 seconds respectively. Declared physical duration still determines episode truncation. Kit job tools accept optional `workerTimeoutSeconds`, default 900 seconds, explicit positive finite non-bool value without a fixed maximum. It covers Kit startup, execution, subprocess file checks, and normal SDK exit. Bounded timeout cleanup, independent post-exit parent checks, and publication are timed separately. It does not change physicalSeconds, controller/scoring guards, or the entire generation budget.

`workerTimeoutSeconds` is a per-call option outside Task, robot configuration, or physics, and changing it does not require source edits. A compatible frozen external evaluator requires `manifest.runtimeExecution.workerTimeoutParameter == 'workerTimeoutSeconds'` for explicit values. Older unsupported packages reject the option. Default calls retain the original request identity. Recheck an existing job on a retry with the same requestId. A deliberate attempt with a changed parameter needs a new requestId. The new job receipt and performance report disclose the effective timeout.

Environments without control ports can still build, run, and record passive physics, without robot demonstration eligibility. A demonstration needs at least one verified native control port. Empty commands may hold initialized persistent targets. Generated scoring and independent review determine task completion.

### Independent stages and layout sets

New `videoOnly=true` projects without locked `environmentRefs` may prepare and `scene_build` the necessary environment and deliver only demonstrations videos. An explicitly selected existing environment remains read-only. The existing-environment requirement of ordinary collection does not apply to new recording projects without references.

New builds declare `capabilities.stageSplit/independentBehavior/robotSetup=true`. At start, the host locks one `requiredOutputs` selection: all three products, `[generation,evaluation]`, or `[demonstrations]`. Scene-only work omits robot selection, preparation, loading, and expert collection. Verify necessary furniture joints, passive physics, scoring, and interfaces. Empty-action `env.step` is supported. Scene delivery does not depend on robot task success. For new collection, select the existing environment's project and lock `environmentRefs=[{revision,split?,taskIds?}]`. Optional split is train/validation/test and taskIds is the explicit collection subset. Scene/evaluation delivery covers all requested frozen tasks. The reviewer cannot exclude tasks to bypass requirements.

`scene_build.environmentSourceRefs` declares the actual environment dependency closure: scene.py/tasks.py and sources needed by them, the rubric, and cases. Include robot profiles only when a robot is part of the environment. Only these files enter isolated build and the behavior entry may be absent. Omission retains compatibility with the legacy all-source input. At the robot stage pass `behavior={sourceRef,helperRefs}`. The host freezes the exact project code closure and the runner saves a separately content-addressed behaviorRef under the same project. Bind each episode to its environment revision and behavior identity. Keep private scoring/case sources out of behavior. `repetitions` repeats one layout, is mutually exclusive with seeds, and cannot create new layouts.

`[demonstrations]` on a locked existing environment forbids `scene_build`. Add a robot to a compatible robot-free environment with the existing `robot_run`:

```python
robot_run(revision=base_revision, taskIds=task_ids,
    behavior={"sourceRef": "behavior.py", "helperRefs": []},
    robotSetup={"robots": [{"id": "arm", "profileRef": "models/arm.robot.json",
                            "at": [0, 0, 0], "orientation": [1, 0, 0, 0]}],
                "resourceRefs": [prepared_report_ref]},
    purpose="demonstration", requestId=stable_request_id)
```

`profileRef` names an actual project `*.robot.json`. `resourceRefs` are reports from `assets_prepare`. They are not interchangeable. robots may contain several named instances with optional `owner/frame/purpose`. Use the original environment's frame for position and wxyz unit quaternion. Reuse the existing loader across models and tasks. Verify the model, algorithm, and real interface. Tools freeze profile and resource closure under `robot-setups/<digest>/` and each episode returns `robotSetupRef/robotSetupSha256`. Supply mutually exclusive `robotSetupRef` on later calls to reuse the installation. A new pose/profile creates a new installation input. Behavior may change independently. Preserve the base revision, task, rubric, manifest, and delivery receipt. Read different installations on the same base from each episode's reference. Never merge I/O on revision alone.

Optional `robotSetup.recording={taskId:completeRecordingDeclaration}` adds actual recording views. Preserve original cameras verbatim and retain the existing primary. Declare wrist or body cameras against real links/frames and required pose streams. Verify native bindings and calibration. Adding an observer view does not grant policy pixels in `measurements`. `scene_observe(episodeId=..., cameraId=...)` reads a published episode's actual camera. Evaluate independently with `examples/evaluate.py --robot-setup-ref <same-reference>`. Low scores do not revoke the base receipt.

Report compatibility gaps when the old package lacks `capabilities.robotSetup` or its runtime does not match. A compatible rebuild requires an explicitly separate scene task. Do not rebuild during collection-only work or edit a manifest. Independent behavior replaces only control source and does not install models.

Freeze distinct layout revisions from the same source using different build seeds. `project_deliver.environmentRefs` may select multiple layouts from one project. Both the internal review receipt and scoped export manifest binding preserve `requiredOutputs/environmentRefs` exactly. Each `coverage.episodes` entry contains `{episodeId,revision,use}`. Scene-only coverage may be empty. Demonstration delivery needs actual episodes for the current collection tasks. Interface checks execute verified same-revision, same-task case actions with fixed consumer_diagnostic use. They require no successful expert and confer no demonstration eligibility.

The consumer report lists dataset references. Associate each layout's tasks, I/O, scoring, and actual episodes independently. A revision can belong to only one split. Labels alone do not establish diversity. Collection-only work does not reconsume unrequested LLM/environment products. Scene-only work does not require dual robot formats. See [Delivery](delivery.md).

### Motion planning and policy reuse

Generated experts and helpers can call the numerical planner directly.

```python
from runtime.motion import create_planner, plan_trajectory

def numerical_plan(robot_config, world_config, start, targets, control_period):
    planner = create_planner(robot_config, world_config, interpolation_dt=control_period)
    return plan_trajectory(planner, start, targets)
```

`create_planner` accepts the original dictionary input and the profile path shown below.

```python
from pathlib import Path
from runtime.motion import create_planner, plan_trajectory

def configured_planner(env, task):
    contract = env.open()
    return create_planner(Path(__file__).parent / task.parameters['robotProfile'], {},
        resources=contract['resources'], io=contract['io'],
        port_map=task.parameters['robotPorts'],
        interpolation_dt=1 / contract['io']['clock']['controlRateHz'])
```

robotPorts must be the `world.robot(...)['ports']` mapping frozen into the task. The expert constructs world_config from permitted observations. The profile entry returns native MotionGen. `plan_trajectory` also returns `portJointIndices` and `profileProvenance`. Use portJointIndices to map each actual port into each returned position row when forming named commands. Validate the start and results against actual `physicalJointLimits`. Unbounded drive references do not define physical ranges. The original dictionary interface and return values remain compatible.

Use the pinned cuRobo planner's native signatures and parameter types. For example, `planner.update_world(...)` accepts `curobo.geom.types.WorldConfig`. Convert with `WorldConfig.from_dict(world_config)`. Support for dict in `create_planner` does not generalize to all native methods. Check their actual signatures.

Construct robot_config/world_config from the frozen native model and current permitted observations. start requires `{jointNames,position,velocity}` and may explicitly include acceleration/jerk. Default missing derivatives to zero only as numerical boundary conditions and disclose them in unprovidedDerivativeAssumptions. Use actual planner.joint_names order. targets maps actual model link names to `[x,y,z,w,x,y,z]` in the model base frame. Include the main end effector and optionally additional end effectors to solve jointly. Results contain `kind='numerical_plan',actualExecution=False,jointOrder,timebase,position,velocity,targets`. Failure has no implicit fallback. Empty world_config means there is no world collision checker. Do not fabricate a distant obstacle or claim collision avoidance.

Planning does not step simulation or provide a host env.motion RPC. Reuse the planner across legal numerical targets within one behavior execution and advance physical state only for selected candidates. Update collision geometry and starting state when the world or held-object relationship changes.

Profile-based plans also include original `start` and `executionBinding={clock,ports}` for `execute_trajectory` in the same module. The old dictionary form still supports numerical calculation. Execution refuses to guess ports for plans without an execution binding.

```python
from runtime.motion import execute_trajectory

result = execute_trajectory(
    env, plan,
    feedback=task.parameters['motionFeedback'],
    start_tolerance=task.parameters['motionStartTolerance'],
    tracking_tolerance=task.parameters['motionTrackingTolerance'],
)
```

`feedback` maps each actual planning port to `{'position': qStream, 'velocity': dqStream}`. Streams may include extra joints but must match the actual paths and joints, be native outputs visible to the policy, and be current at the sampled time. Provide `start_tolerance={'position': [...], 'velocity': [...]}` and `tracking_tolerance=[...]` as finite nonnegative per-joint values in `plan.jointOrder`, using the robot's rad, m, and velocity units. Select tolerances for the actual configuration and control conditions. There is no common model threshold.

Before sending an action, execution checks the complete plan, port identities, physical joint ranges, clock, starting q/dq, and data permissions. There is no implicit resampling or retiming. Point 0 checks the initial state only. N points advance N−1 complete control cycles with all planning ports sent together each cycle. Unplanned ports keep only initialized persistent targets. When needed, provide explicit `hold_action={unplannedPort: fullArray}`. Do not zero-fill or open a gripper automatically. Inspect the committed observation from each `env.step` directly without another observe. Load large arrays with `read_array` under the authorized arrayRoot.

For a starting-state error, retain the actual q/dq inputs, the plan's point-0 q/dq, q/dq immediately before execution, and declared tolerances separately. After grasping, waiting, or any other real step, obtain a fresh measured start. Preserve nonzero joint velocity. Distinguish solver boundary behavior, stale observations, and tracking errors without removing preflight checks or loosening tolerances arbitrarily.

The result includes `kind='trajectory_execution'`, `status=completed/terminated/truncated`, `requestedCycles`, `returnedStepCalls`, `confirmedPhysicalSteps`, `totalPhysicalSteps`, `countStatus`, `lastConfirmedWaypoint`, and `lastObservation`. It does not report task success. Step counts are relative to this execution. The observation's physicsStep remains episode-absolute. An initially terminated/truncated state sends no action. Tracking or feedback errors stop further commands and raise RuntimeError with the confirmed `execution` prefix and original controller exception as cause. After an env.step exception the physical tail may be unknown. Do not infer exact steps from call counts. Preflight failure sends no action. Preserve real I/O errors such as missing files.

The contract draws on [ASPIRE trajectory execution](https://github.com/NVlabs/ASPIRE/blob/fa00f0ef2bc1d91d594b9aa502c22133c9f538f7/src/cap_harness/api.py#L882) and uses existing native I/O without additional RPC, task-specific actions, or an ASPIRE runtime dependency. It provides no per-physics-substep collision avoidance, dynamic balance, rollback, or liquid handling. Generate recovery and control using actual interfaces. Edited or resampled plans do not automatically inherit solver guarantees. Assess physical grasp, stable release, obstacle avoidance, and task success from execution and generated scoring. Multi-arm models and multi-effector planning do not verify a cooperative task. For humanoid walking, match the policy's observations, actions, normalization, timing, and reset through the same step API. Do not fix the root/legs or move the root pose to simulate walking.

### Raw measurements, liquids, and generated calculations

The following raw readers are wired into the current interface.

| Reader | Additional source fields | Data and limits |
| --- | --- | --- |
| joint_positions / joint_velocities | path,joints | q/dq in actual joint order |
| joint_position_targets / joint_velocity_targets / joint_efforts | path,joints | Native drive targets or command buffer. Efforts are commanded, not measured torque. |
| root_pose / root_velocity | path | Actual world pose or velocity of the native Articulation free root |
| rigid_pose / rigid_velocity | path | Native RigidPrim state |
| particle_state | path,field=positions or velocities | Native arrays of an independent particle set. Currently requires an identity world frame. |
| contact_report | path=physics Scene, actors=explicit list of RigidBody paths, field=headers, points, or impulses | Actual SDK contact report. An empty report does not establish separation. |

Pose is xyz+wxyz. Velocity is linear followed by angular. Actual bindings determine dtype/shape/units/frame/rateHz/origin. Report unwired sensors/states as unavailable. An author getter, facts dictionary, attribute name, or absence of a report cannot establish a native measurement. rateHz must be a positive integer divisor of physicsRateHz. Above control rate, physicalSamples retains streams due on each real physics tick. Do not fill samples that were not due. Preserve actual sampleTime/physicsStep. Low-rate caching does not change the policy clock. `required=false` allows sampling to be temporarily unavailable after a successful binding. Initialization still requires actual dtype/shape and other metadata.

Interpret the 13 uint64 contact-header columns using the frozen columns: `eventCode,actor0NativeId,actor1NativeId,collider0NativeId,collider1NativeId,proto0,proto1,offset,count,actor0PathIndex,actor1PathIndex,collider0PathIndex,collider1PathIndex`. NativeId columns 1–4 are process-local. Columns 9–12 index stable USD paths in frozen actorPaths. Select actual same-batch points/impulses rows using offset/count and verify stage identity per sample. IDs are not semantic labels and unreported contact does not prove separation. Public points/impulses each have three columns: world position in m and native impulse in N·s respectively. Do not invent normals or claims about the receiving side of a force.

Express liquids in scene.py with the pinned SDK's native PBD construction. Consult that version's official examples and check GPU physics, particle system, material, actual container collisions, and readback configuration. Do not add World.fluid or a pouring module. Author initial points/velocities in world coordinates. The composed particle set world matrix including ancestors must remain identity within 1e-9. Containers move fluid by real collisions. Do not parent the set under a moving bottle/body. Build source must not prewrite simulationPoints. Require an enabled set/system, one GPU physics owner, and actual physics execution. For fluid particles with effective smoothing, read simulationPoints. Without smoothing, with smoothing disabled, or for solid particles, use SDK-synchronized Points.points. Ignore stale simulationPoints after disabling smoothing. Read Points.velocities independently. Check readback settings by requested field and actual postprocessing. Initial source arrays, disabled readback, and an unstarted simulation are not outputs. Smoothing is optional. Separate raw position/velocity from mass configuration. Compute regional mass, volume estimates, spillage, and conservation in frozen helpers/rubrics from measured particles and explicit mass configuration. Record inputs, formulas, and errors so derived quantities can be recomputed. MassAPI.mass configures total set mass. Use total mass/N and actual region counts only with explicit equal masses, a closed set, and no insertion/deletion. Density needs a corresponding SDK interpretation or calibration. Custom USD attributes remain authored configuration. A particle-motion probe does not establish containment, oil properties, pouring, or full task success.

Configure physical and visual materials separately. PBD density, viscosity, and surface tension govern physics and do not automatically color, reflect, or transmit light at the surface. For visible liquids choose a representation on the same `world.stage` compatible with the pinned SDK and qualified materials. Native particle isosurfaces and `UsdShade` binding are options. Update the visible surface from actual physical particles in the same episode. Do not substitute separate animation, a static surface, or another episode's footage for flow. Use qualified raw streams for measurements independently of rendered geometry. See the [official fluid-rendering guide](https://docs.omniverse.nvidia.com/kit/docs/omni_physics/latest/dev_guide/particles/particles.html#post-processing-for-fluid-rendering) and verify the installed API.

In the installed `extscache/omni.physx-*/omni/physx/scripts/particleUtils.py`, `add_physx_particle_isosurface(stage, path, ...)` applies `PhysxSchema.PhysxParticleIsosurfaceAPI` to the particle-system prim. Choose grid spacing, surface distance, and mesh budget for actual particle scale and observations. The official `extscache/omni.physx.tests-*/omni/physxtests/tests/PhysxParticleIsosurfaceAPI.py` shows surface generation, updates, and particle visibility. Paths were checked against PhysX extension 110.1.11. Recheck on version changes. Qualified visual options include `UsdPreviewSurface` and `UsdShade.MaterialBindingAPI`. Keep physical and visual binding purposes separate. Inspect actual RTX output to verify geometry and shading. Enabling an isosurface or setting opacity does not establish realistic refraction.

Isolated verification covered `UsdGeom.Points` fluid particles, a native isosurface, and `UsdPreviewSurface` without optional particle smoothing. The same recorder captured raw positions/velocities and RTX images. The tested combination of simultaneous smoothing and isosurface lacked qualified raw position readback and has not passed. Other combinations may be verified, or use a representation that does not depend on that postprocessing. Preserve missing-reader gaps without prewriting output or falling back to unverified arrays. This check covers dynamic surfaces and recording, not oil properties, stable containment, or visual realism.

In internal review, check that actual images make the requested dynamic effects and outcomes discernible. Verify provenance and timing against raw state and frame tables. Choose views, resolution, cadence, materials, and lighting for the intended use. Not every internal physical quantity needs to be visible. Record and correct blur, occlusion, or unconfigured appearance where relevant. Particle motion, numerical success, and a decodable video do not establish visual realism. A local visible-material or surface test does not qualify refraction, fluid physics, containment, or the whole task. See [native material limits](#project-and-scene-tree).

The same division applies to SDK soft bodies, ropes, and new sensors. The project authors native construction and algorithms. Add trusted system wiring only for missing raw read/write primitives. A common interface does not imply every physics capability is wired.

### Recording cameras, policy pixels, and semantics

`Task.recording` contains only cameras and primaryCameraId. Each complete declaration has cameraId/frame/at/orientation/resolution/rateHz/modalities/video/required/source. Use world, a scene node, or `{'nativePath':'/World/actualMountPrim'}` for frame. Use metric at and unit wxyz orientation. Check actual extrinsics per frame on the mount. Even for a seemingly static nativePath prim, require one unique same-path `rigid_pose` stream with shape=[7], frame=world_wxyz, origin=native_output, rateHz an integer multiple of cameraHz, and actual samples at frame times. Do not substitute root_pose or an initial matrix. Use world poses for fixed cameras. H.264/yuv420p requires even width/height only for `video=true`. With `video=false`, preserve resolution without cropping or padding.

Choose view count and resolution for the task. Cameras sample at control boundaries. rateHz must be at most controlRateHz and divide it. source distinguishes simulation_observer from simulated_robot_sensor. Neither is a real hardware camera. A missing required view prevents complete media qualification. An optional failure preserves its actual prefix. Supply images, calibration, and clock only via Task.recording and cameraFrames/frames. Do not declare a camera_frame numerical reader. Task.measurements.camera.cameraIds and visibleToPolicy grant policy access. Resolve cameraId within its Task. Keep unauthorized, future, and other episodes' images out of the policy. Read committed RGB read-only at open.sensorSpec.root and frame.path without inline base64.

trajectory.frames is the sole frame table. index is the global row number, while cameraId/frameIndex identifies the frame within its camera. Preserve per-frame clock, pixel SHA, intrinsics, and worldFromCamera. scene_observe takes the global index in frame. Without an episode, cameraId resolves within the first frozen Task only and defaults to its primary view. `nodeId` subtree framing cannot be combined with cameraId or an episode frame. Check actual cameraScope/viewMode/hiddenVisualNodeIds. A cutaway or default view cannot certify full coverage.

Use `scene_observe(cameraIds=[...])` to observe 1–4 distinct frozen cameras of the first frozen Task in one job on the same static revision. Each item in `observations` keeps single-image pixel, camera, clock, scope, and source fields. Each tool result also includes `imageContentIndex`, indexing its `content` array directly (text at 0, images starting at 1). Identical pixels may share a block. Use this index to associate the view. Do not combine the batch argument with `cameraId/nodeId/episodeId/frame`. Existing single-image and episode-frame semantics remain unchanged. Four is a response resource budget, not a Task camera limit. Request additional batches as needed. A missing frame fails the job with diagnostics. All views in a batch must share the stage, physics step, and simulation time. Scene observation cannot replace robot execution or the requested quality review.

Derive semantics from the request, Task.bindings, and frozen entity provenance. Generated scoring may put `{'id':...,'text':...,'bindings':['taskRole']}` in details.annotations. The recorder preserves generated_interpretation, cycle, source, and fact references for committed inputs. Labels do not prove physical success. RGB is the only connected modality. Depth and pixel instance/semantic masks require real native annotators. Text labels are not pixel data. Report unsupported modalities.

### Generated scoring and the three products

For `generation.deliveryContract='producer-exports-v1'`, the generator produces requested construction history, robot data, or simulation evaluation directory and manifest under its own `delivery/`, according to `generation.requiredOutputs`. The [delivery guide](delivery.md) maintains official tool, semantic, layout, and manifest details. Exporters are outside the physical sourceHash. Change formats and re-export from the same real episodes. Pass `producedArtifacts={manifestRef,sha256}` to `project_deliver`, pointing at `projects/<projectId>/delivery/<run>/manifest.json`. The system reads standard formats and invokes existing physical consumers without executing the exporter or repairing mappings. Older tasks without this contract retain their original three-product checks.

The sole entry is `evaluate(task,facts,state)`. phase is reset or transition. state starts None and may contain finite JSON only. reset carries one initialSnapshot. transition carries actual control-cycle observation/nextObservation, targetAction/executedAction, truthBefore/truthAfter, and applicable physicalSamples. Load array refs from the current scoring process's private read-only working directory with `np.load(ref['ref'], allow_pickle=False)`. Only arrays permitted by current facts are mounted, never /sensors or the full episode. Helpers must not leak private scoring state to policies.

```python
# Example return shape. The generated source defines the task semantics.
{'state': None, 'result': {'success': None, 'terminated': False, 'reward': None, 'details': {}}}
```

success can be True/False/None, terminated is independent, and reward is finite or None. Preserve unknown values without filling 0/False. Recorder is the sole commit boundary for facts and actions. Reply only after scoring preview, fsync, and index commit. Preserve actual prefixes and uncommitted tails on scoring/commit failure. The parent recomputes the same frozen program from its initial snapshot and empty private scoring state. Closing does not score again.

Express process requirements, consistent entity identity, and order through `Task.bindings`, real `facts`, and cross-cycle `state`. Bind stable identities of the manipulated object and destination, then advance stages on observed events. Final position or low velocity cannot establish an unobserved prerequisite. Design counterexamples with identical final positions and speeds but wrong identity, ordering, support, or a missing required release. Apply only conditions relevant to this request.

Proximity and labels do not establish a required relation. When contact matters, resolve frozen `actorPaths` using header path indices in `contact_report`. Verify the actual object/target collider pair, associate same-batch `points/impulses` via `offset/count`, then consider relevant geometry and cross-cycle facts. Pair identity or one contact event alone cannot establish support or release. An empty report does not prove separation. Impulse is not measured force. The reader supplies neither contact normals nor measured force. Do not fabricate them or set universal contact/stability thresholds. If release is required, choose observable facts for the actual robot/tool. If true and false situations are indistinguishable from available readings, a label or private state cannot fill the gap. Keep the requirement unverified. Calling evidence approximate does not justify success. Scene-only delivery still does not require a robot, expert, or successful demonstration.

Use synthetic `facts/state` tests for scoring branches and counterexamples. Verify readers, bindings, and required responses separately with real interfaces and physics records. Synthetic inputs do not represent on-site measurement or actual completion. Choose case behaviors, expectations, durations, and observations by task instead of prescribing identical cases or step times.

When robot data is requested, v6 uses one I/O contract to retain named actions, streams, and array sidecars. Pair by time for batch reading without flattening to a fixed robot vector. The `sceneagent-product-consumption-v1` report checks the requested public generation trace, original actual behavior data, or evaluation environment and the cases/interfaces applicable to each. Strictly verify consistency of facts, actions, and scoring within the record. Do not require exact cross-run physical states or a second success by default. External candidates run independently through the matching package's examples/evaluate.py. Their failures or low scores do not change the original environment receipt.

## MCP tools

All calls carry `projectId/generationId`. MCP may fill them from a fixed session context. Otherwise use actual IDs supplied by Studio. Never guess them. The JSON Schema returned by `tools/list` is authoritative for argument shape.

| Tool | Purpose and key fields |
| --- | --- |
| project_inspect | Returns original request, requirements/version/hash, source paths/hash, capabilities, jobs, and gaps. Optional nodeId/baseRevision. |
| assets_lookup | Use query or assetId. libraryTier accepts preferred/broad/all (default all). collection takes an actual ID. requiresJoints true limits results to verified internal movable joints >0. False/omitted is unrestricted. Also accepts kind model/material, category, textured, size, and limit up to 20. Preserve unknowns. |
| assets_prepare | Async preparation. Select one of assetId/url/sourceFile/resourceFile and provide requestId. sourceFile requires expectedSourceHash and may explicitly include materialIds. resourceFile uses explicit dependencyFiles. Online sources require sourcePage/name and retain the license declaration. |
| scene_build | expectedSourceHash, seed, requestId, resourceRefs. environmentSourceRefs explicitly selects the entire environment closure including scene, tasks, scoring, and actual helpers, verification sources, and configurations. Omission preserves whole-project freeze compatibility. baseRevision checks existence only. No incremental compilation. |
| scene_observe | Use revision with cameraId, same-revision cameraIds, or nodeId subtree framing. Use episodeId/cameraId/frame for an episode. output=web_preview separately rebuilds a display preview and is mutually exclusive with camera/node/episode options. Preserve the frozen source. |
| task_check | revision, taskIds, actual verification episodeIds (preferred) or episodeId. If both are supplied, nonempty episodeIds takes precedence, otherwise fall back to episodeId. baseRevision can report a whole-World difference without determining minimum retest scope. |
| robot_run | revision, taskIds, behavior/controller, robotSetup or robotSetupRef, purpose, requestId. Optional rubricSeconds is a cumulative positive finite scoring-exchange budget in seconds, default 30. A new frozen environment must declare adjustableRubricBudget=true for adjustment. Save the budget with the episode and use it for same-fact recomputation. It changes neither physical duration nor score semantics. A new installation requires independent behavior. Read per-task score, actual I/O, installation provenance, and demonstration eligibility. |
| job | jobId, action status/wait/cancel, cursor, waitMs up to 30000. Cancellation becomes terminal on remote confirmation. |
| generation_wait_system | requestId, reason, nonempty list of evidence strings, optional jobIds. Records a system blocker pending explicit resume of the same generation. Does not cancel existing jobs or judge product quality. |
| project_deliver | revision, environmentRefs, expectedRequirementVersion, requestHash, requestId, native review pointer, evidence. New producer-exports-v1 jobs also supply producedArtifacts. Requires independent review and consumers for the locked requiredOutputs to pass before accepted. |

`robot_run.controller` accepts `{"type":"expert"}` or `{"type":"policy",...}` with explicit `policyRef/digest`. policyRef selects a frozen verification source in the task's evaluation.cases. If several cases use the same source, use `caseId` to disambiguate. The digest and verification purpose must still match. A scene-only request permits only the frozen verification source with `purpose=verification`. It returns `controllerKind='case'`. Default/explicit experts, probes, and independent `behavior` collection are rejected before submission in this scope. Verification can drive furniture or use empty commands without robot actions or task success. Use demonstration as the purpose for proposed behavior data. Only demonstrationEligible identifies verified successes. probe/consumer_diagnostic cannot satisfy formal behavior delivery. Tasks in a list reset separately. Express compound order in generated behavior and scoring. Reuse the frozen expert and required views for same-revision collection. resetSpec currently permits only the frozen seed. Repetition does not create scene diversity. External candidates use the independent evaluate entry point. Their scores do not alter the package's generation status.

Submit frozen cases as below. Get `frozen_manifest` from the chosen published revision and select `task_id/case_id` from it. Use a stable requestId. Do not infer a digest from another revision, unfrozen files, or the expert entry.

```python
task = next(t for t in frozen_manifest['tasks'] if t['id'] == task_id)
case = next(c for c in task['evaluation']['cases'] if c['id'] == case_id)
run = robot_run(
    projectId=project_id, generationId=generation_id,
    revision=frozen_manifest['revision'], taskIds=[task_id],
    purpose='verification', requestId=run_request_id,
    controller={'type': 'policy', 'policyRef': case['sourceRef'],
                'caseId': case['id'],
                'digest': frozen_manifest['sourceDigests'][case['sourceRef']]},
)
```

Wait for the job's terminal state and take the actual episode ID from the receipt. Compare with frozen expectations through same-revision `task_check(episodeIds=[...])`. Do not predict an episode ID while pending.

Choose a stable requestId on the first write. Network retries preserve the full original tool name and input. Use a new requestId for a deliberate new attempt after a repair. `changedNodeIds` is an optional deprecated schema field for historical compatibility only. It does not drive impact analysis. Exact same-scope historical matches return their original receipt. New requests with the field and no historical match are explicitly rejected. Long jobs return jobId. Bounded wait pending is normal. Terminal receipts contain the actual result. A URL string cannot substitute for observed image content.

For an online URL, persist the job before downloading/staging in the background and return promptly. HTTP disconnect or MCP wait timeout does not cancel an accepted job. Explicit cancel or service shutdown initiates local cleanup. Shutdown waits only within a bounded local cleanup window. Record unconfirmed exits without replacing the first error. Cancelling at online_stage before compute submission confirms no future compute submission, not that staging SSH has exited. After remote_submit request cancellation by original job ID and wait for confirmation. On restart, interrupted preparation without payload fails explicitly. A payload only establishes submission intent. Reconcile by original jobId without blindly downloading or submitting again. The original ID returns the historical failure. Use a new ID for a new attempt.

CPU runner operations health/assets/inspect/job/check/read/list/write and submit(task.operation=check) can proceed without a current GPU configuration and ignore caller-forged device fields. Other paths such as prepare/observe conservatively require a configured GPU. Actual execution pins index+UUID. These operation names are internal to the runner, not extra MCP tools. A path with CPU stages may still need a device.

Delivery evidence maps natural-language requirements to actual artifactRef values. Do not pass `passed:true` to override host checks. Keep incomplete status if any global requirement, required tree expansion, actual record, or requested consumer remains outstanding.

### Waiting for system support

Report a blocker only after confirming a system tool, runner, or receipt issue outside the project. The generator repairs product failures within scope. runtimeCompatible=false on a project using the current protocol is a normal version gate. A scene task can rebuild and verify in the original project. A collection-only task retains the compatibility gap. Keep older packages. A historical receipt or physical replay difference alone does not establish a current system fault. Call `generation_wait_system(requestId, reason, evidence, jobIds=...)` with the current projectId/generationId. Supply a nonempty evidence list of actual errors, saved log locations, or job receipts. Optional jobIds must belong to this generation. The call records a blocker claim. It does not certify a root cause or replace quality review.

End the current turn after waiting_system. Current calls, accepted jobs, and native subtasks may finish or reconcile. Existing jobs may still be inspected, queried, or cancelled. New computation is rejected. Polling, child completion, bridge reconnection, and ordinary messages do not release the wait. Keep requestId on transport retry. Reusing it with different input is rejected. A new blocker after repair needs a new requestId. A late retry must not relock a resumed job.

For automatic continuation, the host looks for observable durable progress in sources/robot configurations, requirements, actual jobs, or actual native collaboration completion pointers. Reads of existing status and repeated explanations do not count. Reconcile active or uncertain native subtasks first. Lack of observable progress is a reason to wait for handling, not a system fault or product-quality judgment.

After repair, explicitly `POST /api/projects/:projectId/resume` with the same generationId, or use the web resume control. The host verifies native idleness and the absence of outstanding jobs, approvals, or uncertain sends. Preserve requirements, session, and failures. Ordinary supplemental messages during a wait may be saved but cannot launch or steer a native turn. Do not double-count system and approval waits. timeBudgetMs=null retains unlimited total duration and individual processes retain their own timeout guards.

## Independent internal review

The generator convenes the native direct subagent `scene_quality_review` to inspect the complete request and current products. The reviewer is independent, does not author or edit products, and examines the original request, frozen World, scoring sources, applicable verification cases, actual images, and proposed execution results directly. When robots are involved, verify the model, source, and preserved kinematic structure. Do not rely solely on the author's summary. Select views and times appropriate to the task and reuse same-revision records. No fixed first/middle/last frame rule applies. The generator corrects and re-reviews issues within scope, rebuilding the environment only when it changes. Disclose unmet or unverifiable requirements. The review and final quality judgment are internal generator responsibilities.

Apply the five [scene checks](../SKILL.md#scene-checks-and-independent-review) to the complete requested scene, including unnamed visible background furniture. Inspect actual whole-scene and relevant operation details. Recognizable room type, task score, and technical receipts cannot replace these judgments. Interpret intended style, normal assembly, and inapplicable conditions against the request. Do not impose paper metrics or one robot's dimensions on every task.

Start geometry checks from actual initial poses on the current frozen stage. Use the pinned SDK's `UsdGeom.XformCache` for world transforms and `UsdGeom.BBoxCache` for candidate extents of visible geometry. Check support surfaces, contact, or the necessary geometry queries. Distinguish default/render geometry from guide collision proxies. Bounds overlap alone cannot establish solid intersection or containment. Avoid unverified solid-containment algorithms for open meshes, thin shells, or soft bodies. Distinguish visual-mesh from collision-body faults. If needed, write a project-local read-only measurement or verification source with input revision and actual facts. Do not add kernel furniture recipes.

Verify the actual support/mount path for the whole object. Bed legs can support a frame, a wall can hold a cabinet, and a counter can support a sink. Not every part needs ground contact. Group membership or supported_by alone does not excuse errors. Distinguish floors, tabletops, room interiors, and wall/ceiling mounting surfaces. Locate the operation on the usable side of its workspace. Check end-effector approach and moving-part sweeps separately. Do not demand a standing position under a wall cabinet or sink.

Preserve correct fixed mounts, connections, joints, and material types. Use actual records or appropriate cases to check collapse, penetration/fall-through, required motion response, collision shapes, and readable state. Inspect usable cavities and moving space. Counting bodies/joints or observing stillness is insufficient. Do not free all mounted fixtures or fix all movable objects to achieve stability. For liquids and soft bodies, verify the states and appearance the request needs. No universal duration, threshold, or successful robot demonstration applies. Retain uncertainty, approximation error, and unobserved scope. Successful numerical stepping does not establish complete physical correctness.

Use existing requirements, views, issues, or notes to explain actual evidence, problems, and necessary unverified items across the five aspects. A necessary requirement without adequate evidence cannot pass. After the author repairs the product, repeat internal review for the current revision, reusing unchanged same-revision facts. The host does not interpret quality notes, mandate tool sequences, or require new receipt fields for this guidance.

The reviewer returns one complete JSON object with schema `sceneagent-internal-review-v2` as the last agentMessage of its latest completed turn. Use phase final_answer. For protocols returning phase=null, the last message of a completed turn is accepted. Delivery reads the following fields.

- `projectId/generationId/requirementVersion/requestHash/sourceHash/revision` binds the project, generation, full request, and frozen source revision.
- For the scoped contract, return `requiredOutputs/environmentRefs` verbatim at the top level, matching the request and locked selection including revisions and applicable taskIds/split. Do not omit them, default to three products, or reselect the scope. For evaluation, retain every revision's entire frozen task set. taskIds may be omitted or list the complete set. Older contracts retain their frozen fields.
- For the new `producer-exports-v1` contract, return the same top-level `producedArtifacts={manifestRef,sha256}` as the actual delivery request. Changes to exported content or manifest require review of the new digest. Leave this field absent for older contracts without it.
- `decision` is pass or needs_changes. Only pass can request delivery.
- `coverage.episodes` is `[]` without demonstrations. Applicable verification cases still appear in task checks and the evaluation product. For demonstrations it is a nonempty, duplicate-free array of `{episodeId,revision,use}`, with revisions matching environmentRefs. Do not also send legacy episodeIds. Each required revision/task needs at least one actual execution from the current generation. use is `execution_data` or `successful_demonstration`. The former makes no success claim. The latter requires actual complete execution, result.success=true, and demonstrationEligible=true. Requests for completion or successful demonstrations cannot be fulfilled with failed data alone. Older entries follow their frozen definitions.

The reviewer may include requirements, views, rubrics, issues, resolvedIssues, evidence references, and notes for repair and traceability. The host does not judge their quality, inspect per-step tool usage, image inputs, fixed frames, or historical issue-closure mappings. They add no second system score or approval.

Submit `review={threadId,turnId,itemId}` to `project_deliver`, pointing to the direct child reviewer's final pass in its latest completed turn. The host verifies native identity, direct parent/role, latest completed turn, final pointer, current binding, and selected executions. It does not require a complete MCP image-view completion notification. Unrelated sessions in the same directory, self-authored pass files, and historical passes cannot replace a current native receipt.

Match selected executions to completed rollout jobs from the same generation and revision. Within the requested scope, technical consumers verify publication manifests, applicable frozen behavior/verification and scoring sources, actual records and clocks, arrays and media, same-fact scoring, and eligibility for successful uses. Reading a review receipt does not establish successful consumption. Changes to source, requirements, revision, selected executions, or uses require review. New records do not silently enter an older request. Use a new requestId for a deliberate new delivery attempt and retain past failures. During preparation, submission, and finalization, reread only the selected latest native receipt and retain the existing small `.trace/inputs` receipt and digest. Report read/connection failures without accepting cached passes or reconstructing the entire review history. Announce completion only after all requested consumers pass and accepted is saved durably.
