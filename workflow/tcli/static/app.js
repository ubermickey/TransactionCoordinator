/* ── Transaction Coordinator SPA ──────────────────────────────────────────── */

const API = '';
let currentTxn = null;
let currentTab = 'overview';
let txnCache = {};
let phasesCache = {};
let deadlineView = 'table';

// ══════════════════════════════════════════════════════════════════════════════
//  TOAST NOTIFICATIONS
// ══════════════════════════════════════════════════════════════════════════════

const Toast = (() => {
  const icons = {
    success: '\u2713',
    error: '\u2717',
    warning: '\u26A0',
    info: '\u2139',
  };
  const durations = { success: 3000, warning: 5000, info: 4000, error: 0 };

  function show(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    // Cap visible toasts at 3
    while (container.children.length >= 3) {
      container.removeChild(container.firstChild);
    }

    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.innerHTML = `
      <span class="toast-icon">${icons[type] || icons.info}</span>
      <span class="toast-msg">${esc(message)}</span>
      <button class="toast-close" aria-label="Dismiss">&times;</button>`;

    el.querySelector('.toast-close').addEventListener('click', () => dismiss(el));
    container.appendChild(el);

    const dur = durations[type];
    if (dur > 0) {
      setTimeout(() => dismiss(el), dur);
    }
  }

  function dismiss(el) {
    if (!el.parentNode) return;
    el.classList.add('removing');
    el.addEventListener('animationend', () => el.remove());
  }

  return { show };
})();

// ══════════════════════════════════════════════════════════════════════════════
//  LOADING SKELETONS
// ══════════════════════════════════════════════════════════════════════════════

function showSkeleton(type) {
  const el = document.getElementById('tab-content');
  if (!el) return;
  const skeletons = {
    table: `
      <div class="skeleton-card">
        <div class="skeleton skeleton-line long"></div>
        <div class="skeleton skeleton-line medium"></div>
      </div>
      ${Array(5).fill(`<div class="skeleton-row">${Array(4).fill('<div class="skeleton skeleton-cell"></div>').join('')}</div>`).join('')}`,
    cards: `
      <div class="skeleton-card"><div class="skeleton skeleton-line medium"></div><div class="skeleton skeleton-line short"></div></div>
      <div class="card-grid">
        ${Array(3).fill('<div class="skeleton-card"><div class="skeleton skeleton-line short"></div><div class="skeleton" style="height:40px;margin-top:8px"></div></div>').join('')}
      </div>`,
    'gate-cards': `
      <div class="skeleton-card"><div class="skeleton skeleton-line medium"></div><div class="skeleton skeleton-line short"></div></div>
      ${Array(3).fill(`<div class="skeleton-card"><div class="skeleton skeleton-line long"></div><div class="skeleton skeleton-line short"></div><div class="skeleton skeleton-line medium"></div></div>`).join('')}`,
    chat: `<div style="padding:16px">${Array(4).fill('<div class="skeleton skeleton-line medium" style="margin-bottom:14px"></div>').join('')}</div>`,
  };
  el.innerHTML = skeletons[type] || skeletons.table;
}

// ══════════════════════════════════════════════════════════════════════════════
//  API HELPERS (with error handling)
// ══════════════════════════════════════════════════════════════════════════════

