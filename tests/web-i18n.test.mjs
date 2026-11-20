import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const code = await readFile(new URL('../web/i18n.js', import.meta.url), 'utf8');

function i18n({ saved, navigatorLanguage = 'zh-CN', blockedStorage = false } = {}) {
  const values = new Map(saved === undefined ? [] : [['sceneagent.language', saved]]);
  const window = { navigator: { language: navigatorLanguage } };
  Object.defineProperty(window, 'localStorage', { get() {
    if (blockedStorage === 'access') throw new Error('storage unavailable');
    return {
      getItem(key) { if (blockedStorage) throw new Error('storage unavailable'); return values.get(key) ?? null; },
      setItem(key, value) { if (blockedStorage) throw new Error('storage unavailable'); values.set(key, value); },
    };
  } });
  vm.runInNewContext(code, { window });
  return { ...window.SceneAgentI18n, api: window.SceneAgentI18n, values };
}

function element(text, attributes = {}) {
  return { textContent: text, attributes: { ...attributes },
    getAttribute(name) { return this.attributes[name] ?? null; },
    setAttribute(name, value) { this.attributes[name] = value; },
  };
}

function page(elements) {
  return { documentElement: { lang: '' }, title: '',
    querySelectorAll(selector) {
      const attribute = selector.slice(1, -1);
      return elements.filter(entry => Object.hasOwn(entry.attributes, attribute));
    },
  };
}

test('UI labels and templates switch language without translating inserted project content', () => {
  const { api } = i18n();
  assert.equal(api.language, 'zh');
  assert.equal(api.locale, 'zh-CN');
  assert.equal(api.t('场景预览'), '场景预览');
  api.setLanguage('en');
  assert.equal(api.language, 'en');
  assert.equal(api.locale, 'en-US');
  assert.equal(api.t('场景预览'), 'Scene preview');
  const project = '场景预览 <b>厨房</b> {next} $&';
  assert.equal(api.t('当前工程：{name}', { name: project }), `Current project: ${project}`);
  assert.equal(api.t('读取失败：{error}', { error: '原始 stderr: 场景预览' }), 'Read failed: 原始 stderr: 场景预览');
  api.setLanguage('zh');
  assert.equal(api.t('当前工程：{name}', { name: project }), `当前工程：${project}`);
});

test('only exact known keys translate and unknown templates retain their original content', () => {
  const { api } = i18n({ saved: 'en' });
  assert.equal(api.t('用户说：场景预览'), '用户说：场景预览');
  assert.equal(api.t('未知 {name} / {absent}', { name: '原样' }), '未知 原样 / {absent}');
  assert.equal(api.t('schema.executionStatus'), 'schema.executionStatus');
  assert.equal(api.t('{toString}'), '{toString}');
  assert.equal(api.t('{name}', Object.create({ name: 'inherited' })), '{name}');
  assert.equal(api.t('{count}', { count: 0 }), '0');
});

test('valid saved language takes precedence and selections persist under the single language key', () => {
  const first = i18n({ saved: 'zh', navigatorLanguage: 'en-GB' });
  assert.equal(first.api.language, 'zh');
  first.api.setLanguage('en');
  assert.equal(first.values.get('sceneagent.language'), 'en');
  const restored = i18n({ saved: first.values.get('sceneagent.language'), navigatorLanguage: 'zh-CN' });
  assert.equal(restored.api.language, 'en');
  assert.equal(restored.api.locale, 'en-US');
  assert.equal(i18n({ navigatorLanguage: 'en-GB' }).api.language, 'en');
  assert.equal(i18n({ saved: 'invalid', navigatorLanguage: 'ja-JP' }).api.language, 'zh');
});

test('blocked storage never prevents initial display or an in-memory language change', () => {
  for (const blockedStorage of [true, 'access']) {
    const { api } = i18n({ blockedStorage, navigatorLanguage: 'en-US' });
    assert.equal(api.t('设置'), 'Settings');
    assert.doesNotThrow(() => api.setLanguage('zh'));
    assert.equal(api.t('设置'), '设置');
    assert.equal(api.locale, 'zh-CN');
  }
});

test('apply translates only explicit UI text and accessibility attributes while preserving user data', () => {
  const { api } = i18n({ saved: 'en' });
  const label = element('场景预览', { 'data-i18n': '场景预览' });
  const control = element('用户输入不可翻译', {
    'data-i18n-title': '回到全景', 'title': '回到全景',
    'data-i18n-placeholder': '输入场景或任务要求', 'placeholder': '输入场景或任务要求',
    'data-i18n-aria-label': '预览视角', 'aria-label': '预览视角',
  });
  const userContent = element('场景预览\nconst robot = "场景预览";', { title: '场景预览' });
  const rawName = element('厨房工程', { 'aria-label': '用户要求：场景预览' });
  const doc = page([label, control, userContent, rawName]);
  api.apply(doc);
  assert.equal(label.textContent, 'Scene preview');
  assert.equal(control.textContent, '用户输入不可翻译');
  assert.equal(control.getAttribute('title'), 'Frame scene');
  assert.equal(control.getAttribute('placeholder'), 'Describe the scene or task');
  assert.equal(control.getAttribute('aria-label'), 'Preview view');
  assert.equal(userContent.textContent, '场景预览\nconst robot = "场景预览";');
  assert.equal(userContent.getAttribute('title'), '场景预览');
  assert.equal(rawName.getAttribute('aria-label'), '用户要求：场景预览');
  assert.equal(doc.documentElement.lang, 'en');
  assert.equal(doc.title, 'EmbodiedStudio · Scene workspace');
  api.setLanguage('zh');
  api.apply(doc);
  assert.equal(label.textContent, '场景预览');
  assert.equal(control.getAttribute('aria-label'), '预览视角');
  assert.equal(doc.documentElement.lang, 'zh-CN');
  assert.equal(doc.title, 'EmbodiedStudio · 场景工作台');
});

