import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const code = await readFile(new URL('../web/app.js', import.meta.url), 'utf8');
const html = await readFile(new URL('../web/index.html', import.meta.url), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));
const sourceFiles = name => Object.fromEntries(['scene', 'tasks', 'expert', 'rubrics'].map(key => [key, `${name} ${key}`]));

function page() {
  const requests = [], confirmations = [], listeners = {};
  let confirmResult = true;
  const element = id => ({ id, value: '', textContent: '', children: [], dataset: {}, open: false, listeners: {},
    style: { setProperty() {} }, classList: { toggle() {} },
    addEventListener(name, handler) { this.listeners[name] = handler; },
    setAttribute() {}, getAttribute() { return 'false'; },
    append(...rows) { this.children.push(...rows); }, replaceChildren(...rows) { this.children = rows; },
    showModal() { this.open = true; }, close() { this.open = false; }, focus() {} });
  const elements = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => element(match[1]));
  const sourceTabs = [...html.matchAll(/\bdata-source="([^"]+)"/g)].map(match => ({ ...element('tab'), dataset: { source: match[1] } }));
  const document = { getElementById: id => elements.find(row => row.id === id),
    querySelectorAll: selector => selector === '[id]' ? elements : selector === '[data-source]' ? sourceTabs : [],
    querySelector: () => element('query'),
    createElement: element, createTextNode: text => ({ text }) };
  const fetch = (path, options = {}) => new Promise((resolve, reject) => {
    requests.push({ path, options, reject, resolve(value) {
      resolve({ ok: true, headers: { get() { return 'application/json'; } }, async json() { return value; } });
    } });
  });
  // Execute the actual editor functions and handlers without starting networking or WebGL.
  const run = new Function('document', 'window', 'localStorage', 'fetch', 'setTimeout', 'clearTimeout',
    code.slice(0, code.indexOf('\nawait refreshState();')) +
    '\nreturn { dom, selectProject(id) { activeId = id; } };');
  const window = { addEventListener(name, handler) { listeners[name] = handler; },
    confirm(message) { confirmations.push(message); return confirmResult; } };
  const result = run(document, window, { getItem() { return ''; } }, fetch, () => 0, () => {});
  return { ...result, requests, confirmations,
    open(id) { result.selectProject(id); result.dom['edit-source'].onclick(); },
    type(text) {
      assert.notEqual(result.dom['source-editor'].disabled, true, 'the editor must be available for input');
      result.dom['source-editor'].value = text; result.dom['source-editor'].oninput();
    },
    tab(name) { sourceTabs.find(button => button.dataset.source === name).onclick(); },
    allowConfirm(value) { confirmResult = value; },
    warnsBeforeUnload() {
      let warned = false;
      listeners.beforeunload({ preventDefault() { warned = true; } });
      return warned;
    } };
}

test('a late source read cannot replace the source files of the newly opened project', async () => {
  const p = page(), a = sourceFiles('A'), b = sourceFiles('B');
  p.open('p_A');
  p.dom['close-source'].onclick();
  p.open('p_B');
  p.requests[1].resolve(b); await settle();
  assert.equal(p.dom['source-editor'].value, b.scene);
  p.requests[0].resolve(a); await settle();
  assert.equal(p.dom['source-editor'].value, b.scene, 'late A response must not replace the B editor');
  const saving = p.dom['save-source'].onclick();
  assert.equal(p.requests[2].path, '/api/projects/p_B/sources');
  assert.deepEqual(JSON.parse(p.requests[2].options.body), b, 'B must retain all four of its own source files');
  p.requests[2].resolve({ saved: true }); await saving;
});

test('edits made while saving remain unsaved and still require a close confirmation', async () => {
  const p = page();
  p.open('p_A'); p.requests[0].resolve(sourceFiles('A')); await settle();
  p.type('submitted edit');
  const saving = p.dom['save-source'].onclick();
  assert.equal(JSON.parse(p.requests[1].options.body).scene, 'submitted edit');
  p.type('newer unsaved edit');
  p.requests[1].resolve({ saved: true }); await saving;
  assert.equal(p.dom['source-editor'].value, 'newer unsaved edit');
  assert.equal(p.warnsBeforeUnload(), true, 'the save response only acknowledges the earlier submitted text');
  assert.match(p.dom['source-state'].textContent, /未保存/);
  p.dom['close-source'].onclick();
  assert.equal(p.confirmations.length, 1, 'closing must warn about the later unsaved edit');
});

