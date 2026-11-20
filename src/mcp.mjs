import { pathToFileURL } from 'node:url';
import { isDeepStrictEqual } from 'node:util';

const id = { type: 'string', minLength: 1 };
const ids = { type: 'array', items: id, minItems: 1 };
const scope = { projectId: id, generationId: id };
const revision = { revision: id };
const environments = { type: 'array', minItems: 1, items: { type: 'object', properties: { revision: id,
  split: { type: 'string', enum: ['train', 'validation', 'test'] }, taskIds: ids }, required: ['revision'], additionalProperties: false } };
const operation = { requestId: { ...id, description: 'Stable identifier: reuse it for transport retries; use a new identifier for a new attempt.' } };
const kitOperation = { ...operation, workerTimeoutSeconds: { type: 'number', exclusiveMinimum: 0,
  description: 'Optional positive finite whole-Kit wall-clock seconds; omitted uses 900. Covers initialization, execution, child verification and normal SDK shutdown through actual Kit exit. Bounded termination grace after timeout and parent verification/publication are separate. Changing this limit is a new attempt, not a new scene revision.' } };
const retiredChangedNodes = { ...ids, deprecated: true, description: 'Historical exact retries only; new requests must omit this retired hint. Builds and checks retain their actual full scope.' };

function tool(description, properties, required = [], readOnly = false) {
  return { description,
    inputSchema: { type: 'object', properties: { ...scope, ...properties }, required: [...Object.keys(scope), ...required], additionalProperties: false },
    annotations: { readOnlyHint: readOnly, destructiveHint: false, idempotentHint: true, openWorldHint: false } };
}

