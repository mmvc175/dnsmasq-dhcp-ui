/* dnsmasq-dhcp-ui 前端逻辑（原生 JS，无构建步骤） */

const state = {
  data: null,
  tab: 'clients',
  filter: '',
  timer: null,
  editingOriginalMac: '',
};

const $ = (id) => document.getElementById(id);

/* ---------------- 请求 ---------------- */

let authHeader = null;

async function api(path, options = {}) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, options.headers || {});
  if (authHeader) headers['Authorization'] = authHeader;

  let res;
  try {
    res = await fetch(path, Object.assign({}, options, { headers }));
  } catch (err) {
    throw new Error('无法连接服务器');
  }

  if (res.status === 401) {
    authHeader = null;
    showLogin();
    throw new Error('需要认证');
  }

  let payload = null;
  try { payload = await res.json(); } catch (_) { /* 非 JSON 响应 */ }

  if (!res.ok || (payload && payload.ok === false)) {
    const message = (payload && payload.message) || ('请求失败（HTTP ' + res.status + '）');
    const detail = payload && payload.detail ? '\n' + payload.detail : '';
    throw new Error(message + detail);
  }
  return payload ? payload.data : null;
}

const getState = () => api('/api/state');
const getLogs = (limit) => api('/api/logs?limit=' + (limit || 300));
const getRaw = () => api('/api/raw');

function saveConfig(body) {
  return api('/api/config', { method: 'POST', body: JSON.stringify(body) });
}
function saveStatic(entry) {
  return api('/api/static', { method: 'POST', body: JSON.stringify(entry) });
}
function deleteStatic(mac) {
  return api('/api/static/delete', { method: 'POST', body: JSON.stringify({ mac }) });
}
function exportStatic() {
  return api('/api/static/export');
}
function previewStatic(text, mode) {
  return api('/api/static/preview', { method: 'POST', body: JSON.stringify({ text, mode }) });
}
function importStatic(text, mode) {
  return api('/api/static/import', { method: 'POST', body: JSON.stringify({ text, mode }) });
}
function reloadService() {
  return api('/api/service/reload', { method: 'POST', body: JSON.stringify({ hard: '1' }) });
}
function probeAll() {
  return api('/api/probe', { method: 'POST', body: JSON.stringify({}) });
}

/* ---------------- 提示 ---------------- */

function toast(title, desc, kind) {
  const wrap = $('toastWrap');
  const el = document.createElement('div');
  el.className = 'toast ' + (kind || '');
  const t = document.createElement('div');
  t.className = 'title';
  t.textContent = title;
  el.appendChild(t);
  if (desc) {
    const d = document.createElement('div');
    d.className = 'desc';
    d.textContent = desc;
    el.appendChild(d);
  }
  wrap.appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .25s';
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 260);
  }, kind === 'err' ? 5200 : 2800);
}

function showLogin() {
  $('loginMask').classList.add('show');
}
function hideLogin() {
  $('loginMask').classList.remove('show');
}

/* ---------------- 渲染 ---------------- */

function remainClass(remaining) {
  if (!remaining) return '';
  if (remaining.permanent) return 'inf';
  if (remaining.expired) return 'out';
  if (remaining.seconds >= 0 && remaining.seconds <= 600) return 'low';
  return '';
}