async function api(path, opts = {}) {
  try {
    const res = await fetch(API + path, {
      headers: { 'Content-Type': 'application/json' },
      ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    const data = await res.json();
    if (!res.ok) {
      const msg = data.error || data.message || `Request failed (${res.status})`;
      Toast.show(msg, 'error');
      return { _error: true, ...data };
    }
    return data;
  } catch (err) {
    Toast.show('Network error: ' + err.message, 'error');
    return { _error: true, error: err.message };
  }
}

const get  = (p) => api(p);
const post = (p, b) => api(p, { method: 'POST', body: b });
const del  = (p) => api(p, { method: 'DELETE' });

// ══════════════════════════════════════════════════════════════════════════════
//  DARK MODE (auto-detect, no toggle)
// ══════════════════════════════════════════════════════════════════════════════

const DarkMode = (() => {
  function apply() {
    const dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
  }
  function init() {
    apply();
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', apply);
  }
  return { init };
})();

// ══════════════════════════════════════════════════════════════════════════════
//  RESPONSIVE SIDEBAR
// ══════════════════════════════════════════════════════════════════════════════

const Sidebar = (() => {
  let open = false;

  function toggle() {
    open = !open;
    document.getElementById('sidebar').classList.toggle('open', open);
    document.getElementById('sidebar-backdrop').classList.toggle('visible', open);
  }

  function close() {
    open = false;
    document.getElementById('sidebar').classList.remove('open');
    document.getElementById('sidebar-backdrop').classList.remove('visible');
  }

  function init() {
    const btn = document.getElementById('hamburger');
    if (btn) btn.addEventListener('click', toggle);
    const backdrop = document.getElementById('sidebar-backdrop');
    if (backdrop) backdrop.addEventListener('click', close);
  }

  return { init, close };
})();

// ══════════════════════════════════════════════════════════════════════════════
//  CHAT PANEL
// ══════════════════════════════════════════════════════════════════════════════

const ChatPanel = (() => {
  let isOpen = false;
  let messages = {};  // per-transaction: { tid: [{role, content}] }
  let sending = false;

  function open() {
    isOpen = true;
    document.getElementById('chat-panel').classList.add('open');
    document.getElementById('chat-fab').style.display = 'none';
    renderMessages();
    const input = document.querySelector('#chat-input');
    if (input) input.focus();
  }

  function close() {
    isOpen = false;
    document.getElementById('chat-panel').classList.remove('open');
    document.getElementById('chat-fab').style.display = '';
  }

  function toggle() {
    isOpen ? close() : open();
  }

  function renderMessages() {
    const area = document.getElementById('chat-messages');
    if (!area) return;
    const tid = currentTxn || '_global';
    const msgs = messages[tid] || [];
    if (msgs.length === 0) {
      area.innerHTML = '<div class="chat-msg assistant">Ask me anything about this transaction. I have full context on documents, gates, deadlines, and audit history.</div>';
      return;
    }
    area.innerHTML = msgs.map(m =>
      `<div class="chat-msg ${m.role}">${esc(m.content)}</div>`
    ).join('');
    area.scrollTop = area.scrollHeight;
  }

  async function send() {
    if (sending) return;
    const input = document.getElementById('chat-input');
    const text = (input.value || '').trim();
    if (!text) return;

    const tid = currentTxn || '_global';
    if (!messages[tid]) messages[tid] = [];
    messages[tid].push({ role: 'user', content: text });
    input.value = '';
    renderMessages();

    // Show typing indicator
    const area = document.getElementById('chat-messages');
    const typing = document.createElement('div');
    typing.className = 'chat-msg typing';
    typing.textContent = 'Thinking...';
    area.appendChild(typing);
    area.scrollTop = area.scrollHeight;

    sending = true;
    const sendBtn = document.getElementById('chat-send');
    if (sendBtn) sendBtn.disabled = true;

    try {
      const res = await post('/api/chat', {
        message: text,
        txn_id: currentTxn,
        history: (messages[tid] || []).slice(-10),
      });
      typing.remove();
      if (res._error) {
        messages[tid].push({ role: 'assistant', content: 'Sorry, I encountered an error. ' + (res.error || '') });
      } else {
        messages[tid].push({ role: 'assistant', content: res.reply || 'No response' });
      }
    } catch (err) {
      typing.remove();
      messages[tid].push({ role: 'assistant', content: 'Connection error: ' + err.message });
    }

    sending = false;
    if (sendBtn) sendBtn.disabled = false;
    renderMessages();
  }

  function init() {
    const fab = document.getElementById('chat-fab');
    if (fab) fab.addEventListener('click', toggle);

    const closeBtn = document.getElementById('chat-close');
    if (closeBtn) closeBtn.addEventListener('click', close);

    const form = document.getElementById('chat-form');
    if (form) form.addEventListener('submit', e => { e.preventDefault(); send(); });
  }

  return { init, open, close, toggle };
})();

// ══════════════════════════════════════════════════════════════════════════════
//  COMMAND PALETTE
// ══════════════════════════════════════════════════════════════════════════════

const CmdPalette = (() => {
  let isOpen = false;
  let activeIdx = 0;
  let results = [];

  function open() {
    isOpen = true;
    const backdrop = document.getElementById('cmd-backdrop');
    backdrop.classList.add('open');
    const input = document.getElementById('cmd-input');
    input.value = '';
    input.focus();
    activeIdx = 0;
    buildResults('');
  }

  function close() {
    isOpen = false;
    document.getElementById('cmd-backdrop').classList.remove('open');
  }

  function buildResults(query) {
    const q = query.toLowerCase();
    results = [];

    // Gather all searchable items
    // 1. Transactions
    Object.values(txnCache).forEach(t => {
      results.push({
        icon: '\uD83C\uDFE0', // house emoji
        label: t.address,
        hint: 'Transaction',
        action: () => { selectTxn(t.id); },
      });
    });

    // 2. Also search sidebar items for transactions not in cache
    document.querySelectorAll('.txn-item').forEach(el => {
      const addr = el.querySelector('.txn-address');
      if (addr && !Object.values(txnCache).some(t => t.id === el.dataset.id)) {
        results.push({
          icon: '\uD83C\uDFE0',
          label: addr.textContent,
          hint: 'Transaction',
          action: () => { selectTxn(el.dataset.id); },
        });
      }
    });

    // 3. Gates (from current txn cache)
    if (currentTxn && txnCache[currentTxn] && txnCache[currentTxn]._gates) {
      txnCache[currentTxn]._gates.forEach(g => {
        results.push({
          icon: '\uD83D\uDEE1', // shield
          label: g.name || g.gid,
          hint: `Gate \u00B7 ${g.gid}`,
          action: () => { switchTab('gates'); },
        });
      });
    }

    // 4. Deadlines
    if (currentTxn && txnCache[currentTxn] && txnCache[currentTxn]._deadlines) {
      txnCache[currentTxn]._deadlines.forEach(d => {
        results.push({
          icon: '\u23F0', // alarm clock
          label: d.name || d.did,
          hint: `Deadline \u00B7 ${d.did}`,
          action: () => { switchTab('deadlines'); },
        });
      });
    }

    // 5. Docs
    if (currentTxn && txnCache[currentTxn] && txnCache[currentTxn]._docs) {
      txnCache[currentTxn]._docs.forEach(d => {
        results.push({
          icon: '\uD83D\uDCC4', // page
          label: d.name,
          hint: `Doc \u00B7 ${d.code}`,
          action: () => { switchTab('docs'); },
        });
      });
    }

    // Filter
    if (q) {
      results = results.filter(r =>
        r.label.toLowerCase().includes(q) || r.hint.toLowerCase().includes(q)
      );
    }

    // Limit display
    results = results.slice(0, 20);
    activeIdx = 0;
    render();
  }

  function render() {
    const list = document.getElementById('cmd-results');
    if (results.length === 0) {
      list.innerHTML = '<div class="cmd-empty">No results found</div>';
      return;
    }
    list.innerHTML = results.map((r, i) =>
      `<div class="cmd-item${i === activeIdx ? ' active' : ''}" data-idx="${i}">
        <span class="cmd-item-icon">${r.icon}</span>
        <span class="cmd-item-label">${esc(r.label)}</span>
        <span class="cmd-item-hint">${esc(r.hint)}</span>
      </div>`
    ).join('');

    list.querySelectorAll('.cmd-item').forEach(el => {
      el.addEventListener('click', () => selectResult(parseInt(el.dataset.idx)));
    });
  }

  function selectResult(idx) {
    if (results[idx]) {
      close();
      results[idx].action();
    }
  }

  function handleKey(e) {
    if (!isOpen) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      activeIdx = Math.min(activeIdx + 1, results.length - 1);
      render();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      activeIdx = Math.max(activeIdx - 1, 0);
      render();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      selectResult(activeIdx);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      close();
    }
  }

  function init() {
    const input = document.getElementById('cmd-input');
    if (input) {
      input.addEventListener('input', () => buildResults(input.value));
      input.addEventListener('keydown', handleKey);
    }
    const backdrop = document.getElementById('cmd-backdrop');
    if (backdrop) {
      backdrop.addEventListener('click', e => {
        if (e.target === backdrop) close();
      });
    }
  }

  return { init, open, close, isOpen: () => isOpen };
})();

// ══════════════════════════════════════════════════════════════════════════════
//  KEYBOARD SHORTCUTS
// ══════════════════════════════════════════════════════════════════════════════

const Shortcuts = (() => {
  let helpOpen = false;
  const tabKeys = { '1': 'overview', '2': 'docs', '3': 'signatures', '4': 'contingencies', '5': 'gates', '6': 'deadlines', '7': 'audit' };

  function isInput() {
    const tag = document.activeElement?.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
  }

  function toggleHelp() {
    helpOpen = !helpOpen;
    document.getElementById('shortcuts-overlay').classList.toggle('open', helpOpen);
  }

  function closeHelp() {
    helpOpen = false;
    document.getElementById('shortcuts-overlay').classList.remove('open');
  }

  function closeTopmost() {
    // Close in priority order
    if (helpOpen) { closeHelp(); return; }
    if (CmdPalette.isOpen()) { CmdPalette.close(); return; }
    const chatPanel = document.getElementById('chat-panel');
    if (chatPanel && chatPanel.classList.contains('open')) { ChatPanel.close(); return; }
    const modal = document.getElementById('modal-backdrop');
    if (modal && modal.style.display !== 'none') { closeModal(); return; }
  }

  function handler(e) {
    const mod = e.metaKey || e.ctrlKey;

    // Cmd+K - command palette
    if (mod && e.key === 'k') {
      e.preventDefault();
      CmdPalette.isOpen() ? CmdPalette.close() : CmdPalette.open();
      return;
    }

    // Cmd+J - toggle chat
    if (mod && e.key === 'j') {
      e.preventDefault();
      ChatPanel.toggle();
      return;
    }

    // Cmd+N - new transaction
    if (mod && e.key === 'n') {
      e.preventDefault();
      openModal();
      return;
    }

    // Esc - close topmost
    if (e.key === 'Escape') {
      closeTopmost();
      return;
    }

    // Skip the rest if user is typing
    if (isInput()) return;

    // ? - show shortcuts help
    if (e.key === '?') {
      e.preventDefault();
      toggleHelp();
      return;
    }

    // 1-5 - switch tabs
    if (tabKeys[e.key] && currentTxn) {
      e.preventDefault();
      switchTab(tabKeys[e.key]);
      return;
    }
  }

  function init() {
    document.addEventListener('keydown', handler);
    const overlay = document.getElementById('shortcuts-overlay');
    if (overlay) {
      overlay.addEventListener('click', e => {
        if (e.target === overlay) closeHelp();
      });
    }
  }

  return { init };
})();

// ══════════════════════════════════════════════════════════════════════════════
//  INIT
// ══════════════════════════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {
  DarkMode.init();
  Sidebar.init();
  ChatPanel.init();
  CmdPalette.init();
  Shortcuts.init();

  loadBrokerages();
  loadTxns();

  // Modal
  $$('#btn-new, #btn-new-empty').forEach(b => b.addEventListener('click', openModal));
  $('#modal-close').addEventListener('click', closeModal);
  $('#btn-cancel').addEventListener('click', closeModal);
  $('#modal-backdrop').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeModal();
  });
  $('#form-new').addEventListener('submit', handleCreate);

  // Signature modal
  const sigClose = $('#sig-modal-close');
  const sigCancel = $('#sig-btn-cancel');
  const sigBackdrop = $('#sig-modal-backdrop');
  if (sigClose) sigClose.addEventListener('click', closeSigModal);
  if (sigCancel) sigCancel.addEventListener('click', closeSigModal);
  if (sigBackdrop) sigBackdrop.addEventListener('click', e => {
    if (e.target === e.currentTarget) closeSigModal();
  });
  const sigForm = $('#form-add-sig');
  if (sigForm) sigForm.addEventListener('submit', handleAddSig);

  // Contingency modal
  const contClose = $('#cont-modal-close');
  const contCancel = $('#cont-btn-cancel');
  const contBackdrop = $('#cont-modal-backdrop');
  if (contClose) contClose.addEventListener('click', closeContModal);
  if (contCancel) contCancel.addEventListener('click', closeContModal);
  if (contBackdrop) contBackdrop.addEventListener('click', e => {
    if (e.target === e.currentTarget) closeContModal();
  });
  const contForm = $('#form-add-cont');
  if (contForm) contForm.addEventListener('submit', handleAddCont);

  // Tabs
  $$('#tab-bar .tab').forEach(t => {
    t.addEventListener('click', () => switchTab(t.dataset.tab));
  });
});

function $(sel) { return document.querySelector(sel); }
function $$(sel) { return document.querySelectorAll(sel); }