// This is the sole catalog. Studio imports it without starting a server or loading the SDK.
export const TOOL_SCHEMAS = Object.freeze({
  project_inspect: tool('Read the preserved request, source paths/hash, capabilities, jobs and delivery gaps. nodeId selects a subtree and its ancestors/external dependencies.',
    { nodeId: id, baseRevision: id }, [], true),
  assets_lookup: tool('Search actual models or material packs; inspect a stable asset ID. Search preferred collections first, then broad sources when candidates do not fit. A preferred source is not per-object visual or robot validation.',
    { query: { type: 'string' }, assetId: id, kind: { type: 'string', enum: ['model', 'material'] },
      libraryTier: { type: 'string', enum: ['preferred', 'broad', 'all'] },
      collection: { ...id, description: 'Exact collection ID from the catalog; not a fixed source whitelist.' },
      requiresJoints: { type: 'boolean', description: 'True requires source-inspected internal movableJointCount > 0, excluding fixed/root joints; this is structural evidence, not verified operation. False or omitted does not filter joints; unknown stays unknown.' },
      category: id, textured: { type: 'boolean' },
      size: { type: 'array', items: { type: 'number', exclusiveMinimum: 0 }, minItems: 3, maxItems: 3 },
      limit: { type: 'integer', minimum: 1, maximum: 20 } }, [], true),
  assets_prepare: tool('Prepare exactly one assetId, public self-contained GLB url, local resourceFile with explicit dependencyFiles, or frozen sourceFile defining build_asset(materials). Generated sources may return a mesh or declare native output files. Native packages freeze file identities; this does not certify physical/control capability. Large files stay in frozen storage.',
    { ...kitOperation, assetId: id, url: id, sourcePage: id, name: id, license: { type: 'string' },
      sourceFile: id, expectedSourceHash: id, materialIds: ids, resourceFile: id, dependencyFiles: ids,
      mdlSearchRoots: { type: 'array', items: id, uniqueItems: true,
        description: 'Optional absolute module roots for local resourceFile only. Files must still be explicitly declared in dependencyFiles; roots do not copy directories or certify materials.' } }, ['requestId']),
  scene_build: tool('Freeze project source and prepared resourceRefs into one immutable native scene revision. Supply the source hash from a fresh inspect after edits. A job completing does not mean delivery passed.',
    { ...kitOperation, expectedSourceHash: id, seed: { type: 'integer', minimum: 0 }, baseRevision: id, changedNodeIds: retiredChangedNodes, resourceRefs: ids, environmentSourceRefs: ids },
    ['expectedSourceHash', 'requestId']),
  scene_observe: tool('Observe the actual frozen scene or episode frame. output=web_preview derives a published GLB from the unchanged frozen source, including older runtime versions; it cannot mix with camera, node, episode or frame selection and does not execute tasks or robots. cameraId selects one frozen camera; cameraIds batches 1–4 distinct cameras from the first frozen task in one live-scene job and cannot mix with cameraId, nodeId, episodeId or frame. The batch bound limits this response, not the task camera count. frame is the global trajectory index for one recorded camera. Omitted camera selection uses the primary camera; nodeId selects separate subtree framing.',
    { ...revision, ...kitOperation, output: { type: 'string', enum: ['web_preview'] }, nodeId: id, cameraId: id,
      cameraIds: { ...ids, maxItems: 4, uniqueItems: true }, episodeId: id,
      frame: { type: 'integer', minimum: 0 } }, ['revision'], true),
  task_check: tool('Check frozen bindings and generated rubric sources; compare explicitly referenced validation episodes with their reviewed case expectations. Missing execution is unverified, not semantic success.',
    { ...revision, ...operation, taskIds: ids, baseRevision: id, changedNodeIds: retiredChangedNodes, episodeId: id, episodeIds: ids }, ['revision'], true),
  robot_run: tool('Execute the frozen closed-loop expert or an explicitly selected frozen validation program through observe/step, with optional frozen pure numerical planning helpers. robotSetup installs one or more public robot profiles in an independent snapshot without changing the base revision and requires independent behavior; robotSetupRef reuses a frozen installation instead. Each task resets independently; recording and sensor permissions come from its frozen declaration. Job completion, numerical arrival and generated task success are distinct.',
    { ...revision, ...kitOperation, taskIds: ids, repetitions: { type: 'integer', minimum: 1 },
      rubricSeconds: { type: 'number', exclusiveMinimum: 0,
        description: 'Optional positive finite cumulative grading exchange budget in seconds; omitted uses 30. Requires a frozen environment with adjustableRubricBudget=true. Recorded with the episode and reused by grading replay; does not change physical duration or task meaning.' },
      behavior: { type: 'object', properties: { sourceRef: id, helperRefs: { type: 'array', items: id } }, required: ['sourceRef', 'helperRefs'], additionalProperties: false }, seeds: { type: 'array', items: { type: 'integer', minimum: 0 }, minItems: 1 },
      robotSetup: { type: 'object', properties: {
        robots: { type: 'array', minItems: 1, items: { type: 'object', properties: { id,
          profileRef: { ...id, description: 'Current project *.robot.json file; prepared model and planning packages belong in resourceRefs.' },
          owner: id, frame: id, at: { type: 'array', items: { type: 'number' }, minItems: 3, maxItems: 3 },
          orientation: { type: 'array', items: { type: 'number' }, minItems: 4, maxItems: 4 }, purpose: { type: 'string' } },
        required: ['id', 'profileRef'], additionalProperties: false } },
        resourceRefs: { type: 'array', items: id, description: 'Prepared native assets/report.json references; exact resource bytes are frozen with this installation.' },
        recording: { type: 'object', additionalProperties: { type: 'object' },
          description: 'Optional taskId to complete recording declaration map. Preserve base cameras and primaryCameraId; added robot cameras do not gain policy permission.' } },
      required: ['robots', 'resourceRefs'], additionalProperties: false },
      robotSetupRef: { ...id, description: 'Frozen robot installation manifest in this project and base revision; mutually exclusive with robotSetup.' },
      controller: { type: 'object', properties: { type: { type: 'string', enum: ['expert', 'policy'] },
        sourceRef: id, policyRef: id, caseId: { ...id, description: 'Select a frozen validation case when multiple cases share policyRef.' }, digest: id }, required: ['type'], additionalProperties: false },
      purpose: { type: 'string', enum: ['demonstration', 'verification', 'probe'] } }, ['revision', 'taskIds', 'requestId']),
  job: tool('Query, wait or request cancellation for the same persistent job. waitMs is at most 30000; pending is normal. Cancellation is terminal only after remote confirmation.',
    { jobId: id, action: { type: 'string', enum: ['status', 'wait', 'cancel'] },
      cursor: { type: 'integer', minimum: 0 }, waitMs: { type: 'integer', minimum: 0, maximum: 30_000 } },
    ['jobId', 'action']),
  generation_wait_system: tool('Report a major evidenced blocker that cannot be resolved with project edits or public execution controls, and wait for explicit recovery of this same generation. An ordinary timeout alone is not a system blocker: diagnose its phase and budget, then choose a corrected attempt. Explain why the available controls cannot resolve the obstruction. This records the producer claim, not a host severity or quality judgment. Finish the current turn after the receipt; existing jobs remain queryable and are not cancelled.',
    { ...operation, reason: id, evidence: { type: 'array', items: id, minItems: 1 }, jobIds: ids },
    ['requestId', 'reason', 'evidence']),
  project_deliver: tool('Read the completed native independent reviewer receipt and verify its source, current bindings and fixed behavior records and uses; require the current generation selected technical consumers and preserved requirements. The host does not audit the reviewer\'s individual steps or image/tool history. Execution data and successful demonstrations are distinct. Current consumption validates recorded facts and independent environment interfaces; it does not require another physical run to copy all original values or succeed. review points to the direct scene_quality_review child final message, not a self-reported pass.',
    { ...revision, ...kitOperation, environmentRefs: environments, expectedRequirementVersion: { type: 'integer', minimum: 1 }, requestHash: id,
      producedArtifacts: { type: 'object', properties: { manifestRef: id, sha256: id }, required: ['manifestRef', 'sha256'], additionalProperties: false,
        description: 'New producer-exports-v1 generations require the generator-produced manifest pointer and SHA256, also bound by the native reviewer final receipt. Export, read and repair formats in project delivery/ before submitting.' },
      review: { type: 'object', properties: { threadId: id, turnId: id, itemId: id },
        required: ['threadId', 'turnId', 'itemId'], additionalProperties: false,
        description: 'Required for every new delivery attempt; an exact retry of a historical completed request preserves its original receipt.' },
      evidence: { type: 'array', items: { type: 'object', properties: { requirement: id, nodeId: id,
        artifactRef: id, assessedBy: id, note: { type: 'string' } }, required: ['requirement', 'artifactRef'], additionalProperties: false } } },
    ['revision', 'expectedRequirementVersion', 'requestHash', 'requestId']),
});