function renderLeases(rows) {
  const body = $('leaseBody');
  const keyword = state.filter.trim().toLowerCase();
  const list = rows.filter((row) => {
    if (!keyword) return true;
    return [row.hostname, row.ip, row.mac, row.note].join(' ').toLowerCase().indexOf(keyword) >= 0;
  });

  body.innerHTML = '';
  $('leaseEmpty').style.display = list.length ? 'none' : 'block';

  list.forEach((row) => {
    const tr = document.createElement('tr');

    const tdDot = document.createElement('td');
    const dot = document.createElement('span');
    dot.className = 'dot ' + (row.online ? 'online' : 'offline');
    dot.title = row.online ? '在线' : '离线';
    tdDot.appendChild(dot);

    const tdHost = document.createElement('td');
    const wrap = document.createElement('div');
    wrap.className = 'host-cell';
    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.textContent = (row.hostname || row.ip || '?').trim().charAt(0).toUpperCase();
    const meta = document.createElement('div');
    const name = document.createElement('div');
    name.className = 'host-name';
    name.textContent = row.hostname || '(未命名)';
    const sub = document.createElement('div');
    sub.className = 'host-sub';
    sub.textContent = row.note || (row.clientId ? 'ID ' + row.clientId : '');
    meta.appendChild(name);
    meta.appendChild(sub);
    wrap.appendChild(avatar);
    wrap.appendChild(meta);
    tdHost.appendChild(wrap);

    const tdIp = document.createElement('td');
    tdIp.className = 'mono nowrap';
    tdIp.textContent = row.ip;

    const tdMac = document.createElement('td');
    tdMac.className = 'mono nowrap';
    tdMac.textContent = row.mac;

    const tdType = document.createElement('td');
    const badge = document.createElement('span');
    badge.className = 'badge ' + (row.isStatic ? 'static' : 'dynamic');
    badge.textContent = row.isStatic ? '静态保留' : '动态分配';
    tdType.appendChild(badge);
    if (row.isStatic && row.active === false) {
      tdType.appendChild(document.createTextNode(' '));
      const off = document.createElement('span');
      off.className = 'badge off';
      off.textContent = '已停用';
      tdType.appendChild(off);
    }

    const tdRemain = document.createElement('td');
    tdRemain.className = 'remain nowrap ' + remainClass(row.remaining);
    tdRemain.textContent = row.remaining ? row.remaining.text : '-';
    if (row.expireAt) tdRemain.title = '到期时间 ' + row.expireAt;

    const tdSeen = document.createElement('td');
    tdSeen.className = 'dim nowrap';
    tdSeen.textContent = row.lastSeenText || '-';

    const tdOps = document.createElement('td');
    tdOps.style.textAlign = 'right';
    tdOps.className = 'nowrap';
    if (row.isStatic) {
      const edit = document.createElement('button');
      edit.className = 'sm ghost';
      edit.textContent = '编辑';
      edit.onclick = () => openStaticModal(row);
      tdOps.appendChild(edit);
    } else {
      const pin = document.createElement('button');
      pin.className = 'sm ghost';
      pin.textContent = '设为静态';
      pin.onclick = () => openStaticModal(row);
      tdOps.appendChild(pin);
    }

    tr.append(tdDot, tdHost, tdIp, tdMac, tdType, tdRemain, tdSeen, tdOps);
    body.appendChild(tr);
  });
}

function renderStatic(items) {
  const body = $('staticBody');
  body.innerHTML = '';
  $('staticEmpty').style.display = items.length ? 'none' : 'block';

  items.forEach((item) => {
    const tr = document.createElement('tr');

    const cells = [
      { text: item.name || '-', cls: '' },
      { text: item.mac, cls: 'mono nowrap' },
      { text: item.ip, cls: 'mono nowrap' },
      { text: item.gateway || '继承', cls: 'mono dim' },
      { text: (item.dns_servers || []).join(', ') || '继承', cls: 'mono dim' },
    ];

    cells.forEach((cell) => {
      const td = document.createElement('td');
      td.className = cell.cls;
      td.textContent = cell.text;
      tr.appendChild(td);
    });

    const tdStatus = document.createElement('td');
    const badge = document.createElement('span');
    badge.className = 'badge ' + (item.enabled === false ? 'off' : 'ok');
    badge.textContent = item.enabled === false ? '停用' : '启用';
    tdStatus.appendChild(badge);

    const tdOps = document.createElement('td');
    tdOps.style.textAlign = 'right';
    tdOps.className = 'nowrap';
    const edit = document.createElement('button');
    edit.className = 'sm ghost';
    edit.textContent = '编辑';
    edit.onclick = () => openStaticModal(item);
    const del = document.createElement('button');
    del.className = 'sm danger';
    del.textContent = '删除';
    del.onclick = () => confirmDelete(item);
    tdOps.append(edit, document.createTextNode(' '), del);

    tr.append(tdStatus, tdOps);
    body.appendChild(tr);
  });
}

// 用户开始编辑配置表单后，后台自动刷新不再覆盖输入内容
let formDirty = false;