// ── Brokerages ──────────────────────────────────────────────────────────────

async function loadBrokerages() {
  const list = await get('/api/brokerages');
  if (list._error) return;
  const sel = $('#inp-brokerage');
  sel.innerHTML = '<option value="">None</option>';
  list.forEach(b => {
    const opt = document.createElement('option');
    opt.value = b;
    opt.textContent = b.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    sel.appendChild(opt);
  });
}

// ── Transaction List ────────────────────────────────────────────────────────

async function loadTxns() {
  const txns = await get('/api/txns');
  if (txns._error) return;
  const list = $('#txn-list');
  const empty = $('#sidebar-empty');
  const emptyState = $('#empty-state');
  const detail = $('#txn-detail');

  if (txns.length === 0) {
    list.innerHTML = '';
    empty.style.display = '';
    emptyState.style.display = '';
    detail.style.display = 'none';
    return;
  }

  empty.style.display = 'none';
  emptyState.style.display = 'none';

  list.innerHTML = txns.map(t => {
    const ds = t.doc_stats || {};
    const pct = ds.total ? Math.round((ds.received / ds.total) * 100) : 0;
    const active = currentTxn === t.id ? ' active' : '';
    return `
      <li class="txn-item${active}" data-id="${t.id}">
        <div class="txn-address">${esc(t.address)}</div>
        <div class="txn-meta">
          <span class="type-badge ${t.txn_type}">${t.txn_type}</span>
          <span class="type-badge ${t.party_role}">${t.party_role}</span>
        </div>
        <div class="txn-phase">${formatPhase(t.phase)}</div>
        ${ds.total ? `<div class="progress-bar"><div class="progress-bar-fill" style="width:${pct}%"></div></div>` : ''}
      </li>`;
  }).join('');

  $$('.txn-item').forEach(el => {
    el.addEventListener('click', () => {
      selectTxn(el.dataset.id);
      Sidebar.close();  // close mobile sidebar on selection
    });
  });

  // Auto-select
  if (!currentTxn && txns.length > 0) {
    selectTxn(txns[0].id);
  } else if (currentTxn) {
    selectTxn(currentTxn);
  }
}

async function selectTxn(id) {
  currentTxn = id;
  $$('.txn-item').forEach(el => {
    el.classList.toggle('active', el.dataset.id === id);
  });

  $('#empty-state').style.display = 'none';
  $('#txn-detail').style.display = '';

  const t = await get(`/api/txns/${id}`);
  if (t._error) return;
  txnCache[id] = t;

  // Load phases if not cached
  const txnType = t.txn_type || 'sale';
  if (!phasesCache[txnType]) {
    phasesCache[txnType] = await get(`/api/phases/${txnType}`);
  }

  renderHeader(t);
  renderStepper(t, phasesCache[txnType]);
  switchTab(currentTab);

  // Pre-cache gates, deadlines, docs for command palette
  prefetchForPalette(id);
}

async function prefetchForPalette(tid) {
  const [gates, deadlines, docs] = await Promise.all([
    get(`/api/txns/${tid}/gates`),
    get(`/api/txns/${tid}/deadlines`),
    get(`/api/txns/${tid}/docs`),
  ]);
  if (txnCache[tid]) {
    if (!gates._error) txnCache[tid]._gates = gates;
    if (!deadlines._error) txnCache[tid]._deadlines = deadlines;
    if (!docs._error) txnCache[tid]._docs = docs;
  }
}

// ── Header ──────────────────────────────────────────────────────────────────

function renderHeader(t) {
  $('#detail-header').innerHTML = `
    <h1>${esc(t.address)}</h1>
    <div class="detail-meta">
      <span class="type-badge ${t.txn_type}">${t.txn_type}</span>
      <span class="type-badge ${t.party_role}">${t.party_role}</span>
      ${t.brokerage ? `<span>${esc(t.brokerage.replace(/_/g, ' '))}</span>` : ''}
      <span>ID: ${t.id}</span>
    </div>`;
}

// ── Phase Stepper ───────────────────────────────────────────────────────────

function renderStepper(t, phases) {
  if (!phases || phases._error) return;
  const currentIdx = phases.findIndex(p => p.id === t.phase);
  const el = $('#phase-stepper');
  el.innerHTML = phases.map((p, i) => {
    let cls = 'future';
    if (i < currentIdx) cls = 'completed';
    else if (i === currentIdx) cls = 'current';
    const connector = i < phases.length - 1
      ? `<div class="phase-connector ${i < currentIdx ? 'completed' : ''}"></div>`
      : '';
    return `
      <div class="phase-step">
        <div class="phase-dot ${cls}"></div>
        <span class="phase-label ${cls}">${esc(p.name || p.id)}</span>
      </div>${connector}`;
  }).join('');
}

// ── Tabs ────────────────────────────────────────────────────────────────────

function switchTab(tab) {
  currentTab = tab;
  $$('#tab-bar .tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === tab);
  });
  const render = {
    overview: renderOverview,
    docs: renderDocs,
    signatures: renderSignatures,
    contingencies: renderContingencies,
    gates: renderGates,
    deadlines: renderDeadlines,
    audit: renderAudit,
  };
  (render[tab] || render.overview)();
}

// ── Overview Tab ────────────────────────────────────────────────────────────

async function renderOverview() {
  const t = txnCache[currentTxn];
  if (!t) return;
  const el = $('#tab-content');

  // Advance bar
  const phases = phasesCache[t.txn_type || 'sale'] || [];
  const phaseObj = phases.find ? phases.find(p => p.id === t.phase) : null;
  const phaseName = phaseObj ? phaseObj.name : t.phase;
  const isLast = phases.length > 0 && phases[phases.length - 1].id === t.phase;

  let html = `
    <div class="advance-bar">
      <div>
        <span class="phase-info">Current Phase:</span>
        <span class="phase-name">${esc(phaseName)}</span>
      </div>
      ${!isLast ? '<button class="btn btn-primary btn-sm" onclick="advancePhase()">Advance Phase</button>' : '<span class="badge badge-verified">Final Phase</span>'}
    </div>`;

  // Stats
  const ds = t.doc_stats || {};
  html += `
    <div class="card-grid">
      <div class="stat-card">
        <div class="stat-value">${t.gates_verified}/${t.gate_count}</div>
        <div class="stat-label">Gates Verified</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">${ds.received || 0}/${ds.total || 0}</div>
        <div class="stat-label">Docs Received</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">${(t.deadlines || []).length}</div>
        <div class="stat-label">Deadlines</div>
      </div>
    </div>`;

  // Parties
  const parties = (t.data || {}).parties || {};
  if (Object.values(parties).some(v => v)) {
    html += `<div class="card" style="margin-top:12px">
      <div class="card-title">Parties</div>
      <div class="kv-grid">
        ${Object.entries(parties).map(([k, v]) => v ? `<span class="kv-key">${esc(k)}</span><span class="kv-val">${esc(v)}</span>` : '').join('')}
      </div></div>`;
  }

  // Financial
  const fin = (t.data || {}).financial || {};
  if (Object.values(fin).some(v => v)) {
    html += `<div class="card">
      <div class="card-title">Financial</div>
      <div class="kv-grid">
        ${Object.entries(fin).map(([k, v]) => v ? `<span class="kv-key">${esc(k)}</span><span class="kv-val">${typeof v === 'number' ? '$' + v.toLocaleString() : esc(String(v))}</span>` : '').join('')}
      </div></div>`;
  }

  // Property flags
  const allFlags = [
    'is_condo', 'is_pre_1978', 'has_solar', 'has_hoa', 'is_trust_sale',
    'price_above_5m', 'has_pool', 'has_septic', 'is_manufactured',
    'is_new_construction', 'is_short_sale', 'is_probate', 'has_tenant'
  ];
  const props = t.props || {};
  html += `<div class="card">
    <div class="card-title">Property Flags</div>
    ${allFlags.map(f => `
      <div class="toggle-row">
        <span class="toggle-label">${esc(f.replace(/^(is_|has_)/, '').replace(/_/g, ' '))}</span>
        <label class="toggle">
          <input type="checkbox" ${props[f] ? 'checked' : ''} onchange="toggleFlag('${f}', this.checked)">
          <div class="toggle-track"></div>
          <div class="toggle-thumb"></div>
        </label>
      </div>`).join('')}
  </div>`;

  // Delete
  html += `<div class="delete-section">
    <button class="btn btn-danger btn-sm" onclick="deleteTxn()">Delete Transaction</button>
  </div>`;

  el.innerHTML = html;
}

