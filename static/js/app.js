/**
 * FlareCloud Web Dashboard — Frontend Logic
 */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const dom = {
    webhookUrl: $('#webhook-url'),
    bannedWebhook: $('#banned-webhook'),
    unbannedWebhook: $('#unbanned-webhook'),
    threadCount: $('#thread-count'),
    proxyType: $('#proxy-type'),
    autoProxy: $('#auto-proxy'),
    toggleConfig: $('#toggle-config'),
    configGrid: $('#config-grid'),
    comboDropzone: $('#combo-dropzone'),
    comboFile: $('#combo-file'),
    comboInfo: $('#combo-info'),
    proxyDropzone: $('#proxy-dropzone'),
    proxyFile: $('#proxy-file'),
    proxyInfo: $('#proxy-info'),
    btnStart: $('#btn-start'),
    btnStop: $('#btn-stop'),
    btnDownload: $('#btn-download'),
    statusBadge: $('#status-badge'),
    progressSection: $('#progress-section'),
    progressFill: $('#progress-fill'),
    progressText: $('#progress-text'),
    cpmValue: $('#cpm-value'),
    retriesValue: $('#retries-value'),
    errorsValue: $('#errors-value'),
    dashboard: $('#dashboard'),
    livePreview: $('#live-preview'),
    previewTerminal: $('#preview-terminal'),
    logCount: $('#log-count'),
    autoScroll: $('#auto-scroll'),
    btnClearLog: $('#btn-clear-log'),
    statHits: $('#stat-hits'),
    statBads: $('#stat-bads'),
    statSfa: $('#stat-sfa'),
    statMfa: $('#stat-mfa'),
    statTwofa: $('#stat-twofa'),
    statXgp: $('#stat-xgp'),
    statXgpu: $('#stat-xgpu'),
    statOther: $('#stat-other'),
    statValid: $('#stat-valid'),
};

let statusSource = null;
let logSource = null;
let previousValues = {};
let logLineCount = 0;

// ═══════════════════════════════════════════
//  Toast Notifications
// ═══════════════════════════════════════════

function getToastContainer() {
    let c = $('.toast-container');
    if (!c) { c = document.createElement('div'); c.className = 'toast-container'; document.body.appendChild(c); }
    return c;
}

function toast(message, type = 'info') {
    const c = getToastContainer();
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    const icons = { success: '✓', error: '✕', info: 'ℹ' };
    el.innerHTML = `<span>${icons[type] || 'ℹ'}</span><span>${message}</span>`;
    c.appendChild(el);
    setTimeout(() => { el.style.animation = 'toastOut 0.3s ease forwards'; setTimeout(() => el.remove(), 300); }, 4000);
}

// ═══════════════════════════════════════════
//  Config Toggle
// ═══════════════════════════════════════════

dom.toggleConfig.addEventListener('click', () => {
    dom.configGrid.classList.toggle('collapsed');
    dom.toggleConfig.classList.toggle('collapsed');
});

dom.autoProxy.addEventListener('change', () => { if (dom.autoProxy.checked) dom.proxyType.value = '5'; });
dom.proxyType.addEventListener('change', () => { dom.autoProxy.checked = dom.proxyType.value === '5'; });

// ═══════════════════════════════════════════
//  File Upload
// ═══════════════════════════════════════════

function setupDropzone(dropzone, fileInput, uploadUrl, infoEl) {
    dropzone.addEventListener('click', () => fileInput.click());
    ['dragenter', 'dragover'].forEach(evt => {
        dropzone.addEventListener(evt, (e) => { e.preventDefault(); e.stopPropagation(); dropzone.classList.add('dragover'); });
    });
    ['dragleave', 'drop'].forEach(evt => {
        dropzone.addEventListener(evt, (e) => { e.preventDefault(); e.stopPropagation(); dropzone.classList.remove('dragover'); });
    });
    dropzone.addEventListener('drop', (e) => { if (e.dataTransfer.files.length > 0) uploadFile(e.dataTransfer.files[0], uploadUrl, dropzone, infoEl); });
    fileInput.addEventListener('change', () => { if (fileInput.files.length > 0) uploadFile(fileInput.files[0], uploadUrl, dropzone, infoEl); });
}