function fillForm(data) {
  if (formDirty) return;
  const cfg = data.config || {};
  const dhcp = cfg.dhcp || {};
  const dns = cfg.dns || {};

  $('dhcpEnabled').checked = dhcp.enabled !== false;
  $('dhcpEnabledText').textContent = dhcp.enabled !== false ? '已启用' : '已停用';
  $('dhcpAuthoritative').checked = dhcp.authoritative !== false;
  $('dhcpInterface').value = dhcp.interface || '';
  $('dhcpDomain').value = dhcp.domain || '';
  $('rangeStart').value = dhcp.range_start || '';
  $('rangeEnd').value = dhcp.range_end || '';
  $('netmask').value = dhcp.netmask || '';
  $('leaseTime').value = dhcp.lease_time || '';
  $('gateway').value = dhcp.gateway || '';
  $('dhcpDns').value = (dhcp.dns_servers || []).join(', ');
  $('customOptions').value = dhcp.custom_options || '';

  $('dnsEnabled').checked = dns.enabled !== false;
  $('dnsEnabledText').textContent = dns.enabled !== false ? '已启用' : '已停用';
  $('dnsNoResolv').checked = dns.no_resolv !== false;
  $('dnsForwarders').value = (dns.forwarders || []).join(', ');
}

function renderMeta(data) {
  const summary = data.summary || {};
  $('statTotal').textContent = summary.total ?? 0;
  $('statOnline').textContent = summary.online ?? 0;
  $('statStatic').textContent = summary.static ?? 0;
  $('statDynamic').textContent = summary.dynamic ?? 0;

  const svc = data.service || {};
  const dot = $('svcDot');
  dot.className = 'dot ' + (svc.running ? 'online' : 'offline');
  $('svcText').textContent = svc.running
    ? 'dnsmasq 运行中 · PID ' + svc.pid
    : 'dnsmasq 未运行';

  const meta = data.meta || {};
  const uptime = meta.uptime || 0;
  const hours = Math.floor(uptime / 3600);
  const minutes = Math.floor((uptime % 3600) / 60);
  $('metaLine').textContent =
    'v' + (meta.version || '-') +
    ' · 已运行 ' + (hours ? hours + ' 小时 ' : '') + minutes + ' 分钟' +
    ' · 租约文件 ' + (meta.leaseFile || '-');
}

/* ---------------- 数据刷新 ---------------- */

async function refresh(quiet) {
  try {
    const data = await getState();
    hideLogin();
    state.data = data;
    renderMeta(data);
    renderLeases(data.leases || []);
    renderStatic(data.config.static_leases || []);
    fillForm(data);
    if (!quiet) maybeLoadLogs();
  } catch (err) {
    if (String(err.message).indexOf('认证') < 0) {
      toast('刷新失败', err.message, 'err');
    }
  }
}

async function maybeLoadLogs() {
  if (state.tab !== 'logs') return;
  try {
    const data = await getLogs(300);
    $('logView').textContent = (data.logs || []).join('\n') || '(暂无日志)';
  } catch (err) {
    $('logView').textContent = '日志加载失败：' + err.message;
  }
}

/* ---------------- 交互 ---------------- */

function switchTab(name) {
  state.tab = name;
  document.querySelectorAll('.tab').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.tab === name);
  });
  ['clients', 'static', 'dhcp', 'dns', 'logs'].forEach((page) => {
    const el = $('page-' + page);
    if (el) el.style.display = page === name ? '' : 'none';
  });
  if (name === 'logs') maybeLoadLogs();
}

function openStaticModal(row) {
  const isStatic = Boolean(row && row.isStatic);
  state.editingOriginalMac = isStatic ? row.mac : '';
  $('staticModalTitle').textContent = isStatic ? '编辑静态绑定' : '新增静态绑定';
  $('fName').value = row && row.hostname ? row.hostname : '';
  $('fMac').value = row && row.mac ? row.mac : '';
  $('fIp').value = row && row.ip ? row.ip : '';

  let gateway = '', dns = '', note = '', lease = '', enabled = true;
  if (isStatic && state.data) {
    const found = (state.data.config.static_leases || []).find(
      (item) => item.mac.toUpperCase() === row.mac.toUpperCase()
    );
    if (found) {
      gateway = found.gateway || '';
      dns = (found.dns_servers || []).join(', ');
      note = found.note || '';
      lease = found.lease_time || '';
      enabled = found.enabled !== false;
    }
  }
  $('fGateway').value = gateway;
  $('fDns').value = dns;
  $('fLease').value = lease;
  $('fNote').value = note;
  $('fEnabled').checked = enabled;

  $('staticModal').classList.add('show');
  setTimeout(() => $('fMac').focus(), 60);
}

