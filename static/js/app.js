/**
 * yetcloud Web Dashboard — Frontend Logic
 * Cyberpunk Neon Version
 */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const dom = {
    webhookUrl: $('#webhook-url'),
    bannedWebhook: $('#banned-webhook'),
    unbannedWebhook: $('#unbanned-webhook'),
    threadCount: $('#thread-count'),
    proxyType: $('#proxy-type'),
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
    progressText: $('#progress-text'), // This is now the "Checked" hero stat
    cpmValue: $('#cpm-value'),
    errorsValue: $('#errors-value'),
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
let checkingChart = null;
let chartStartTime = null;

// ═══════════════════════════════════════════
//  Chart Initialization
// ═══════════════════════════════════════════

function initChart() {
    const ctx = document.getElementById('checkingChart').getContext('2d');
    
    checkingChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: [],
            datasets: [
                {
                    label: 'VALID',
                    borderColor: '#00f5a0',
                    backgroundColor: 'rgba(0, 245, 160, 0.1)',
                    data: [],
                    borderWidth: 2,
                    pointRadius: 0,
                    fill: true,
                    tension: 0.4
                },
                {
                    label: 'INVALID',
                    borderColor: '#ff0055',
                    backgroundColor: 'rgba(255, 0, 85, 0.1)',
                    data: [],
                    borderWidth: 2,
                    pointRadius: 0,
                    fill: true,
                    tension: 0.4
                },
                {
                    label: '2FA',
                    borderColor: '#f9d423',
                    backgroundColor: 'rgba(249, 212, 35, 0.1)',
                    data: [],
                    borderWidth: 2,
                    pointRadius: 0,
                    fill: true,
                    tension: 0.4
                },
                {
                    label: 'ERRORS',
                    borderColor: '#ff7e5f',
                    backgroundColor: 'rgba(255, 126, 95, 0.1)',
                    data: [],
                    borderWidth: 2,
                    pointRadius: 0,
                    fill: true,
                    tension: 0.4
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    display: true,
                    labels: { color: '#8892b0', font: { size: 10, weight: 'bold' } }
                }
            },
            scales: {
                x: {
                    grid: { color: 'rgba(255, 255, 255, 0.05)' },
                    ticks: { color: '#555', maxTicksLimit: 10 }
                },
                y: {
                    grid: { color: 'rgba(255, 255, 255, 0.05)' },
                    ticks: { color: '#8892b0' },
                    beginAtZero: true
                }
            },
            animations: {
                y: { duration: 0 }
            }
        }
    });
}

function updateChart(data) {
    if (!checkingChart) return;
    
    if (!chartStartTime) chartStartTime = Date.now();
    const elapsed = Math.round((Date.now() - chartStartTime) / 1000);
    
    // Limit data points to show a sliding window
    if (checkingChart.data.labels.length > 60) {
        checkingChart.data.labels.shift();
        checkingChart.data.datasets.forEach(ds => ds.data.shift());
    }

    checkingChart.data.labels.push(elapsed + 's');
    checkingChart.data.datasets[0].data.push(data.hits);
    checkingChart.data.datasets[1].data.push(data.bad);
    checkingChart.data.datasets[2].data.push(data.mfa + data.twofa);
    checkingChart.data.datasets[3].data.push(data.errors);
    
    checkingChart.update('none'); // Update without animation for performance
}

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
    try {
        const res = await fetch(url, { method: 'POST', body: formData });
        const data = await res.json();
        if (data.success) {
            dropzone.classList.add('uploaded');
            infoEl.textContent = data.unique !== undefined
                ? `${data.filename} — ${data.unique} combos`
                : `${data.filename} — ${data.count} proxies`;
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
        threads: parseInt(dom.threadCount.value) || 10,
        proxyType: dom.proxyType.value,
        webhook: dom.webhookUrl.value,
        bannedWebhook: dom.bannedWebhook.value,
        unbannedWebhook: dom.unbannedWebhook.value,
    };
    
    dom.btnStart.disabled = true;
    dom.btnStop.disabled = false;
    dom.btnDownload.disabled = true;

    try {
        const res = await fetch('/api/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config),
        });
        const data = await res.json();
        if (data.success) {
            toast(`Started — ${data.total} combos`, 'success');
            setStatus('RUNNING', '#00f5a0');
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
        if (data.success) toast('Stopping...', 'info');
    } catch (err) { toast('Failed to stop', 'error'); }
});