async function advancePhase() {
  const res = await post(`/api/txns/${currentTxn}/advance`);
  if (res._error) return;
  if (res.ok) {
    Toast.show('Phase advanced successfully', 'success');
    await selectTxn(currentTxn);
    loadTxns();
  } else {
    Toast.show('Cannot advance: ' + (res.blocking || []).join(', '), 'warning');
  }
}

async function toggleFlag(flag, value) {
  await post(`/api/txns/${currentTxn}/props`, { flag, value });
  const t = await get(`/api/txns/${currentTxn}`);
  if (!t._error) {
    txnCache[currentTxn] = t;
    loadTxns();
  }
}

async function deleteTxn() {
  // Use a simple native confirm since it's a destructive action (clear intent)
  if (!confirm('Delete this transaction? This cannot be undone.')) return;
  const res = await del(`/api/txns/${currentTxn}`);
  if (res._error) return;
  Toast.show('Transaction deleted', 'info');
  currentTxn = null;
  loadTxns();
}

// ══════════════════════════════════════════════════════════════════════════════
//  DOCUMENTS TAB (with filter bar)
// ══════════════════════════════════════════════════════════════════════════════

let _docsData = [];

async function renderDocs() {
  showSkeleton('table');
  const docs = await get(`/api/txns/${currentTxn}/docs`);
  if (docs._error) return;
  _docsData = docs;

  // Cache for command palette
  if (txnCache[currentTxn]) txnCache[currentTxn]._docs = docs;

  const el = $('#tab-content');

  if (docs.length === 0) {
    el.innerHTML = '<div class="card"><p style="color:var(--text-secondary)">No documents tracked. Create a transaction with a brokerage to auto-populate.</p></div>';
    return;
  }

  // Gather filter values
  const phases = [...new Set(docs.map(d => d.phase))];
  const statuses = [...new Set(docs.map(d => d.status))];

  let html = `<div class="filter-bar" id="docs-filter">
    <input type="text" placeholder="Search documents..." id="docs-search">
    <select id="docs-phase-filter">
      <option value="">All Phases</option>
      ${phases.map(p => `<option value="${p}">${esc(formatPhase(p))}</option>`).join('')}
    </select>
    <select id="docs-status-filter">
      <option value="">All Statuses</option>
      ${statuses.map(s => `<option value="${s}">${s}</option>`).join('')}
    </select>
  </div>`;

  // Stats
  const stats = {};
  docs.forEach(d => { stats[d.status] = (stats[d.status] || 0) + 1; });
  const total = docs.length;
  const recv = (stats.received || 0) + (stats.verified || 0);
  html += `<div class="summary-bar">
    <span><span class="count">${total}</span> total</span>
    <span><span class="count">${recv}</span> received</span>
    <span><span class="count">${stats.verified || 0}</span> verified</span>
    <span><span class="count">${stats.required || 0}</span> required</span>
    <span><span class="count">${stats.na || 0}</span> N/A</span>
  </div>`;

  html += '<div id="docs-table-area"></div>';
  el.innerHTML = html;

  renderDocsTable(docs);

  // Attach filter listeners
  const searchEl = document.getElementById('docs-search');
  const phaseEl = document.getElementById('docs-phase-filter');
  const statusEl = document.getElementById('docs-status-filter');
  const filterFn = () => {
    const q = searchEl.value.toLowerCase();
    const p = phaseEl.value;
    const s = statusEl.value;
    const filtered = _docsData.filter(d => {
      if (q && !d.name.toLowerCase().includes(q) && !d.code.toLowerCase().includes(q)) return false;
      if (p && d.phase !== p) return false;
      if (s && d.status !== s) return false;
      return true;
    });
    renderDocsTable(filtered);
  };
  searchEl.addEventListener('input', filterFn);
  phaseEl.addEventListener('change', filterFn);
  statusEl.addEventListener('change', filterFn);
}

function renderDocsTable(docs) {
  const area = document.getElementById('docs-table-area');
  if (!area) return;

  if (docs.length === 0) {
    area.innerHTML = '<div class="card"><p style="color:var(--text-secondary)">No documents match your filters.</p></div>';
    return;
  }

  const groups = {};
  docs.forEach(d => {
    if (!groups[d.phase]) groups[d.phase] = [];
    groups[d.phase].push(d);
  });

  let html = '<div class="table-wrap"><table><thead><tr><th>Code</th><th>Document</th><th>Status</th><th>Actions</th></tr></thead><tbody>';
  Object.entries(groups).forEach(([phase, items]) => {
    html += `<tr class="phase-group-header"><td colspan="4">${esc(formatPhase(phase))}</td></tr>`;
    items.forEach(d => {
      html += `<tr>
        <td><code>${esc(d.code)}</code></td>
        <td>${esc(d.name)}</td>
        <td><span class="badge badge-${d.status}">${d.status}</span></td>
        <td class="actions">${docActions(d)}</td>
      </tr>`;
    });
  });
  html += '</tbody></table></div>';
  area.innerHTML = html;
}

function docActions(d) {
  if (d.status === 'verified') return '<span style="color:var(--green)">\u2713</span>';
  if (d.status === 'na') return '<span style="color:var(--text-secondary)">&mdash;</span>';
  let btns = '';
  if (d.status === 'required') {
    btns += `<button class="btn btn-warning btn-sm" onclick="docAction('${currentTxn}','${d.code}','receive')">Receive</button>`;
  }
  if (d.status === 'received' || d.status === 'required') {
    btns += `<button class="btn btn-success btn-sm" onclick="docAction('${currentTxn}','${d.code}','verify')">Verify</button>`;
  }
  if (d.status !== 'na') {
    btns += `<button class="btn btn-muted btn-sm" onclick="docAction('${currentTxn}','${d.code}','na')">N/A</button>`;
  }
  return btns;
}

async function docAction(tid, code, action) {
  const res = await post(`/api/txns/${tid}/docs/${code}/${action}`);
  if (res._error) return;
  Toast.show(`Document ${code} ${action === 'receive' ? 'received' : action === 'verify' ? 'verified' : 'marked N/A'}`, 'success');
  renderDocs();
  const t = await get(`/api/txns/${tid}`);
  if (!t._error) {
    txnCache[tid] = t;
    loadTxns();
  }
}

// ══════════════════════════════════════════════════════════════════════════════
//  GATES TAB (with filter bar)
// ══════════════════════════════════════════════════════════════════════════════

let _gatesData = [];

async function renderGates() {
  showSkeleton('gate-cards');
  const gates = await get(`/api/txns/${currentTxn}/gates`);
  if (gates._error) return;
  _gatesData = gates;

  // Cache for command palette
  if (txnCache[currentTxn]) txnCache[currentTxn]._gates = gates;

  const el = $('#tab-content');

  if (gates.length === 0) {
    el.innerHTML = '<div class="card"><p style="color:var(--text-secondary)">No gates tracked.</p></div>';
    return;
  }

  const verified = gates.filter(g => g.status === 'verified').length;
  const types = [...new Set(gates.map(g => g.type).filter(Boolean))];
  const statuses = [...new Set(gates.map(g => g.status).filter(Boolean))];

  let html = `<div class="filter-bar" id="gates-filter">
    <input type="text" placeholder="Search gates..." id="gates-search">
    <select id="gates-type-filter">
      <option value="">All Types</option>
      ${types.map(t => `<option value="${t}">${t}</option>`).join('')}
    </select>
    <select id="gates-status-filter">
      <option value="">All Statuses</option>
      ${statuses.map(s => `<option value="${s}">${s}</option>`).join('')}
    </select>
  </div>`;

  html += `<div class="summary-bar">
    <span><span class="count">${gates.length}</span> total</span>
    <span><span class="count">${verified}</span> verified</span>
    <span><span class="count">${gates.length - verified}</span> pending</span>
  </div>`;

  html += '<div id="gates-list-area"></div>';
  el.innerHTML = html;

  renderGatesList(gates);

  // Filters
  const searchEl = document.getElementById('gates-search');
  const typeEl = document.getElementById('gates-type-filter');
  const statusEl = document.getElementById('gates-status-filter');
  const filterFn = () => {
    const q = searchEl.value.toLowerCase();
    const tp = typeEl.value;
    const st = statusEl.value;
    const filtered = _gatesData.filter(g => {
      if (q && !(g.name || '').toLowerCase().includes(q) && !g.gid.toLowerCase().includes(q)) return false;
      if (tp && g.type !== tp) return false;
      if (st && g.status !== st) return false;
      return true;
    });
    renderGatesList(filtered);
  };
  searchEl.addEventListener('input', filterFn);
  typeEl.addEventListener('change', filterFn);
  statusEl.addEventListener('change', filterFn);
}

