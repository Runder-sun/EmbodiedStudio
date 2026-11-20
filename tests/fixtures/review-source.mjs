import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { Store } from '../../src/store.mjs';
import { createInternalReviewReader } from '../../src/server.mjs';

// Isolated receipt fixtures: these are not generator-produced scenes or quality decisions.
export const digest = bytes => createHash('sha256').update(bytes).digest('hex');
export async function fixture(t) {
  const root = await mkdtemp(path.join(tmpdir(), 'native-review-receipt-'));
  const store = await new Store({ root }).load();
  const project = await store.create({ name: '内部审核回执接线测试', prompt: '隔离系统验证用途' });
  store.beginGeneration(project.id); project.threadId = 'root';
  delete project.generation.deliveryContract; // Existing native-receipt fixtures exercise the historical contract.
  const source = await store.sourceSnapshot(project.id);
  const job = { generationId: project.generation.id, requirementVersion: 1, requestHash: project.generation.requestHash,
    sourceHash: source.sourceHash, revision: 'rev', requiredTaskIds: ['task'] };
  const pointer = { threadId: 'review', turnId: 'review_turn', itemId: 'final' };
  const binding = { projectId: project.id, generationId: job.generationId, requirementVersion: 1,
    requestHash: job.requestHash, sourceHash: source.sourceHash, revision: 'rev' };
  const episode = { projectId: project.id, episode: 'ep', taskId: 'task', revision: 'rev' };
  project.episodes = [episode];
  const run = { id: 'run', generationId: job.generationId, kind: 'rollout', status: 'completed', revision: 'rev',
    result: { projectId: project.id, revision: 'rev', episodes: [structuredClone(episode)] } };
  project.jobs.push(run);
  const report = { schema: 'sceneagent-internal-review-v2', ...binding, decision: 'pass',
    coverage: { episodes: [{ episodeId: 'ep', use: 'successful_demonstration' }] } };
  const childItems = [{ id: 'final', type: 'agentMessage', phase: null, text: JSON.stringify(report) }];
  const childTurns = [{ id: 'review_turn', status: 'completed', rootTurnId: null }];
  const histories = new Map([['review/review_turn', childItems]]);
  const metadata = new Map([['review', { id: 'review', parentThreadId: 'root', status: { type: 'idle' },
    source: { subAgent: { thread_spawn: { parent_thread_id: 'root', agent_path: '/root/scene_quality_review' } } } }]]);
  const calls = [], publications = [];
  const page = (rows, options) => {
    const ordered = options.sortDirection === 'desc' ? [...rows].reverse() : rows;
    const from = options.cursor ? Number(options.cursor) : 0, size = options.limit || 50;
    return { data: ordered.slice(from, from + size), nextCursor: from + size < ordered.length ? String(from + size) : null };
  };
  const codex = {
    async readThread(id, options) {
      calls.push({ method: 'read', id, options }); assert.equal(id, 'review'); assert.equal(options.includeTurns, false);
      return metadata.get(id);
    },
    async listTurns(id, options) {
      calls.push({ method: 'turns', id, options }); assert.equal(id, 'review'); return page(childTurns, options);
    },
    async listItems(id, options) {
      calls.push({ method: 'items', id, options }); assert.equal(id, 'review');
      const rows = histories.get(id + '/' + options.turnId); assert.ok(rows, 'fixture turn exists');
      const result = page(rows, options);
      return { ...result, data: result.data.map(item => ({ turnId: options.turnId, item })) };
    },
  };
  const publishSnapshot = async (...args) => { publications.push(args); };
  const reader = createInternalReviewReader({ store, codex, publishSnapshot });
  const final = () => { childItems.find(row => row.id === 'final').text = JSON.stringify(report); };
  t.after(() => rm(root, { recursive: true, force: true }));
  return { store, project, job, pointer, binding, report, reader, codex, metadata, childTurns, childItems,
    histories, calls, publications, run, final, publishSnapshot };
}