function closeModal(id) {
  $(id).classList.remove('show');
}

async function confirmDelete(item) {
  const label = (item.name || item.mac) + '（' + item.ip + '）';
  if (!window.confirm('确定删除静态绑定 ' + label + ' 吗？\n删除后该设备下次将自动获取动态地址。')) return;
  try {
    await deleteStatic(item.mac);
    toast('已删除', label);
    await refresh(true);
  } catch (err) {
    toast('删除失败', err.message, 'err');
  }
}

function splitList(text) {
  return String(text || '')
    .split(/[,\s]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/* ---------------- 批量编辑 ---------------- */

let bulkTimer = null;

function setBulkStatus(kind, text) {
  const el = $('bulkStatus');
  el.className = 'bulk-status' + (kind ? ' ' + kind : '');
  el.textContent = text;
}

function renderBulkDetail(rows) {
  const box = $('bulkDetail');
  box.innerHTML = '';
  if (!rows.length) {
    box.className = 'bulk-list';
    return;
  }
  box.className = 'bulk-list ' + (rows.some((r) => r.cls === 'err') ? 'err' : 'warn');
  rows.slice(0, 40).forEach((row) => {
    const div = document.createElement('div');
    div.className = 'row';
    const ln = document.createElement('span');
    ln.className = 'ln';
    ln.textContent = row.line;
    const msg = document.createElement('span');
    msg.className = 'msg';
    msg.textContent = row.msg;
    div.append(ln, msg);
    box.appendChild(div);
  });
}

function renderBulkPreviewResult(data) {
  const errors = data.errors || [];
  const conflicts = data.conflicts || [];
  const ignored = data.ignored || [];
  const stats = data.stats || {};
  const rows = [];

  errors.forEach((item) => {
    rows.push({
      cls: 'err',
      line: '第 ' + item.line + ' 行',
      msg: item.reason + (item.content ? '　→ ' + item.content : ''),
    });
  });
  conflicts.forEach((item) => {
    rows.push({
      cls: 'err',
      line: item.ip,
      msg: '地址已被 ' + item.existingMac + (item.existingName ? '（' + item.existingName + '）' : '') + ' 占用',
    });
  });
  ignored.slice(0, 6).forEach((item) => {
    rows.push({ cls: 'warn', line: '第 ' + item.line + ' 行', msg: '已忽略：' + item.content });
  });

  if (errors.length || conflicts.length) {
    setBulkStatus('err', '有 ' + (errors.length + conflicts.length) + ' 处问题需要修正后才能保存');
    $('btnBulkSave').disabled = true;
  } else if (!data.entries.length) {
    setBulkStatus('warn', '没有解析到任何静态绑定');
    $('btnBulkSave').disabled = true;
  } else {
    const parts = ['解析到 ' + data.entries.length + ' 条绑定'];
    if (stats.options) parts.push(stats.options + ' 条网关/DNS 选项');
    if (ignored.length) parts.push('忽略 ' + ignored.length + ' 行');
    setBulkStatus('ok', parts.join(' · '));
    $('btnBulkSave').disabled = false;
  }
  renderBulkDetail(rows);
}

async function runBulkPreview() {
  const text = $('bulkText').value;
  const mode = $('bulkMode').value;
  if (!text.trim()) {
    setBulkStatus('', '在上方粘贴 dnsmasq 配置，或先点"新增单条"录入第一条');
    $('bulkDetail').innerHTML = '';
    $('btnBulkSave').disabled = true;
    return;
  }
  try {
    const data = await previewStatic(text, mode);
    renderBulkPreviewResult(data);
  } catch (err) {
    setBulkStatus('err', '预览失败：' + err.message);
    $('btnBulkSave').disabled = true;
  }
}

function scheduleBulkPreview() {
  clearTimeout(bulkTimer);
  bulkTimer = setTimeout(runBulkPreview, 350);
}

async function openBulkModal(initialText) {
  $('bulkMode').value = 'merge';
  if (typeof initialText === 'string') {
    $('bulkText').value = initialText;
  } else {
    $('bulkText').value = '';
    setBulkStatus('', '正在载入当前配置…');
    try {
      const data = await exportStatic();
      $('bulkText').value = data.text || '';
    } catch (err) {
      setBulkStatus('err', '载入失败：' + err.message);
    }
  }
  $('bulkModal').classList.add('show');
  await runBulkPreview();
  setTimeout(() => $('bulkText').focus(), 60);
}

function downloadText(filename, text) {
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ---------------- 事件绑定 ---------------- */

function collectDhcpForm() {
  return {
    enabled: $('dhcpEnabled').checked,
    authoritative: $('dhcpAuthoritative').checked,
    interface: $('dhcpInterface').value.trim(),
    domain: $('dhcpDomain').value.trim(),
    range_start: $('rangeStart').value.trim(),
    range_end: $('rangeEnd').value.trim(),
    netmask: $('netmask').value.trim(),
    lease_time: $('leaseTime').value.trim(),
    gateway: $('gateway').value.trim(),
    dns_servers: splitList($('dhcpDns').value),
    custom_options: $('customOptions').value,
  };
}

function bindEvents() {
  document.querySelectorAll('.tab').forEach((btn) => {
    btn.onclick = () => switchTab(btn.dataset.tab);
  });

  document.querySelectorAll('#page-dhcp input, #page-dhcp textarea, #page-dhcp select')
    .forEach((el) => {
      el.addEventListener('input', () => { formDirty = true; });
      el.addEventListener('change', () => { formDirty = true; });
    });

  $('btnRefresh').onclick = () => {
    formDirty = false;
    refresh();
  };
  $('btnRefreshLogs').onclick = () => maybeLoadLogs();

  $('btnProbe').onclick = async () => {
    const btn = $('btnProbe');
    btn.disabled = true;
    btn.textContent = '探测中…';
    try {
      const data = await probeAll();
      toast('探测完成', '检查 ' + data.checked + ' 个地址，存活 ' + data.alive + ' 个', 'ok');
      await refresh(true);
    } catch (err) {
      toast('探测失败', err.message, 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = '刷新在线';
    }
  };

  $('autoRefresh').onchange = (e) => {
    if (state.timer) clearInterval(state.timer);
    state.timer = null;
    if (e.target.checked) state.timer = setInterval(() => refresh(true), 10000);
  };

  $('clientFilter').oninput = (e) => {
    state.filter = e.target.value;
    if (state.data) renderLeases(state.data.leases || []);
  };

  $('btnAddStatic').onclick = () => openStaticModal(null);

  $('btnBulkEdit').onclick = () => openBulkModal();
  $('bulkText').oninput = scheduleBulkPreview;
  $('bulkMode').onchange = runBulkPreview;

  $('btnBulkSave').onclick = async () => {
    const btn = $('btnBulkSave');
    btn.disabled = true;
    try {
      const data = await importStatic($('bulkText').value, $('bulkMode').value);
      closeModal('bulkModal');
      toast('导入成功', '已导入 ' + data.count + ' 条，当前共 ' + data.total + ' 条', 'ok');
      await refresh(true);
    } catch (err) {
      toast('导入失败', err.message, 'err');
      btn.disabled = false;
    }
  };

  $('btnExportFile').onclick = async () => {
    try {
      const data = await exportStatic();
      if (!data.count) {
        toast('没有可导出的内容', '当前没有启用状态的静态绑定', 'err');
        return;
      }
      const stamp = new Date().toISOString().slice(0, 10);
      downloadText('static-leases-' + stamp + '.conf', data.text);
      toast('已导出', data.count + ' 条绑定', 'ok');
    } catch (err) {
      toast('导出失败', err.message, 'err');
    }
  };

  $('btnImportFile').onclick = () => $('importFileInput').click();
  $('importFileInput').onchange = (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      openBulkModal(String(reader.result || ''));
      toast('已读入文件', file.name + '，确认无误后点保存');
    };
    reader.onerror = () => toast('读取失败', '无法读取 ' + file.name, 'err');
    reader.readAsText(file, 'utf-8');
    e.target.value = '';
  };

  document.querySelectorAll('[data-close]').forEach((el) => {
    el.onclick = () => closeModal(el.dataset.close);
  });
  $('staticModal').onclick = (e) => {
    if (e.target === $('staticModal')) closeModal('staticModal');
  };
  $('bulkModal').onclick = (e) => {
    if (e.target === $('bulkModal')) closeModal('bulkModal');
  };

  $('staticForm').onsubmit = async (e) => {
    e.preventDefault();
    const entry = {
      name: $('fName').value.trim(),
      mac: $('fMac').value.trim(),
      ip: $('fIp').value.trim(),
      gateway: $('fGateway').value.trim(),
      dns_servers: splitList($('fDns').value),
      lease_time: $('fLease').value.trim(),
      note: $('fNote').value.trim(),
      enabled: $('fEnabled').checked,
    };
    try {
      await saveStatic(entry);
      closeModal('staticModal');
      toast('保存成功', entry.mac + ' → ' + entry.ip, 'ok');
      await refresh(true);
    } catch (err) {
      toast('保存失败', err.message, 'err');
    }
  };

  $('dhcpForm').onsubmit = async (e) => {
    e.preventDefault();
    const btn = $('btnSaveDhcp');
    btn.disabled = true;
    try {
      await saveConfig({ dhcp: collectDhcpForm() });
      toast('已保存', 'DHCP 配置已生效', 'ok');
      await refresh(true);
    } catch (err) {
      toast('保存失败', err.message, 'err');
    } finally {
      btn.disabled = false;
    }
  };

  $('customForm').onsubmit = async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector('button[type=submit]');
    btn.disabled = true;
    try {
      await saveConfig({ dhcp: collectDhcpForm() });
      toast('已保存', '自定义配置已生效', 'ok');
      await refresh(true);
    } catch (err) {
      toast('保存失败', err.message, 'err');
    } finally {
      btn.disabled = false;
    }
  };

  $('dnsForm').onsubmit = async (e) => {
    e.preventDefault();
    // 只提交 DNS 段落，避免把 DHCP 表单里未保存的改动一并写回
    const body = {
      dns: {
        enabled: $('dnsEnabled').checked,
        no_resolv: $('dnsNoResolv').checked,
        forwarders: splitList($('dnsForwarders').value),
      },
    };
    try {
      await saveConfig(body);
      toast('已保存', 'DNS 配置已生效', 'ok');
      await refresh(true);
    } catch (err) {
      toast('保存失败', err.message, 'err');
    }
  };

  $('btnReload').onclick = async () => {
    try {
      await reloadService();
      toast('已重载', 'dnsmasq 配置已重新加载', 'ok');
      await refresh(true);
    } catch (err) {
      toast('重载失败', err.message, 'err');
    }
  };

  $('btnShowRaw').onclick = async () => {
    const view = $('rawView');
    if (view.style.display === 'none') {
      view.style.display = '';
      try {
        const data = await getRaw();
        view.textContent = '# /etc/dnsmasq.d/10-managed.conf\n\n' + (data.managed || '');
      } catch (err) {
        view.textContent = '加载失败：' + err.message;
      }
    } else {
      view.style.display = 'none';
    }
  };

  document.querySelectorAll('.switch input').forEach((input) => {
    input.addEventListener('change', () => {
      const map = { dhcpEnabled: 'dhcpEnabledText', dnsEnabled: 'dnsEnabledText' };
      const target = map[input.id];
      if (target) $(target).textContent = input.checked ? '已启用' : '已停用';
    });
  });

  $('loginForm').onsubmit = async (e) => {
    e.preventDefault();
    const user = $('loginUser').value;
    const pass = $('loginPass').value;
    authHeader = 'Basic ' + btoa(unescape(encodeURIComponent(user + ':' + pass)));
    try {
      await refresh();
      $('loginPass').value = '';
    } catch (err) {
      authHeader = null;
      toast('登录失败', err.message, 'err');
    }
  };

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeModal('staticModal');
      closeModal('bulkModal');
    }
  });
}

/* ---------------- 启动 ---------------- */

bindEvents();
refresh();
setInterval(() => refresh(true), 60000);