async function uploadFile(file, url, dropzone, infoEl) {
    const formData = new FormData();
    formData.append('file', file);
    infoEl.textContent = 'Uploading...';
    infoEl.className = 'upload-info';
    try {
        const res = await fetch(url, { method: 'POST', body: formData });
        const data = await res.json();
        if (data.success) {
            dropzone.classList.add('uploaded');
            infoEl.textContent = data.unique !== undefined
                ? `✓ ${data.filename} — ${data.unique} combos (${data.dupes} dupes removed)`
                : `✓ ${data.filename} — ${data.count} proxies`;
            infoEl.classList.add('loaded');
            toast(`${data.filename} loaded`, 'success');
        } else {
            infoEl.textContent = `Error: ${data.error}`;
            toast(data.error, 'error');
        }
    } catch (err) {
        infoEl.textContent = 'Upload failed';
        toast('Upload failed: ' + err.message, 'error');
    }
}

setupDropzone(dom.comboDropzone, dom.comboFile, '/api/upload/combos', dom.comboInfo);
setupDropzone(dom.proxyDropzone, dom.proxyFile, '/api/upload/proxies', dom.proxyInfo);

// ═══════════════════════════════════════════
//  Start / Stop / Download
// ═══════════════════════════════════════════

dom.btnStart.addEventListener('click', async () => {
    const config = {
        threads: parseInt(dom.threadCount.value) || 5,
        proxyType: dom.proxyType.value,
        webhook: dom.webhookUrl.value,
        bannedWebhook: dom.bannedWebhook.value,
        unbannedWebhook: dom.unbannedWebhook.value,
    };
    dom.btnStart.disabled = true;
    dom.btnStop.disabled = false;
    dom.btnDownload.disabled = true;
    dom.btnDownload.classList.remove('ready');

    try {
        const res = await fetch('/api/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config),
        });
        const data = await res.json();
        if (data.success) {
            toast(`Checking started — ${data.total} combos`, 'success');
            setStatus('running', 'Checking...');
            showDashboard();
            startSSE();
            startLogSSE();
        } else {
            toast(data.error, 'error');
            dom.btnStart.disabled = false;
            dom.btnStop.disabled = true;
        }
    } catch (err) {
        toast('Failed to start: ' + err.message, 'error');
        dom.btnStart.disabled = false;
        dom.btnStop.disabled = true;
    }
});

dom.btnStop.addEventListener('click', async () => {
    try {
        const res = await fetch('/api/stop', { method: 'POST' });
        const data = await res.json();
        if (data.success) toast('Stopping checker...', 'info');
    } catch (err) { toast('Failed to stop', 'error'); }
});

dom.btnDownload.addEventListener('click', () => { window.location.href = '/api/download'; });

// ═══════════════════════════════════════════
//  SSE — Stats Stream
// ═══════════════════════════════════════════

function startSSE() {
    if (statusSource) statusSource.close();
    statusSource = new EventSource('/api/status');
    statusSource.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            updateDashboard(data);
            if (data.finished) onFinished();
        } catch (e) {}
    };
    statusSource.onerror = () => {};
}

function updateDashboard(data) {
    animateStat(dom.statHits, 'hits', data.hits);
    animateStat(dom.statBads, 'bad', data.bad);
    animateStat(dom.statSfa, 'sfa', data.sfa);
    animateStat(dom.statMfa, 'mfa', data.mfa);
    animateStat(dom.statTwofa, 'twofa', data.twofa);
    animateStat(dom.statXgp, 'xgp', data.xgp);
    animateStat(dom.statXgpu, 'xgpu', data.xgpu);
    animateStat(dom.statOther, 'other', data.other);
    animateStat(dom.statValid, 'vm', data.vm);
    const pct = data.total > 0 ? ((data.checked / data.total) * 100).toFixed(1) : 0;
    dom.progressFill.style.width = pct + '%';
    dom.progressText.textContent = `${data.checked} / ${data.total}`;
    dom.cpmValue.textContent = data.cpm || 0;
    dom.retriesValue.textContent = data.retries;
    dom.errorsValue.textContent = data.errors;
}