export async function createSceneAgentMcp({ studioUrl = process.env.SCENEAGENT_URL,
  projectId = process.env.SCENEAGENT_PROJECT_ID, generationId = process.env.SCENEAGENT_GENERATION_ID,
  fetchImpl = fetch } = {}) {
  if (!studioUrl) throw new Error('SCENEAGENT_URL must point to the running local Studio');
  const url = new URL(studioUrl);
  if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) {
    throw new Error('SceneAgent MCP only connects to a local Studio HTTP endpoint');
  }
  const [{ Server }, { ListToolsRequestSchema, CallToolRequestSchema }] = await Promise.all([
    import('@modelcontextprotocol/sdk/server/index.js'), import('@modelcontextprotocol/sdk/types.js'),
  ]);
  const server = new Server({ name: 'sceneagent', version: '0.1.0' }, { capabilities: { tools: {} } });
  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools:
    Object.entries(TOOL_SCHEMAS).map(([name, definition]) => ({ name, ...definition,
      inputSchema: { ...definition.inputSchema, required: definition.inputSchema.required.filter(key =>
        !(key === 'projectId' && projectId || key === 'generationId' && generationId)) } })) }));
  server.setRequestHandler(CallToolRequestSchema, async (request, extra) => {
    const { name, arguments: input = {} } = request.params;
    if (!Object.hasOwn(TOOL_SCHEMAS, name)) throw new Error(`Unknown SceneAgent tool: ${name}`);
    try {
      const args = { ...input, projectId: input.projectId ?? projectId, generationId: input.generationId ?? generationId };
      if (!args.projectId || !args.generationId) throw new Error('projectId and generationId are required; read the current Studio context');
      if (projectId && args.projectId !== projectId || generationId && args.generationId !== generationId) {
        throw new Error('Tool scope does not match this SceneAgent session');
      }
      const signal = AbortSignal.any([extra.signal, AbortSignal.timeout(45_000)]);
      const response = await fetchImpl(new URL(`/api/tools/${name}`, url), {
        method: 'POST', headers: { 'content-type': 'application/json', 'x-sceneagent-origin': 'codex-mcp' },
        body: JSON.stringify(args), signal, redirect: 'error',
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error ?? `Studio HTTP ${response.status}`);
      if (name === 'project_inspect' && !result.isError && result.structuredContent != null
        && result.content?.length === 1 && result.content[0].type === 'text') {
        if (!isDeepStrictEqual(JSON.parse(result.content[0].text), result.structuredContent)) throw new Error('project_inspect duplicate results conflict');
        const { structuredContent, ...single } = result;
        return single;
      }
      if (Array.isArray(result.content)) return result;
      if (name === 'project_inspect') return { content: [{ type: 'text', text: JSON.stringify(result) }] };
      return { structuredContent: result, content: [{ type: 'text', text: JSON.stringify(result) }] };
    } catch (error) {
      return { isError: true, content: [{ type: 'text', text: error.message }] };
    }
  });
  return server;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const { StdioServerTransport } = await import('@modelcontextprotocol/sdk/server/stdio.js');
  const server = await createSceneAgentMcp();
  await server.connect(new StdioServerTransport());
}