test('a save response from a closed project cannot clear edits in a later project', async () => {
  const p = page();
  p.open('p_A'); p.requests[0].resolve(sourceFiles('A')); await settle();
  p.type('A submitted edit');
  const savingA = p.dom['save-source'].onclick();
  p.dom['close-source'].onclick();
  p.open('p_B'); p.requests[2].resolve(sourceFiles('B')); await settle();
  p.type('B unsaved edit');
  const bState = p.dom['source-state'].textContent;
  p.requests[1].resolve({ saved: true }); await savingA;
  assert.equal(p.dom['source-editor'].value, 'B unsaved edit');
  assert.equal(p.warnsBeforeUnload(), true, 'saving A must not acknowledge B edits');
  assert.equal(p.dom['source-state'].textContent, bState, 'A save status must not appear in the B editor');
});

test('reloading keeps only the latest read, including late failures and button cleanup', async () => {
  for (const oldFails of [false, true]) for (const latestFirst of [false, true]) {
    const p = page(); p.open('p_A'); p.dom['reload-source'].onclick();
    assert.equal(p.requests.length, 2);
    const finishOld = async () => {
      if (oldFails) p.requests[0].reject(new Error('old read failed'));
      else p.requests[0].resolve(sourceFiles('old'));
      await settle();
    };
    if (latestFirst) {
      p.requests[1].resolve(sourceFiles('latest')); await settle();
      await finishOld();
    } else {
      const loading = p.dom['source-state'].textContent;
      await finishOld();
      assert.equal(p.dom['source-state'].textContent, loading);
      assert.equal(p.dom['source-editor'].disabled, true);
      assert.equal(p.dom['save-source'].disabled, true);
      p.requests[1].resolve(sourceFiles('latest')); await settle();
    }
    assert.equal(p.dom['source-editor'].value, 'latest scene');
    assert.equal(p.dom['source-editor'].disabled, false);
    assert.equal(p.dom['save-source'].disabled, false);
    assert.equal(p.dom.toast.textContent, '', 'an obsolete read must not show an error toast');
  }
});

test('closing and reopening the same project invalidates old reads and their errors', async () => {
  for (const oldFails of [false, true]) {
    const p = page(); p.open('p_A'); p.dom['close-source'].onclick(); p.open('p_A');
    p.requests[1].resolve(sourceFiles('reopened')); await settle();
    p.type('reopened unsaved edit');
    if (oldFails) p.requests[0].reject(new Error('closed session failed'));
    else p.requests[0].resolve(sourceFiles('closed'));
    await settle();
    assert.equal(p.dom['source-editor'].value, 'reopened unsaved edit');
    assert.equal(p.warnsBeforeUnload(), true);
    assert.match(p.dom['source-state'].textContent, /未保存/);
    assert.equal(p.dom['source-editor'].disabled, false);
    assert.equal(p.dom['save-source'].disabled, false);
    assert.equal(p.dom.toast.textContent, '');
  }
});

test('a failed read cannot save blank sources and can be retried normally', async () => {
  const p = page(); p.open('p_A');
  await p.dom['save-source'].onclick();
  assert.equal(p.requests.length, 1, 'saving must be blocked while reading');
  p.requests[0].reject(new Error('read unavailable')); await settle();
  assert.match(p.dom['source-state'].textContent, /读取失败/);
  assert.equal(p.dom['source-editor'].disabled, true);
  assert.equal(p.dom['save-source'].disabled, true);
  await p.dom['save-source'].onclick();
  assert.equal(p.requests.length, 1, 'failed reads must not permit an empty PUT');
  p.dom['reload-source'].onclick(); p.requests[1].resolve(sourceFiles('retry')); await settle();
  assert.equal(p.dom['source-editor'].value, 'retry scene');
  assert.equal(p.dom['source-editor'].disabled, false);
  assert.equal(p.dom['save-source'].disabled, false);
});