function renderGatesList(gates) {
  const area = document.getElementById('gates-list-area');
  if (!area) return;

  if (gates.length === 0) {
    area.innerHTML = '<div class="card"><p style="color:var(--text-secondary)">No gates match your filters.</p></div>';
    return;
  }

  area.innerHTML = gates.map(g => {
    const isVerified = g.status === 'verified';
    return `
      <div class="gate-card" id="gate-${g.gid}" onclick="toggleGate('${g.gid}')">
        <div class="gate-header">
          <div>
            <span class="gate-name">${esc(g.name)}</span>
            <span class="gate-id">${g.gid}</span>
          </div>
          <div style="display:flex;gap:8px;align-items:center">
            <span class="type-badge ${g.type}">${g.type}</span>
            <span class="badge badge-${isVerified ? 'verified' : 'pending'}">${g.status}</span>
            ${!isVerified ? `<button class="btn btn-success btn-sm" onclick="event.stopPropagation();verifyGate('${g.gid}')">Verify</button>` : ''}
          </div>
        </div>
        <div class="gate-detail">
          ${g.legal_basis && g.legal_basis.statute ? `
            <h4>Legal Basis</h4>
            <p>${esc(g.legal_basis.statute)}</p>
            <p style="margin-top:4px">${esc(g.legal_basis.obligation || '')}</p>
          ` : ''}
          ${g.what_agent_verifies && g.what_agent_verifies.length ? `
            <h4>What to Verify</h4>
            <ul>${g.what_agent_verifies.map(item => `<li>${esc(item)}</li>`).join('')}</ul>
          ` : ''}
          ${g.cannot_proceed_until ? `<p style="margin-top:8px;font-style:italic">${esc(g.cannot_proceed_until)}</p>` : ''}
        </div>
      </div>`;
  }).join('');
}

function toggleGate(gid) {
  const card = document.getElementById('gate-' + gid);
  if (card) card.classList.toggle('expanded');
}

async function verifyGate(gid) {
  const res = await post(`/api/txns/${currentTxn}/gates/${gid}/verify`);
  if (res._error) return;
  Toast.show(`Gate ${gid} verified`, 'success');
  renderGates();
  const t = await get(`/api/txns/${currentTxn}`);
  if (!t._error) {
    txnCache[currentTxn] = t;
    loadTxns();
  }
}

// ══════════════════════════════════════════════════════════════════════════════
//  DEADLINES TAB (with filter bar + timeline)
// ══════════════════════════════════════════════════════════════════════════════

let _deadlinesData = [];

async function renderDeadlines() {
  showSkeleton('table');
  const dls = await get(`/api/txns/${currentTxn}/deadlines`);
  if (dls._error) return;
  _deadlinesData = dls;

  // Cache for command palette
  if (txnCache[currentTxn]) txnCache[currentTxn]._deadlines = dls;

  const el = $('#tab-content');

  if (dls.length === 0) {
    el.innerHTML = '<div class="card"><p style="color:var(--text-secondary)">No deadlines. Extract a contract PDF to calculate deadlines.</p></div>';
    return;
  }

  // Urgency options
  const urgencies = ['overdue', 'urgent', 'soon', 'ok'];

  let html = `<div class="filter-bar">
    <input type="text" placeholder="Search deadlines..." id="deadlines-search">
    <select id="deadlines-urgency-filter">
      <option value="">All Urgencies</option>
      ${urgencies.map(u => `<option value="${u}">${u}</option>`).join('')}
    </select>
  </div>`;

  html += `<div class="view-toggle">
    <button class="btn ${deadlineView === 'table' ? 'active' : ''}" onclick="setDeadlineView('table')">Table</button>
    <button class="btn ${deadlineView === 'timeline' ? 'active' : ''}" onclick="setDeadlineView('timeline')">Timeline</button>
  </div>`;

  html += '<div id="deadlines-area"></div>';
  el.innerHTML = html;

  renderDeadlinesView(_deadlinesData);

  // Filter
  const searchEl = document.getElementById('deadlines-search');
  const urgencyEl = document.getElementById('deadlines-urgency-filter');
  const filterFn = () => {
    const q = searchEl.value.toLowerCase();
    const u = urgencyEl.value;
    const filtered = _deadlinesData.filter(d => {
      if (q && !(d.name || '').toLowerCase().includes(q) && !(d.did || '').toLowerCase().includes(q)) return false;
      if (u) {
        const cls = urgencyClass(d.days_remaining);
        if (cls !== u) return false;
      }
      return true;
    });
    renderDeadlinesView(filtered);
  };
  searchEl.addEventListener('input', filterFn);
  urgencyEl.addEventListener('change', filterFn);
}

function urgencyClass(days) {
  if (days === null || days === undefined) return '';
  if (days < 0) return 'overdue';
  if (days <= 1) return 'urgent';
  if (days <= 5) return 'soon';
  return 'ok';
}

function setDeadlineView(view) {
  deadlineView = view;
  // Re-render just the view area, keeping filters
  const el = document.querySelector('.view-toggle');
  if (el) {
    el.querySelectorAll('.btn').forEach(b => {
      b.classList.toggle('active', b.textContent.toLowerCase() === view);
    });
  }
  renderDeadlinesView(_deadlinesData);
}

function renderDeadlinesView(dls) {
  const area = document.getElementById('deadlines-area');
  if (!area) return;

  if (dls.length === 0) {
    area.innerHTML = '<div class="card"><p style="color:var(--text-secondary)">No deadlines match your filters.</p></div>';
    return;
  }

  if (deadlineView === 'timeline') {
    renderTimeline(area, dls);
  } else {
    renderDeadlinesTable(area, dls);
  }
}

function renderDeadlinesTable(area, dls) {
  let html = '<div class="table-wrap"><table><thead><tr><th>ID</th><th>Deadline</th><th>Type</th><th>Due</th><th>Days</th></tr></thead><tbody>';
  dls.forEach(d => {
    const days = d.days_remaining;
    const pillCls = urgencyClass(days);
    html += `<tr>
      <td><code>${esc(d.did)}</code></td>
      <td>${esc(d.name)}</td>
      <td>${esc(d.type || '')}</td>
      <td>${d.due || '&mdash;'}</td>
      <td>${days !== null ? `<span class="days-pill ${pillCls}">${days}d</span>` : '&mdash;'}</td>
    </tr>`;
  });
  html += '</tbody></table></div>';
  area.innerHTML = html;
}

function renderTimeline(area, dls) {
  // Only show items with due dates
  const items = dls.filter(d => d.due);
  if (items.length === 0) {
    area.innerHTML = '<div class="card"><p style="color:var(--text-secondary)">No deadlines have due dates set.</p></div>';
    return;
  }

  // Calculate date range
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const dates = items.map(d => new Date(d.due + 'T00:00:00'));
  const minDate = new Date(Math.min(today, ...dates));
  const maxDate = new Date(Math.max(today, ...dates));

  // Add padding: 3 days each side
  minDate.setDate(minDate.getDate() - 3);
  maxDate.setDate(maxDate.getDate() + 3);

  const range = maxDate - minDate;
  const todayPct = ((today - minDate) / range) * 100;

  let dotsHtml = '';
  items.forEach(d => {
    const dt = new Date(d.due + 'T00:00:00');
    const pct = ((dt - minDate) / range) * 100;
    const days = d.days_remaining;
    let colorCls = 'tl-green';
    if (days !== null) {
      if (days < 0) colorCls = 'tl-red';
      else if (days <= 2) colorCls = 'tl-orange';
      else if (days <= 5) colorCls = 'tl-yellow';
    }
    dotsHtml += `
      <div class="timeline-dot ${colorCls}" style="left:${pct}%"
           data-name="${esc(d.name)}" data-due="${esc(d.due)}" data-days="${days}"
           onmouseenter="showTimelineTooltip(event, this)"
           onmouseleave="hideTimelineTooltip()">
        <div class="timeline-dot-label">${esc(d.name)}</div>
      </div>`;
  });

  area.innerHTML = `
    <div class="timeline-container">
      <div class="timeline-axis">
        <div class="timeline-line"></div>
        <div class="timeline-today" style="left:${todayPct}%"></div>
        ${dotsHtml}
      </div>
    </div>`;
}

