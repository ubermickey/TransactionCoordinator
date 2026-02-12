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
  const suppress = opts._suppressToast;
  delete opts._suppressToast;
  try {
    const res = await fetch(API + path, {
      headers: { 'Content-Type': 'application/json' },
      ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    const data = await res.json();
    if (!res.ok) {
      if (!suppress) {
        const msg = data.error || data.message || `Request failed (${res.status})`;
        Toast.show(msg, 'error');
      }
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

// ── Response Cache (TTL-based) for fast tab switches ──
const _apiCache = new Map();
const CACHE_TTL = 8000; // 8 seconds
function getCached(path, ttl = CACHE_TTL) {
  const key = path;
  const cached = _apiCache.get(key);
  if (cached && Date.now() - cached.ts < ttl) return Promise.resolve(cached.data);
  return get(path).then(data => {
    if (!data._error) _apiCache.set(key, { data, ts: Date.now() });
    return data;
  });
}
function invalidateCache(prefix) {
  for (const key of _apiCache.keys()) {
    if (key.startsWith(prefix)) _apiCache.delete(key);
  }
}

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
          action: () => { switchTab('compliance'); },
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
          action: () => { switchTab('overview'); },
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
  const tabKeys = { '1': 'overview', '2': 'docs', '3': 'signatures', '4': 'contingencies', '5': 'parties', '6': 'compliance', '7': 'activity' };

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
    const pdfViewer = document.getElementById('pdf-viewer');
    if (pdfViewer && pdfViewer.style.display !== 'none') { PdfViewer.close(); return; }
    if (helpOpen) { closeHelp(); return; }
    if (CmdPalette.isOpen()) { CmdPalette.close(); return; }
    const reviewPanel = document.getElementById('review-panel');
    if (reviewPanel && reviewPanel.classList.contains('open')) { ReviewMode.close(); return; }
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

    // Cmd+R - review mode
    if (mod && e.key === 'r') {
      e.preventDefault();
      ReviewMode.toggle();
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

    // 1-7 - switch tabs
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
//  PDF VIEWER
// ══════════════════════════════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════════════════════════════
//  BUG REPORTER — screenshot capture, action log, submit
// ══════════════════════════════════════════════════════════════════════════════

const BugReporter = (() => {
  const MAX_LOG = 100;
  let actionLog = [];
  let screenshotData = '';

  function logAction(action, detail) {
    const ts = new Date().toISOString().slice(11, 19);
    actionLog.push({ ts, action, detail: (detail || '').slice(0, 200) });
    if (actionLog.length > MAX_LOG) actionLog.shift();
  }

  // Monkey-patch fetch to log API calls
  function hookFetch() {
    const origFetch = window.fetch;
    window.fetch = function(url, opts) {
      const method = (opts && opts.method) || 'GET';
      logAction(`${method} ${url}`, '');
      return origFetch.apply(this, arguments).then(resp => {
        logAction(`${resp.status} ${url}`, '');
        return resp;
      }).catch(err => {
        logAction(`ERR ${url}`, err.message);
        throw err;
      });
    };
  }

  // Capture clicks
  function hookClicks() {
    document.addEventListener('click', (e) => {
      const el = e.target.closest('button, a, .tab, .txn-item, .sig-field-card, .pdf-marker');
      if (!el) return;
      const text = (el.textContent || '').trim().slice(0, 60);
      const tag = el.tagName;
      const cls = (el.className || '').split(' ')[0];
      logAction('click', `${tag}.${cls}: ${text}`);
    }, true);
  }

  // Toast capture
  function hookToasts() {
    const origShow = Toast.show;
    Toast.show = function(msg, type) {
      logAction('toast', `${type}: ${msg}`);
      return origShow.apply(this, arguments);
    };
  }

  async function captureScreenshot() {
    if (typeof html2canvas === 'undefined') {
      Toast.show('html2canvas not loaded', 'warning');
      return '';
    }
    try {
      const canvas = await html2canvas(document.body, {
        scale: 0.5,
        logging: false,
        useCORS: true,
        ignoreElements: (el) => el.id === 'bug-modal-backdrop',
        onclone: (clonedDoc) => {
          // html2canvas can't parse color-mix() — strip it from cloned DOM
          clonedDoc.querySelectorAll('*').forEach(el => {
            const cs = el.style;
            for (let i = cs.length - 1; i >= 0; i--) {
              const prop = cs[i];
              if (cs.getPropertyValue(prop).includes('color-mix')) {
                cs.setProperty(prop, 'transparent');
              }
            }
            // Also fix computed styles applied via classes
            const computed = clonedDoc.defaultView.getComputedStyle(el);
            ['background', 'backgroundColor', 'borderColor', 'color'].forEach(p => {
              const v = computed[p];
              if (v && v.includes('color-mix')) {
                el.style[p] = 'transparent';
              }
            });
          });
        },
      });
      return canvas.toDataURL('image/png', 0.7);
    } catch (e) {
      logAction('screenshot_error', e.message);
      return '';
    }
  }

  async function openModal() {
    // Capture screenshot first (before modal is visible)
    screenshotData = await captureScreenshot();

    const backdrop = document.getElementById('bug-modal-backdrop');
    backdrop.style.display = '';

    // Show screenshot preview
    const preview = document.getElementById('bug-screenshot-preview');
    if (screenshotData) {
      preview.innerHTML = `<img src="${screenshotData}" alt="Screenshot" class="bug-screenshot-img">`;
    } else {
      preview.innerHTML = '<p style="color:var(--text-secondary);font-size:12px">Screenshot unavailable</p>';
    }

    // Show action log
    document.getElementById('bug-log-count').textContent = actionLog.length;
    document.getElementById('bug-action-log-pre').textContent =
      actionLog.map(e => `[${e.ts}] ${e.action} ${e.detail}`).join('\n');

    document.getElementById('bug-summary').focus();
  }

  function closeModal() {
    document.getElementById('bug-modal-backdrop').style.display = 'none';
    document.getElementById('bug-summary').value = '';
    document.getElementById('bug-description').value = '';
    screenshotData = '';
  }

  async function submit() {
    const summary = document.getElementById('bug-summary').value.trim();
    if (!summary) { Toast.show('Summary required', 'warning'); return; }
    const description = document.getElementById('bug-description').value.trim();

    const body = {
      summary,
      description,
      screenshot: screenshotData,
      action_log: actionLog.slice(-50),
      url: window.location.href,
    };

    const res = await post('/api/bug-reports', body);
    if (res._error) return;
    Toast.show('Bug report submitted (#' + res.id + ')', 'success');
    closeModal();
  }

  function init() {
    hookFetch();
    hookClicks();
    hookToasts();

    document.getElementById('bug-fab').addEventListener('click', openModal);
    document.getElementById('bug-modal-close').addEventListener('click', closeModal);
    document.getElementById('bug-btn-cancel').addEventListener('click', closeModal);
    document.getElementById('bug-btn-submit').addEventListener('click', submit);
    document.getElementById('bug-modal-backdrop').addEventListener('click', (e) => {
      if (e.target === e.currentTarget) closeModal();
    });

    // Log page loads
    logAction('page_load', window.location.pathname);
  }

  return { init, logAction, openModal };
})();

const PdfViewer = (() => {
  let pdfDoc = null;
  let pdfjsApi = null;
  let pdfjsLoadPromise = null;
  let currentPage = 1;
  let totalPages = 1;
  let scale = 1.5;
  let fields = [];
  let annotations = {};
  let dirty = false;
  let folder = '';
  let filename = '';
  let txnId = '';
  let activeFieldIdx = -1;
  let pageWidth = 0;
  let pageHeight = 0;

  async function ensurePdfJs() {
    if (pdfjsApi) return pdfjsApi;

    if (typeof window.pdfjsLib !== 'undefined') {
      pdfjsApi = window.pdfjsLib;
    } else {
      if (!pdfjsLoadPromise) {
        pdfjsLoadPromise = import('/static/vendor/pdf.min.mjs')
          .then((mod) => {
            const lib = (mod && mod.default && mod.default.getDocument) ? mod.default : mod;
            if (!lib || typeof lib.getDocument !== 'function') {
              throw new Error('pdf.js did not load correctly');
            }
            window.pdfjsLib = lib;
            return lib;
          })
          .catch((err) => {
            pdfjsLoadPromise = null;
            throw err;
          });
      }
      pdfjsApi = await pdfjsLoadPromise;
    }

    if (pdfjsApi.GlobalWorkerOptions) {
      pdfjsApi.GlobalWorkerOptions.workerSrc = '/static/vendor/pdf.worker.min.mjs';
    }
    return pdfjsApi;
  }

  function defaultStatus(f) {
    if (f.filled || f.value) return 'filled';
    const cat = (f.category || '').toLowerCase();
    if (cat === 'entry_days') return 'days';
    // Mandatory categories
    if (cat === 'entry_signature' || cat === 'entry_license' || cat === 'entry_dollar') return 'empty';
    // Dates: optional unless context has escrow/acceptance keywords
    if (cat === 'entry_date') return 'optional';
    // Other blanks: check context for mandatory keywords
    const ctx = (f.context || '').toLowerCase();
    if (cat === 'entry_blank' && /purchase\s*price|deposit|escrow|acceptance|apn|parcel|city|county|zip|agent|broker|firm|buyer|seller|tenant|signature|initial/.test(ctx)) return 'empty';
    return 'optional';
  }

  async function open(f, fn, tid) {
    folder = f;
    filename = fn;
    txnId = tid || '';
    dirty = false;
    activeFieldIdx = -1;

    const backdrop = document.getElementById('pdf-viewer');
    backdrop.style.display = '';
    document.getElementById('pdf-viewer-title').textContent = filename.replace('.pdf', '');

    let pdfjs;
    try {
      pdfjs = await ensurePdfJs();
    } catch (e) {
      Toast.show('PDF viewer unavailable: ' + e.message, 'error');
      return;
    }

    // Load fields + annotations + PDF in parallel
    const [fieldsData, annData] = await Promise.all([
      get(`/api/doc-packages/${encodeURIComponent(folder)}/${encodeURIComponent(filename)}/fields`),
      get(`/api/field-annotations/${encodeURIComponent(folder)}/${encodeURIComponent(filename)}?txn=${encodeURIComponent(txnId)}`),
    ]);

    fields = fieldsData._error ? [] : fieldsData;
    const saved = (!annData._error && annData.annotations) ? annData.annotations : {};

    // Build annotations: start with defaults, overlay saved
    annotations = {};
    fields.forEach((f, i) => {
      annotations[i] = saved[String(i)] || defaultStatus(f);
    });

    // Load PDF
    try {
      const url = `/api/doc-packages/${encodeURIComponent(folder)}/${encodeURIComponent(filename)}/pdf`;
      pdfDoc = await pdfjs.getDocument(url).promise;
      totalPages = pdfDoc.numPages;
      currentPage = 1;
      await renderPage(currentPage);
    } catch (e) {
      Toast.show('Failed to load PDF: ' + e.message, 'error');
    }

    renderSidebar();
    updatePageInfo();
  }

  function close() {
    if (dirty) {
      saveAnnotations();
    }
    document.getElementById('pdf-viewer').style.display = 'none';
    pdfDoc = null;
    fields = [];
    annotations = {};
  }

  async function renderPage(num) {
    if (!pdfDoc) return;
    const page = await pdfDoc.getPage(num);
    const viewport = page.getViewport({ scale });
    pageWidth = viewport.width;
    pageHeight = viewport.height;

    const canvas = document.getElementById('pdf-canvas');
    const ctx = canvas.getContext('2d');
    canvas.width = viewport.width;
    canvas.height = viewport.height;

    await page.render({ canvasContext: ctx, viewport }).promise;
    renderOverlay();
    updatePageInfo();
  }

  function renderOverlay() {
    const svg = document.getElementById('pdf-overlay');
    const canvas = document.getElementById('pdf-canvas');
    const wrap = document.getElementById('pdf-canvas-wrap');

    // Position SVG to overlay the canvas
    const canvasRect = canvas.getBoundingClientRect();
    const wrapRect = wrap.getBoundingClientRect();
    const offsetLeft = canvasRect.left - wrapRect.left + wrap.scrollLeft;
    const offsetTop = canvasRect.top - wrapRect.top + wrap.scrollTop;

    svg.setAttribute('width', canvas.width);
    svg.setAttribute('height', canvas.height);
    svg.style.left = offsetLeft + 'px';
    svg.style.top = offsetTop + 'px';
    svg.style.transform = 'translateX(0)'; // override the 50% centering

    // Get page dimensions for coordinate mapping
    // PDF coords: (0,0) at bottom-left. Viewport flips y.
    // field bbox: { x0, y0, x1, y1 } in PDF points
    // We need to scale based on rendered size vs page points
    let svgHTML = '';

    const PAD = 2;
    fields.forEach((f, idx) => {
      if (f.page !== currentPage) return;
      const bbox = f.bbox || {};
      if (!bbox.x0 && bbox.x0 !== 0) return;

      const x = (bbox.x0 - PAD) * scale;
      const y = (bbox.y0 - PAD) * scale;
      const w = (bbox.x1 - bbox.x0 + PAD * 2) * scale;
      const h = (bbox.y1 - bbox.y0 + PAD * 2) * scale;
      const rx = 3; // rounded corners

      const status = annotations[idx] || 'optional';
      const isActive = idx === activeFieldIdx;
      svgHTML += `<rect class="pdf-marker pdf-marker-${status}${isActive ? ' active' : ''}"
        x="${x}" y="${y}" width="${w}" height="${h}" rx="${rx}" ry="${rx}" data-idx="${idx}"/>`;
    });

    svg.innerHTML = svgHTML;

    // Click handlers
    svg.querySelectorAll('.pdf-marker').forEach(el => {
      el.addEventListener('click', (e) => {
        e.stopPropagation();
        const idx = parseInt(el.getAttribute('data-idx'));
        cycleStatus(idx);
      });
    });
  }

  function cycleStatus(idx) {
    const current = annotations[idx] || 'optional';
    const def = defaultStatus(fields[idx] || {});
    let next;

    if (current === 'ignored') {
      next = def;
    } else if (def === 'optional') {
      const cycle = { optional: 'filled', filled: 'empty', empty: 'ignored', ignored: 'optional' };
      next = cycle[current] || 'optional';
    } else {
      next = 'ignored';
    }

    // Apply to this field
    annotations[idx] = next;

    // Also toggle identical fields (same field name + category)
    const f = fields[idx];
    if (f) {
      const fname = (f.field || '').toLowerCase();
      const fcat = (f.category || '').toLowerCase();
      if (fname) {
        fields.forEach((other, otherIdx) => {
          if (otherIdx === idx) return;
          if ((other.field || '').toLowerCase() === fname &&
              (other.category || '').toLowerCase() === fcat) {
            annotations[otherIdx] = next;
          }
        });
      }
    }

    dirty = true;
    activeFieldIdx = idx;

    // Persist immediately for audit trail
    if (txnId && f) {
      post(`/api/field-annotations/${encodeURIComponent(folder)}/${encodeURIComponent(filename)}`, {
        txn: txnId, field_idx: idx, status: next,
        field_name: f.field || `Field ${idx}`,
      });
    }

    renderOverlay();
    renderSidebar();
  }

  function renderSidebar() {
    // Stats
    const statsEl = document.getElementById('pdf-sidebar-stats');
    const counts = { filled: 0, empty: 0, optional: 0, days: 0, ignored: 0 };
    Object.values(annotations).forEach(s => { counts[s] = (counts[s] || 0) + 1; });
    statsEl.innerHTML = `
      <span class="stat-item"><span class="stat-dot" style="background:#34c759"></span>${counts.filled}</span>
      <span class="stat-item"><span class="stat-dot" style="background:#ff3b30"></span>${counts.empty}</span>
      <span class="stat-item"><span class="stat-dot" style="background:#ffcc00"></span>${counts.optional}</span>
      <span class="stat-item"><span class="stat-dot" style="background:#ff8c00"></span>${counts.days}</span>
      <span class="stat-item"><span class="stat-dot" style="background:#8e8e93;opacity:0.5"></span>${counts.ignored}</span>
    `;

    // Group fields by page
    const listEl = document.getElementById('pdf-sidebar-list');
    const byPage = {};
    fields.forEach((f, idx) => {
      const p = f.page || 1;
      if (!byPage[p]) byPage[p] = [];
      byPage[p].push({ field: f, idx });
    });

    let html = '';
    Object.keys(byPage).sort((a, b) => a - b).forEach(p => {
      html += `<div class="pdf-sidebar-page-header">Page ${p}</div>`;
      byPage[p].forEach(({ field, idx }) => {
        const status = annotations[idx] || 'optional';
        const name = field.field || field.label || `Field ${idx}`;
        const cat = field.category || '';
        const isActive = idx === activeFieldIdx;
        html += `<div class="pdf-field-item${isActive ? ' active' : ''}" data-idx="${idx}" data-page="${p}">
          <span class="pdf-field-dot ${status}"></span>
          <span class="pdf-field-name" title="${esc(name)}">${esc(name)}</span>
          ${cat ? `<span class="pdf-field-cat">${esc(cat)}</span>` : ''}
        </div>`;
      });
    });
    listEl.innerHTML = html;

    // Click to navigate
    listEl.querySelectorAll('.pdf-field-item').forEach(el => {
      el.addEventListener('click', () => {
        const idx = parseInt(el.dataset.idx);
        const page = parseInt(el.dataset.page);
        activeFieldIdx = idx;
        if (page !== currentPage) {
          currentPage = page;
          renderPage(currentPage).then(() => renderSidebar());
        } else {
          renderOverlay();
          renderSidebar();
        }
      });
    });
  }

  async function saveAnnotations() {
    if (Object.keys(annotations).length === 0) return;
    const res = await post(
      `/api/field-annotations/${encodeURIComponent(folder)}/${encodeURIComponent(filename)}/bulk`,
      { txn: txnId, annotations: Object.fromEntries(
        Object.entries(annotations).map(([k, v]) => [String(k), v])
      )}
    );
    if (!res._error) {
      dirty = false;
      Toast.show('Annotations saved', 'success');
    }
  }

  function updatePageInfo() {
    document.getElementById('pdf-page-info').textContent = `${currentPage} / ${totalPages}`;
    document.getElementById('pdf-zoom-level').textContent = `${Math.round(scale * 100)}%`;
  }

  function prevPage() {
    if (currentPage > 1) { currentPage--; renderPage(currentPage); }
  }

  function nextPage() {
    if (currentPage < totalPages) { currentPage++; renderPage(currentPage); }
  }

  function zoomIn() {
    scale = Math.min(scale + 0.25, 4);
    renderPage(currentPage);
  }

  function zoomOut() {
    scale = Math.max(scale - 0.25, 0.5);
    renderPage(currentPage);
  }

  function fitWidth() {
    const wrap = document.getElementById('pdf-canvas-wrap');
    if (!pdfDoc || !wrap) return;
    pdfDoc.getPage(currentPage).then(page => {
      const vp = page.getViewport({ scale: 1 });
      const available = wrap.clientWidth - 32;
      scale = available / vp.width;
      renderPage(currentPage);
    });
  }

  function init() {
    document.getElementById('pdf-close').addEventListener('click', close);
    document.getElementById('pdf-prev').addEventListener('click', prevPage);
    document.getElementById('pdf-next').addEventListener('click', nextPage);
    document.getElementById('pdf-zoom-in').addEventListener('click', zoomIn);
    document.getElementById('pdf-zoom-out').addEventListener('click', zoomOut);
    document.getElementById('pdf-fit').addEventListener('click', fitWidth);
    document.getElementById('pdf-save-ann').addEventListener('click', saveAnnotations);

    // Reposition overlay on scroll
    document.getElementById('pdf-canvas-wrap').addEventListener('scroll', () => {
      if (document.getElementById('pdf-viewer').style.display !== 'none') {
        renderOverlay();
      }
    });
  }

  return { open, close, init };
})();

// ══════════════════════════════════════════════════════════════════════════════
//  REVIEW MODE — guided page-by-page feedback
// ══════════════════════════════════════════════════════════════════════════════

const ReviewMode = (() => {
  let isOpen = false;
  let pageIdx = 0;
  let notes = [];

  const PAGES = [
    { id: 'home',          name: 'Home',          isTxn: false },
    { id: 'overview',      name: 'Dashboard',     isTxn: true },
    { id: 'docs',          name: 'Documents',     isTxn: true },
    { id: 'signatures',    name: 'Signatures',    isTxn: true },
    { id: 'contingencies', name: 'Contingencies', isTxn: true },
    { id: 'parties',       name: 'Parties',       isTxn: true },
    { id: 'compliance',    name: 'Compliance',    isTxn: true },
    { id: 'activity',      name: 'Activity',      isTxn: true },
  ];

  function currentPage() { return PAGES[pageIdx]; }

  function detectPage() {
    if (!currentTxn) return 0;
    const tab = currentTab || 'overview';
    const idx = PAGES.findIndex(p => p.id === tab);
    return idx >= 0 ? idx : 1;
  }

  async function open() {
    isOpen = true;
    pageIdx = detectPage();
    document.getElementById('review-panel').classList.add('open');
    document.getElementById('review-fab').style.display = 'none';
    await loadNotes();
    render();
    document.getElementById('review-note-input').focus();
  }

  function close() {
    isOpen = false;
    document.getElementById('review-panel').classList.remove('open');
    document.getElementById('review-fab').style.display = '';
  }

  function toggle() { isOpen ? close() : open(); }

  async function loadNotes() {
    const res = await get('/api/review-notes');
    notes = res._error ? [] : res;
  }

  function pageNotes() {
    const pg = currentPage();
    return notes.filter(n => n.page === pg.id);
  }

  function render() {
    const pg = currentPage();
    document.getElementById('review-progress').textContent = `Page ${pageIdx + 1} of ${PAGES.length}`;
    document.getElementById('review-page-name').textContent = pg.name;

    const list = document.getElementById('review-notes-list');
    const pn = pageNotes();
    if (pn.length === 0) {
      list.innerHTML = '';
    } else {
      list.innerHTML = pn.map(n => `
        <div class="review-note-item ${n.status === 'done' ? 'done' : ''}">
          <div class="review-note-text">${esc(n.note)}</div>
          <div class="review-note-meta">
            <span class="review-note-time">${n.created_at || ''}</span>
            <span class="review-note-actions">
              ${n.status !== 'done' ? `<button class="btn-done" onclick="ReviewMode.resolveNote(${n.id})">Done</button>` : ''}
              <button class="btn-del" onclick="ReviewMode.deleteNote(${n.id})">Delete</button>
            </span>
          </div>
        </div>`).join('');
    }

    document.getElementById('review-prev').disabled = pageIdx === 0;
    document.getElementById('review-next').disabled = pageIdx === PAGES.length - 1;
  }

  function navigateTo(idx) {
    if (idx < 0 || idx >= PAGES.length) return;
    pageIdx = idx;
    const pg = currentPage();

    if (pg.id === 'home') {
      showHome();
    } else if (currentTxn) {
      switchTab(pg.id);
    }

    render();
  }

  function prev() { navigateTo(pageIdx - 1); }
  function next() { navigateTo(pageIdx + 1); }

  async function addNote() {
    const input = document.getElementById('review-note-input');
    const text = (input.value || '').trim();
    if (!text) return;
    const pg = currentPage();
    const res = await post('/api/review-notes', { page: pg.id, note: text });
    if (!res._error) {
      input.value = '';
      notes.unshift(res);
      render();
      Toast.show('Note added', 'success');
    }
  }

  async function resolveNote(id) {
    const res = await post(`/api/review-notes/${id}/resolve`);
    if (!res._error) {
      const idx = notes.findIndex(n => n.id === id);
      if (idx >= 0) notes[idx] = res;
      render();
    }
  }

  async function deleteNote(id) {
    const res = await del(`/api/review-notes/${id}`);
    if (!res._error) {
      notes = notes.filter(n => n.id !== id);
      render();
    }
  }

  function showSummary() {
    const backdrop = document.getElementById('review-summary-backdrop');
    const body = document.getElementById('review-summary-body');
    const pending = notes.filter(n => n.status !== 'done');
    const done = notes.filter(n => n.status === 'done');

    if (notes.length === 0) {
      body.innerHTML = '<div class="review-summary-empty">No review notes yet.</div>';
    } else {
      let html = '';
      PAGES.forEach(pg => {
        const pgNotes = notes.filter(n => n.page === pg.id);
        if (pgNotes.length === 0) return;
        html += `<div class="review-summary-group"><h4>${esc(pg.name)}</h4>`;
        pgNotes.forEach(n => {
          html += `<div class="review-summary-note ${n.status === 'done' ? 'done' : ''}">${esc(n.note)}</div>`;
        });
        html += '</div>';
      });
      const counts = `${pending.length} pending, ${done.length} done`;
      html = `<div style="font-size:12px;color:var(--text-secondary);margin-bottom:12px">${counts}</div>` + html;
      body.innerHTML = html;
    }
    backdrop.style.display = '';
  }

  function closeSummary() {
    document.getElementById('review-summary-backdrop').style.display = 'none';
  }

  function exportSummary() {
    let text = 'REVIEW NOTES\n' + '='.repeat(40) + '\n\n';
    PAGES.forEach(pg => {
      const pgNotes = notes.filter(n => n.page === pg.id);
      if (pgNotes.length === 0) return;
      text += `## ${pg.name}\n`;
      pgNotes.forEach(n => {
        const status = n.status === 'done' ? '[DONE]' : '[PENDING]';
        text += `  ${status} ${n.note}\n`;
      });
      text += '\n';
    });
    const pending = notes.filter(n => n.status !== 'done').length;
    text += `---\nTotal: ${notes.length} notes (${pending} pending)\n`;

    const blob = new Blob([text], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'review-notes.txt';
    a.click();
    URL.revokeObjectURL(url);
    Toast.show('Exported review notes', 'success');
  }

  function init() {
    document.getElementById('review-fab').addEventListener('click', open);
    document.getElementById('review-close').addEventListener('click', close);
    document.getElementById('review-add-btn').addEventListener('click', addNote);
    document.getElementById('review-prev').addEventListener('click', prev);
    document.getElementById('review-next').addEventListener('click', next);
    document.getElementById('review-summary-btn').addEventListener('click', showSummary);
    document.getElementById('review-summary-close').addEventListener('click', closeSummary);
    document.getElementById('review-summary-close2').addEventListener('click', closeSummary);
    document.getElementById('review-export-btn').addEventListener('click', exportSummary);
    document.getElementById('review-summary-backdrop').addEventListener('click', e => {
      if (e.target === e.currentTarget) closeSummary();
    });

    // Enter key in textarea adds note
    document.getElementById('review-note-input').addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        addNote();
      }
    });
  }

  return { init, open, close, toggle, resolveNote, deleteNote, isOpen: () => isOpen };
})();

// ══════════════════════════════════════════════════════════════════════════════
//  INIT
// ══════════════════════════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {
  BugReporter.init();
  DarkMode.init();
  Sidebar.init();
  ChatPanel.init();
  CmdPalette.init();
  Shortcuts.init();
  PdfViewer.init();
  SidebarTools.init();
  ReviewMode.init();

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

  // Address validation on blur
  const addrInput = $('#inp-address');
  if (addrInput) {
    let addrTimeout;
    addrInput.addEventListener('input', () => {
      clearTimeout(addrTimeout);
      const v = addrInput.value.trim();
      if (v.length > 10) {
        addrTimeout = setTimeout(() => validateAddress(v), 600);
      } else {
        const valEl = $('#address-validation');
        if (valEl) valEl.innerHTML = '';
      }
    });
  }

  // Home button
  const homeBtn = $('#btn-home');
  if (homeBtn) homeBtn.addEventListener('click', showHome);

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

let allTxns = [];

async function loadTxns() {
  const txns = await get('/api/txns');
  if (txns._error) return;
  allTxns = txns;

  // Fetch dashboard data for urgency indicators
  const dash = await get('/api/dashboard');
  const healthMap = {};
  if (!dash._error) {
    dash.forEach(d => { healthMap[d.id] = d; });
  }

  // Store health data on txns
  txns.forEach(t => { t._health = healthMap[t.id] || {}; });

  // If we're on the home view, render it
  const homeView = $('#home-view');
  if (homeView && homeView.style.display !== 'none') {
    renderHome(txns);
  }
}

function renderHome(txns) {
  const grid = $('#home-grid');
  const homeEmpty = $('#home-empty');
  if (!grid) return;

  if (!txns || txns.length === 0) {
    grid.innerHTML = '';
    if (homeEmpty) grid.appendChild(homeEmpty);
    homeEmpty.style.display = '';
    return;
  }

  if (homeEmpty) homeEmpty.style.display = 'none';

  grid.innerHTML = txns.map(t => {
    const ds = t.doc_stats || {};
    const docPct = ds.total ? Math.round((ds.received / ds.total) * 100) : 0;
    const gatePct = t.gate_count ? Math.round((t.gates_verified / t.gate_count) * 100) : 0;
    const h = t._health || {};
    const health = h.health || 'green';
    const urgentCount = (h.overdue || 0) + (h.soon || 0);
    const nextDl = (h.urgent_deadlines || [])[0];

    return `
      <div class="txn-card" data-id="${t.id}">
        <div class="txn-card-top">
          <span class="health-dot health-${health}" title="${health === 'red' ? 'Overdue items' : health === 'yellow' ? 'Items due soon' : 'On track'}"></span>
          <div class="txn-card-address">${esc(t.address)}</div>
        </div>
        ${urgentCount > 0 ? `<div class="txn-card-urgency"><span class="urgency-badge urgency-${health}">${urgentCount}</span></div>` : ''}
        <div class="txn-card-meta">
          <span class="type-badge ${t.txn_type}">${t.txn_type}</span>
          <span class="type-badge ${t.party_role}">${t.party_role}</span>
          ${t.brokerage ? `<span class="type-badge" style="background:var(--border);color:var(--text-secondary)">${esc(t.brokerage.replace(/_/g,' '))}</span>` : ''}
        </div>
        <div class="txn-card-phase">${formatPhase(t.phase)}</div>
        <div class="txn-card-bars">
          <div class="txn-card-bar">
            <div class="txn-card-bar-label"><span>Docs</span><span>${docPct}%</span></div>
            <div class="txn-card-bar-track"><div class="txn-card-bar-fill docs" style="width:${docPct}%"></div></div>
          </div>
          <div class="txn-card-bar">
            <div class="txn-card-bar-label"><span>Gates</span><span>${gatePct}%</span></div>
            <div class="txn-card-bar-track"><div class="txn-card-bar-fill gates" style="width:${gatePct}%"></div></div>
          </div>
        </div>
        ${nextDl ? `<div class="txn-card-deadline">${esc(nextDl.name)}: ${_formatCountdown(nextDl.days_left)}</div>` : ''}
        ${h.sig_total ? `<div class="txn-card-sigs"><span class="sig-icon">${h.sig_signed === h.sig_total ? '\u2705' : '\u270D\uFE0F'}</span> ${h.sig_signed || 0}/${h.sig_total} signed${h.sig_pending ? `, ${h.sig_pending} pending` : ''}</div>` : ''}
      </div>`;
  }).join('');

  // Click to select transaction
  $$('.txn-card').forEach(el => {
    el.addEventListener('click', () => selectTxn(el.dataset.id));
  });
}

function showHome() {
  currentTxn = null;
  const homeView = $('#home-view');
  const detail = $('#txn-detail');
  if (homeView) homeView.style.display = '';
  if (detail) detail.style.display = 'none';
  renderHome(allTxns);
  updateSidebarTimeline(null);
  SidebarTools.updateState(false);
}

async function selectTxn(id) {
  currentTxn = id;

  // Switch from home to detail view
  const homeView = $('#home-view');
  const detail = $('#txn-detail');
  if (homeView) homeView.style.display = 'none';
  if (detail) detail.style.display = '';

  const t = await get(`/api/txns/${id}`);
  if (t._error) return;
  txnCache[id] = t;

  // Load phases if not cached
  const txnType = t.txn_type || 'sale';
  if (!phasesCache[txnType]) {
    phasesCache[txnType] = await get(`/api/phases/${txnType}`);
  }

  renderHeader(t);
  switchTab(currentTab);

  // Update sidebar timeline + enable tools
  updateSidebarTimeline(t, phasesCache[txnType]);
  SidebarTools.updateState(true);

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
    parties: renderParties,
    compliance: renderGates,
    activity: renderAudit,
  };
  (render[tab] || render.overview)();
}

// ── Dashboard Tab ───────────────────────────────────────────────────────────

async function renderOverview() {
  const t = txnCache[currentTxn];
  if (!t) return;
  const el = $('#tab-content');

  // Fetch data — use cache for fast tab switches, fast sig-counts for dashboard
  const [docsAll, dashAll, audit, notesRes, contData, sigCounts] = await Promise.all([
    getCached(`/api/txns/${currentTxn}/docs`),
    getCached('/api/dashboard'),
    getCached(`/api/txns/${currentTxn}/audit`),
    getCached(`/api/txns/${currentTxn}/notes`),
    getCached(`/api/txns/${currentTxn}/contingencies`),
    getCached(`/api/txns/${currentTxn}/sig-counts`),
  ]);
  const docs = (!docsAll._error && Array.isArray(docsAll)) ? docsAll : [];
  const dash = (!dashAll._error ? dashAll : []).find(d => d.id === currentTxn) || {};
  const auditRows = (!audit._error ? audit : []).slice(0, 8);
  const notes = (!notesRes._error ? notesRes.notes : '') || '';
  const contItems = (!contData._error && contData.items) ? contData.items : [];
  const sc = (!sigCounts._error ? sigCounts : {});

  const ds = t.doc_stats || {};
  const docsReceived = (ds.received || 0);
  const docsTotal = (ds.total || 0);

  // Critical docs needed to unlock dashboard mode
  const CRITICAL_DOCS = [
    { code: 'RPA', name: 'Residential Purchase Agreement', why: 'Dates, price, contingencies' },
    { code: 'AD',  name: 'Agency Disclosure', why: 'Required for all parties' },
    { code: 'TDS', name: 'Transfer Disclosure Statement', why: 'Seller disclosures' },
    { code: 'SPQ', name: 'Seller Property Questionnaire', why: 'Property condition details' },
    { code: 'AVID', name: 'Agent Visual Inspection', why: 'Agent disclosure obligation' },
    { code: 'NHD', name: 'Natural Hazard Disclosure', why: 'Hazard zone status' },
  ];
  // Build doc status lookup
  const docByCode = {};
  docs.forEach(d => { docByCode[d.code] = d; });

  // RPA is the gatekeeper — without it, dates/deadlines/contingencies can't be set
  const rpaDoc = docByCode['RPA'];
  const rpaReceived = rpaDoc && (rpaDoc.status === 'received' || rpaDoc.status === 'verified');
  const dashboardMode = rpaReceived;

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

  // ══════════════════════════════════════════════════════════════════════════
  //  SETUP MODE — Before RPA is received
  // ══════════════════════════════════════════════════════════════════════════
  if (!dashboardMode) {
    // Count critical docs received
    const critReceived = CRITICAL_DOCS.filter(cd => {
      const d = docByCode[cd.code];
      return d && (d.status === 'received' || d.status === 'verified');
    }).length;

    html += `<div class="setup-mode">
      <div class="setup-hero" id="overview-drop-zone">
        <div class="setup-hero-icon">
          <svg width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="12" y1="18" x2="12" y2="12"/><polyline points="9 15 12 12 15 15"/></svg>
        </div>
        <h2>Upload Your RPA to Get Started</h2>
        <p class="setup-hero-sub">The purchase agreement contains the dates, price, and contingency periods needed to build your transaction timeline.</p>
        <button class="btn btn-primary btn-lg" onclick="triggerUpload()">Upload Contract PDF</button>
        <p class="setup-hero-hint">Supports combined PDF packets — we'll auto-split and categorize multiple forms</p>
      </div>

      <div class="card setup-checklist-card">
        <div class="card-title">Documents Needed to Start</div>
        <p class="setup-checklist-sub">Upload these to unlock deadline tracking, contingency management, and compliance gates.</p>
        <div class="setup-checklist">
          ${CRITICAL_DOCS.map(cd => {
            const d = docByCode[cd.code];
            const received = d && (d.status === 'received' || d.status === 'verified');
            const isRPA = cd.code === 'RPA';
            return `<div class="setup-check-item ${received ? 'check-done' : ''} ${isRPA ? 'check-rpa' : ''}">
              <span class="setup-check-icon">${received ? '\u2705' : (isRPA ? '\u26A0\uFE0F' : '\u25CB')}</span>
              <div class="setup-check-info">
                <span class="setup-check-code">${esc(cd.code)}</span>
                <span class="setup-check-name">${esc(cd.name)}</span>
                <span class="setup-check-why">${esc(cd.why)}</span>
              </div>
              <div class="setup-check-action">
                ${received
                  ? '<span class="badge badge-verified">Received</span>'
                  : `<button class="btn btn-ghost btn-sm" onclick="triggerUploadFor('${esc(cd.code)}')">Upload</button>`}
              </div>
            </div>`;
          }).join('')}
        </div>
        <div class="setup-progress">
          <div class="setup-progress-bar">
            <div class="setup-progress-fill" style="width:${Math.round((critReceived / CRITICAL_DOCS.length) * 100)}%"></div>
          </div>
          <span class="setup-progress-text">${critReceived} of ${CRITICAL_DOCS.length} critical documents received</span>
        </div>
      </div>`;

    // Still show notes and property flags in setup mode
    html += `<div class="card">
      <div class="card-title">Transaction Notes</div>
      <textarea class="notes-area" id="txn-notes" placeholder="Add notes about this transaction...">${esc(notes)}</textarea>
      <button class="btn btn-primary btn-sm" onclick="saveNotes()" style="margin-top:6px">Save Notes</button>
    </div>`;

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

    html += `<div class="delete-section">
      <button class="btn btn-danger btn-sm" onclick="deleteTxn()">Delete Transaction</button>
    </div>`;

    html += `</div>`; // close .setup-mode

    el.innerHTML = html;

    // Attach drag & drop to setup hero
    const dropZone = document.getElementById('overview-drop-zone');
    if (dropZone) {
      dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
      dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
      dropZone.addEventListener('drop', async e => {
        e.preventDefault();
        dropZone.classList.remove('drag-over');
        const file = e.dataTransfer.files[0];
        if (file && file.type === 'application/pdf') {
          await _uploadFile(file);
          renderOverview();
        } else {
          Toast.show('Please drop a PDF file', 'warning');
        }
      });
    }
    return;
  }

  // ══════════════════════════════════════════════════════════════════════════
  //  DASHBOARD MODE — RPA is in, show the full command center
  // ══════════════════════════════════════════════════════════════════════════

  // Fetch gates for compliance section
  const gatesData = await getCached(`/api/txns/${currentTxn}/gates`);
  const allGates = (!gatesData._error && Array.isArray(gatesData)) ? gatesData : [];
  const currentPhaseGates = allGates.filter(g => g.phase === t.phase);
  const blockingGates = currentPhaseGates.filter(g => g.status !== 'verified' && g.type === 'HARD_GATE');

  // ── Attention Required ──
  const urgentDl = dash.urgent_deadlines || [];
  const urgentCont = dash.urgent_contingencies || [];
  const hasUrgent = urgentDl.length > 0 || urgentCont.length > 0 || blockingGates.length > 0;

  if (hasUrgent) {
    html += `<div class="attention-card">
      <div class="card-title">Attention Required</div>
      <div class="attention-items">`;
    urgentDl.forEach(d => {
      const cls = d.days_left < 0 ? 'overdue' : d.days_left <= 3 ? 'urgent' : 'soon';
      html += `<div class="attention-item attention-${cls} ${d.days_left >= 0 && d.days_left <= 3 ? 'pulse-warn' : ''}">
        <span class="attention-icon">${d.days_left < 0 ? '\u26A0' : '\u23F3'}</span>
        <span>${esc(d.name)}</span>
        <span class="attention-days">${_formatCountdown(d.days_left)}</span>
      </div>`;
    });
    urgentCont.forEach(d => {
      const cls = d.days_left < 0 ? 'overdue' : d.days_left <= 3 ? 'urgent' : 'soon';
      html += `<div class="attention-item attention-${cls} ${d.days_left >= 0 && d.days_left <= 3 ? 'pulse-warn' : ''}">
        <span class="attention-icon">\u25CB</span>
        <span>${esc(d.name)} contingency</span>
        <span class="attention-days">${_formatCountdown(d.days_left)}</span>
      </div>`;
    });
    if (blockingGates.length > 0) {
      html += `<div class="attention-item attention-soon" onclick="switchTab('compliance')" style="cursor:pointer">
        <span class="attention-icon">\u2610</span>
        <span>${blockingGates.length} compliance item${blockingGates.length > 1 ? 's' : ''} blocking phase advance</span>
      </div>`;
    }
    html += `</div></div>`;
  }

  // ── Next Contingency Countdown ──
  const activeCont = contItems.filter(c => c.status === 'active');
  const removedCont = contItems.filter(c => c.status !== 'active');
  const nextCont = activeCont
    .filter(c => c.deadline_date)
    .sort((a, b) => a.deadline_date.localeCompare(b.deadline_date))[0];

  if (nextCont) {
    const daysLeft = Math.ceil((new Date(nextCont.deadline_date) - new Date()) / 86400000);
    const isUrgent = daysLeft <= 3;
    const countdownText = daysLeft <= 0 ? 'EXPIRED' : daysLeft * 24 <= 72 ? `${daysLeft * 24}h` : `${daysLeft}d`;
    html += `<div class="card contingency-countdown ${isUrgent ? 'countdown-urgent' : ''} ${isUrgent && daysLeft > 0 ? 'pulse-warn' : ''}">
      <div class="countdown-row">
        <div>
          <div class="card-title" style="margin-bottom:2px">Next Contingency</div>
          <div class="countdown-name">${esc(nextCont.name)}</div>
          <div class="countdown-date">${esc(nextCont.deadline_date)}</div>
        </div>
        <div class="countdown-value ${daysLeft <= 0 ? 'expired' : daysLeft <= 3 ? 'urgent' : ''}">${countdownText}</div>
      </div>
    </div>`;
  }

  // ── Stats ──
  const compliancePct = t.gate_count ? Math.round((t.gates_verified / t.gate_count) * 100) : 0;
  const docPct = docsTotal ? Math.round((docsReceived / docsTotal) * 100) : 0;
  const sigTotal = sc.total || 0;
  const sigSigned = sc.filled || 0;
  const sigPending = sc.pending || 0;
  const sigPct = sigTotal ? Math.round((sigSigned / sigTotal) * 100) : 0;
  const contTotal = activeCont.length + removedCont.length;
  const contPct = contTotal ? Math.round((removedCont.length / contTotal) * 100) : 0;

  const _ring = (pct, color, val, label, tab) => `
    <div class="stat-card clickable" onclick="switchTab('${tab}')">
      <div class="stat-ring" style="--pct:${pct}; --ring-color:${color}">
        <svg viewBox="0 0 36 36"><circle cx="18" cy="18" r="15.9" class="ring-bg"/>
        <circle cx="18" cy="18" r="15.9" class="ring-fg" style="stroke-dasharray:${pct} 100"/></svg>
        <span class="ring-label">${pct}%</span>
      </div>
      <div class="stat-value">${val}</div>
      <div class="stat-label">${label}</div>
    </div>`;

  html += `<div class="card-grid card-grid-4">
    ${_ring(compliancePct, 'var(--accent)', `${t.gates_verified}/${t.gate_count}`, 'Compliance', 'gates')}
    ${_ring(docPct, 'var(--green)', `${docsReceived}/${docsTotal}`, 'Documents', 'docs')}
    ${_ring(sigPct, sigPending > 0 ? 'var(--orange)' : 'var(--green)', `${sigSigned}/${sigTotal}`, 'Signatures', 'signatures')}
    ${_ring(contPct, contTotal ? 'var(--accent)' : 'var(--green)', `${removedCont.length}/${contTotal}`, 'Contingencies', 'contingencies')}
  </div>`;

  // ── Transaction Schedule ──
  const scheduleData = await getCached(`/api/txns/${currentTxn}/closing-plan`);
  const schedule = !scheduleData._error ? scheduleData : {};
  const daysToClose = schedule.days_to_close;
  const pendingSigs = (schedule.pending_signatures || []).length;
  const activeContCount = (schedule.active_contingencies || []).length;
  const nextContDeadline = (schedule.active_contingencies || []).find(c => c.deadline_date);

  html += `<div class="card schedule-card">
    <div class="card-title">Transaction Schedule</div>
    <div class="schedule-stats">
      <div class="schedule-stat">
        <span class="schedule-stat-value ${daysToClose !== null && daysToClose <= 7 ? 'urgent' : ''}">${daysToClose !== null ? daysToClose : '--'}</span>
        <span class="schedule-stat-label">Days to Close</span>
        ${schedule.coe_date ? `<span class="schedule-stat-sub">${esc(schedule.coe_date)}</span>` : ''}
      </div>
      <div class="schedule-stat">
        <span class="schedule-stat-value ${activeContCount > 0 ? 'active' : ''}">${activeContCount}</span>
        <span class="schedule-stat-label">Active Contingencies</span>
        ${nextContDeadline ? `<span class="schedule-stat-sub">Next: ${esc(nextContDeadline.deadline_date)}</span>` : ''}
      </div>
      <div class="schedule-stat">
        <span class="schedule-stat-value ${pendingSigs > 0 ? 'pending' : ''}">${pendingSigs}</span>
        <span class="schedule-stat-label">Pending Signatures</span>
      </div>
    </div>
  </div>`;

  // ── Agenda — unified timeline of deadlines + contingency expirations + COE ──
  const dlData = await getCached(`/api/txns/${currentTxn}/deadlines`);
  const dls = (!dlData._error && Array.isArray(dlData)) ? dlData : [];
  const agendaItems = [];

  // Add deadlines
  dls.forEach(d => {
    if (d.due) agendaItems.push({ type: 'deadline', name: d.name, date: d.due, days: d.days_remaining, status: d.status });
  });

  // Add contingency expirations
  contItems.filter(c => c.status === 'active' && c.deadline_date).forEach(c => {
    agendaItems.push({ type: 'contingency', name: c.name, date: c.deadline_date, days: c.days_remaining, status: c.status });
  });

  // Add COE as milestone
  const txnData = t.data || {};
  const coeDate = (txnData.dates || {}).close_of_escrow;
  if (coeDate) {
    const coeDays = Math.ceil((new Date(coeDate) - new Date()) / 86400000);
    agendaItems.push({ type: 'milestone', name: 'Close of Escrow', date: coeDate, days: coeDays, status: 'pending' });
  }

  agendaItems.sort((a, b) => (a.date || '').localeCompare(b.date || ''));

  if (agendaItems.length > 0) {
    const _urgCls = (days) => {
      if (days === null || days === undefined) return '';
      if (days < 0) return 'overdue';
      if (days <= 1) return 'urgent';
      if (days <= 5) return 'soon';
      return 'ok';
    };
    const typeIcons = { deadline: '\u23F3', contingency: '\u25CB', milestone: '\u2B50' };
    html += `<div class="card">
      <div class="card-title">Agenda</div>
      <div class="agenda-list">
        ${agendaItems.slice(0, 15).map(a => {
          const uc = _urgCls(a.days);
          return `<div class="agenda-item ${uc}">
            <span class="agenda-icon">${typeIcons[a.type] || '\u23F3'}</span>
            <span class="agenda-date">${esc(a.date)}</span>
            <span class="agenda-name">${esc(a.name)}</span>
            <span class="agenda-days days-pill ${uc}">${a.days !== null && a.days !== undefined ? a.days + 'd' : '--'}</span>
          </div>`;
        }).join('')}
      </div>
    </div>`;
  }

  // ── Quick Actions (collapsible) ──
  const actionsCollapsed = localStorage.getItem('quickActionsCollapsed') === 'true';
  html += `<div class="card quick-actions ${actionsCollapsed ? 'collapsed' : ''}" id="quick-actions-card">
    <div class="card-title-row">
      <span class="card-title">Quick Actions</span>
      <button class="collapse-toggle" onclick="toggleActionsCollapsed()" title="${actionsCollapsed ? 'Expand quick actions' : 'Collapse quick actions'}">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="${actionsCollapsed ? '6 9 12 15 18 9' : '18 15 12 9 6 15'}"/>
        </svg>
      </button>
    </div>
    <div class="quick-actions-content">
      <div class="quick-actions-grid">
        <button class="quick-action-btn" onclick="triggerUpload()" title="Upload a PDF document">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
          Upload PDF
        </button>
        <button class="quick-action-btn" onclick="switchTab('signatures')" title="View signature status">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>
          Signatures
        </button>
        <button class="quick-action-btn" onclick="switchTab('contingencies')" title="Manage contingency periods">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
          Contingencies
        </button>
        <button class="quick-action-btn" onclick="switchTab('parties')" title="View transaction parties">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
          Parties
        </button>
        <button class="quick-action-btn" onclick="showClosingPlan()" title="View closing plan checklist">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>
          Closing Plan
        </button>
      </div>
    </div>
  </div>`;

  // ── Blocking Compliance Items ──
  if (blockingGates.length > 0) {
    html += `<div class="card">
      <div class="card-title">Blocking Compliance Items <span class="badge badge-required" style="margin-left:6px">${blockingGates.length}</span></div>
      <p style="font-size:12px;color:var(--text-secondary);margin:0 0 10px">These must be verified before advancing to the next phase.</p>
      <div class="blocking-gates-list">
        ${blockingGates.slice(0, 8).map(g => `<div class="blocking-gate-item">
          <span class="blocking-gate-icon">\u2610</span>
          <span class="blocking-gate-name">${esc(g.name || g.gid)}</span>
          <button class="btn btn-primary btn-sm" onclick="switchTab('compliance')">Review</button>
        </div>`).join('')}
        ${blockingGates.length > 8 ? `<div style="font-size:12px;color:var(--text-secondary);padding:4px 0">+ ${blockingGates.length - 8} more</div>` : ''}
      </div>
    </div>`;
  }

  // ── Signature Tracker ──
  if (sigTotal > 0) {
    const sentEnv = dash.sig_pending || 0;
    html += `<div class="card">
      <div class="card-title">Signature Tracking</div>
      <div class="sig-tracker">
        <div class="sig-tracker-row">
          <span class="sig-tracker-dot" style="background:var(--green)"></span>
          <span><strong>${sigSigned}</strong> signed</span>
          <span class="sig-tracker-bar"><span class="sig-tracker-bar-fill" style="width:${sigPct}%;background:var(--green)"></span></span>
        </div>
        <div class="sig-tracker-row">
          <span class="sig-tracker-dot" style="background:var(--orange)"></span>
          <span><strong>${sigPending}</strong> awaiting signature</span>
          <span class="sig-tracker-bar"><span class="sig-tracker-bar-fill" style="width:${sigTotal ? Math.round((sigPending/sigTotal)*100) : 0}%;background:var(--orange)"></span></span>
        </div>
        ${sentEnv > 0 ? `<div class="sig-tracker-row">
          <span class="sig-tracker-dot" style="background:var(--accent)"></span>
          <span><strong>${sentEnv}</strong> sent via DocuSign</span>
        </div>` : ''}
      </div>
      <button class="btn btn-ghost btn-sm" onclick="switchTab('signatures')" style="margin-top:6px">View All Signatures</button>
    </div>`;
  }

  // ── Critical Docs Status (inline in dashboard) ──
  const CRITICAL_CODES = ['RPA', 'AD', 'TDS', 'SPQ', 'AVID', 'NHD', 'SBSA', 'EMD_REC'];
  const critMissing = CRITICAL_CODES.filter(c => { const d = docByCode[c]; return !d || d.status === 'required'; });
  if (critMissing.length > 0) {
    html += `<div class="card dash-missing-docs">
      <div class="card-title">Missing Critical Documents</div>
      <div class="dash-missing-list">
        ${critMissing.map(code => {
          const d = docByCode[code];
          return `<div class="dash-missing-item">
            <span class="dash-missing-icon">\u26D4</span>
            <span class="dash-missing-code">${esc(code)}</span>
            <span class="dash-missing-name clickable-doc" onclick="openDocPdf('${esc(code)}')">${d ? esc(d.name) : esc(code)}</span>
            <button class="btn btn-ghost btn-sm" onclick="triggerUploadFor('${esc(code)}')">Upload</button>
          </div>`;
        }).join('')}
      </div>
    </div>`;
  }

  // ── Notes ──
  html += `<div class="card">
    <div class="card-title">Transaction Notes</div>
    <textarea class="notes-area" id="txn-notes" placeholder="Add notes about this transaction...">${esc(notes)}</textarea>
    <button class="btn btn-primary btn-sm" onclick="saveNotes()" style="margin-top:6px">Save Notes</button>
  </div>`;

  // ── Recent Activity ──
  if (auditRows.length > 0) {
    html += `<div class="card">
      <div class="card-title">Recent Activity</div>
      <div class="activity-feed">
        ${auditRows.map(r => `
          <div class="activity-item">
            <span class="activity-action">${esc(r.action)}</span>
            <span class="activity-detail">${esc(r.detail || '')}</span>
            <span class="activity-time">${esc(r.ts || '')}</span>
          </div>`).join('')}
      </div>
      <button class="btn btn-ghost btn-sm" onclick="switchTab('activity')" style="margin-top:4px">View Full Audit</button>
    </div>`;
  }

  // ── Parties ──
  const parties = (t.data || {}).parties || {};
  if (Object.values(parties).some(v => v)) {
    html += `<div class="card">
      <div class="card-title">Parties</div>
      <div class="kv-grid">
        ${Object.entries(parties).map(([k, v]) => v ? `<span class="kv-key">${esc(k)}</span><span class="kv-val">${esc(v)}</span>` : '').join('')}
      </div></div>`;
  }

  // ── Financial ──
  const fin = (t.data || {}).financial || {};
  if (Object.values(fin).some(v => v)) {
    html += `<div class="card">
      <div class="card-title">Financial</div>
      <div class="kv-grid">
        ${Object.entries(fin).map(([k, v]) => v ? `<span class="kv-key">${esc(k)}</span><span class="kv-val">${typeof v === 'number' ? '$' + v.toLocaleString() : esc(String(v))}</span>` : '').join('')}
      </div></div>`;
  }

  // ── Property flags ──
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

async function saveNotes() {
  const notes = document.getElementById('txn-notes')?.value || '';
  const res = await post(`/api/txns/${currentTxn}/notes`, { notes });
  if (res._error) return;
  Toast.show('Notes saved', 'success');
}

async function advancePhase() {
  const res = await api(`/api/txns/${currentTxn}/advance`, { method: 'POST', body: {}, _suppressToast: true });
  if (res.ok) {
    Toast.show('Phase advanced successfully', 'success');
    await selectTxn(currentTxn);
    loadTxns();
  } else if (res.blockers) {
    showAdvanceBlockersModal(res.blockers);
  } else if (res.blocking && res.blocking.length > 0) {
    Toast.show('Cannot advance — ' + res.blocking.length + ' blocking gate(s): ' + res.blocking.slice(0, 3).join(', '), 'warning');
  } else {
    Toast.show('Cannot advance phase', 'warning');
  }
}

function toggleActionsCollapsed() {
  const card = document.getElementById('quick-actions-card');
  if (!card) return;
  const isCollapsed = card.classList.toggle('collapsed');
  localStorage.setItem('quickActionsCollapsed', isCollapsed);
  // Update toggle icon
  const svg = card.querySelector('.collapse-toggle svg polyline');
  if (svg) {
    svg.setAttribute('points', isCollapsed ? '6 9 12 15 18 9' : '18 15 12 9 6 15');
  }
  const btn = card.querySelector('.collapse-toggle');
  if (btn) {
    btn.title = isCollapsed ? 'Expand quick actions' : 'Collapse quick actions';
  }
}

async function showClosingPlan() {
  if (!currentTxn) return;
  const data = await get(`/api/txns/${currentTxn}/closing-plan`);
  if (data._error) return;

  const t = txnCache[currentTxn] || {};
  const modal = document.getElementById('closing-plan-modal');
  const body = document.getElementById('closing-plan-body');
  if (!modal || !body) return;

  let html = '';

  // Days to close countdown
  const daysToClose = data.days_to_close;
  const countdownClass = daysToClose !== null && daysToClose <= 7 ? 'urgent' : (daysToClose !== null && daysToClose <= 14 ? 'soon' : '');
  html += `<div class="closing-countdown ${countdownClass}">
    <div class="closing-countdown-value">${daysToClose !== null ? daysToClose : '--'}</div>
    <div class="closing-countdown-label">Days to Close${data.coe_date ? ` (${esc(data.coe_date)})` : ''}</div>
  </div>`;

  // Active Contingencies
  const conts = data.active_contingencies || [];
  if (conts.length > 0) {
    html += `<div class="closing-section">
      <h4>Active Contingencies (${conts.length})</h4>
      <ul class="closing-list">
        ${conts.map(c => `<li class="closing-item cont">
          <span class="closing-item-icon">\u25CB</span>
          <span class="closing-item-name">${esc(c.name)}</span>
          ${c.deadline_date ? `<span class="closing-item-date">${esc(c.deadline_date)}</span>` : ''}
        </li>`).join('')}
      </ul>
    </div>`;
  }

  // Pending Documents
  const docs = data.pending_docs || [];
  if (docs.length > 0) {
    const byUrgency = { required: [], received: [] };
    docs.forEach(d => {
      if (d.status === 'required') byUrgency.required.push(d);
      else byUrgency.received.push(d);
    });
    html += `<div class="closing-section">
      <h4>Pending Documents (${docs.length})</h4>`;
    if (byUrgency.required.length > 0) {
      html += `<div class="closing-subsection"><span class="closing-subsection-label">Not Yet Received</span>
        <ul class="closing-list">${byUrgency.required.slice(0, 10).map(d => `<li class="closing-item doc required">
          <span class="closing-item-icon">\u26D4</span>
          <span class="closing-item-code">${esc(d.code)}</span>
          <span class="closing-item-name">${esc(d.name)}</span>
        </li>`).join('')}</ul>
        ${byUrgency.required.length > 10 ? `<div class="closing-more">+ ${byUrgency.required.length - 10} more</div>` : ''}
      </div>`;
    }
    if (byUrgency.received.length > 0) {
      html += `<div class="closing-subsection"><span class="closing-subsection-label">Awaiting Verification</span>
        <ul class="closing-list">${byUrgency.received.slice(0, 5).map(d => `<li class="closing-item doc received">
          <span class="closing-item-icon">\u23F3</span>
          <span class="closing-item-code">${esc(d.code)}</span>
          <span class="closing-item-name">${esc(d.name)}</span>
        </li>`).join('')}</ul>
        ${byUrgency.received.length > 5 ? `<div class="closing-more">+ ${byUrgency.received.length - 5} more</div>` : ''}
      </div>`;
    }
    html += `</div>`;
  }

  // Pending Signatures
  const sigs = data.pending_signatures || [];
  if (sigs.length > 0) {
    html += `<div class="closing-section">
      <h4>Pending Signatures (${sigs.length})</h4>
      <ul class="closing-list">
        ${sigs.slice(0, 8).map(s => `<li class="closing-item sig">
          <span class="closing-item-icon">\u270D</span>
          <span class="closing-item-name">${esc(s.field_name)}</span>
          <span class="closing-item-sub">${esc(s.doc_code)} p.${s.page}</span>
        </li>`).join('')}
      </ul>
      ${sigs.length > 8 ? `<div class="closing-more">+ ${sigs.length - 8} more</div>` : ''}
    </div>`;
  }

  // Blocking Compliance Items
  const gates = data.blocking_gates || [];
  if (gates.length > 0) {
    html += `<div class="closing-section">
      <h4>Compliance Items (${gates.length})</h4>
      <ul class="closing-list">
        ${gates.slice(0, 6).map(g => `<li class="closing-item gate">
          <span class="closing-item-icon">\u2610</span>
          <span class="closing-item-name">${esc(g.name || g.gid)}</span>
        </li>`).join('')}
      </ul>
      ${gates.length > 6 ? `<div class="closing-more">+ ${gates.length - 6} more</div>` : ''}
    </div>`;
  }

  if (!conts.length && !docs.length && !sigs.length && !gates.length) {
    html += `<div class="closing-empty">\u2705 All clear! Ready to close.</div>`;
  }

  body.innerHTML = html;
  modal.style.display = '';
}

function closeClosingPlan() {
  const modal = document.getElementById('closing-plan-modal');
  if (modal) modal.style.display = 'none';
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

// Trigger file upload from anywhere (overview CTA, docs tab, etc.)
function triggerUpload() {
  if (!currentTxn) { Toast.show('Select a transaction first', 'warning'); return; }
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.accept = '.pdf';
  inp.style.display = 'none';
  document.body.appendChild(inp);
  inp.addEventListener('change', async () => {
    if (inp.files[0]) await _uploadFile(inp.files[0]);
    inp.remove();
  });
  inp.click();
}

async function _uploadFile(file) {
  Toast.show(`Uploading ${file.name}...`, 'info');
  const form = new FormData();
  form.append('file', file);
  try {
    const resp = await fetch(`/api/txns/${currentTxn}/upload`, { method: 'POST', body: form });
    const data = await resp.json();
    if (!resp.ok) {
      Toast.show(data.error || 'Upload failed', 'error');
      return;
    }
    let msg = `Uploaded ${data.filename}`;
    if (data.split_docs && data.split_docs.length > 0) {
      const codes = data.split_docs.map(d => d.code).join(', ');
      msg = `Split into ${data.split_docs.length} documents: ${codes}`;
      const matched = data.split_docs.filter(d => d.matched).length;
      if (matched > 0) msg += ` (${matched} auto-filed)`;
    } else if (data.fields_detected) {
      msg += ` \u2014 ${data.fields_detected} fields detected`;
    }
    if (data.matched_doc && !data.split_docs?.length) msg += ` \u2014 matched to ${data.matched_doc}`;
    Toast.show(msg, 'success');
    // Show RPA extraction results
    if (data.rpa_extraction && data.rpa_extraction.deadlines_populated) {
      const ext = data.rpa_extraction;
      const prov = ext.acceptance_provisional ? ' (provisional)' : '';
      Toast.show(`RPA analyzed: acceptance ${ext.acceptance_date}${prov}, COE ${ext.coe_date}. Deadlines & contingencies populated.`, 'success');
      // Show waived contingencies
      if (ext.contingencies_waived) {
        const waived = Object.entries(ext.contingencies_waived)
          .map(([type, status]) => `${type} (${status})`)
          .join(', ');
        Toast.show(`Contingencies auto-detected as not required: ${waived}`, 'info');
      }
    }
    // Invalidate cache + refresh
    invalidateCache(`/api/txns/${currentTxn}`);
    invalidateCache('/api/dashboard');
    const t = await get(`/api/txns/${currentTxn}`);
    if (!t._error) { txnCache[currentTxn] = t; loadTxns(); }
    const tab = document.querySelector('.tab.active');
    if (tab) switchTab(tab.dataset.tab);
  } catch (e) {
    Toast.show('Upload failed: ' + e.message, 'error');
  }
}

async function bulkReceive() {
  if (!confirm('Mark all required documents as received?')) return;
  const res = await post(`/api/txns/${currentTxn}/docs/bulk-receive`);
  if (res._error) return;
  Toast.show(`${res.updated} documents marked received`, 'success');
  renderDocs();
  const t = await get(`/api/txns/${currentTxn}`);
  if (!t._error) { txnCache[currentTxn] = t; loadTxns(); }
}

async function bulkVerify() {
  if (!confirm('Mark all received documents as verified?')) return;
  const res = await post(`/api/txns/${currentTxn}/docs/bulk-verify`);
  if (res._error) return;
  Toast.show(`${res.updated} documents verified`, 'success');
  renderDocs();
  const t = await get(`/api/txns/${currentTxn}`);
  if (!t._error) { txnCache[currentTxn] = t; loadTxns(); }
}

// ══════════════════════════════════════════════════════════════════════════════
//  DOCUMENTS TAB (with filter bar)
// ══════════════════════════════════════════════════════════════════════════════

let _docsData = [];

async function renderDocs() {
  showSkeleton('table');
  const [docs, discRes, verifyData] = await Promise.all([
    get(`/api/txns/${currentTxn}/docs`),
    get(`/api/txns/${currentTxn}/disclosures`),
    get('/api/contracts'),
  ]);
  if (docs._error) return;
  _docsData = docs;
  _discData = (!discRes._error && discRes.items) ? discRes.items : [];
  _discSummary = (!discRes._error && discRes.summary) ? discRes.summary : {};

  // Cache for command palette
  if (txnCache[currentTxn]) txnCache[currentTxn]._docs = docs;

  const el = $('#tab-content');

  // Upload drop zone
  let html = `<div class="docs-upload-zone" id="docs-drop-zone">
    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
    <span>Drop PDF here or <button class="btn btn-primary btn-sm" onclick="triggerUpload()">Browse</button></span>
  </div>`;

  if (docs.length === 0) {
    html += `<div class="card" style="text-align:center;padding:32px">
      <p style="color:var(--text-secondary);margin-bottom:12px">No documents tracked yet. Select a brokerage on the transaction to auto-populate, or upload documents directly.</p>
    </div>`;
  } else {
    // Gather filter values
    const phases = [...new Set(docs.map(d => d.phase))];
    const statuses = [...new Set(docs.map(d => d.status))];

    html += `<div class="filter-bar" id="docs-filter">
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
      <span class="bulk-actions">
        ${stats.received ? `<button class="btn btn-accent btn-sm" onclick="startVerifyAllWorkflow()">Review & Verify All (${stats.received})</button>` : ''}
      </span>
    </div>`;

    html += '<div id="docs-table-area"></div>';
  }

  // ── Contract Verification (collapsible) ──
  const verifyItems = (!verifyData._error && verifyData.items) ? verifyData.items : [];
  const verifyOpen = verifyItems.length > 0;
  html += `<div class="collapsible-section" id="docs-verify-section">
    <button class="collapsible-toggle ${verifyOpen ? 'open' : ''}" onclick="toggleCollapsible(this)">
      <svg class="collapsible-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>
      <span>Contract Verification</span>
      <span class="collapsible-count">${verifyItems.length} contracts</span>
    </button>
    <div class="collapsible-content" ${verifyOpen ? '' : 'style="display:none"'}>
      <div style="display:flex;gap:8px;margin-bottom:10px">
        <button class="btn btn-primary btn-sm" onclick="verifyScan()">Scan Contracts</button>
      </div>
      <div id="docs-verify-list"></div>
    </div>
  </div>`;

  // ── Contract Review (collapsible) ──
  html += `<div class="collapsible-section" id="docs-review-section">
    <button class="collapsible-toggle" onclick="toggleCollapsible(this)">
      <svg class="collapsible-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>
      <span>Contract Review</span>
      <span class="collapsible-count" id="review-count">—</span>
    </button>
    <div class="collapsible-content" style="display:none">
      <p class="text-muted" style="margin-bottom:10px;font-size:13px">
        Clause-by-clause analysis against CA RPA playbook standards. Flags risk levels, clause interactions, and missing items.
      </p>
      <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap">
        <button class="btn btn-primary btn-sm" onclick="runContractReview()" id="btn-run-review">Review Uploaded Contract</button>
        <button class="btn btn-ghost btn-sm" onclick="loadContractReviews()">View Past Reviews</button>
      </div>
      <div id="contract-review-results"></div>
    </div>
  </div>`;

  // ── Disclosures (collapsible) ──
  const discOpen = _discData.length > 0;
  const ds = _discSummary;
  html += `<div class="collapsible-section" id="docs-disc-section">
    <button class="collapsible-toggle ${discOpen ? 'open' : ''}" onclick="toggleCollapsible(this)">
      <svg class="collapsible-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>
      <span>Disclosures</span>
      <span class="collapsible-count">${ds.total || 0} items</span>
    </button>
    <div class="collapsible-content" ${discOpen ? '' : 'style="display:none"'}>
      <div style="display:flex;gap:8px;margin-bottom:10px">
        <button class="btn btn-primary btn-sm" onclick="openDiscForm()">+ Add Disclosure</button>
      </div>
      <div class="disc-add-form card" id="disc-add-form" style="display:none">
        <div class="card-title">Add Disclosure</div>
        <div class="field-row">
          <label class="field-label">Type
            <select id="disc-type">${DISC_TYPES.map(d => `<option value="${d.value}">${d.label}</option>`).join('')}</select>
          </label>
          <label class="field-label">Due Date <input type="date" id="disc-due-date"></label>
        </div>
        <label class="field-label">Notes <textarea id="disc-notes" rows="2" placeholder="Optional notes..."></textarea></label>
        <div style="display:flex;gap:8px;margin-top:8px">
          <button class="btn btn-primary btn-sm" onclick="submitDisc()">Add</button>
          <button class="btn btn-ghost btn-sm" onclick="closeDiscForm()">Cancel</button>
        </div>
      </div>
      <div id="docs-disc-list"></div>
    </div>
  </div>`;

  // Hidden verify workflow area
  html += '<div id="verify-workflow" style="display:none"></div>';

  el.innerHTML = html;

  // Render doc table
  if (docs.length > 0) {
    await _ensurePdfMap();
    renderDocsTable(docs);
  }

  // Render verify contract list inline
  if (verifyItems.length > 0) {
    _verifyContracts = verifyItems;
    document.getElementById('docs-verify-list').innerHTML = _buildVerifyContractListHTML(verifyItems);
  }

  // Render disclosures inline
  if (_discData.length > 0) {
    document.getElementById('docs-disc-list').innerHTML = _buildDiscListHTML(_discData);
  }

  // Attach drop zone
  const dropZone = document.getElementById('docs-drop-zone');
  if (dropZone) {
    dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
    dropZone.addEventListener('drop', async e => {
      e.preventDefault();
      dropZone.classList.remove('drag-over');
      const file = e.dataTransfer.files[0];
      if (file && file.type === 'application/pdf') {
        await _uploadFile(file);
        renderDocs();
      } else {
        Toast.show('Please drop a PDF file', 'warning');
      }
    });
  }

  // Attach filter listeners (only if doc list is rendered)
  const searchEl = document.getElementById('docs-search');
  const phaseEl = document.getElementById('docs-phase-filter');
  const statusEl = document.getElementById('docs-status-filter');
  if (searchEl && phaseEl && statusEl) {
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
}

// PDF package map: { normalized_name: { folder, file } }
let _pdfPackageMap = null;

async function _ensurePdfMap() {
  if (_pdfPackageMap) return _pdfPackageMap;
  const packages = await get('/api/doc-packages');
  _pdfPackageMap = {};
  if (!packages._error) {
    for (const pkg of packages) {
      for (const file of pkg.files) {
        const key = file.replace('.pdf', '').replace(/_/g, ' ').toLowerCase();
        _pdfPackageMap[key] = { folder: pkg.folder, file };
      }
    }
  }
  return _pdfPackageMap;
}

// Map checklist doc codes → keywords found in CAR PDF filenames
const _CODE_TO_PDF = {
  'rpa': 'residential purchase agreement',
  'tds': 'transfer disclosure statement',
  'spq': 'seller property questionnaire',
  'ad': 'disclosure information advisory',
  'dia': 'disclosure information advisory',
  'avid': 'agent visual inspection disclosure',
  'sbsa': 'statewide buyer and seller advisory',
  'lbp': 'lead-based paint',
  'rla': 'residential listing agreement',
  'mca': 'market conditions advisory',
  'fhds': 'fire hardening',
  'ehd': 'environmental hazards',
  'eq': 'earthquake hazards',
  'abda': 'affiliated business arrangement',
  'de_supp': 'de supplemental disclosures',
  'nhd': 'natural hazard disclosure',
  'whsd': 'water heater',
  'ssd': 'smoke',
  'meg': 'megan',
};

function _findPdf(docName, docCode) {
  if (!_pdfPackageMap) return null;
  const nameL = (docName || '').toLowerCase();
  const codeL = (docCode || '').toLowerCase();

  // 1. Code-to-pattern lookup (most reliable)
  const pattern = _CODE_TO_PDF[codeL];
  if (pattern) {
    const patWords = pattern.split(/\s+/);
    for (const [key, val] of Object.entries(_pdfPackageMap)) {
      const allMatch = patWords.every(w => key.includes(w));
      if (allMatch) return val;
    }
  }

  // 2. Exact-ish match on full name
  for (const [key, val] of Object.entries(_pdfPackageMap)) {
    if (key.includes(nameL) || nameL.includes(key)) return val;
  }

  // 3. Partial word overlap — 1 match for long words, 2 for short
  const nameWords = nameL.split(/[\s()\/,-]+/).filter(w => w.length >= 3);
  for (const [key, val] of Object.entries(_pdfPackageMap)) {
    const longMatches = nameWords.filter(w => w.length >= 6 && key.includes(w));
    if (longMatches.length >= 1) return val;
    const shortMatches = nameWords.filter(w => key.includes(w));
    if (shortMatches.length >= 2) return val;
  }

  return null;
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
      // Prefer the uploaded file stored on the doc record, fall back to package map
      const uploaded = (d.folder && d.filename) ? { folder: d.folder, file: d.filename } : null;
      const pdf = uploaded || _findPdf(d.name, d.code);
      const nameCell = pdf
        ? `<a href="#" class="doc-pdf-link" data-folder="${esc(pdf.folder)}" data-file="${esc(pdf.file)}">${esc(d.name)}</a>`
        : `<a href="#" class="doc-pdf-link doc-no-pdf" data-code="${esc(d.code)}">${esc(d.name)}</a>`;
      html += `<tr>
        <td><code>${esc(d.code)}</code></td>
        <td>${nameCell}</td>
        <td><span class="badge badge-${d.status}">${d.status}</span></td>
        <td class="actions">${docActions(d)}</td>
      </tr>`;
    });
  });
  html += '</tbody></table></div>';
  area.innerHTML = html;

  // Attach click handlers for PDF links
  area.querySelectorAll('.doc-pdf-link').forEach(el => {
    el.addEventListener('click', e => {
      e.preventDefault();
      if (el.classList.contains('doc-no-pdf')) {
        const code = el.dataset.code;
        Toast.show('No PDF uploaded yet — opening upload dialog', 'info');
        triggerUploadFor(code);
        return;
      }
      PdfViewer.open(el.dataset.folder, el.dataset.file, currentTxn || '');
    });
  });
}

function docActions(d) {
  if (d.status === 'verified') {
    return `<span class="doc-status-icon verified" title="Verified">\u2705</span> <button class="btn btn-muted btn-sm" onclick="docAction('${currentTxn}','${d.code}','unverify')" title="Revert to received">Unverify</button> <button class="btn btn-ghost btn-sm" onclick="docAction('${currentTxn}','${d.code}','reset')" title="Reset to required">Reset</button>`;
  }
  if (d.status === 'na') {
    return `<span class="doc-status-icon na" title="N/A">&mdash;</span> <button class="btn btn-ghost btn-sm" onclick="docAction('${currentTxn}','${d.code}','reset')" title="Reset to required">Reset</button>`;
  }
  let btns = '';
  if (d.status === 'required') {
    btns += `<span class="doc-status-icon missing" title="Not received">\u26D4</span>`;
    btns += `<button class="btn btn-primary btn-sm" onclick="triggerUploadFor('${esc(d.code)}')" title="Upload this document">Upload</button>`;
  }
  if (d.status === 'received') {
    btns += `<span class="doc-status-icon received" title="Received">\uD83D\uDC4D</span>`;
    const hasFile = d.folder && d.filename;
    if (hasFile) {
      btns += `<button class="btn btn-accent btn-sm" onclick="docReview('${esc(d.code)}','${esc(d.folder)}','${esc(d.filename)}')" title="Open PDF to review">Review</button>`;
    }
    btns += `<button class="btn btn-success btn-sm" onclick="docAction('${currentTxn}','${d.code}','verify')" title="Confirm document is complete">Verify</button>`;
    btns += `<button class="btn btn-ghost btn-sm" onclick="docAction('${currentTxn}','${d.code}','reset')" title="Reset to required">Reset</button>`;
  }
  if (d.status !== 'na' && d.status !== 'received') {
    btns += `<button class="btn btn-muted btn-sm" onclick="docAction('${currentTxn}','${d.code}','na')" title="Mark as not applicable">N/A</button>`;
  }
  return btns;
}

// Open PDF for review before verification
function docReview(code, folder, filename) {
  _pendingVerifyCode = code;
  PdfViewer.open(folder, filename, currentTxn || '');
}
let _pendingVerifyCode = null;

// Global: open a document's PDF by code — tries uploaded file, then package map
function openDocPdf(code) {
  if (!currentTxn) return;
  const docs = _docsData || [];
  const d = docs.find(x => x.code === code);
  if (d && d.folder && d.filename) {
    PdfViewer.open(d.folder, d.filename, currentTxn);
    return;
  }
  const pdf = _findPdf(d ? d.name : code, code);
  if (pdf) {
    PdfViewer.open(pdf.folder, pdf.file, currentTxn);
    return;
  }
  // No PDF found — offer to upload
  triggerUploadFor(code);
}

// ══════════════════════════════════════════════════════════════════════════════
//  BULK REVIEW MODULE
// ══════════════════════════════════════════════════════════════════════════════

const BulkReview = (() => {
  let queue = [];       // Array of {code, folder, filename, name} to review
  let currentIdx = 0;
  let active = false;

  function start(docs) {
    // Filter to docs that have files (received status with folder/filename)
    queue = docs.filter(d => d.status === 'received' && d.folder && d.filename)
      .map(d => ({ code: d.code, folder: d.folder, filename: d.filename, name: d.name }));

    if (queue.length === 0) {
      Toast.show('No documents to review', 'info');
      return;
    }

    currentIdx = 0;
    active = true;
    _showBar();
    _openCurrent();
  }

  function _showBar() {
    const bar = document.getElementById('pdf-review-bar');
    bar.style.display = '';
    _updateBar();
  }

  function _hideBar() {
    const bar = document.getElementById('pdf-review-bar');
    bar.style.display = 'none';
  }

  function _updateBar() {
    document.getElementById('pdf-review-idx').textContent = currentIdx + 1;
    document.getElementById('pdf-review-total').textContent = queue.length;
    const doc = queue[currentIdx];
    document.getElementById('pdf-review-docname').textContent = doc ? doc.name : '';
  }

  function _openCurrent() {
    if (currentIdx >= queue.length) {
      _finish();
      return;
    }
    const doc = queue[currentIdx];
    _updateBar();
    PdfViewer.open(doc.folder, doc.filename, currentTxn || '');
  }

  async function verify() {
    if (!active || currentIdx >= queue.length) return;
    const doc = queue[currentIdx];

    // Mark as verified
    const res = await post(`/api/txns/${currentTxn}/docs/${doc.code}/verify`, {});
    if (!res._error) {
      Toast.show(`✓ ${doc.name} verified`, 'success');
    }

    // Advance to next
    currentIdx++;
    if (currentIdx < queue.length) {
      _openCurrent();
    } else {
      _finish();
    }
  }

  function skip() {
    if (!active || currentIdx >= queue.length) return;
    const doc = queue[currentIdx];
    Toast.show(`Skipped ${doc.name}`, 'info');

    currentIdx++;
    if (currentIdx < queue.length) {
      _openCurrent();
    } else {
      _finish();
    }
  }

  function cancel() {
    active = false;
    queue = [];
    _hideBar();
    PdfViewer.close();
    Toast.show('Review cancelled', 'info');
    // Refresh docs list
    invalidateCache(`/api/txns/${currentTxn}`);
    invalidateCache('/api/dashboard');
    renderDocs();
  }

  function _finish() {
    active = false;
    queue = [];
    _hideBar();
    PdfViewer.close();
    Toast.show('Review complete!', 'success');
    // Refresh docs list
    invalidateCache(`/api/txns/${currentTxn}`);
    invalidateCache('/api/dashboard');
    renderDocs();
  }

  function isActive() { return active; }

  return { start, verify, skip, cancel, isActive };
})();

// Start bulk verification workflow
async function startVerifyAllWorkflow() {
  if (!currentTxn) return;
  const docs = await get(`/api/txns/${currentTxn}/docs`);
  if (docs._error) return;
  BulkReview.start(docs);
}

// Upload targeting a specific doc code
function triggerUploadFor(code) {
  if (!currentTxn) return;
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.accept = '.pdf';
  inp.style.display = 'none';
  document.body.appendChild(inp);
  inp.addEventListener('change', async () => {
    if (inp.files[0]) await _uploadFile(inp.files[0]);
    inp.remove();
  });
  inp.click();
}

async function docAction(tid, code, action) {
  const res = await post(`/api/txns/${tid}/docs/${code}/${action}`, {});
  if (res._error) return;
  const actionLabel = { receive: 'received', verify: 'verified', unverify: 'unverified', na: 'marked N/A', reset: 'reset to required' }[action] || action;
  Toast.show(`Document ${code} ${actionLabel}`, 'success');
  invalidateCache(`/api/txns/${tid}`);
  invalidateCache('/api/dashboard');
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
            ${!isVerified ? `<button class="btn btn-success btn-sm" onclick="event.stopPropagation();verifyGate('${g.gid}')" title="Mark compliance item as complete">Verify</button>` : `<button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();resetGate('${g.gid}')" title="Reset to pending">Reset</button>`}
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

async function resetGate(gid) {
  const res = await post(`/api/txns/${currentTxn}/gates/${gid}/reset`);
  if (res._error) return;
  Toast.show(`Gate ${gid} reset to pending`, 'success');
  invalidateCache(`/api/txns/${currentTxn}`);
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
  const anyUnconfirmed = dls.some(d => d.confirmed === false);
  let html = '';
  if (anyUnconfirmed) {
    html += '<div class="unconfirmed-banner">Dates are estimated from acceptance date. Upload contracts to confirm actual dates.</div>';
  }
  html += '<div class="table-wrap"><table><thead><tr><th>ID</th><th>Deadline</th><th>Type</th><th>Due</th><th>Days</th></tr></thead><tbody>';
  dls.forEach(d => {
    const days = d.days_remaining;
    const pillCls = urgencyClass(days);
    const unconf = d.confirmed === false ? ' <span class="unconfirmed-tag">est.</span>' : '';
    html += `<tr>
      <td><code>${esc(d.did)}</code></td>
      <td>${esc(d.name)}</td>
      <td>${esc(d.type || '')}</td>
      <td>${d.due ? d.due + unconf : '&mdash;'}</td>
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
let _emailSandbox = true;

async function fetchSandboxStatus() {
  const res = await get('/api/sandbox-status');
  if (!res._error) _emailSandbox = res.email_sandbox;
}

async function renderSignatures() {
  showSkeleton('gate-cards');

  // Fetch sandbox status, signatures, and envelopes in parallel
  const [sbRes, sigRes, envRes] = await Promise.all([
    get('/api/sandbox-status'),
    get(`/api/txns/${currentTxn}/signatures`),
    get(`/api/txns/${currentTxn}/envelopes`),
  ]);

  if (!sbRes._error) _emailSandbox = sbRes.email_sandbox;
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

  // Email sandbox banner
  if (_emailSandbox) {
    html += '<div class="sandbox-banner">Email sandbox — outgoing emails are captured but not sent. Set TC_EMAIL_SANDBOX=0 to send real email.</div>';
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
          html += `<button class="btn btn-simulate btn-sm" onclick="sigSimulate(${item.id})">Simulate Sign</button>`;
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
      <span>Email Outbox${_emailSandbox ? ' (sandbox)' : ''}</span>
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
  const waivedCount = (s.waived || 0) + (s.not_required || 0);
  html += `<div class="cont-summary">
    <div class="cont-summary-item"><span class="cont-summary-val">${s.total || 0}</span><span class="cont-summary-lbl">Total</span></div>
    <div class="cont-summary-item"><span class="cont-summary-val cont-active">${s.active || 0}</span><span class="cont-summary-lbl">Active</span></div>
    <div class="cont-summary-item"><span class="cont-summary-val cont-removed">${s.removed || 0}</span><span class="cont-summary-lbl">Removed</span></div>
    ${waivedCount > 0 ? `<div class="cont-summary-item"><span class="cont-summary-val cont-waived">${waivedCount}</span><span class="cont-summary-lbl">Not Required</span></div>` : ''}
    <div class="cont-summary-item"><span class="cont-summary-val cont-overdue">${s.overdue || 0}</span><span class="cont-summary-lbl">Overdue</span></div>
    <button class="btn btn-primary btn-sm" onclick="openContModal()" title="Add a contingency manually">+ Add</button>
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
    const statusCls = { active: 'cont-status-active', removed: 'cont-status-removed', waived: 'cont-status-waived', not_required: 'cont-status-not-required', expired: 'cont-status-expired' }[item.status] || '';
    const isActive = item.status === 'active';
    const isNotRequired = item.status === 'waived' || item.status === 'not_required';

    // Progress bar computation
    const totalDays = item.default_days || 17;
    const elapsed = totalDays - (item.days_remaining || 0);
    const pct = Math.min(100, Math.max(0, (elapsed / totalDays) * 100));

    // Status display text
    const statusText = item.status === 'not_required' ? 'NOT REQUIRED' : esc(item.status).toUpperCase();
    const notRequiredReason = isNotRequired && item.notes ? item.notes.replace(/\[Auto-detected.*?\]/g, '').trim() : '';

    let html = `<div class="cont-card ${urgencyCls} ${isNotRequired ? 'cont-not-required' : ''}">
      <div class="cont-card-header">
        <div class="cont-card-title">
          <span class="cont-type-icon">${contIcon(item.type)}</span>
          <span class="cont-name">${esc(item.name || item.type)}</span>
          <span class="cont-status-badge ${statusCls}">${statusText}</span>
        </div>
        <div class="cont-deadline-info">
          ${item.deadline_date && !isNotRequired ? `<span class="cont-deadline-date">${esc(item.deadline_date)}</span>` : ''}
          ${isNotRequired ? `<span class="cont-auto-detected">\u2713 Auto-detected from documents</span>` : ''}
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
      html += `<span class="cont-gate-link" onclick="switchTab('compliance')">${esc(item.related_gate)}</span>`;
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

    // Inspection Checklist (investigation contingencies)
    if (item.type === 'investigation' && (item.items_total > 0 || isActive)) {
      const itemsDone = item.items_done || 0;
      const itemsTotal = item.items_total || 0;
      html += `<div class="cont-items-section">
        <button class="cont-items-toggle" onclick="event.stopPropagation();toggleContItems(${item.id})">
          <svg class="collapsible-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>
          Inspection Checklist
          <span class="cont-items-count">${itemsDone}/${itemsTotal}</span>
        </button>
        <div class="cont-items-list" id="cont-items-${item.id}" style="display:none"></div>
      </div>`;
    }

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

// ── Inspection Checklist Functions ─────────────────────────────────────────

async function toggleContItems(cid) {
  const el = document.getElementById('cont-items-' + cid);
  if (!el) return;
  const toggle = el.previousElementSibling;
  const isHidden = el.style.display === 'none';
  el.style.display = isHidden ? '' : 'none';
  if (toggle) toggle.classList.toggle('open', isHidden);
  if (isHidden) await loadContItems(cid);
}

async function loadContItems(cid) {
  const el = document.getElementById('cont-items-' + cid);
  if (!el) return;
  const items = await get(`/api/txns/${currentTxn}/contingencies/${cid}/items`);
  if (items._error) return;

  let html = items.map(item => {
    const isDone = item.status === 'complete';
    const statusBadge = { pending: '', scheduled: '<span class="cont-item-badge scheduled">SCHEDULED</span>', complete: '<span class="cont-item-badge complete">DONE</span>', waived: '<span class="cont-item-badge waived">WAIVED</span>' }[item.status] || '';
    return `<div class="cont-item ${isDone ? 'cont-item-done' : ''}">
      <label class="cont-item-check">
        <input type="checkbox" ${isDone ? 'checked' : ''} onchange="contItemToggle(${cid},${item.id},this.checked)">
        <span class="cont-item-name">${esc(item.name)}</span>
      </label>
      ${statusBadge}
      ${item.inspector ? `<span class="cont-item-inspector">${esc(item.inspector)}</span>` : ''}
      ${item.scheduled_date ? `<span class="cont-item-date">${esc(item.scheduled_date)}</span>` : ''}
      <button class="btn btn-ghost btn-sm cont-item-edit" onclick="editContItem(${cid},${item.id})" title="Edit">&#9998;</button>
    </div>`;
  }).join('');

  html += `<div class="cont-item-add">
    <input type="text" id="cont-item-new-${cid}" placeholder="Add custom inspection..." class="cont-item-input">
    <button class="btn btn-primary btn-sm" onclick="addContItem(${cid})">+ Add</button>
  </div>`;

  el.innerHTML = html;
}

async function contItemToggle(cid, iid, checked) {
  const status = checked ? 'complete' : 'pending';
  await api(`/api/txns/${currentTxn}/contingencies/${cid}/items/${iid}`, {
    method: 'PUT', body: { status }
  });
  loadContItems(cid);
}

async function addContItem(cid) {
  const input = document.getElementById('cont-item-new-' + cid);
  const name = (input?.value || '').trim();
  if (!name) return;
  await post(`/api/txns/${currentTxn}/contingencies/${cid}/items`, { name });
  input.value = '';
  loadContItems(cid);
}

async function editContItem(cid, iid) {
  const inspector = prompt('Inspector name (leave blank to skip):');
  if (inspector === null) return;
  const scheduled = prompt('Scheduled date (YYYY-MM-DD, leave blank to skip):');
  const body = {};
  if (inspector) body.inspector = inspector;
  if (scheduled) body.scheduled_date = scheduled;
  if (Object.keys(body).length > 0) {
    await api(`/api/txns/${currentTxn}/contingencies/${cid}/items/${iid}`, {
      method: 'PUT', body
    });
    loadContItems(cid);
  }
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

// ══════════════════════════════════════════════════════════════════════════════
//  PARTIES TAB
// ══════════════════════════════════════════════════════════════════════════════

const PARTY_ROLES = [
  { value: 'buyer', label: 'Buyer' },
  { value: 'seller', label: 'Seller' },
  { value: 'buyer_agent', label: "Buyer's Agent" },
  { value: 'seller_agent', label: "Seller's Agent" },
  { value: 'escrow_officer', label: 'Escrow Officer' },
  { value: 'lender', label: 'Lender' },
  { value: 'title_rep', label: 'Title Representative' },
  { value: 'inspector', label: 'Inspector' },
  { value: 'appraiser', label: 'Appraiser' },
  { value: 'transaction_coordinator', label: 'Transaction Coordinator' },
  { value: 'other', label: 'Other' },
];

let _partiesData = [];

async function renderParties() {
  showSkeleton('cards');
  const parties = await get(`/api/txns/${currentTxn}/parties`);
  if (parties._error) return;
  _partiesData = parties;

  // Auto-populate: if there are placeholder parties (TBD), try to fill from signatures
  const hasPlaceholders = parties.some(p => p.name && p.name.includes('(TBD)'));
  if (hasPlaceholders) {
    await _autoPopulatePartiesFromSigs(parties);
    // Re-fetch after auto-populate
    const updated = await get(`/api/txns/${currentTxn}/parties`);
    if (!updated._error) _partiesData = updated;
  }

  const el = $('#tab-content');

  // Summary counts by role
  const byRole = {};
  parties.forEach(p => { byRole[p.role] = (byRole[p.role] || 0) + 1; });

  let html = `<div class="filter-bar">
    <input type="text" placeholder="Search parties..." id="parties-search">
    <select id="parties-role-filter">
      <option value="">All Roles</option>
      ${PARTY_ROLES.filter(r => byRole[r.value]).map(r => `<option value="${r.value}">${r.label} (${byRole[r.value]})</option>`).join('')}
    </select>
    <button class="btn btn-ghost btn-sm" onclick="detectPartiesFromSigs()" title="Detect parties from signature fields">Import from Signatures</button>
    <button class="btn btn-primary btn-sm" onclick="openPartyForm()" title="Add a new party manually">+ Add Party</button>
  </div>`;

  html += `<div class="summary-bar">
    <span><span class="count">${parties.length}</span> total parties</span>
    ${Object.entries(byRole).map(([r, c]) => {
      const label = PARTY_ROLES.find(pr => pr.value === r)?.label || r;
      return `<span><span class="count">${c}</span> ${label.toLowerCase()}</span>`;
    }).join('')}
  </div>`;

  // Inline add form (hidden by default)
  html += `<div class="party-add-form card" id="party-add-form" style="display:none">
    <div class="card-title">Add Party</div>
    <div class="field-row">
      <label class="field-label">Role
        <select id="party-role">${PARTY_ROLES.map(r => `<option value="${r.value}">${r.label}</option>`).join('')}</select>
      </label>
      <label class="field-label">Name <input type="text" id="party-name" placeholder="Full name" required></label>
    </div>
    <div class="field-row">
      <label class="field-label">Email <input type="email" id="party-email" placeholder="email@example.com"></label>
      <label class="field-label">Phone <input type="tel" id="party-phone" placeholder="(555) 555-1234"></label>
    </div>
    <div class="field-row">
      <label class="field-label">Company <input type="text" id="party-company" placeholder="Brokerage / Company"></label>
      <label class="field-label">License # <input type="text" id="party-license" placeholder="DRE#"></label>
    </div>
    <div style="display:flex;gap:8px;margin-top:8px">
      <button class="btn btn-primary btn-sm" onclick="submitParty()">Add</button>
      <button class="btn btn-ghost btn-sm" onclick="closePartyForm()">Cancel</button>
    </div>
  </div>`;

  html += '<div id="parties-list-area"></div>';
  el.innerHTML = html;
  renderPartiesList(parties);

  // Filters
  const searchEl = document.getElementById('parties-search');
  const roleEl = document.getElementById('parties-role-filter');
  const filterFn = () => {
    const q = searchEl.value.toLowerCase();
    const r = roleEl.value;
    const filtered = _partiesData.filter(p => {
      if (q && !p.name.toLowerCase().includes(q) && !(p.email || '').toLowerCase().includes(q)
            && !(p.company || '').toLowerCase().includes(q)) return false;
      if (r && p.role !== r) return false;
      return true;
    });
    renderPartiesList(filtered);
  };
  searchEl.addEventListener('input', filterFn);
  roleEl.addEventListener('change', filterFn);
}

function renderPartiesList(parties) {
  const area = document.getElementById('parties-list-area');
  if (!area) return;

  if (parties.length === 0) {
    area.innerHTML = '<div class="card"><p style="color:var(--text-secondary)">No parties added yet. Add the transaction parties to track contacts.</p></div>';
    return;
  }

  // Group by role
  const groups = {};
  parties.forEach(p => {
    if (!groups[p.role]) groups[p.role] = [];
    groups[p.role].push(p);
  });

  const roleOrder = PARTY_ROLES.map(r => r.value);
  const sortedRoles = Object.keys(groups).sort((a, b) => roleOrder.indexOf(a) - roleOrder.indexOf(b));

  let html = '';
  sortedRoles.forEach(role => {
    const label = PARTY_ROLES.find(r => r.value === role)?.label || role;
    html += `<div class="party-group">
      <div class="party-group-header">${esc(label)}</div>`;
    groups[role].forEach(p => {
      html += `<div class="party-card">
        <div class="party-card-top">
          <div class="party-avatar">${esc((p.name || '?')[0].toUpperCase())}</div>
          <div class="party-info">
            <div class="party-name">${esc(p.name)}</div>
            ${p.company ? `<div class="party-company">${esc(p.company)}</div>` : ''}
          </div>
          <div class="party-actions">
            <button class="btn btn-muted btn-sm" onclick="editParty(${p.id})" title="Edit party details">Edit</button>
            <button class="btn btn-danger btn-sm" onclick="deleteParty(${p.id})" title="Remove this party">Delete</button>
          </div>
        </div>
        <div class="party-details">
          ${p.email ? `<span class="party-detail"><span class="party-detail-icon">@</span>${esc(p.email)}</span>` : ''}
          ${p.phone ? `<span class="party-detail"><span class="party-detail-icon">#</span>${esc(p.phone)}</span>` : ''}
          ${p.license_no ? `<span class="party-detail"><span class="party-detail-icon">L</span>${esc(p.license_no)}</span>` : ''}
        </div>
        ${p.notes ? `<div class="party-notes">${esc(p.notes)}</div>` : ''}
      </div>`;
    });
    html += '</div>';
  });

  area.innerHTML = html;
}

function openPartyForm() {
  document.getElementById('party-add-form').style.display = '';
  document.getElementById('party-name').focus();
}

function closePartyForm() {
  document.getElementById('party-add-form').style.display = 'none';
  ['party-name', 'party-email', 'party-phone', 'party-company', 'party-license'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
}

async function submitParty() {
  const body = {
    role: document.getElementById('party-role').value,
    name: document.getElementById('party-name').value.trim(),
    email: document.getElementById('party-email').value.trim(),
    phone: document.getElementById('party-phone').value.trim(),
    company: document.getElementById('party-company').value.trim(),
    license_no: document.getElementById('party-license').value.trim(),
  };
  if (!body.name) { Toast.show('Name is required', 'warning'); return; }
  const res = await post(`/api/txns/${currentTxn}/parties`, body);
  if (res._error) return;
  Toast.show(`${body.name} added as ${body.role}`, 'success');
  closePartyForm();
  renderParties();
}

async function editParty(pid) {
  const party = _partiesData.find(p => p.id === pid);
  if (!party) return;
  const name = prompt('Name:', party.name);
  if (name === null) return;
  const email = prompt('Email:', party.email || '');
  if (email === null) return;
  const phone = prompt('Phone:', party.phone || '');
  if (phone === null) return;
  const res = await api(`/api/txns/${currentTxn}/parties/${pid}`, {
    method: 'PUT', body: { name: name || party.name, email, phone },
  });
  if (res._error) return;
  Toast.show('Party updated', 'success');
  renderParties();
}

async function deleteParty(pid) {
  if (!confirm('Remove this party?')) return;
  const res = await del(`/api/txns/${currentTxn}/parties/${pid}`);
  if (res._error) return;
  Toast.show('Party removed', 'info');
  renderParties();
}

async function _autoPopulatePartiesFromSigs(existingParties) {
  // Silently auto-populate placeholder parties from signature fields
  const res = await get(`/api/txns/${currentTxn}/signatures/detected-parties`);
  if (res._error || !res.detected || res.detected.length === 0) return;

  // Find which roles have placeholders that need filling
  const placeholderRoles = new Set();
  existingParties.forEach(p => {
    if (p.name && p.name.includes('(TBD)')) {
      placeholderRoles.add(p.role);
    }
  });

  // Build parties to import - only for roles that have placeholders
  const toImport = [];
  res.detected.forEach(d => {
    if (placeholderRoles.has(d.role)) {
      // Use actual detected name if available, otherwise role label
      const name = d.suggested_name || d.role_label;
      toImport.push({ role: d.role, name: name });
    }
  });

  if (toImport.length === 0) return;

  // Import silently
  const importRes = await post(`/api/txns/${currentTxn}/parties/import`, { parties: toImport });
  if (!importRes._error && importRes.imported && importRes.imported.length > 0) {
    Toast.show(`Auto-populated ${importRes.imported.length} parties from documents`, 'success');
  }
}

async function detectPartiesFromSigs() {
  if (!currentTxn) return;
  const res = await get(`/api/txns/${currentTxn}/signatures/detected-parties`);
  if (res._error) return;

  const detected = res.detected || [];
  if (detected.length === 0) {
    Toast.show('No party roles detected from signature fields', 'info');
    return;
  }

  // Build confirmation modal content
  let html = `<div class="import-parties-modal">
    <p style="margin-bottom:12px">Detected ${detected.length} party role(s) from signature fields:</p>
    <div class="import-parties-list">`;
  detected.forEach((d, i) => {
    html += `<div class="import-party-item">
      <input type="checkbox" id="import-party-${i}" checked>
      <label for="import-party-${i}">
        <strong>${esc(d.role_label)}</strong>
        <span class="import-party-source">from: ${esc(d.source_fields.slice(0, 2).join(', '))}</span>
      </label>
      <input type="text" class="import-party-name" value="${esc(d.suggested_name)}" data-role="${esc(d.role)}" placeholder="Name">
    </div>`;
  });
  html += `</div>
    <div style="margin-top:16px;display:flex;gap:8px;justify-content:flex-end">
      <button class="btn btn-ghost" onclick="closeImportPartiesModal()">Cancel</button>
      <button class="btn btn-primary" onclick="confirmImportParties()">Import Selected</button>
    </div>
  </div>`;

  // Show in a simple modal overlay
  const overlay = document.createElement('div');
  overlay.id = 'import-parties-overlay';
  overlay.className = 'modal-backdrop';
  overlay.style.display = '';
  overlay.innerHTML = `<div class="modal" style="width:480px"><div class="modal-header"><h3>Import Parties from Signatures</h3><button class="modal-close" onclick="closeImportPartiesModal()">&times;</button></div><div class="modal-body">${html}</div></div>`;
  document.body.appendChild(overlay);
}

function closeImportPartiesModal() {
  const overlay = document.getElementById('import-parties-overlay');
  if (overlay) overlay.remove();
}

async function confirmImportParties() {
  const items = document.querySelectorAll('#import-parties-overlay .import-party-item');
  const parties = [];
  items.forEach((item, i) => {
    const checkbox = item.querySelector(`#import-party-${i}`);
    const nameInput = item.querySelector('.import-party-name');
    if (checkbox && checkbox.checked && nameInput) {
      parties.push({ role: nameInput.dataset.role, name: nameInput.value.trim() });
    }
  });

  if (parties.length === 0) {
    Toast.show('No parties selected', 'warning');
    return;
  }

  const res = await post(`/api/txns/${currentTxn}/parties/import`, { parties });
  if (res._error) return;

  closeImportPartiesModal();
  Toast.show(`Imported ${res.imported.length} parties`, 'success');
  renderParties();
}

// ══════════════════════════════════════════════════════════════════════════════
//  DISCLOSURES TAB
// ══════════════════════════════════════════════════════════════════════════════

const DISC_TYPES = [
  { value: 'tds', label: 'Transfer Disclosure Statement (TDS)' },
  { value: 'spq', label: 'Seller Property Questionnaire (SPQ)' },
  { value: 'nhd', label: 'Natural Hazard Disclosure (NHD)' },
  { value: 'avid_listing', label: 'Agent Visual Inspection - Listing (AVID)' },
  { value: 'avid_buyer', label: 'Agent Visual Inspection - Buyer (AVID)' },
  { value: 'lead_paint', label: 'Lead-Based Paint Disclosure' },
  { value: 'water_heater', label: 'Water Heater Statement' },
  { value: 'smoke_co', label: 'Smoke/CO Detector Compliance' },
  { value: 'megan_law', label: "Megan's Law Disclosure" },
  { value: 'preliminary_title', label: 'Preliminary Title Report' },
  { value: 'hoa_docs', label: 'HOA Documents Package' },
  { value: 'local', label: 'Local Supplemental Disclosures' },
  { value: 'other', label: 'Other Disclosure' },
];

let _discData = [];
let _discSummary = {};

async function renderDisclosures() {
  showSkeleton('gate-cards');
  const res = await get(`/api/txns/${currentTxn}/disclosures`);
  if (res._error) return;
  _discData = res.items || [];
  _discSummary = res.summary || {};

  const el = $('#tab-content');
  let html = '';

  // Summary bar
  const s = _discSummary;
  html += `<div class="disc-summary">
    <div class="disc-summary-item"><span class="disc-summary-val">${s.total || 0}</span><span class="disc-summary-lbl">Total</span></div>
    <div class="disc-summary-item"><span class="disc-summary-val disc-pending">${s.pending || 0}</span><span class="disc-summary-lbl">Pending</span></div>
    <div class="disc-summary-item"><span class="disc-summary-val disc-received">${s.received || 0}</span><span class="disc-summary-lbl">Received</span></div>
    <div class="disc-summary-item"><span class="disc-summary-val disc-reviewed">${s.reviewed || 0}</span><span class="disc-summary-lbl">Reviewed</span></div>
    <div class="disc-summary-item"><span class="disc-summary-val disc-waived">${s.waived || 0}</span><span class="disc-summary-lbl">Waived/NA</span></div>
    <button class="btn btn-primary btn-sm" onclick="openDiscForm()">+ Add</button>
  </div>`;

  // Inline add form
  html += `<div class="disc-add-form card" id="disc-add-form" style="display:none">
    <div class="card-title">Add Disclosure</div>
    <div class="field-row">
      <label class="field-label">Type
        <select id="disc-type">${DISC_TYPES.map(d => `<option value="${d.value}">${d.label}</option>`).join('')}</select>
      </label>
      <label class="field-label">Due Date <input type="date" id="disc-due-date"></label>
    </div>
    <label class="field-label">Notes <textarea id="disc-notes" rows="2" placeholder="Optional notes..."></textarea></label>
    <div style="display:flex;gap:8px;margin-top:8px">
      <button class="btn btn-primary btn-sm" onclick="submitDisc()">Add</button>
      <button class="btn btn-ghost btn-sm" onclick="closeDiscForm()">Cancel</button>
    </div>
  </div>`;

  if (_discData.length === 0) {
    html += '<div class="card"><p style="color:var(--text-secondary)">No disclosures tracked. Add required disclosures for this transaction.</p></div>';
    el.innerHTML = html;
    return;
  }

  html += '<div id="disc-list-area"></div>';
  el.innerHTML = html;
  renderDiscList(_discData);
}

function renderDiscList(items) {
  const area = document.getElementById('disc-list-area');
  if (!area) return;

  area.innerHTML = items.map(item => {
    const urgencyCls = { overdue: 'disc-overdue', urgent: 'disc-urgent', soon: 'disc-soon', ok: 'disc-ok', none: '' }[item.urgency] || '';
    const statusCls = { pending: 'disc-status-pending', ordered: 'disc-status-ordered', received: 'disc-status-received', reviewed: 'disc-status-reviewed', waived: 'disc-status-waived', na: 'disc-status-waived' }[item.status] || '';
    const isPending = item.status === 'pending' || item.status === 'ordered';
    const isReceived = item.status === 'received';

    let html = `<div class="disc-card ${urgencyCls}">
      <div class="disc-card-header">
        <div class="disc-card-title">
          <span class="disc-name">${esc(item.name || item.type)}</span>
          <span class="disc-status-badge ${statusCls}">${esc(item.status).toUpperCase()}</span>
        </div>
        <div class="disc-dates">
          ${item.due_date ? `<span class="disc-due">Due: ${esc(item.due_date)}</span>` : ''}
          ${item.days_until_due !== null ? `<span class="disc-days-pill ${urgencyCls}">${item.days_until_due}d</span>` : ''}
        </div>
      </div>`;

    // Metadata
    html += '<div class="disc-meta-row">';
    if (item.responsible) {
      const rName = PARTY_ROLES.find(r => r.value === item.responsible)?.label || item.responsible;
      html += `<span class="disc-responsible">Responsible: ${esc(rName)}</span>`;
    }
    if (item.received_date) html += `<span class="disc-date">Received: ${esc(item.received_date)}</span>`;
    if (item.reviewed_date) html += `<span class="disc-date">Reviewed: ${esc(item.reviewed_date)}</span>`;
    if (item.reviewer) html += `<span class="disc-reviewer">By: ${esc(item.reviewer)}</span>`;
    if (item.notes) html += `<span class="disc-note">"${esc(item.notes)}"</span>`;
    html += '</div>';

    // Actions
    html += '<div class="disc-actions">';
    if (isPending) {
      html += `<button class="btn btn-warning btn-sm" onclick="discReceive(${item.id})">Mark Received</button>`;
      html += `<button class="btn btn-muted btn-sm" onclick="discWaive(${item.id})">Waive / N/A</button>`;
    }
    if (isReceived) {
      html += `<button class="btn btn-success btn-sm" onclick="discReview(${item.id})">Mark Reviewed</button>`;
    }
    html += '</div></div>';
    return html;
  }).join('');
}

function openDiscForm() {
  document.getElementById('disc-add-form').style.display = '';
}

function closeDiscForm() {
  document.getElementById('disc-add-form').style.display = 'none';
}

async function submitDisc() {
  const body = {
    type: document.getElementById('disc-type').value,
    due_date: document.getElementById('disc-due-date').value,
    notes: document.getElementById('disc-notes').value.trim(),
  };
  const res = await post(`/api/txns/${currentTxn}/disclosures`, body);
  if (res._error) return;
  Toast.show('Disclosure added', 'success');
  closeDiscForm();
  renderDocs();
}

async function discReceive(did) {
  const res = await post(`/api/txns/${currentTxn}/disclosures/${did}/receive`);
  if (res._error) return;
  Toast.show('Disclosure marked received', 'success');
  renderDocs();
}

async function discReview(did) {
  const res = await post(`/api/txns/${currentTxn}/disclosures/${did}/review`);
  if (res._error) return;
  Toast.show('Disclosure reviewed', 'success');
  renderDocs();
}

async function discWaive(did) {
  if (!confirm('Mark this disclosure as waived/N/A?')) return;
  const res = await post(`/api/txns/${currentTxn}/disclosures/${did}/waive`);
  if (res._error) return;
  Toast.show('Disclosure waived', 'info');
  renderDocs();
}

// ── Collapsible Sections ──────────────────────────────────────────────────

function toggleCollapsible(btn) {
  btn.classList.toggle('open');
  const content = btn.nextElementSibling;
  content.style.display = content.style.display === 'none' ? '' : 'none';
}

// ── Inline Verify Contract List Builder ──────────────────────────────────

function _buildVerifyContractListHTML(items) {
  const groups = {};
  items.forEach(c => { const k = c.scenario || 'default'; if (!groups[k]) groups[k] = []; groups[k].push(c); });
  let html = '';
  Object.entries(groups).forEach(([scenario, contracts]) => {
    html += `<div class="verify-scenario-group"><div class="verify-scenario-header">${esc(scenario.replace(/-/g, ' '))}</div>`;
    contracts.forEach(c => {
      const pct = c.total_fields ? Math.round((c.verified_count / c.total_fields) * 100) : 0;
      const statusCls = c.status === 'verified' ? 'verified' : (c.unfilled_mandatory > 0 ? 'has-mandatory' : '');
      const displayName = c.filename.replace('.pdf', '').replace(/_/g, ' ');
      html += `<div class="verify-contract-card ${statusCls}" data-cid="${c.id}">
        <div class="verify-contract-top">
          <span class="verify-contract-name" title="${esc(c.filename)}">${esc(displayName)}</span>
          <span class="badge badge-${c.status}">${c.status}</span>
        </div>
        <div class="verify-contract-meta">
          <span class="verify-field-count">${c.total_fields} fields</span>
          <span style="color:var(--green)">${c.filled_fields} filled</span>
          ${c.unfilled_mandatory > 0 ? `<span style="color:var(--red)">${c.unfilled_mandatory} unfilled mandatory</span>` : ''}
        </div>
        <div class="verify-progress-bar"><div class="verify-progress-fill" style="width:${pct}%"></div></div>
        <div class="verify-contract-actions">
          <button class="btn btn-danger btn-sm" onclick="verifyStartGuided(${c.id},'quick')">Quick Scan</button>
          <button class="btn btn-primary btn-sm" onclick="verifyStartGuided(${c.id},'review')">Standard Review</button>
          <button class="btn btn-muted btn-sm" onclick="verifyStartGuided(${c.id},'full')">Full Verification</button>
          <button class="btn btn-success btn-sm" onclick="verifyAllFilled(${c.id})">Auto-verify Filled</button>
        </div>
      </div>`;
    });
    html += '</div>';
  });
  return html;
}

// ── Inline Disclosure List Builder ───────────────────────────────────────

function _buildDiscListHTML(items) {
  return items.map(item => {
    const urgencyCls = { overdue: 'disc-overdue', urgent: 'disc-urgent', soon: 'disc-soon', ok: 'disc-ok', none: '' }[item.urgency] || '';
    const statusCls = { pending: 'disc-status-pending', ordered: 'disc-status-ordered', received: 'disc-status-received', reviewed: 'disc-status-reviewed', waived: 'disc-status-waived', na: 'disc-status-waived' }[item.status] || '';
    const isPending = item.status === 'pending' || item.status === 'ordered';
    const isReceived = item.status === 'received';

    let html = `<div class="disc-card ${urgencyCls}">
      <div class="disc-card-header">
        <div class="disc-card-title">
          <span class="disc-name">${esc(item.name || item.type)}</span>
          <span class="disc-status-badge ${statusCls}">${esc(item.status).toUpperCase()}</span>
        </div>
        <div class="disc-dates">
          ${item.due_date ? `<span class="disc-due">Due: ${esc(item.due_date)}</span>` : ''}
          ${item.days_until_due !== null ? `<span class="disc-days-pill ${urgencyCls}">${item.days_until_due}d</span>` : ''}
        </div>
      </div>`;
    html += '<div class="disc-meta-row">';
    if (item.responsible) {
      const rName = PARTY_ROLES.find(r => r.value === item.responsible)?.label || item.responsible;
      html += `<span class="disc-responsible">Responsible: ${esc(rName)}</span>`;
    }
    if (item.received_date) html += `<span class="disc-date">Received: ${esc(item.received_date)}</span>`;
    if (item.reviewed_date) html += `<span class="disc-date">Reviewed: ${esc(item.reviewed_date)}</span>`;
    if (item.notes) html += `<span class="disc-note">"${esc(item.notes)}"</span>`;
    html += '</div>';
    html += '<div class="disc-actions">';
    if (isPending) {
      html += `<button class="btn btn-warning btn-sm" onclick="discReceive(${item.id})">Mark Received</button>`;
      html += `<button class="btn btn-muted btn-sm" onclick="discWaive(${item.id})">Waive / N/A</button>`;
    }
    if (isReceived) {
      html += `<button class="btn btn-success btn-sm" onclick="discReview(${item.id})">Mark Reviewed</button>`;
    }
    html += '</div></div>';
    return html;
  }).join('');
}

// ── Advance Blockers Modal ───────────────────────────────────────────────

function showAdvanceBlockersModal(blockers) {
  const body = document.getElementById('advance-blockers-body');
  if (!body) return;
  let html = '';

  // Gates
  if (blockers.gates && blockers.gates.length > 0) {
    html += `<div class="blocker-group">
      <h4>Compliance Gates (${blockers.gates.length})</h4>
      ${blockers.gates.map(g => `<div class="blocker-item">
        <div class="blocker-item-info">
          <span class="blocker-item-id">${esc(g.gid)}</span>
          <span class="blocker-item-name">${esc(g.name)}</span>
        </div>
        <div class="blocker-actions">
          <button class="btn btn-success btn-sm" onclick="verifyGate('${esc(g.gid)}');closeAdvanceBlockersModal()">Verify</button>
        </div>
      </div>`).join('')}
    </div>`;
  }

  // Missing docs
  if (blockers.missing_docs && blockers.missing_docs.length > 0) {
    html += `<div class="blocker-group">
      <h4>Missing Documents (${blockers.missing_docs.length})</h4>
      ${blockers.missing_docs.map(d => `<div class="blocker-item">
        <div class="blocker-item-info">
          <span class="blocker-item-id">${esc(d.code)}</span>
          <span class="blocker-item-name clickable-doc" onclick="openDocPdf('${esc(d.code)}')">${esc(d.name)}</span>
        </div>
        <div class="blocker-actions">
          <button class="btn btn-primary btn-sm" onclick="triggerUploadFor('${esc(d.code)}');closeAdvanceBlockersModal()">Upload</button>
        </div>
      </div>`).join('')}
    </div>`;
  }

  // Open contingencies
  if (blockers.open_contingencies && blockers.open_contingencies.length > 0) {
    html += `<div class="blocker-group">
      <h4>Active Contingencies (${blockers.open_contingencies.length})</h4>
      ${blockers.open_contingencies.map(c => `<div class="blocker-item">
        <div class="blocker-item-info">
          <span class="blocker-item-name">${esc(c.name)}</span>
          ${c.deadline_date ? `<span class="blocker-item-sub">Due: ${esc(c.deadline_date)}</span>` : ''}
        </div>
        <div class="blocker-actions">
          <button class="btn btn-success btn-sm" onclick="contRemove(${c.id});closeAdvanceBlockersModal()">Remove</button>
          <button class="btn btn-muted btn-sm" onclick="contWaive(${c.id});closeAdvanceBlockersModal()">Waive</button>
        </div>
      </div>`).join('')}
    </div>`;
  }

  // Unsigned fields
  if (blockers.unsigned_fields && blockers.unsigned_fields.length > 0) {
    html += `<div class="blocker-group">
      <h4>Unsigned Fields (${blockers.unsigned_fields.length})</h4>
      ${blockers.unsigned_fields.map(u => `<div class="blocker-item">
        <div class="blocker-item-info">
          <span class="blocker-item-name">${esc(u.field_name)}</span>
          <span class="blocker-item-sub">${esc(u.doc_code)}</span>
        </div>
        <div class="blocker-actions">
          <button class="btn btn-primary btn-sm" onclick="closeAdvanceBlockersModal();switchTab('signatures')">Go to Signatures</button>
        </div>
      </div>`).join('')}
    </div>`;
  }

  // Contacts
  if (blockers.parties && blockers.parties.length > 0) {
    html += `<div class="blocker-group blocker-contacts">
      <h4>Quick Contacts</h4>
      ${blockers.parties.map(p => `<div class="blocker-contact">
        <span class="blocker-contact-role">${esc(p.role.replace(/_/g, ' '))}</span>
        <span class="blocker-contact-name">${esc(p.name)}</span>
        ${p.email ? `<a href="mailto:${esc(p.email)}" class="btn btn-ghost btn-sm">Email</a>` : ''}
        ${p.phone ? `<a href="tel:${esc(p.phone)}" class="btn btn-ghost btn-sm">Call</a>` : ''}
      </div>`).join('')}
    </div>`;
  }

  if (!html) {
    html = '<p style="color:var(--text-secondary);padding:16px">No specific blockers identified. Check compliance gates.</p>';
  }

  body.innerHTML = html;
  document.getElementById('advance-blockers-modal').style.display = '';
}

function closeAdvanceBlockersModal() {
  const modal = document.getElementById('advance-blockers-modal');
  if (modal) modal.style.display = 'none';
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

// ══════════════════════════════════════════════════════════════════════════════
//  VERIFY TAB — Quick verification workflow with crop images
// ══════════════════════════════════════════════════════════════════════════════

let _verifyContracts = [];
let _verifyActiveContract = null;
let _verifyFields = [];
let _verifyIdx = 0;
let _verifyStats = {};
let _verifyMode = 'review';
let _verifyKeyHandler = null;

async function renderVerify() {
  const el = $('#tab-content');
  el.innerHTML = `
    <div class="card" style="margin-bottom:12px">
      <div class="card-title" style="display:flex;justify-content:space-between;align-items:center">
        <span>Contract Verification</span>
        <button class="btn btn-primary btn-sm" onclick="verifyScan()">Scan Contracts</button>
      </div>
      <p style="font-size:13px;color:var(--text-secondary);margin:4px 0 0">
        Guided field-by-field verification with zoomed screenshots and keyboard shortcuts.
      </p>
    </div>
    <div id="verify-summary"></div>
    <div id="verify-contract-list"></div>
    <div id="verify-workflow" style="display:none"></div>`;

  const data = await get('/api/contracts');
  if (data._error || !data.items || data.items.length === 0) {
    document.getElementById('verify-summary').innerHTML =
      '<div class="card"><p style="color:var(--text-secondary)">No contracts scanned yet. Click "Scan Contracts" to import PDFs.</p></div>';
    return;
  }
  _verifyContracts = data.items;
  renderVerifySummary(data);
  renderVerifyContractList(data.items);
}

function renderVerifySummary(data) {
  const s = data.summary;
  const items = data.items;
  const totalFields = items.reduce((a, c) => a + c.total_fields, 0);
  const filledFields = items.reduce((a, c) => a + c.filled_fields, 0);
  const unfilledMand = items.reduce((a, c) => a + c.unfilled_mandatory, 0);
  const verified = items.reduce((a, c) => a + c.verified_count, 0);

  document.getElementById('verify-summary').innerHTML = `
    <div class="verify-stats-bar">
      <div class="verify-stat"><span class="verify-stat-num">${s.total}</span><span class="verify-stat-label">Contracts</span></div>
      <div class="verify-stat"><span class="verify-stat-num">${totalFields}</span><span class="verify-stat-label">Total Fields</span></div>
      <div class="verify-stat filled"><span class="verify-stat-num">${filledFields}</span><span class="verify-stat-label">Filled</span></div>
      <div class="verify-stat mandatory"><span class="verify-stat-num">${unfilledMand}</span><span class="verify-stat-label">Unfilled Mandatory</span></div>
      <div class="verify-stat verified-stat"><span class="verify-stat-num">${verified}</span><span class="verify-stat-label">Verified</span></div>
    </div>`;
}

function renderVerifyContractList(items) {
  const groups = {};
  items.forEach(c => { const k = c.scenario || 'default'; if (!groups[k]) groups[k] = []; groups[k].push(c); });

  let html = '';
  Object.entries(groups).forEach(([scenario, contracts]) => {
    html += `<div class="verify-scenario-group">
      <div class="verify-scenario-header">${esc(scenario.replace(/-/g, ' '))}</div>`;
    contracts.forEach(c => {
      const pct = c.total_fields ? Math.round((c.verified_count / c.total_fields) * 100) : 0;
      const statusCls = c.status === 'verified' ? 'verified' : (c.unfilled_mandatory > 0 ? 'has-mandatory' : '');
      const displayName = c.filename.replace('.pdf', '').replace(/_/g, ' ');
      html += `<div class="verify-contract-card ${statusCls}" data-cid="${c.id}">
        <div class="verify-contract-top">
          <span class="verify-contract-name" title="${esc(c.filename)}">${esc(displayName)}</span>
          <span class="badge badge-${c.status}">${c.status}</span>
        </div>
        <div class="verify-contract-meta">
          <span class="verify-field-count">${c.total_fields} fields</span>
          <span style="color:var(--green)">${c.filled_fields} filled</span>
          ${c.unfilled_mandatory > 0 ? `<span style="color:var(--red)">${c.unfilled_mandatory} unfilled mandatory</span>` : ''}
        </div>
        <div class="verify-progress-bar"><div class="verify-progress-fill" style="width:${pct}%"></div></div>
        <div class="verify-contract-actions">
          <button class="btn btn-danger btn-sm" onclick="verifyStartGuided(${c.id},'quick')" title="Review mandatory unfilled fields only">Quick Scan</button>
          <button class="btn btn-primary btn-sm" onclick="verifyStartGuided(${c.id},'review')" title="Review all unfilled fields">Standard Review</button>
          <button class="btn btn-muted btn-sm" onclick="verifyStartGuided(${c.id},'full')" title="Review every single field">Full Verification</button>
          <button class="btn btn-success btn-sm" onclick="verifyAllFilled(${c.id})" title="Auto-verify all scanner-detected filled fields">Auto-verify Filled</button>
        </div>
      </div>`;
    });
    html += '</div>';
  });
  document.getElementById('verify-contract-list').innerHTML = html;
}

async function verifyScan() {
  Toast.show('Scanning contracts...', 'info');
  const res = await post('/api/contracts/scan', { target: 'all' });
  if (res._error) return;
  Toast.show(`Scanned ${res.scanned} contracts, ${res.total_fields} fields (${res.unfilled_mandatory} unfilled mandatory)`, 'success');
  renderDocs();
}

// ── Contract Review (Clause-Level Analysis) ──────────────────────────────────

async function runContractReview() {
  if (!currentTxn) return Toast.show('No transaction selected', 'error');
  const btn = document.getElementById('btn-run-review');
  const el = document.getElementById('contract-review-results');
  if (!el) return;

  // Let user pick: upload a file or review from existing uploaded docs
  const hasDocs = _docsData && _docsData.some(d => d.file_path);
  let body = {};

  if (hasDocs) {
    // Find the first doc that has an uploaded file (prefer RPA)
    const rpa = _docsData.find(d => d.file_path && /rpa/i.test(d.code));
    const anyDoc = _docsData.find(d => d.file_path);
    const doc = rpa || anyDoc;
    if (!doc) return Toast.show('No uploaded documents found', 'error');
    body = { doc_code: doc.code };
    el.textContent = '';
    const loadDiv = document.createElement('div');
    loadDiv.className = 'review-loading';
    const spinner = document.createElement('div');
    spinner.className = 'spinner';
    const loadText = document.createElement('span');
    loadText.textContent = 'Reviewing ' + (doc.name || doc.code) + '... This takes 15-30 seconds.';
    loadDiv.append(spinner, loadText);
    el.appendChild(loadDiv);
  } else {
    // No uploaded docs — prompt file upload
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.pdf';
    input.onchange = async () => {
      if (!input.files[0]) return;
      el.textContent = '';
      const loadDiv = document.createElement('div');
      loadDiv.className = 'review-loading';
      const spinner = document.createElement('div');
      spinner.className = 'spinner';
      const loadText = document.createElement('span');
      loadText.textContent = 'Reviewing ' + input.files[0].name + '... This takes 15-30 seconds.';
      loadDiv.append(spinner, loadText);
      el.appendChild(loadDiv);
      btn.disabled = true;
      try {
        const formData = new FormData();
        formData.append('file', input.files[0]);
        const resp = await fetch('/api/txns/' + currentTxn + '/review-contract', {
          method: 'POST', body: formData
        });
        const result = await resp.json();
        if (result.error) {
          el.textContent = '';
          const errDiv = document.createElement('div');
          errDiv.className = 'alert alert-error';
          errDiv.textContent = result.error;
          el.appendChild(errDiv);
        } else {
          renderContractReview(result);
        }
      } catch (e) {
        el.textContent = '';
        const errDiv = document.createElement('div');
        errDiv.className = 'alert alert-error';
        errDiv.textContent = 'Review failed: ' + e.message;
        el.appendChild(errDiv);
      }
      btn.disabled = false;
    };
    input.click();
    return;
  }

  btn.disabled = true;
  try {
    const result = await post('/api/txns/' + currentTxn + '/review-contract', body);
    if (result._error || result.error) {
      el.textContent = '';
      const errDiv = document.createElement('div');
      errDiv.className = 'alert alert-error';
      errDiv.textContent = result.error || 'Review failed';
      el.appendChild(errDiv);
    } else {
      renderContractReview(result);
    }
  } catch (e) {
    el.textContent = '';
    const errDiv = document.createElement('div');
    errDiv.className = 'alert alert-error';
    errDiv.textContent = 'Review failed: ' + e.message;
    el.appendChild(errDiv);
  }
  btn.disabled = false;
}

function _createReviewClauseEl(cl, riskColors) {
  const r = cl.risk || 'GREEN';
  const div = document.createElement('div');
  div.className = 'review-clause review-clause-' + r.toLowerCase();

  const header = document.createElement('div');
  header.className = 'review-clause-header';
  const dot = document.createElement('span');
  dot.className = 'risk-dot';
  dot.style.background = riskColors[r] || '#34c759';
  const areaEl = document.createElement('strong');
  areaEl.textContent = cl.area || '';
  const tag = document.createElement('span');
  tag.className = 'risk-tag risk-tag-' + r.toLowerCase();
  tag.textContent = r;
  header.append(dot, areaEl, tag);

  const finding = document.createElement('div');
  finding.className = 'review-clause-finding';
  finding.textContent = cl.finding || '';

  div.append(header, finding);

  if (cl.standard) {
    const std = document.createElement('div');
    std.className = 'review-clause-standard';
    const stdB = document.createElement('strong');
    stdB.textContent = 'Standard: ';
    std.append(stdB, document.createTextNode(cl.standard));
    div.appendChild(std);
  }
  if (cl.suggestion) {
    const sug = document.createElement('div');
    sug.className = 'review-clause-suggestion';
    const sugB = document.createElement('strong');
    sugB.textContent = 'Suggestion: ';
    sug.append(sugB, document.createTextNode(cl.suggestion));
    div.appendChild(sug);
  }
  return div;
}

function renderContractReview(review) {
  const el = document.getElementById('contract-review-results');
  if (!el) return;
  el.textContent = '';

  const riskColors = { RED: '#ff3b30', YELLOW: '#f5a623', GREEN: '#34c759' };
  const riskLabels = { RED: 'High Risk', YELLOW: 'Review Needed', GREEN: 'Standard' };
  const risk = review.overall_risk || 'GREEN';

  const report = document.createElement('div');
  report.className = 'review-report';

  // Executive summary + overall risk badge
  const summaryHeader = document.createElement('div');
  summaryHeader.className = 'review-summary-header';
  const badge = document.createElement('span');
  badge.className = 'risk-badge risk-' + risk.toLowerCase();
  badge.textContent = riskLabels[risk] || risk;
  const summaryText = document.createElement('p');
  summaryText.className = 'review-executive-summary';
  summaryText.textContent = review.executive_summary || '';
  summaryHeader.append(badge, summaryText);
  report.appendChild(summaryHeader);

  // Clause-by-clause
  const clauses = review.clauses || [];
  if (clauses.length) {
    const clauseSection = document.createElement('div');
    clauseSection.className = 'review-clauses';
    const clauseH = document.createElement('h4');
    clauseH.style.margin = '16px 0 8px';
    clauseH.textContent = 'Clause Analysis (' + clauses.length + ')';
    clauseSection.appendChild(clauseH);
    const sorted = [...clauses].sort((a, b) => {
      const order = { RED: 0, YELLOW: 1, GREEN: 2 };
      return (order[a.risk] || 2) - (order[b.risk] || 2);
    });
    for (const cl of sorted) {
      clauseSection.appendChild(_createReviewClauseEl(cl, riskColors));
    }
    report.appendChild(clauseSection);
  }

  // Interaction warnings
  const interactions = review.interactions || [];
  if (interactions.length) {
    const ixSection = document.createElement('div');
    ixSection.className = 'review-interactions';
    const ixH = document.createElement('h4');
    ixH.style.margin = '16px 0 8px';
    ixH.textContent = 'Clause Interactions (' + interactions.length + ')';
    ixSection.appendChild(ixH);
    for (const ix of interactions) {
      ixSection.appendChild(_createReviewClauseEl({
        area: ix.condition || '',
        risk: ix.risk || 'YELLOW',
        finding: ix.explanation || '',
        suggestion: ix.suggestion || null,
      }, riskColors));
    }
    report.appendChild(ixSection);
  }

  // Missing items
  const missing = review.missing_items || [];
  if (missing.length) {
    const missSection = document.createElement('div');
    missSection.className = 'review-missing';
    const missH = document.createElement('h4');
    missH.style.margin = '16px 0 8px';
    missH.textContent = 'Missing Items (' + missing.length + ')';
    missSection.appendChild(missH);
    const ul = document.createElement('ul');
    ul.className = 'review-missing-list';
    for (const m of missing) {
      const li = document.createElement('li');
      li.textContent = m;
      ul.appendChild(li);
    }
    missSection.appendChild(ul);
    report.appendChild(missSection);
  }

  el.appendChild(report);

  // Update section count
  const countEl = document.getElementById('review-count');
  if (countEl) {
    const reds = clauses.filter(c => c.risk === 'RED').length;
    const yellows = clauses.filter(c => c.risk === 'YELLOW').length;
    countEl.textContent = reds > 0 ? reds + ' high risk' : yellows > 0 ? yellows + ' to review' : 'All clear';
  }
}

async function loadContractReviews() {
  if (!currentTxn) return;
  const el = document.getElementById('contract-review-results');
  if (!el) return;
  el.textContent = '';
  const loadDiv = document.createElement('div');
  loadDiv.className = 'review-loading';
  const spinner = document.createElement('div');
  spinner.className = 'spinner';
  loadDiv.append(spinner, document.createTextNode(' Loading...'));
  el.appendChild(loadDiv);
  const reviews = await get('/api/txns/' + currentTxn + '/contract-reviews');
  if (reviews._error || !reviews.length) {
    el.textContent = '';
    const p = document.createElement('p');
    p.className = 'text-muted';
    p.style.padding = '8px';
    p.textContent = 'No reviews yet.';
    el.appendChild(p);
    return;
  }
  // Show the most recent review
  renderContractReview(reviews[0]);
}

// ── Contract Verification helpers ────────────────────────────────────────────

async function verifyAllFilled(cid) {
  const res = await post(`/api/contracts/${cid}/verify-filled`);
  if (res._error) return;
  Toast.show(`Auto-verified ${res.verified} filled fields`, 'success');
  renderDocs();
}

// ── Guided Verification Agent ────────────────────────────────────────────────

async function verifyStartGuided(cid, mode) {
  _verifyActiveContract = cid;
  _verifyIdx = 0;
  _verifyMode = mode;

  const data = await get(`/api/contracts/${cid}/fields/verify-queue?mode=${mode}`);
  if (data._error) return;

  _verifyFields = data.fields;
  _verifyStats = data.stats;

  if (_verifyFields.length === 0) {
    Toast.show('No fields to review in this mode', 'success');
    return;
  }

  // Hide docs content, show workflow
  const docsVerify = document.getElementById('docs-verify-section');
  if (docsVerify) docsVerify.style.display = 'none';
  // Also try old IDs for backward compat
  const vcl = document.getElementById('verify-contract-list');
  if (vcl) vcl.style.display = 'none';
  const vs = document.getElementById('verify-summary');
  if (vs) vs.style.display = 'none';
  const wf = document.getElementById('verify-workflow');
  wf.style.display = '';

  // Preload first crop image
  const firstField = _verifyFields[0];
  new Image().src = `/api/contracts/${cid}/fields/${firstField.id}/crop?zoom=3&padding=40`;

  // Install keyboard handler
  _verifyKeyHandler = _handleVerifyKeys;
  document.addEventListener('keydown', _verifyKeyHandler);

  renderVerifyStep();
}

function _handleVerifyKeys(e) {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  const f = _verifyFields[_verifyIdx];
  if (!f) return;

  switch (e.key.toLowerCase()) {
    case 'y': case '1': e.preventDefault(); verifyFieldAction(f.id, 'verified'); break;
    case 'n': case 'f': case '2': e.preventDefault(); verifyFieldAction(f.id, 'flagged'); break;
    case 'i': case '3': e.preventDefault(); verifyFieldAction(f.id, 'ignored'); break;
    case 's': case ' ': case 'arrowright': e.preventDefault(); verifySkip(); break;
    case 'arrowleft': e.preventDefault(); verifyPrev(); break;
    case 'escape': case 'q': e.preventDefault(); verifyExitWorkflow(); break;
  }
}

function renderVerifyStep() {
  const wf = document.getElementById('verify-workflow');

  // Complete state
  if (_verifyIdx >= _verifyFields.length) {
    wf.innerHTML = `
      <div class="vg-complete">
        <div class="vg-complete-icon">&#10003;</div>
        <h2>Verification Complete</h2>
        <p>${_verifyFields.length} fields reviewed</p>
        <div class="vg-complete-stats">
          <span>Total: ${_verifyStats.total}</span>
          <span style="color:var(--green)">Verified: ${_verifyStats.verified}</span>
          ${_verifyStats.flagged > 0 ? `<span style="color:var(--red)">Flagged: ${_verifyStats.flagged}</span>` : ''}
        </div>
        <button class="btn btn-primary" onclick="verifyExitWorkflow()" style="margin-top:20px">Done</button>
      </div>`;
    return;
  }

  const f = _verifyFields[_verifyIdx];
  const total = _verifyFields.length;
  const num = _verifyIdx + 1;
  const pct = Math.round((num / total) * 100);

  const catLabel = (f.category || '').replace(/^entry_/, '').toUpperCase();
  const isMandatory = f.mandatory;
  const isFilled = f.is_filled;

  // Detection status
  let detectBadge;
  if (isFilled) {
    detectBadge = '<span class="vg-detect vg-detect-filled">FILLED</span>';
  } else if (isMandatory) {
    detectBadge = '<span class="vg-detect vg-detect-empty-mandatory">EMPTY - MANDATORY</span>';
  } else {
    detectBadge = '<span class="vg-detect vg-detect-empty">EMPTY</span>';
  }

  // Field status if already verified/flagged
  let statusBadge = '';
  if (f.status === 'verified') statusBadge = '<span class="vg-detect" style="background:var(--green);color:#fff">VERIFIED</span>';
  else if (f.status === 'flagged') statusBadge = '<span class="vg-detect" style="background:var(--red);color:#fff">FLAGGED</span>';
  else if (f.status === 'ignored') statusBadge = '<span class="vg-detect" style="background:var(--text-secondary);color:#fff">IGNORED</span>';

  // Mode label
  const modeLabels = { quick: 'Quick Scan', review: 'Standard Review', full: 'Full Verification' };

  // Preload next image
  if (_verifyIdx + 1 < total) {
    const nextF = _verifyFields[_verifyIdx + 1];
    new Image().src = `/api/contracts/${_verifyActiveContract}/fields/${nextF.id}/crop?zoom=3&padding=40`;
  }

  wf.innerHTML = `
    <div class="vg-header">
      <div class="vg-header-left">
        <button class="btn btn-ghost btn-sm" onclick="verifyExitWorkflow()" title="Exit (Esc)">&#x2715; Exit</button>
        <span class="vg-mode-badge">${modeLabels[_verifyMode]}</span>
      </div>
      <div class="vg-header-center">
        <span class="vg-counter">${num} of ${total}</span>
      </div>
      <div class="vg-header-right">
        <span class="vg-page-badge">Page ${f.page}</span>
      </div>
    </div>
    <div class="vg-progress"><div class="vg-progress-fill" style="width:${pct}%"></div></div>

    <div class="vg-body">
      <div class="vg-field-info">
        <div class="vg-field-name">${esc(f.field_name || f.label || 'Unnamed field')}</div>
        <div class="vg-badges">
          <span class="vg-cat-badge vg-cat-${(f.category||'').replace('entry_','')}">${catLabel}</span>
          ${detectBadge}
          ${statusBadge}
        </div>
        ${f.notes ? `<div class="vg-notes">${esc(f.notes)}</div>` : ''}
      </div>

      <div class="vg-crop-container">
        <img src="/api/contracts/${_verifyActiveContract}/fields/${f.id}/crop?zoom=3&padding=40"
             alt="Field ${num}" class="vg-crop-img"
             onerror="this.alt='Crop unavailable'; this.style.minHeight='100px'">
      </div>

      <div class="vg-actions">
        <button class="btn btn-success vg-action-btn" onclick="verifyFieldAction(${f.id},'verified')">
          <span class="vg-action-key">Y</span> Verified
        </button>
        <button class="btn btn-danger vg-action-btn" onclick="verifyFieldAction(${f.id},'flagged')">
          <span class="vg-action-key">F</span> Flag Missing
        </button>
        <button class="btn btn-muted vg-action-btn" onclick="verifyFieldAction(${f.id},'ignored')">
          <span class="vg-action-key">I</span> N/A
        </button>
        <button class="btn btn-ghost vg-action-btn" onclick="verifySkip()">
          <span class="vg-action-key">&rarr;</span> Skip
        </button>
      </div>

      <div class="vg-nav">
        <button class="btn btn-ghost btn-sm" onclick="verifyPrev()" ${_verifyIdx === 0 ? 'disabled' : ''}>
          &larr; Previous
        </button>
        <span class="vg-shortcut-hint">Y=Verify  F=Flag  I=N/A  Space=Skip  Esc=Exit</span>
        <button class="btn btn-ghost btn-sm" onclick="verifySkip()">
          Next &rarr;
        </button>
      </div>
    </div>`;
}

async function verifyFieldAction(fid, status) {
  const res = await post(`/api/contracts/${_verifyActiveContract}/fields/${fid}/verify`, { status });
  if (!res._error) {
    // Update local stats
    if (status === 'verified' || status === 'ignored') _verifyStats.verified++;
    if (status === 'flagged') _verifyStats.flagged++;
    _verifyIdx++;
    renderVerifyStep();
  }
}

function verifySkip() {
  _verifyIdx++;
  renderVerifyStep();
}

function verifyPrev() {
  if (_verifyIdx > 0) {
    _verifyIdx--;
    renderVerifyStep();
  }
}

function verifyExitWorkflow() {
  // Remove keyboard handler
  if (_verifyKeyHandler) {
    document.removeEventListener('keydown', _verifyKeyHandler);
    _verifyKeyHandler = null;
  }
  const wf = document.getElementById('verify-workflow');
  if (wf) wf.style.display = 'none';
  _verifyActiveContract = null;
  _verifyFields = [];
  renderDocs();
}

// ── Modal ───────────────────────────────────────────────────────────────────

// ══════════════════════════════════════════════════════════════════════════════
//  SIDEBAR TOOLS
// ══════════════════════════════════════════════════════════════════════════════

const SidebarTools = (() => {
  // Hidden file input for uploads
  let _fileInput = null;

  const actions = {
    'upload': () => {
      if (!currentTxn) return;
      if (!_fileInput) {
        _fileInput = document.createElement('input');
        _fileInput.type = 'file';
        _fileInput.accept = '.pdf';
        _fileInput.style.display = 'none';
        document.body.appendChild(_fileInput);
        _fileInput.addEventListener('change', async () => {
          const file = _fileInput.files[0];
          if (!file) return;
          await _handleUpload(file);
          _fileInput.value = '';
        });
      }
      _fileInput.click();
    },
    'verify': () => {
      if (!currentTxn) return;
      switchTab('docs');
      setTimeout(() => {
        const btn = document.querySelector('#docs-verify-section .collapsible-toggle');
        if (btn && !btn.classList.contains('open')) toggleCollapsible(btn);
        const sect = document.getElementById('docs-verify-section');
        if (sect) sect.scrollIntoView({ behavior: 'smooth' });
      }, 300);
    },
    'send-sig': () => { if (currentTxn) switchTab('signatures'); },
    'request-docs': () => { if (currentTxn) switchTab('docs'); },
    'add-deadline': () => { if (currentTxn) switchTab('overview'); },
    'add-party': () => { if (currentTxn) switchTab('parties'); },
    'compliance': () => { if (currentTxn) switchTab('compliance'); },
    'report': () => { if (currentTxn) switchTab('overview'); },
  };

  async function _handleUpload(file) {
    Toast.show(`Uploading ${file.name}...`, 'info');
    const form = new FormData();
    form.append('file', file);
    try {
      const resp = await fetch(`/api/txns/${currentTxn}/upload`, { method: 'POST', body: form });
      const data = await resp.json();
      if (!resp.ok) {
        Toast.show(data.error || 'Upload failed', 'error');
        return;
      }
      let msg = `Uploaded ${data.filename}`;
      if (data.fields_detected) msg += ` — ${data.fields_detected} fields detected`;
      if (data.matched_doc) msg += ` — matched to ${data.matched_doc}`;
      Toast.show(msg, 'success');
      // Refresh current tab
      const tab = document.querySelector('.tab.active');
      if (tab) switchTab(tab.dataset.tab);
    } catch (e) {
      Toast.show('Upload failed: ' + e.message, 'error');
    }
  }

  function init() {
    $$('.tool-btn[data-tool]').forEach(btn => {
      btn.addEventListener('click', () => {
        const tool = btn.dataset.tool;
        if (actions[tool]) actions[tool]();
        if (tool !== 'upload') Sidebar.close(); // close on mobile (not for upload — file picker)
      });
    });
  }

  function updateState(hasTxn) {
    $$('.tool-btn[data-tool]').forEach(btn => {
      btn.disabled = !hasTxn;
    });
  }

  return { init, updateState };
})();

// ══════════════════════════════════════════════════════════════════════════════
//  SIDEBAR TIMELINE
// ══════════════════════════════════════════════════════════════════════════════

function updateSidebarTimeline() {
  // Timeline removed per user feedback (#29)
}

// ══════════════════════════════════════════════════════════════════════════════
//  ADDRESS VALIDATION
// ══════════════════════════════════════════════════════════════════════════════

let _addrValidResult = null;

async function validateAddress(address) {
  const el = $('#address-validation');
  if (!el) return;

  el.className = 'address-validation validating';
  el.textContent = 'Validating address...';
  _addrValidResult = null;

  const res = await post('/api/validate-address', { address });

  if (res._error || res.valid === null) {
    el.className = 'address-validation';
    el.innerHTML = '<span style="color:var(--text-secondary)">Could not verify (will still create)</span>';
    return;
  }

  if (res.valid) {
    _addrValidResult = res;
    el.className = 'address-validation valid';
    const matched = res.matched_address;
    const inp = $('#inp-address');

    // Build property detail block
    let details = '';
    if (res.city || res.state || res.zip) {
      details += `<div class="addr-details">`;
      details += `<span>${esc(res.city)}, ${esc(res.state)} ${esc(res.zip)}</span>`;
      if (res.county) details += ` <span class="addr-county">${esc(res.county)} County</span>`;
      details += `</div>`;
    }

    if (inp && inp.value.trim().toLowerCase() !== matched.toLowerCase()) {
      el.innerHTML = `\u2713 Verified &mdash; <span class="addr-suggestion" title="Click to use">${esc(matched)}</span>${details}`;
      el.querySelector('.addr-suggestion').addEventListener('click', () => {
        inp.value = matched;
        el.innerHTML = `\u2713 ${esc(matched)}${details}`;
        el.className = 'address-validation valid';
      });
    } else {
      el.innerHTML = `\u2713 ${esc(matched)}${details}`;
    }
  } else {
    el.className = 'address-validation invalid';
    el.textContent = '\u2717 Address not found \u2014 check street, city, and zip';
  }
}

function openModal() {
  $('#modal-backdrop').style.display = '';
  $('#inp-address').focus();
  const valEl = $('#address-validation');
  if (valEl) { valEl.innerHTML = ''; valEl.className = 'address-validation'; }
  _addrValidResult = null;
  // Default acceptance date to today
  const today = new Date().toISOString().split('T')[0];
  const dateInp = $('#inp-acceptance-date');
  if (dateInp) dateInp.value = today;
}

function closeModal() {
  $('#modal-backdrop').style.display = 'none';
  $('#form-new').reset();
}

async function handleCreate(e) {
  e.preventDefault();
  // Use validated address if available, otherwise use raw input
  let address = $('#inp-address').value;
  if (_addrValidResult && _addrValidResult.matched_address) {
    address = _addrValidResult.matched_address;
  }
  const body = {
    address,
    type: $('#inp-type').value,
    role: $('#inp-role').value,
    brokerage: $('#inp-brokerage').value,
    acceptance_date: $('#inp-acceptance-date').value || '',
  };
  const res = await post('/api/txns', body);
  if (res._error) return;
  if (res.id) {
    closeModal();
    Toast.show('Transaction created', 'success');
    await loadTxns();
    selectTxn(res.id);
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

function _formatCountdown(days) {
  if (days === null || days === undefined) return '';
  if (days < 0) return `${Math.abs(days)}d overdue`;
  if (days <= 3) {
    const hours = days * 24;
    return hours <= 0 ? 'TODAY' : `${hours}h`;
  }
  return `${days}d`;
}