test('saving locks reloading and duplicate saves while preserving edits across all source tabs', async () => {
  const p = page(), expected = sourceFiles('A');
  p.open('p_A'); p.requests[0].resolve(expected); await settle();
  p.type('submitted scene'); expected.scene = 'submitted scene';
  const firstSave = p.dom['save-source'].onclick();
  assert.deepEqual(JSON.parse(p.requests[1].options.body), expected);
  assert.equal(p.dom['save-source'].disabled, true);
  assert.equal(p.dom['reload-source'].disabled, true);
  p.dom['reload-source'].onclick(); await p.dom['save-source'].onclick();
  assert.equal(p.requests.length, 2, 'saving must not overlap a same-session read or save');
  assert.equal(p.confirmations.length, 0, 'a blocked reload must not ask to discard edits');
  for (const key of ['tasks', 'expert', 'rubrics', 'scene']) {
    p.tab(key); expected[key] = `new ${key}\n`; p.type(expected[key]);
  }
  p.requests[1].resolve({ saved: true }); await firstSave;
  assert.equal(p.warnsBeforeUnload(), true);
  assert.match(p.dom['source-state'].textContent, /未保存/);
  assert.equal(p.dom['save-source'].disabled, false);
  assert.equal(p.dom['reload-source'].disabled, false);
  const secondSave = p.dom['save-source'].onclick();
  assert.deepEqual(JSON.parse(p.requests[2].options.body), expected, 'the next save must include current text from every tab');
  p.requests[2].resolve({ saved: true }); await secondSave;
  assert.equal(p.warnsBeforeUnload(), false);
  assert.match(p.dom['source-state'].textContent, /已保存/);
  p.dom['close-source'].onclick();
  assert.equal(p.confirmations.length, 0);
});

test('changing text and restoring the submitted bytes during a save leaves the editor clean', async () => {
  const p = page(); p.open('p_A'); p.requests[0].resolve(sourceFiles('A')); await settle();
  p.type('submitted scene\n'); const saving = p.dom['save-source'].onclick();
  p.type('temporary change'); p.type('submitted scene\n');
  p.tab('tasks'); p.type('temporary task'); p.type('A tasks');
  p.requests[1].resolve({ saved: true }); await saving;
  assert.equal(p.warnsBeforeUnload(), false);
  assert.match(p.dom['source-state'].textContent, /已保存/);
});

test('a failed save preserves unsaved input and restores save and reload controls for retry', async () => {
  const p = page(); p.open('p_A'); p.requests[0].resolve(sourceFiles('A')); await settle();
  p.type('unsaved scene'); const saving = p.dom['save-source'].onclick();
  p.requests[1].reject(new Error('save unavailable')); await saving;
  assert.equal(p.dom['source-editor'].value, 'unsaved scene');
  assert.equal(p.warnsBeforeUnload(), true);
  assert.match(p.dom['source-state'].textContent, /保存失败/);
  assert.equal(p.dom['source-editor'].disabled, false);
  assert.equal(p.dom['save-source'].disabled, false);
  assert.equal(p.dom['reload-source'].disabled, false);
  const retry = p.dom['save-source'].onclick();
  assert.equal(JSON.parse(p.requests[2].options.body).scene, 'unsaved scene');
  p.requests[2].resolve({ saved: true }); await retry;
  assert.equal(p.warnsBeforeUnload(), false);
});

test('obsolete save success, failure and cleanup cannot affect another project or its failed read', async () => {
  for (const saveFails of [false, true]) for (const readFails of [false, true]) {
    const p = page(); p.open('p_A'); p.requests[0].resolve(sourceFiles('A')); await settle();
    p.type('submitted A'); const saving = p.dom['save-source'].onclick();
    p.dom['close-source'].onclick(); p.open('p_B');
    if (readFails) { p.requests[2].reject(new Error('current read failed')); await settle(); }
    const state = p.dom['source-state'].textContent, toast = p.dom.toast.textContent;
    if (saveFails) p.requests[1].reject(new Error('old save failed'));
    else p.requests[1].resolve({ saved: true });
    await saving;
    assert.equal(p.dom['source-state'].textContent, state);
    assert.equal(p.dom.toast.textContent, toast);
    assert.equal(p.dom['source-editor'].disabled, true);
    assert.equal(p.dom['save-source'].disabled, true, 'old cleanup must not unlock an unread editor');
    assert.equal(p.dom['reload-source'].disabled, false);
    if (!readFails) { p.requests[2].resolve(sourceFiles('current')); await settle(); }
  }
});

test('an obsolete read cannot replace text or unlock controls while a newer read is being saved', async () => {
  const p = page(); p.open('p_A'); p.dom['reload-source'].onclick();
  p.requests[1].resolve(sourceFiles('latest')); await settle();
  p.type('latest submitted edit'); const saving = p.dom['save-source'].onclick();
  p.requests[0].resolve(sourceFiles('old')); await settle();
  assert.equal(p.dom['source-editor'].value, 'latest submitted edit');
  assert.equal(p.dom['save-source'].disabled, true);
  assert.equal(p.dom['reload-source'].disabled, true);
  p.requests[2].resolve({ saved: true }); await saving;
  assert.equal(p.warnsBeforeUnload(), false);
  assert.equal(p.dom['save-source'].disabled, false);
});