function showTimelineTooltip(event, el) {
  // Remove any existing tooltip
  hideTimelineTooltip();
  const tt = document.createElement('div');
  tt.className = 'timeline-tooltip';
  tt.id = 'timeline-tt';
  const days = el.dataset.days;
  const daysText = days === 'null' ? 'No date' :
    (parseInt(days) < 0 ? `${Math.abs(days)} days overdue` :
      (parseInt(days) === 0 ? 'Due today' : `${days} days remaining`));
  tt.innerHTML = `
    <div class="tt-name">${el.dataset.name}</div>
    <div class="tt-date">${el.dataset.due}</div>
    <div class="tt-days">${daysText}</div>`;
  document.body.appendChild(tt);
  const rect = el.getBoundingClientRect();
  tt.style.left = rect.left + rect.width / 2 - tt.offsetWidth / 2 + 'px';
  tt.style.top = rect.top - tt.offsetHeight - 8 + 'px';
}

function hideTimelineTooltip() {
  const tt = document.getElementById('timeline-tt');
  if (tt) tt.remove();
}

// ══════════════════════════════════════════════════════════════════════════════
//  SIGNATURES TAB
// ══════════════════════════════════════════════════════════════════════════════

let _sigData = [];
let _sigSummary = {};
let _sigEnvelopes = {};  // keyed by sig_review_id
let _sandboxMode = true;

async function fetchSandboxStatus() {
  const res = await get('/api/sandbox-status');
  if (!res._error) _sandboxMode = res.sandbox;
}

async function renderSignatures() {
  showSkeleton('gate-cards');

  // Fetch sandbox status, signatures, and envelopes in parallel
  const [sbRes, sigRes, envRes] = await Promise.all([
    get('/api/sandbox-status'),
    get(`/api/txns/${currentTxn}/signatures`),
    get(`/api/txns/${currentTxn}/envelopes`),
  ]);

  if (!sbRes._error) _sandboxMode = sbRes.sandbox;
  if (sigRes._error) return;
  _sigData = sigRes.items || [];
  _sigSummary = sigRes.summary || {};

  // Index envelopes by sig_review_id
  _sigEnvelopes = {};
  if (!envRes._error) {
    (envRes || []).forEach(e => { _sigEnvelopes[e.sig_review_id] = e; });
  }

  const el = $('#tab-content');
  let html = '';

  // Sandbox banner
  if (_sandboxMode) {
    html += '<div class="sandbox-banner">Sandbox mode — emails and API calls are simulated. No real messages are sent.</div>';
  }

  if (_sigData.length === 0) {
    html += '<div class="card"><p style="color:var(--text-secondary)">No signature or initial fields detected. Fields are auto-populated from document manifests when available.</p></div>';
    el.innerHTML = html;
    return;
  }

  // Filter bar
  html += `<div class="filter-bar" id="sig-filter">
    <input type="text" placeholder="Search fields..." id="sig-search">
    <select id="sig-type-filter">
      <option value="">All Types</option>
      <option value="signature">Signatures</option>
      <option value="initials">Initials</option>
    </select>
    <select id="sig-status-filter">
      <option value="">All Statuses</option>
      <option value="pending">Pending</option>
      <option value="reviewed">Reviewed</option>
      <option value="flagged">Flagged</option>
      <option value="unfilled">Unfilled Only</option>
    </select>
    <button class="btn btn-primary btn-sm" onclick="openSigModal()">+ Add Field</button>
  </div>`;

  // Summary cards
  const s = _sigSummary;
  html += `<div class="sig-summary">
    <div class="sig-summary-item"><span class="sig-summary-val">${s.total}</span><span class="sig-summary-lbl">Total</span></div>
    <div class="sig-summary-item"><span class="sig-summary-val sig-filled">${s.filled}</span><span class="sig-summary-lbl">Filled</span></div>
    <div class="sig-summary-item"><span class="sig-summary-val sig-empty">${s.empty}</span><span class="sig-summary-lbl">Empty</span></div>
    <div class="sig-summary-item"><span class="sig-summary-val sig-reviewed">${s.reviewed}</span><span class="sig-summary-lbl">Reviewed</span></div>
    <div class="sig-summary-item"><span class="sig-summary-val sig-flagged">${s.flagged}</span><span class="sig-summary-lbl">Flagged</span></div>
  </div>`;

  html += '<div id="sig-list-area"></div>';
  html += '<div id="sig-outbox-area"></div>';
  el.innerHTML = html;

  renderSigList(_sigData);
  renderOutbox();

  // Attach filters
  const searchEl = document.getElementById('sig-search');
  const typeEl = document.getElementById('sig-type-filter');
  const statusEl = document.getElementById('sig-status-filter');
  const filterFn = () => {
    const q = searchEl.value.toLowerCase();
    const tp = typeEl.value;
    const st = statusEl.value;
    const filtered = _sigData.filter(item => {
      if (q && !(item.field_name || '').toLowerCase().includes(q)
            && !(item.doc_name || '').toLowerCase().includes(q)
            && !(item.doc_code || '').toLowerCase().includes(q)) return false;
      if (tp && item.field_type !== tp) return false;
      if (st === 'unfilled' && item.is_filled) return false;
      if (st && st !== 'unfilled' && item.review_status !== st) return false;
      return true;
    });
    renderSigList(filtered);
  };
  searchEl.addEventListener('input', filterFn);
  typeEl.addEventListener('change', filterFn);
  statusEl.addEventListener('change', filterFn);
}

function renderSigList(items) {
  const area = document.getElementById('sig-list-area');
  if (!area) return;

  if (items.length === 0) {
    area.innerHTML = '<div class="card"><p style="color:var(--text-secondary)">No fields match your filters.</p></div>';
    return;
  }

  // Group by doc_code
  const groups = {};
  items.forEach(item => {
    const key = item.doc_code || 'unknown';
    if (!groups[key]) groups[key] = { name: item.doc_name || item.doc_code, items: [] };
    groups[key].items.push(item);
  });

  let html = '';
  Object.entries(groups).forEach(([code, group]) => {
    const count = group.items.length;
    html += `<div class="sig-doc-group">
      <div class="sig-doc-header">
        <span class="sig-doc-name">${esc(group.name || code)}</span>
        <span class="sig-doc-meta">${esc(code)} &middot; ${count} field${count !== 1 ? 's' : ''}</span>
      </div>
      <div class="sig-fields">`;

    group.items.forEach(item => {
      const typeBadge = item.field_type === 'initials' ? 'INI' : 'SIG';
      const typeCls = item.field_type === 'initials' ? 'sig-badge-ini' : 'sig-badge-sig';
      const fillCls = item.is_filled ? 'sig-fill-yes' : 'sig-fill-no';
      const fillText = item.is_filled ? 'Filled' : 'Empty';

      let statusIcon = '';
      let statusCls = '';
      switch (item.review_status) {
        case 'reviewed':
          statusIcon = '\u2705'; statusCls = 'sig-status-reviewed'; break;
        case 'flagged':
          statusIcon = '\u26A0\uFE0F'; statusCls = 'sig-status-flagged'; break;
        case 'manual':
          statusIcon = '\u2795'; statusCls = 'sig-status-manual'; break;
        default:
          statusIcon = '\u23F3'; statusCls = 'sig-status-pending'; break;
      }

      const truncName = (item.field_name || '').length > 50
        ? item.field_name.substring(0, 50) + '...'
        : (item.field_name || '');

      // Check if there's an envelope for this field
      const env = _sigEnvelopes[item.id];

      html += `<div class="sig-field-card">
        <div class="sig-field-top">
          <div class="sig-field-info">
            <span class="sig-badge ${typeCls}">${typeBadge}</span>
            <span class="sig-field-name">${esc(truncName)}</span>
            <span class="sig-field-page">p${item.page || '?'}</span>
            <span class="sig-fill-pill ${fillCls}">${fillText}</span>
          </div>
          <div class="sig-field-actions">`;

      if (item.review_status === 'pending' || item.review_status === 'manual') {
        html += `<button class="btn btn-success btn-sm" onclick="sigReview(${item.id}, 'reviewed')">Review</button>`;
        html += `<button class="btn btn-warning btn-sm" onclick="sigFlag(${item.id})">Flag</button>`;
      } else if (item.review_status === 'reviewed') {
        html += `<button class="btn btn-warning btn-sm" onclick="sigFlag(${item.id})">Flag</button>`;
      } else if (item.review_status === 'flagged') {
        html += `<button class="btn btn-success btn-sm" onclick="sigReview(${item.id}, 'reviewed')">Review</button>`;
      }

      if (item.source === 'manual') {
        html += `<button class="btn btn-danger btn-sm" onclick="sigDelete(${item.id})">Delete</button>`;
      }

      html += `</div></div>
        <div class="sig-field-bottom">
          <span class="sig-status ${statusCls}">${statusIcon} ${esc(item.review_status)}</span>`;

      if (item.reviewer_note) {
        html += `<span class="sig-note">"${esc(item.reviewer_note)}"</span>`;
      }
      if (item.reviewed_at) {
        html += `<span class="sig-time">${esc(item.reviewed_at)}</span>`;
      }
      if (item.source === 'manual') {
        html += '<span class="sig-source-badge">manual</span>';
      }

      html += `</div>`;

      // ── Follow-up section ──
      html += `<div class="sig-followup">`;
      if (env) {
        // Envelope exists — show status + reminder/simulate
        const envStatusCls = 'envelope-pill env-' + env.status;
        html += `<div class="sig-followup-row">
          <span class="sig-followup-label">Signer:</span>
          <span>${esc(env.recipient_name)} &lt;${esc(env.recipient_email)}&gt;</span>
        </div>
        <div class="sig-followup-row">
          <span class="sig-followup-label">${esc(env.provider || 'DocuSign')}:</span>
          <span class="${envStatusCls}">${esc(env.status)}</span>
          ${env.sent_at ? `<span class="sig-time">${esc(env.sent_at)}</span>` : ''}
        </div>`;
        if (item.reminder_count) {
          html += `<div class="sig-followup-row">
            <span class="sig-followup-label">Reminders sent:</span>
            <span>${item.reminder_count}</span>
          </div>`;
        }
        html += `<div class="sig-followup-actions">`;
        if (env.status !== 'signed' && env.status !== 'declined') {
          html += `<button class="btn btn-warning btn-sm" onclick="sigRemind(${item.id})">Send Reminder</button>`;
          if (_sandboxMode) {
            html += `<button class="btn btn-simulate btn-sm" onclick="sigSimulate(${item.id})">Simulate Sign</button>`;
          }
        }
        html += `</div>`;
      } else if (!item.is_filled) {
        // No envelope — show send form
        html += `<div class="sig-send-inline" id="sig-send-${item.id}">
          <input type="email" placeholder="Signer email" id="sig-email-${item.id}"
                 value="${esc(item.signer_email || '')}" class="sig-send-input">
          <input type="text" placeholder="Signer name" id="sig-name-${item.id}"
                 value="${esc(item.signer_name || '')}" class="sig-send-input">
          <button class="btn btn-primary btn-sm" onclick="sigSend(${item.id})">Send for Signing</button>
        </div>`;
      }
      html += `</div>`;

      html += `</div>`;
    });

    html += '</div></div>';
  });

  area.innerHTML = html;
}