test('invalid language requests are rejected without changing current language or saved preference', () => {
  const { api, values } = i18n({ saved: 'en' });
  for (const invalid of ['fr', 'EN', '', null, undefined, 0]) {
    assert.throws(() => api.setLanguage(invalid), /Unsupported language/);
    assert.equal(api.language, 'en');
    assert.equal(values.get('sceneagent.language'), 'en');
  }
});

test('switching languages updates explicit templates without replacing questions, answers or pending controls', () => {
  const { api } = i18n();
  const questionText = '请选择油瓶，保留 "用户原文" <b>场景预览</b> {next}';
  const values = JSON.stringify({ question: questionText, name: '厨房工程' });
  const question = element(questionText);
  const label = element('', { 'data-i18n': '{question}：自由回答', 'data-i18n-values': values });
  const answer = element('', { 'data-i18n-aria-label': '{question}：自由回答',
    'data-i18n-placeholder': '填写你的回答', 'data-i18n-title': '当前工程：{name}', 'data-i18n-values': values });
  answer.value = '我尚未提交的中文答案';
  answer.disabled = false;
  answer.selectionStart = 3;
  const submit = element('提交回答', { 'data-i18n': '提交回答' });
  submit.disabled = true;
  const submitHandler = () => answer.value;
  submit.onclick = submitHandler;
  const source = element('未保存源码');
  source.value = 'robot = "场景预览"\n# 未保存';
  source.disabled = false;
  const controls = [question, label, answer, submit, source];
  const doc = page(controls);
  api.setLanguage('en'); api.apply(doc);
  assert.equal(label.textContent, `${questionText}: free-form answer`);
  assert.equal(answer.getAttribute('aria-label'), `${questionText}: free-form answer`);
  assert.equal(answer.getAttribute('title'), 'Current project: 厨房工程');
  assert.equal(answer.getAttribute('placeholder'), 'Enter your answer');
  assert.equal(submit.textContent, 'Submit answer');
  api.setLanguage('zh'); api.apply(doc);
  assert.equal(label.textContent, `${questionText}：自由回答`);
  assert.equal(answer.getAttribute('aria-label'), `${questionText}：自由回答`);
  assert.equal(question.textContent, questionText);
  assert.equal(answer.value, '我尚未提交的中文答案');
  assert.equal(answer.selectionStart, 3);
  assert.equal(answer.disabled, false);
  assert.equal(submit.disabled, true);
  assert.equal(submit.onclick, submitHandler);
  assert.equal(submit.onclick(), answer.value);
  assert.equal(source.value, 'robot = "场景预览"\n# 未保存');
  assert.equal(source.disabled, false);
  assert.deepEqual(controls, [question, label, answer, submit, source]);
});

test('explicit values preserve unknown templates and invalid JSON cannot interrupt other UI labels', () => {
  const { api } = i18n({ saved: 'en' });
  const raw = '场景预览 <img src=x> $&';
  const unknown = element('', { 'data-i18n': '未知模板 {name}', 'data-i18n-values': JSON.stringify({ name: raw }) });
  const broken = ['{', 'null', '["value"]', '"value"', '3'].map(value => element('', {
    'data-i18n': '当前工程：{name}', 'data-i18n-values': value,
  }));
  const label = element('', { 'data-i18n': '场景预览' });
  assert.doesNotThrow(() => api.apply(page([unknown, ...broken, label])));
  assert.equal(unknown.textContent, `未知模板 ${raw}`);
  for (const entry of broken) assert.equal(entry.textContent, 'Current project: {name}');
  assert.equal(label.textContent, 'Scene preview');
  assert.equal(api.t('{count} 进行中', { count: 2 }), '2 running');
  assert.equal(api.t('{count} 待确认', { count: 1 }), '1 awaiting confirmation');
  assert.equal(api.t('Codex · 就绪'), 'Codex · Ready');
  assert.equal(api.t('Codex · 未就绪'), 'Codex · Not ready');
  assert.equal(api.t('Codex 尚未就绪'), 'Codex is not ready');
  assert.equal(api.t('真实执行记录 · 由仿真生成'), 'Actual execution recording · Generated by simulation');
});