test('cancelled close, Escape and reload preserve unsaved edits until discard is confirmed', async () => {
  const p = page(); p.open('p_A'); p.requests[0].resolve(sourceFiles('A')); await settle();
  p.type('unsaved scene'); p.allowConfirm(false);
  p.dom['close-source'].onclick(); p.dom['reload-source'].onclick();
  let prevented = false;
  p.dom['source-dialog'].listeners.cancel({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(p.dom['source-dialog'].open, true);
  assert.equal(p.dom['source-editor'].value, 'unsaved scene');
  assert.equal(p.warnsBeforeUnload(), true);
  assert.equal(p.requests.length, 1);
  p.allowConfirm(true); p.dom['close-source'].onclick(); p.open('p_B');
  assert.equal(p.warnsBeforeUnload(), false, 'confirmed discarded edits must not leak into a new project');
  p.requests[1].resolve(sourceFiles('B')); await settle();
  assert.equal(p.dom['source-editor'].value, 'B scene');
});

test('reopening the same project waits for the previous save before reading or saving again', async () => {
  for (const saveFails of [false, true]) {
    const p = page(); let stored = sourceFiles('initial');
    p.open('p_A'); p.requests[0].resolve(stored); await settle();
    p.type('older save'); const olderSave = p.dom['save-source'].onclick();
    p.dom['close-source'].onclick(); p.open('p_A');
    await p.dom['save-source'].onclick();
    p.dom['reload-source'].onclick(); p.dom['reload-source'].onclick();
    assert.equal(p.requests.length, 2, 'reopening A must wait for its pending PUT before a GET or another PUT');
    assert.equal(p.dom['source-editor'].disabled, true);
    assert.equal(p.dom['save-source'].disabled, true);
    assert.equal(p.dom['reload-source'].disabled, true);
    if (saveFails) p.requests[1].reject(new Error('older save failed'));
    else { stored = JSON.parse(p.requests[1].options.body); p.requests[1].resolve({ saved: true }); }
    await olderSave; await settle();
    assert.equal(p.dom.toast.textContent, '', 'the closed save must not report success or failure in the reopened editor');
    assert.equal(p.requests[2].path, '/api/projects/p_A/sources');
    assert.equal(p.requests[2].options.method, undefined, 'the next request must read after the old save settled');
    p.requests[2].resolve(stored); await settle();
    assert.equal(p.dom['source-editor'].value, stored.scene);
    p.type('newer save'); const newerSave = p.dom['save-source'].onclick();
    stored = JSON.parse(p.requests[3].options.body); p.requests[3].resolve({ saved: true }); await newerSave;
    assert.equal(stored.scene, 'newer save');
    assert.equal(p.dom['source-editor'].value, stored.scene);
    assert.equal(p.warnsBeforeUnload(), false);
  }
});

test('repeated closes while waiting for a save do not block another project or revive obsolete reads', async () => {
  for (const saveFails of [false, true]) {
    const p = page(); p.open('p_A'); p.requests[0].resolve(sourceFiles('A')); await settle();
    p.type('pending A'); const savingA = p.dom['save-source'].onclick();
    for (let i = 0; i < 2; i++) {
      p.dom['close-source'].onclick(); p.open('p_A');
      assert.equal(p.requests.length, 2, 'closing and reopening must not forget the same pending save');
      assert.equal(p.dom['source-dialog'].open, true);
      p.dom['reload-source'].onclick();
    }
    p.dom['close-source'].onclick(); p.open('p_B');
    assert.equal(p.requests[2].path, '/api/projects/p_B/sources', 'B must not wait for A');
    p.requests[2].resolve(sourceFiles('B')); await settle();
    p.type('independent B'); const savingB = p.dom['save-source'].onclick();
    assert.equal(p.requests[3].path, '/api/projects/p_B/sources');
    p.requests[3].resolve({ saved: true }); await savingB;
    assert.equal(p.warnsBeforeUnload(), false);
    p.dom['close-source'].onclick(); p.open('p_A');
    if (saveFails) p.requests[1].reject(new Error('A save failed'));
    else p.requests[1].resolve({ saved: true });
    await savingA; await settle();
    assert.equal(p.requests.length, 5, 'only the latest open A session may issue its deferred GET');
    assert.equal(p.requests[4].path, '/api/projects/p_A/sources');
    p.requests[4].resolve(sourceFiles('A final')); await settle();
    assert.equal(p.dom['source-editor'].value, 'A final scene');
    assert.equal(p.dom['save-source'].disabled, false);
  }
});
