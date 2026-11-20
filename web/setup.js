export function createSetupView({ document, api, t, i18n, onActivated }) {
  const $ = id => document.getElementById(id);
  const dialog = $('setup-dialog');
  let snapshot, editHash, original, dirty = false, openedForSetup = false, request = 0, pollTimer;
  let gpuEdited = false, hardwareChanged = false, edits = 0;
  const receipts = new Map();
  const fields = {
    workspaceRoot: 'workspace-root', port: 'port', runtimeMode: 'runtime-mode', remoteHost: 'ssh-host',
    remoteRoot: 'data-root', remotePython: 'python', legacyRoot:'legacy-root', codexBinary: 'codex-binary', codexScript: 'codex-script',
    codexModel: 'codex-model', remoteGpuPeakMiB:'gpu-peak', motionDependenciesFile: 'motion-dependencies',
  };
  const names = {
    'workspace-dependencies': '工作台依赖', 'codex-cli': 'Codex CLI', 'isaac6-runtime': 'Isaac Sim 环境',
    'motion-backend': '运动规划后端', 'verify-runtime': '验证新环境',
    'register-existing-assets': '登记已有资产', 'publish-staged-assets': '发布已准备资产',
    'install-artvip': 'ArtVIP', 'install-robodojo-assets': 'RoboDojo',
    'install-simready-selection': 'SimReady 优选资产', 'install-robocasa-lightwheel': 'RoboCasa / Lightwheel',
  };
  const optionNames = {
    destination: '全新的准备或回执目录', prefix: '尚未启用的运行环境目录', version: '明确版本号',
    basePython: '用于创建环境的 Python（可选）', packageIds: '具体资产包（可多选）',
    catalogFile: '计算主机上的资产目录清单', robotPackage: '已有机器人冻结包', stageRequestId: '已有准备记录',
    peakMiB: '预计显存峰值（MiB）',
  };
  const statusNames = { running: '正在安装', completed: '已完成', failed: '执行失败', cancel_requested: '正在请求取消',
    cancelled: '已取消', interrupted: '安装状态待确认', unknown: '退出状态未确认' };
  const text = (element, key, values = {}) => {
    element.setAttribute('data-i18n', key); element.setAttribute('data-i18n-values', JSON.stringify(values));
    element.textContent = t(key, values); return element;
  };
  const node = (tag, key, className) => {
    const element = document.createElement(tag); if (className) element.className = className;
    if (key !== undefined) text(element, key); return element;
  };
  const raw = (tag, value, className) => { const element = node(tag, undefined, className); element.textContent = String(value ?? ''); return element; };
  const error = message => { $('setup-error').textContent = message || ''; $('setup-error').hidden = !message; };
  const bytes = value => Number.isFinite(value) ? (value / 1024 ** 3).toFixed(2) + ' GiB' : t('未提供');
  const targetConfig = () => $('setup-target').value === 'draft' ? snapshot?.configuration.draft : snapshot?.configuration.effective;
  const selectedRecipe = () => snapshot?.recipes.find(row => row.id === $('setup-recipe').value);
  const signature = value => JSON.stringify([value.runtimeMode, value.remoteHost, value.remoteRoot, value.remotePython]);
  function formValue() {
    const value = { ...original };
    for (const [key, id] of Object.entries(fields)) {
      const entry = $('setup-' + id).value.trim();
      if (key === 'port') value[key] = Number(entry);
      else if (key === 'remoteGpuPeakMiB') { delete value[key]; if (entry) value[key] = Number(entry); }
      else if (['codexBinary', 'codexScript', 'codexModel', 'motionDependenciesFile'].includes(key)) { delete value[key]; if (entry) value[key] = entry; }
      else value[key] = entry || null;
    }
    if (value.codexScript) delete value.codexBinary;
    if (value.runtimeMode === 'local') value.remoteHost = null;
    if (hardwareChanged) value.gpuDevices = [];
    if (gpuEdited) {
      value.gpuIndices = [...$('setup-gpus').querySelectorAll('input[data-setup-gpu]')].filter(row => row.checked).map(row => Number(row.value));
      delete value.gpuDevices;
    }
    return value;
  }
  function showGpus(gpus = [], selected = []) {
    const box = $('setup-gpus');
    const legend = node('legend', '可用仿真显卡（首选为默认）');
    box.replaceChildren(legend);
    if (!gpus.length) { box.append(node('p', '先检测计算主机，再选择真实显卡；已有选择会保留。')); return; }
    for (const gpu of gpus) {
      const label = node('label', undefined, 'setup-check');
      const input = node('input'); input.type = 'checkbox'; input.value = String(gpu.index);
      input.setAttribute('data-setup-gpu', ''); input.checked = selected.includes(gpu.index);
      input.onchange = () => { gpuEdited = true; dirty = true; ++edits; };
      label.append(input, raw('span', 'GPU ' + gpu.index + ' · ' + (gpu.name || gpu.uuid)
        + (Number.isFinite(gpu.memoryFreeMiB) ? ' · ' + gpu.memoryFreeMiB + ' MiB ' + t('空闲显存') : '')));
      label.title = gpu.uuid; box.append(label);
    }
  }
  function fillConfig() {
    const configuration = snapshot.configuration;
    original = configuration.draft || configuration.effective || {};
    editHash = configuration.editHash;
    for (const [key, id] of Object.entries(fields)) $('setup-' + id).value = original[key] ?? (key === 'port' ? 8765 : '');
    dirty = gpuEdited = hardwareChanged = false;
    showGpus(original.gpuDevices, (original.gpuDevices || []).map(row => row.index));
    $('setup-ssh-host').disabled = original.runtimeMode !== 'ssh';
  }
  function showConfiguration() {
    const configuration = snapshot.configuration;
    text($('setup-summary'), '工作台：{platform} · 配置文件：{file}', { platform: snapshot.platform.platform, file: configuration.file });
    if (!configuration.restartRequired && !configuration.draftHash && configuration.diagnostics?.length)
      $('setup-config-status').textContent = configuration.diagnostics.join(' ');
    else text($('setup-config-status'), configuration.restartRequired ? '已写入配置，当前进程仍使用原配置；请在任务空闲后手动重启。'
      : configuration.draftHash ? '已有配置草稿，当前任务继续使用生效配置。' : '尚未保存新草稿。');
    $('setup-activate').disabled = !configuration.draftHash;
    const draftOption = [...$('setup-target').options].find(row => row.value === 'draft');
    if (draftOption) draftOption.disabled = !configuration.draftHash;
    if (!configuration.effectiveHash && configuration.draftHash) $('setup-target').value = 'draft';
    if ($('setup-target').value === 'draft' && !configuration.draftHash) $('setup-target').value = 'effective';
  }
  function link(parent, value) {
    if (!value) return;
    if (/^https:\/\//.test(value)) { const anchor = raw('a', value); anchor.href = value; anchor.target = '_blank'; anchor.rel = 'noopener'; parent.append(anchor); }
    else parent.append(raw('span', value));
  }
  function renderLicenses(recipe) {
    const box = $('setup-license-list'); if (!box) return;
    const accepted = [...box.querySelectorAll('input[data-setup-license]')].filter(row => row.checked).map(row => row.value);
    const packages = selectedPackages(recipe);
    const licenses = [...new Set([...(recipe.licenseIds || []), ...packages.flatMap(row => row.licenseIds || [])])];
    box.replaceChildren();
    for (const license of licenses) {
      const label = node('label', undefined, 'setup-check'); const input = node('input');
      input.type = 'checkbox'; input.value = license; input.setAttribute('data-setup-license', ''); input.checked = accepted.includes(license);
      label.append(input, node('span', '我已阅读并接受：{license}', undefined));
      text(label.children[1], '我已阅读并接受：{license}', { license }); box.append(label);
    }
    for (const entry of packages) if (entry.licenseEvidence) box.append(raw('p', JSON.stringify(entry.licenseEvidence), 'setup-note'));
    if (licenses.length) link(box, recipe.licenseUrl || recipe.source);
    if (packages.length) box.append(node('p', '许可记录不代表第三方素材权利已全部核清，请核对所选包的来源与使用条件。', 'setup-note'));
  }
  function selectedPackages(recipe) {
    const select = $('setup-option-packageIds');
    const ids = select ? [...select.options].filter(row => row.selected).map(row => row.value) : [];
    return (recipe?.packages || []).filter(row => ids.includes(row.id));
  }
  function renderRecipe() {
    const container = $('setup-recipes'); container.replaceChildren();
    const recipe = selectedRecipe(); $('setup-install').disabled = !recipe || recipe.available === false;
    if (!recipe) return;
    const info = node('p', recipe.effect === 'prepare' ? '准备新目录，不切换当前任务。' : recipe.effect === 'gpu_verify'
      ? '使用所选配置的显卡执行验证，需要相关显卡可用。' : '此操作会发布资产，需相关资源空闲。', 'setup-note');
    const source = node('p', undefined, 'setup-source'); link(source, recipe.source);
    if (recipe.version) source.append(raw('span', ' · ' + recipe.version));
    if (recipe.diskBytes) source.append(raw('span', ' · ' + bytes(recipe.diskBytes)));
    container.append(info, source);
    if (recipe.reason) container.append(raw('p', recipe.reason, 'setup-note'));
    const grid = node('div', undefined, 'setup-grid');
    for (const option of recipe.options || []) {
      const label = node('label'); label.append(node('span', optionNames[option.key] || option.key));
      const input = node(option.type === 'packages' ? 'select' : 'input'); input.id = 'setup-option-' + option.key;
      input.required = option.required === true;
      if (option.type === 'packages') {
        input.multiple = true; input.size = 5;
        for (const entry of recipe.packages || []) { const choice = raw('option', entry.name + ' · ' + bytes(entry.bytes)); choice.value = entry.id; input.append(choice); }
        input.onchange = () => renderLicenses(recipe);
        label.className = 'setup-wide';
      } else { input.type = option.type === 'number' ? 'number' : 'text'; input.min = '1'; input.autocomplete = 'off'; }
      label.append(input); grid.append(label);
    }
    const licenses = node('div', undefined, 'setup-licenses'); licenses.id = 'setup-license-list';
    container.append(grid, licenses); renderLicenses(recipe);
  }
  function renderRecipes() {
    const selected = $('setup-recipe').value;
    $('setup-recipe').replaceChildren(node('option', '请选择'));
    $('setup-recipe').options[0].value = '';
    for (const recipe of snapshot.recipes) {
      const option = node('option', names[recipe.id] || recipe.id); option.value = recipe.id;
      option.disabled = recipe.available === false; $('setup-recipe').append(option);
    }
    $('setup-recipe').value = selected; renderRecipe();
  }
  function renderProbe(result) {
    const rows = [['检测时间', result.checkedAt], ['检测范围', result.scope], ['检测主机', result.target?.host || result.target?.runtimeMode], ['工作台系统', result.workspace?.platform]];
    if (result.workspace?.codex) {
      rows.push(['Codex', result.workspace.codex.version || result.workspace.codex.error || result.workspace.codex.status],
        ['Codex 登录', t(result.workspace.codex.loggedIn ? '已登录' : '未登录')]);
    }
    if (result.ssh) rows.push(['SSH', result.ssh.connected ? t('连接成功') : result.ssh.error]);
    const compute = result.compute;
    if (compute?.python) rows.push(['计算系统', compute.platform], ['Python', compute.python.executable + ' · ' + compute.python.version],
      ['可用磁盘', bytes(compute.disk?.freeBytes)], ['隔离环境', compute.isolation?.bwrap ? 'bwrap' : t('未检测到')]);
    for (const library of compute?.libraries || []) rows.push(['已登记资产', library.path + ' · ' + (library.error || library.assets)]);
    for (const [name, version] of Object.entries(compute?.packages || {})) rows.push([name, version || t('未检测到')]);
    if (compute?.error) rows.push(['计算检测错误', compute.error]);
    if (compute?.gpuError) rows.push(['显卡检测', compute.gpuError]);
    const container = $('setup-probe-result'); container.replaceChildren();
    for (const [key, value] of rows) if (value !== undefined) { const row = node('div', undefined, 'setup-report-row'); row.append(node('strong', key), raw('span', value)); container.append(row); }
    container.append(node('p', '以上仅为连接和元数据检测，不代表 SDK、机器人任务或正式仿真已验证。', 'setup-note'));
  }
  const pendingReceipts = new Set();
  async function fetchReceipt(previous) {
    if (pendingReceipts.has(previous.requestId)) return;
    pendingReceipts.add(previous.requestId);
    try {
      const receipt = await api('/api/setup/install/' + encodeURIComponent(previous.requestId) + '?cursor=' + (previous.nextCursor || 0));
      receipts.set(receipt.requestId, { ...receipt, events: [...(previous.events || []), ...(receipt.events || [])].slice(-300) });
    } catch (failure) { error(failure.message); }
    finally { pendingReceipts.delete(previous.requestId); }
  }
  function renderInstalls() {
    const expanded = new Set([...$('setup-installs').children].filter(row => row.open).map(row => row.dataset.requestId));
    $('setup-installs').replaceChildren(...[...receipts.values()].map(receipt => {
      const detail = node('details', undefined, 'setup-install-receipt');
      detail.dataset.requestId = receipt.requestId; detail.open = expanded.has(receipt.requestId);
      detail.ontoggle = async () => {
        if (detail.open && receipt.nextCursor === undefined && !pendingReceipts.has(receipt.requestId)) {
          await fetchReceipt(receipt); if (dialog.open) renderInstalls();
        }
      };
      const summary = node('summary'); summary.append(raw('code', receipt.requestId), node('span', statusNames[receipt.status] || receipt.status));
      detail.append(summary);
      if (receipt.error) detail.append(raw('p', receipt.error, 'form-error'));
      if (['unknown', 'interrupted'].includes(receipt.status)) detail.append(node('p', '远端退出尚未确认；请保留目录和记录，不要把连接退出当作安装已取消。', 'setup-note'));
      for (const item of receipt.items || []) {
        if (item.catalogFile) detail.append(raw('p', item.catalogFile, 'setup-note'));
        if (item.registered) detail.append(text(node('p', undefined, 'setup-note'), '资产索引：{count} 项', { count:item.assetCount }));
        const settings = formValue();
        const sameTarget = receipt.target?.runtimeMode === settings.runtimeMode
          && receipt.target?.host === settings.remoteHost && receipt.target?.root === settings.remoteRoot;
        const script = item.recipeId === 'codex-cli' ? item.actual?.command?.[1] : null;
        for (const [id, value, label] of [
          ['setup-codex-script', script, '将 Codex 路径填入设置'],
          ['setup-python', sameTarget && item.actual?.metadata?.python?.executable, '将 Python 路径填入设置'],
          ['setup-motion-dependencies', sameTarget && item.motionDependenciesFile, '将依赖清单填入设置'],
        ]) {
          if (!value) continue;
          const button = node('button', label, 'button secondary'); button.type = 'button';
          button.onclick = () => { $(id).value = value; $(id).oninput(); $(id).focus(); };
          detail.append(raw('p', value, 'setup-note'), button);
        }
      }
      const log = raw('pre', (receipt.events || []).map(row => row.time + ' ' + row.type + ' ' + row.message).join('\n'), 'setup-log');
      detail.append(log);
      if (['running', 'cancel_requested'].includes(receipt.status)) {
        const button = node('button', '请求取消此安装', 'button secondary'); button.type = 'button';
        button.disabled = receipt.status === 'cancel_requested';
        button.onclick = async () => { button.disabled = true; try { await api('/api/setup/install/' + encodeURIComponent(receipt.requestId) + '/cancel', { method: 'POST', body: {} }); await poll(); } catch (failure) { error(failure.message); button.disabled = false; } };
        detail.append(button);
      }
      return detail;
    }));
  }
  async function poll() {
    clearTimeout(pollTimer); if (!dialog.open) return;
    const active = row => ['running', 'cancel_requested'].includes(row.status) || row.moreEvents;
    await Promise.all([...receipts.values()].filter(active).map(fetchReceipt));
    if (dialog.open) { renderInstalls(); if ([...receipts.values()].some(active)) pollTimer = setTimeout(poll, 2500); }
  }
  async function load(fill = false) {
    const version = ++request, requestedEdits = edits; error(''); text($('setup-summary'), '正在读取设置…');
    try {
      const value = await api('/api/setup'); if (version !== request) return;
      snapshot = value; $('setup-content').hidden = false;
      if (fill && requestedEdits === edits || !original) fillConfig();
      showConfiguration(); renderRecipes();
      for (const receipt of value.installations || []) if (!receipts.has(receipt.requestId)) receipts.set(receipt.requestId, receipt);
      if (value.lastProbe) renderProbe(value.lastProbe);
      renderInstalls(); poll();
    } catch (failure) {
      if (version !== request) return;
      $('setup-content').hidden = true;
      text($('setup-summary'), '当前工作台尚未提供设置接口；现有工程和预览可继续使用。');
      error(failure.message);
    }
  }
  async function open() { if (!dialog.open) dialog.showModal(); await load(!dirty); }
  function close() { clearTimeout(pollTimer); ++request; dialog.close(); }
  async function guarded(button, work) {
    if (button.dataset.pending === 'true') return;
    button.dataset.pending = 'true'; button.disabled = true; error('');
    try { await work(); } catch (failure) { error(failure.message); }
    finally { button.disabled = false; delete button.dataset.pending; }
  }
  $('open-settings').onclick = open;
  $('setup-close').onclick = close;
  dialog.addEventListener('cancel', () => { clearTimeout(pollTimer); ++request; });
  $('setup-refresh').onclick = () => {
    if (dirty && !document.defaultView.confirm(t('重新读取会丢弃未保存的设置，继续吗？'))) return;
    return load(true);
  };
  for (const [key, id] of Object.entries(fields)) $('setup-' + id).oninput = () => {
    dirty = true; ++edits;
    if (['runtimeMode', 'remoteHost', 'remoteRoot', 'remotePython'].includes(key)) {
      hardwareChanged = true; gpuEdited = false; showGpus();
    }
    $('setup-ssh-host').disabled = $('setup-runtime-mode').value !== 'ssh';
  };
  $('setup-runtime-mode').onchange = $('setup-runtime-mode').oninput;
  $('setup-form').onsubmit = event => {
    event.preventDefault(); if (!snapshot) return;
    return guarded($('setup-save'), async () => {
      const value = formValue(), submittedEdits = edits;
      const result = await api('/api/setup/config', { method: 'PUT', body: { action: 'save', baseHash: editHash, value } });
      await load(submittedEdits === edits);
      const unchanged = submittedEdits === edits; dirty = !unchanged;
      if (!unchanged) editHash = result.draftHash;
      text($('setup-config-status'), unchanged ? '配置草稿已保存，尚未启用。' : '此前输入已保存；当前仍有未保存修改。'); return result;
    });
  };
  $('setup-activate').onclick = () => {
    if (!snapshot?.configuration.draftHash) return;
    return guarded($('setup-activate'), async () => {
      await api('/api/setup/config', { method: 'PUT', body: { action: 'activate', draftHash: snapshot.configuration.draftHash } });
      await load(false); text($('setup-config-status'), '已写入配置，当前进程仍使用原配置；请在任务空闲后手动重启。');
      await onActivated?.();
    });
  };
  for (const button of document.querySelectorAll('[data-setup-probe]')) button.onclick = () => guarded(button, async () => {
    const settings = formValue(), captured = signature(settings);
    delete settings.gpuIndices;
    const result = await api('/api/setup/probe', { method: 'POST', body: { target: button.dataset.setupProbe, settings } });
    if (captured !== signature(formValue())) { error(t('检测期间连接配置已改变，请重新检测。')); return; }
    renderProbe(result);
    if (button.dataset.setupProbe === 'compute' && result.compute?.gpus) {
      const selection = gpuEdited ? [...$('setup-gpus').querySelectorAll('input[data-setup-gpu]')].filter(row => row.checked).map(row => Number(row.value))
        : hardwareChanged ? [] : (original.gpuDevices || []).map(row => row.index);
      showGpus(result.compute.gpus, selection);
    }
  });
  $('setup-recipe').onchange = renderRecipe;
  $('setup-target').onchange = renderRecipe;
  $('setup-install-form').onsubmit = event => {
    event.preventDefault();
    return guarded($('setup-install'), async () => {
      const recipe = selectedRecipe(); if (!recipe || recipe.available === false) throw new Error(t('请选择具体安装项目。'));
      const options = {};
      for (const option of recipe.options || []) {
        const input = $('setup-option-' + option.key);
        const value = option.type === 'packages' ? [...input.options].filter(row => row.selected).map(row => row.value) : input.value.trim();
        if (option.required && (!value || Array.isArray(value) && !value.length)) throw new Error(t('请填写：{field}', { field: t(optionNames[option.key] || option.key) }));
        if (value && (!Array.isArray(value) || value.length)) options[option.key] = value;
      }
      const required = [...new Set([...(recipe.licenseIds || []), ...selectedPackages(recipe).flatMap(row => row.licenseIds || [])])];
      const acceptedLicenses = [...$('setup-license-list').querySelectorAll('input[data-setup-license]')].filter(row => row.checked).map(row => row.value);
      if (required.some(id => !acceptedLicenses.includes(id))) throw new Error(t('请先阅读并明确接受所选项目的许可。'));
      const configuration = $('setup-target').value;
      const targetHash = snapshot.configuration[configuration === 'draft' ? 'draftHash' : 'effectiveHash'];
      if (!targetHash || !targetConfig()) throw new Error(t('请先保存完整配置草稿，或选择已有生效配置。'));
      const requestId = 'install_' + crypto.randomUUID().replaceAll('-', '');
      const receipt = await api('/api/setup/install', { method: 'POST', body: { requestId, configuration, targetHash, items: [{ recipeId: recipe.id, options, acceptedLicenses }] } });
      receipts.set(receipt.requestId, receipt); renderInstalls(); poll();
    });
  };
  function tunnelCommand() {
    const host = $('setup-tunnel-host').value.trim(), local = Number($('setup-tunnel-port').value), remote = Number($('setup-tunnel-remote-port').value);
    const valid = /^[a-zA-Z0-9][a-zA-Z0-9_.@:[\]-]{0,199}$/.test(host) && [local, remote].every(value => Number.isInteger(value) && value > 0 && value < 65536);
    $('setup-tunnel-command').textContent = valid ? `ssh -NT -o ExitOnForwardFailure=yes -o ForwardAgent=no -o StrictHostKeyChecking=yes -L 127.0.0.1:${local}:127.0.0.1:${remote} "${host}"` : t('填写已有 SSH 别名和有效端口后显示连接命令。');
  }
  for (const id of ['setup-tunnel-host', 'setup-tunnel-port', 'setup-tunnel-remote-port']) $(id).oninput = tunnelCommand;
  tunnelCommand();
  return { open, close,
    applyState(state) { if (state.config?.configured === false && !openedForSetup) { openedForSetup = true; open(); } },
    languageChanged() { i18n.apply(document); tunnelCommand(); },
  };
}