async function renderOutbox() {
  const area = document.getElementById('sig-outbox-area');
  if (!area) return;

  const outbox = await get(`/api/txns/${currentTxn}/outbox`);
  if (outbox._error || !outbox.length) {
    area.innerHTML = '';
    return;
  }

  let html = `<div class="outbox-section">
    <div class="outbox-header" onclick="document.getElementById('outbox-list').classList.toggle('collapsed')">
      <span>Email Outbox${_sandboxMode ? ' (sandbox)' : ''}</span>
      <span class="outbox-count">${outbox.length}</span>
    </div>
    <div class="outbox-list" id="outbox-list">`;

  outbox.forEach(msg => {
    const statusCls = msg.status === 'sandbox' ? 'outbox-sandbox' :
                      msg.status === 'sent' ? 'outbox-sent' : 'outbox-queued';
    html += `<div class="outbox-card">
      <div class="outbox-card-header">
        <span class="outbox-to">To: ${esc(msg.to_addr)}</span>
        <span class="outbox-status ${statusCls}">${esc(msg.status)}</span>
      </div>
      <div class="outbox-subject">${esc(msg.subject)}</div>
      <div class="outbox-meta">${esc(msg.created_at || '')}</div>
      <details class="outbox-body-toggle">
        <summary>View body</summary>
        <pre class="outbox-body">${esc(msg.body)}</pre>
      </details>
    </div>`;
  });

  html += '</div></div>';
  area.innerHTML = html;
}

// ── Signature review/flag/delete actions ────────────────────────────────

async function sigReview(sigId, status) {
  const res = await post(`/api/txns/${currentTxn}/signatures/${sigId}/review`, { status, note: '' });
  if (res._error) return;
  Toast.show(`Field marked as ${status}`, 'success');
  renderSignatures();
}

async function sigFlag(sigId) {
  const note = prompt('Flag note (optional):') || '';
  const res = await post(`/api/txns/${currentTxn}/signatures/${sigId}/review`, { status: 'flagged', note });
  if (res._error) return;
  Toast.show('Field flagged', 'warning');
  renderSignatures();
}

async function sigDelete(sigId) {
  if (!confirm('Delete this manually added field?')) return;
  const res = await del(`/api/txns/${currentTxn}/signatures/${sigId}`);
  if (res._error) return;
  Toast.show('Field removed', 'info');
  renderSignatures();
}

// ── Follow-up actions (send, remind, simulate) ─────────────────────────

async function sigSend(sigId) {
  const emailEl = document.getElementById(`sig-email-${sigId}`);
  const nameEl = document.getElementById(`sig-name-${sigId}`);
  const email = (emailEl ? emailEl.value : '').trim();
  const name = (nameEl ? nameEl.value : '').trim();
  if (!email || !name) {
    Toast.show('Email and name are required', 'warning');
    return;
  }
  const res = await post(`/api/txns/${currentTxn}/signatures/${sigId}/send`, { email, name });
  if (res._error) return;
  Toast.show(`Sent for signing to ${email}`, 'success');
  renderSignatures();
}

async function sigRemind(sigId) {
  const res = await post(`/api/txns/${currentTxn}/signatures/${sigId}/remind`);
  if (res._error) return;
  Toast.show(`Reminder sent (${res.reminder_count})`, 'success');
  renderSignatures();
}

async function sigSimulate(sigId) {
  const res = await post(`/api/txns/${currentTxn}/signatures/${sigId}/simulate`);
  if (res._error) return;
  Toast.show('Signature simulated', 'success');
  renderSignatures();
}

// ── Signature Add Modal ─────────────────────────────────────────────────

function openSigModal() {
  // Populate doc dropdown
  const sel = document.getElementById('sig-doc-code');
  if (sel) {
    sel.innerHTML = '<option value="">Select document...</option>';
    const docs = (txnCache[currentTxn] && txnCache[currentTxn]._docs) || [];
    docs.forEach(d => {
      const opt = document.createElement('option');
      opt.value = d.code;
      opt.textContent = `${d.name} (${d.code})`;
      sel.appendChild(opt);
    });
  }
  document.getElementById('sig-modal-backdrop').style.display = '';
  const nameInput = document.getElementById('sig-field-name');
  if (nameInput) nameInput.focus();
}

function closeSigModal() {
  document.getElementById('sig-modal-backdrop').style.display = 'none';
  const form = document.getElementById('form-add-sig');
  if (form) form.reset();
}

async function handleAddSig(e) {
  e.preventDefault();
  const docCode = document.getElementById('sig-doc-code').value;
  const fieldName = document.getElementById('sig-field-name').value.trim();
  const fieldType = document.querySelector('input[name="sig-type"]:checked').value;
  const page = parseInt(document.getElementById('sig-page').value) || 1;
  const note = document.getElementById('sig-note').value.trim();

  if (!docCode || !fieldName) {
    Toast.show('Document and field name are required', 'warning');
    return;
  }

  const res = await post(`/api/txns/${currentTxn}/signatures/add`, {
    doc_code: docCode,
    field_name: fieldName,
    field_type: fieldType,
    page,
    note,
  });
  if (res._error) return;
  Toast.show('Field added', 'success');
  closeSigModal();
  renderSignatures();
}

// ══════════════════════════════════════════════════════════════════════════════
//  CONTINGENCIES TAB
// ══════════════════════════════════════════════════════════════════════════════

let _contData = [];
let _contSummary = {};