function animateStat(el, key, newValue) {
    const prev = previousValues[key] || 0;
    if (newValue !== prev) {
        el.textContent = newValue;
        el.classList.add('changed');
        setTimeout(() => el.classList.remove('changed'), 300);
        previousValues[key] = newValue;
    }
}

// ═══════════════════════════════════════════
//  SSE — Live Log Stream
// ═══════════════════════════════════════════

function startLogSSE() {
    if (logSource) logSource.close();
    logSource = new EventSource('/api/logs');
    logSource.onmessage = (event) => {
        try {
            const entry = JSON.parse(event.data);
            appendLogLine(entry);
        } catch (e) {}
    };
    logSource.onerror = () => {};
}

const TAG_LABELS = {
    hit: 'HIT', bad: 'BAD', twofa: '2FA', valid: 'MAIL',
    xgp: 'XGP', xgpu: 'XGPU', other: 'OTHER',
    system: 'SYS', error: 'ERR', info: 'LOG'
};

function appendLogLine(entry) {
    // Remove welcome message on first log
    const welcome = dom.previewTerminal.querySelector('.terminal-welcome');
    if (welcome) welcome.remove();

    const line = document.createElement('div');
    line.className = `log-line ${entry.type}`;

    const tag = TAG_LABELS[entry.type] || 'LOG';
    line.innerHTML = `<span class="log-time">${entry.time}</span><span class="log-tag">${tag}</span><span class="log-text">${escapeHtml(entry.text)}</span>`;

    dom.previewTerminal.appendChild(line);
    logLineCount++;
    dom.logCount.textContent = `${logLineCount} lines`;

    // Auto-scroll
    if (dom.autoScroll.checked) {
        dom.previewTerminal.scrollTop = dom.previewTerminal.scrollHeight;
    }

    // Limit visible lines to 500 for performance
    while (dom.previewTerminal.children.length > 500) {
        dom.previewTerminal.removeChild(dom.previewTerminal.firstChild);
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Clear log button
dom.btnClearLog.addEventListener('click', () => {
    dom.previewTerminal.innerHTML = '';
    logLineCount = 0;
    dom.logCount.textContent = '0 lines';
});

// ═══════════════════════════════════════════
//  UI State
// ═══════════════════════════════════════════

function onFinished() {
    if (statusSource) { statusSource.close(); statusSource = null; }
    if (logSource) { logSource.close(); logSource = null; }
    setStatus('finished', 'Finished');
    dom.btnStart.disabled = false;
    dom.btnStop.disabled = true;
    dom.btnDownload.disabled = false;
    dom.btnDownload.classList.add('ready');
    toast('🎉 Checking complete! Download your results.', 'success');
}

function setStatus(state, text) {
    dom.statusBadge.className = 'status-badge ' + state;
    dom.statusBadge.querySelector('.status-text').textContent = text;
}

function showDashboard() {
    dom.progressSection.style.display = 'block';
    dom.dashboard.style.display = 'block';
    dom.livePreview.style.display = 'block';
    previousValues = {};
    logLineCount = 0;
    [dom.statHits, dom.statBads, dom.statSfa, dom.statMfa, dom.statTwofa,
     dom.statXgp, dom.statXgpu, dom.statOther, dom.statValid].forEach(el => el.textContent = '0');
    dom.progressFill.style.width = '0%';
    dom.progressText.textContent = '0 / 0';
    dom.cpmValue.textContent = '0';
    dom.retriesValue.textContent = '0';
    dom.errorsValue.textContent = '0';
    dom.previewTerminal.innerHTML = '<div class="terminal-welcome"><span class="terminal-cursor">▍</span> Waiting for checker output...</div>';
    dom.logCount.textContent = '0 lines';
}