dom.btnDownload.addEventListener('click', () => { window.location.href = '/api/download'; });

// ═══════════════════════════════════════════
//  SSE Streams
// ═══════════════════════════════════════════

function startSSE() {
    if (statusSource) statusSource.close();
    statusSource = new EventSource('/api/status');
    statusSource.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            updateDashboard(data);
            updateChart(data);
            if (data.finished) onFinished();
        } catch (e) {}
    };
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
    
    dom.progressText.textContent = data.checked;
    dom.cpmValue.textContent = data.cpm || 0;
    dom.errorsValue.textContent = data.errors;

    // If we receive data that indicates it is running, update UI state
    if (data.running && dom.btnStart.disabled === false) {
        dom.btnStart.disabled = true;
        dom.btnStop.disabled = false;
        setStatus('RUNNING', '#00f5a0');
    }

    // Enable download if finished
    if (data.finished) {
        dom.btnDownload.disabled = false;
        dom.btnDownload.classList.add('ready');
        if (dom.btnStart.disabled) {
             dom.btnStart.disabled = false;
             dom.btnStop.disabled = true;
             setStatus('FINISHED', 'var(--neon-blue)');
        }
    }
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

function startLogSSE() {
    if (logSource) logSource.close();
    logSource = new EventSource('/api/logs');
    logSource.onmessage = (event) => {
        try {
            const entry = JSON.parse(event.data);
            appendLogLine(entry);
        } catch (e) {}
    };
}

const TAG_COLORS = {
    hit: '#00f5a0', bad: '#ff0055', twofa: '#f9d423', valid: '#ff00d4',
    xgp: '#00d2ff', xgpu: '#f9d423', other: '#ff7e5f',
    system: '#9d50bb', error: '#ff0055', info: '#8892b0'
};

function appendLogLine(entry) {
    const line = document.createElement('div');
    line.className = `log-line`;
    
    const color = TAG_COLORS[entry.type] || '#8892b0';
    const tag = (entry.type || 'LOG').toUpperCase();
    
    line.innerHTML = `
        <span class="log-time">[${entry.time}]</span>
        <span class="log-tag" style="color: ${color}">[${tag}]</span>
        <span class="log-text">${escapeHtml(entry.text)}</span>
    `;

    dom.previewTerminal.appendChild(line);
    logLineCount++;
    dom.logCount.textContent = `${logLineCount} Lines`;

    dom.previewTerminal.scrollTop = dom.previewTerminal.scrollHeight;

    while (dom.previewTerminal.children.length > 200) {
        dom.previewTerminal.removeChild(dom.previewTerminal.firstChild);
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

dom.btnClearLog.addEventListener('click', () => {
    dom.previewTerminal.innerHTML = '';
    logLineCount = 0;
    dom.logCount.textContent = '0 Lines';
});

// ═══════════════════════════════════════════
//  UI State Management
// ═══════════════════════════════════════════

function onFinished() {
    if (statusSource) { statusSource.close(); statusSource = null; }
    if (logSource) { logSource.close(); logSource = null; }
    setStatus('FINISHED', 'var(--neon-blue)');
    dom.btnStart.disabled = false;
    dom.btnStop.disabled = true;
    dom.btnDownload.disabled = false;
    toast('🎉 Complete!', 'success');
}

function setStatus(text, color) {
    dom.statusBadge.querySelector('.status-text').textContent = text;
    dom.statusBadge.querySelector('.status-text').style.color = color;
}

function showDashboard() {
    previousValues = {};
    logLineCount = 0;
    chartStartTime = null;
    if (checkingChart) {
        checkingChart.data.labels = [];
        checkingChart.data.datasets.forEach(ds => ds.data = []);
        checkingChart.update();
    }
    
    [dom.statHits, dom.statBads, dom.statSfa, dom.statMfa, dom.statTwofa,
     dom.statXgp, dom.statXgpu, dom.statOther, dom.statValid].forEach(el => el.textContent = '0');
    dom.progressText.textContent = '0';
    dom.cpmValue.textContent = '0';
    dom.errorsValue.textContent = '0';
    dom.previewTerminal.innerHTML = '';
    dom.logCount.textContent = '0 Lines';
}

// Initial Setup
document.addEventListener('DOMContentLoaded', () => {
    initChart();
    // Auto-connect to catch updates if already running
    startSSE();
    startLogSSE();
});