async function renderContingencies() {
  showSkeleton('gate-cards');
  const res = await get(`/api/txns/${currentTxn}/contingencies`);
  if (res._error) return;
  _contData = res.items || [];
  _contSummary = res.summary || {};

  const el = $('#tab-content');
  let html = '';

  // Summary bar
  const s = _contSummary;
  html += `<div class="cont-summary">
    <div class="cont-summary-item"><span class="cont-summary-val">${s.total || 0}</span><span class="cont-summary-lbl">Total</span></div>
    <div class="cont-summary-item"><span class="cont-summary-val cont-active">${s.active || 0}</span><span class="cont-summary-lbl">Active</span></div>
    <div class="cont-summary-item"><span class="cont-summary-val cont-removed">${s.removed || 0}</span><span class="cont-summary-lbl">Removed</span></div>
    <div class="cont-summary-item"><span class="cont-summary-val cont-overdue">${s.overdue || 0}</span><span class="cont-summary-lbl">Overdue</span></div>
    <button class="btn btn-primary btn-sm" onclick="openContModal()">+ Add</button>
  </div>`;

  if (_contData.length === 0) {
    html += '<div class="card"><p style="color:var(--text-secondary)">No contingencies tracked. Add contingencies manually or extract a contract PDF to auto-populate.</p></div>';
    el.innerHTML = html;
    return;
  }

  html += '<div id="cont-list-area"></div>';
  el.innerHTML = html;
  renderContList(_contData);
}

function renderContList(items) {
  const area = document.getElementById('cont-list-area');
  if (!area) return;

  area.innerHTML = items.map(item => {
    const urgencyCls = { overdue: 'cont-overdue', urgent: 'cont-urgent', soon: 'cont-soon', ok: 'cont-ok' }[item.urgency] || '';
    const statusCls = { active: 'cont-status-active', removed: 'cont-status-removed', waived: 'cont-status-waived', expired: 'cont-status-expired' }[item.status] || '';
    const isActive = item.status === 'active';

    // Progress bar computation
    const totalDays = item.default_days || 17;
    const elapsed = totalDays - (item.days_remaining || 0);
    const pct = Math.min(100, Math.max(0, (elapsed / totalDays) * 100));

    let html = `<div class="cont-card ${urgencyCls}">
      <div class="cont-card-header">
        <div class="cont-card-title">
          <span class="cont-type-icon">${contIcon(item.type)}</span>
          <span class="cont-name">${esc(item.name || item.type)}</span>
          <span class="cont-status-badge ${statusCls}">${esc(item.status).toUpperCase()}</span>
        </div>
        <div class="cont-deadline-info">
          ${item.deadline_date ? `<span class="cont-deadline-date">${esc(item.deadline_date)}</span>` : ''}
        </div>
      </div>`;

    if (isActive && item.deadline_date) {
      html += `<div class="cont-progress-section">
        <div class="cont-progress-bar">
          <div class="cont-progress-fill ${urgencyCls}" style="width:${pct}%"></div>
        </div>
        <span class="cont-days-label">${item.days_remaining ?? '?'} of ${totalDays} days remaining</span>
      </div>`;
    }

    // NBP section
    if (item.nbp_sent_at) {
      const nbpCls = (item.nbp_days_remaining || 0) <= 0 ? 'cont-nbp-expired' : 'cont-nbp-active';
      html += `<div class="cont-nbp-section ${nbpCls}">
        <span class="cont-nbp-label">NBP Issued:</span>
        <span>${esc(item.nbp_sent_at)}</span>
        <span class="cont-nbp-expires">Expires: ${esc(item.nbp_expires_at)}</span>
        ${item.nbp_days_remaining !== null ? `<span class="cont-nbp-countdown">${item.nbp_days_remaining}d remaining</span>` : ''}
      </div>`;
    }

    // Metadata row
    html += `<div class="cont-meta-row">`;
    if (item.related_gate) {
      html += `<span class="cont-gate-link" onclick="switchTab('gates')">${esc(item.related_gate)}</span>`;
    }
    if (item.related_deadline) {
      html += `<span class="cont-dl-link">${esc(item.related_deadline)}</span>`;
    }
    if (item.removed_at) {
      html += `<span class="cont-removed-date">Removed: ${esc(item.removed_at)}</span>`;
    }
    if (item.waived_at) {
      html += `<span class="cont-waived-date">Waived: ${esc(item.waived_at)}</span>`;
    }
    if (item.notes) {
      html += `<span class="cont-note">"${esc(item.notes)}"</span>`;
    }
    html += `</div>`;

    // Actions
    if (isActive) {
      html += `<div class="cont-actions">
        <button class="btn btn-success btn-sm" onclick="contRemove(${item.id})">Remove (CR-1 Signed)</button>
        <button class="btn btn-warning btn-sm" onclick="contNBP(${item.id})"${item.nbp_sent_at ? ' disabled' : ''}>Issue NBP</button>
        <button class="btn btn-muted btn-sm" onclick="contWaive(${item.id})">Waive</button>
      </div>`;
    }

    html += `</div>`;
    return html;
  }).join('');
}

function contIcon(type) {
  return { investigation: '\uD83D\uDD0D', appraisal: '\uD83C\uDFE0', loan: '\uD83C\uDFE6', hoa: '\uD83C\uDFE2' }[type] || '\uD83D\uDCCB';
}

async function contRemove(cid) {
  if (!confirm('Confirm contingency removal — this means the CR-1 has been signed by the buyer.')) return;
  const res = await post(`/api/txns/${currentTxn}/contingencies/${cid}/remove`);
  if (res._error) return;
  Toast.show('Contingency removed, gate auto-verified', 'success');
  renderContingencies();
}

async function contNBP(cid) {
  if (!confirm('Issue Notice to Buyer to Perform? Buyer will have 2 days to remove or cancel.')) return;
  const res = await post(`/api/txns/${currentTxn}/contingencies/${cid}/nbp`);
  if (res._error) return;
  Toast.show('NBP issued — 2 day countdown started', 'warning');
  renderContingencies();
}

async function contWaive(cid) {
  if (!confirm('Mark this contingency as waived per original contract terms?')) return;
  const res = await post(`/api/txns/${currentTxn}/contingencies/${cid}/waive`);
  if (res._error) return;
  Toast.show('Contingency waived', 'info');
  renderContingencies();
}

// ── Contingency Add Modal ────────────────────────────────────────────────

function openContModal() {
  document.getElementById('cont-modal-backdrop').style.display = '';
  document.getElementById('cont-type').focus();
}

function closeContModal() {
  document.getElementById('cont-modal-backdrop').style.display = 'none';
  const form = document.getElementById('form-add-cont');
  if (form) form.reset();
}

async function handleAddCont(e) {
  e.preventDefault();
  const ctype = document.getElementById('cont-type').value;
  const days = parseInt(document.getElementById('cont-days').value) || 17;
  const deadline = document.getElementById('cont-deadline').value;
  const notes = document.getElementById('cont-notes').value.trim();

  if (!ctype) {
    Toast.show('Type is required', 'warning');
    return;
  }

  const body = { type: ctype, days, notes };
  if (deadline) body.deadline_date = deadline;

  const res = await post(`/api/txns/${currentTxn}/contingencies`, body);
  if (res._error) return;
  Toast.show('Contingency added', 'success');
  closeContModal();
  renderContingencies();
}

// ── Audit Tab ───────────────────────────────────────────────────────────────

async function renderAudit() {
  showSkeleton('table');
  const logs = await get(`/api/txns/${currentTxn}/audit`);
  if (logs._error) return;
  const el = $('#tab-content');

  if (logs.length === 0) {
    el.innerHTML = '<div class="card"><p style="color:var(--text-secondary)">No audit entries.</p></div>';
    return;
  }

  let html = '<div class="card"><ul class="audit-list">';
  logs.forEach(e => {
    html += `<li class="audit-item">
      <span class="audit-ts">${esc(e.ts || '')}</span>
      <span class="audit-action">${esc(e.action)}</span>
      <span class="audit-detail">${esc(e.detail || '')}</span>
    </li>`;
  });
  html += '</ul></div>';
  el.innerHTML = html;
}

// ── Modal ───────────────────────────────────────────────────────────────────

function openModal() {
  $('#modal-backdrop').style.display = '';
  $('#inp-address').focus();
}

function closeModal() {
  $('#modal-backdrop').style.display = 'none';
  $('#form-new').reset();
}

async function handleCreate(e) {
  e.preventDefault();
  const body = {
    address: $('#inp-address').value,
    type: $('#inp-type').value,
    role: $('#inp-role').value,
    brokerage: $('#inp-brokerage').value,
  };
  const res = await post('/api/txns', body);
  if (res._error) return;
  if (res.id) {
    currentTxn = res.id;
    closeModal();
    Toast.show('Transaction created', 'success');
    loadTxns();
  } else {
    Toast.show(res.error || 'Failed to create', 'error');
  }
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function esc(s) {
  if (!s) return '';
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function formatPhase(id) {
  if (!id) return '';
  return id.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}
