// JavaScript for Screenshot AI desktop interface

const pdfListUl = document.getElementById('pdf-list');
const pdfInput = document.getElementById('pdf-input');
const modelSelect = document.getElementById('model-select');
const promptSelect = document.getElementById('prompt-select');
const promptContentTextarea = document.getElementById('prompt-content');
const newPromptNameInput = document.getElementById('new-prompt-name');
const captureModeSelect = document.getElementById('capture-mode-select');
const hotkeyInput = document.getElementById('hotkey-input');
const hotkeyRecordingIndicator = document.getElementById('hotkey-recording-indicator');
const totalTokensSpan = document.getElementById('total-tokens');
const todayTokensSpan = document.getElementById('today-tokens');
const logOutputPre = document.getElementById('log-output');
const logLevelSelect = document.getElementById('log-level-select');
const logCountBadge = document.getElementById('log-count-badge');
const startStopButton = document.getElementById('start-stop-button');
const stateStatusDiv = document.getElementById('state-status');
const historyListDiv = document.getElementById('history-list');
const historyCountSpan = document.getElementById('history-count');
const apiKeysListDiv = document.getElementById('api-keys-list');
const keysCountSpan = document.getElementById('keys-count');
const newApiKeyInput = document.getElementById('new-api-key-input');
const imageModal = document.getElementById('image-modal');
const modalImg = document.getElementById('modal-img');
const modalOpenExternalBtn = document.getElementById('modal-open-external-btn');

let currentModalImagePath = '';

let currentPdfSources = [];
let queryHistory = [];
let currentApiKeys = [];
let uiState = 'configuring';
let isRecordingHotkey = false;

// Log level filtering state & threshold map
let allLogs = [];
let currentLogLevel = 'INFO';
const LOG_LEVEL_MAP = {
    'DEBUG': 10,
    'INFO': 20,
    'WARNING': 30,
    'ERROR': 40,
    'CRITICAL': 50,
};

// --- ANSI to HTML mapping ---
const ansiColorMap = {
    30: 'ansi-black',
    31: 'ansi-red',
    32: 'ansi-green',
    33: 'ansi-yellow',
    34: 'ansi-blue',
    35: 'ansi-magenta',
    36: 'ansi-cyan',
    37: 'ansi-white',
    90: 'ansi-grey',
    91: 'ansi-bright-red',
    92: 'ansi-bright-green',
    93: 'ansi-bright-yellow',
    94: 'ansi-bright-blue',
    95: 'ansi-bright-magenta',
    96: 'ansi-bright-cyan',
    97: 'ansi-bright-white',
    1: 'ansi-bold',
    4: 'ansi-underline',
};

function escapeHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function ansiToHtml(text) {
    const ansiRegex = /\x1b\[((?:\d|;)*?)m/g;
    let html = '';
    let lastIndex = 0;
    const openSpans = [];
    let isBold = false;

    let match;
    while ((match = ansiRegex.exec(text)) !== null) {
        html += escapeHtml(text.slice(lastIndex, match.index));
        lastIndex = ansiRegex.lastIndex;

        const params = match[1].split(';').map(Number);
        for (const p of params) {
            if (p === 0) {
                if (isBold) {
                    html += '</strong>';
                    isBold = false;
                }
                while (openSpans.length) {
                    html += '</span>';
                    openSpans.pop();
                }
            } else if (p === 1) {
                if (!isBold) {
                    html += '<strong>';
                    isBold = true;
                }
            } else if (ansiColorMap[p] && ansiColorMap[p] !== 'bold') {
                const cls = ansiColorMap[p];
                html += `<span class="${cls}">`;
                openSpans.push(cls);
            }
        }
    }

    html += escapeHtml(text.slice(lastIndex));
    if (isBold) html += '</strong>';
    while (openSpans.length) {
        html += '</span>';
        openSpans.pop();
    }
    return html;
}

// --- Initial State Synchronization ---
function setInitialState(state) {
    if (!state) return;
    console.log("JS: Received initial state:", state);

    currentPdfSources = state.pdfSources || [];
    populatePdfList();

    populateModelSelect(state.availableModels || [], state.selectedModel);
    populatePromptSelect(state.availablePrompts || [], state.selectedPrompt, state.promptContent);
    populateCaptureMode(state.captureMode || 'cursor_monitor');
    populateHotkey(state.currentHotkey || 'ctrl+shift+q');

    const total = (state.tokenUsage && state.tokenUsage.total) || 0;
    const daily = (state.tokenUsage && state.tokenUsage.daily) || {};
    const today = daily[getTodayDateString()] || 0;
    updateTokenDisplay(total, today);

    queryHistory = state.history || [];
    renderHistory();

    currentApiKeys = state.apiKeys || [];
    renderApiKeys(currentApiKeys);

    if (state.logLevel) {
        populateLogLevel(state.logLevel);
    }

    setUIState(state.isListening ? 'listening' : 'configuring');
}

// Two-way initialization with pywebview
window.addEventListener('pywebviewready', () => {
    console.log("JS: pywebview is ready, requesting initial state…");
    if (window.pywebview && window.pywebview.api && window.pywebview.api.get_initial_state) {
        window.pywebview.api.get_initial_state().then(state => {
            if (state) {
                setInitialState(state);
            }
        }).catch(err => {
            console.error("JS: Error requesting initial state:", err);
        });
    }
});

// --- UI State Management ---
function setUIState(state) {
    uiState = state;
    document.body.classList.remove('configuring-state', 'listening-state');
    document.body.classList.add(state + '-state');

    const hotkeyDisplay = (hotkeyInput.value || 'CTRL+SHIFT+Q').toUpperCase();

    if (state === 'configuring') {
        stateStatusDiv.innerHTML = '<i class="fas fa-gear mr-1"></i> Configuring';
        stateStatusDiv.className = 'status-badge configuring';

        startStopButton.innerHTML = '<i class="fas fa-play mr-2"></i> Start Listening';
        startStopButton.className = 'px-8 py-2.5 bg-emerald-600 text-white rounded-xl text-sm font-semibold hover:bg-emerald-700 shadow-md transition flex items-center gap-2';
    } else {
        stateStatusDiv.innerHTML = `<i class="fas fa-keyboard mr-1"></i> Listening Active (${hotkeyDisplay})`;
        stateStatusDiv.className = 'status-badge listening';

        startStopButton.innerHTML = '<i class="fas fa-stop mr-2"></i> Stop Listening';
        startStopButton.className = 'px-8 py-2.5 bg-red-600 text-white rounded-xl text-sm font-semibold hover:bg-red-700 shadow-md transition flex items-center gap-2';
    }

    // Disable configuration controls while listening
    const disableSelectors = '#pdf-section input, #pdf-section button, #model-select, #prompt-select, #prompt-content, #new-prompt-name, #new-prompt-btn, #save-prompt-btn, #capture-mode-select, #hotkey-input, #apply-hotkey-btn, #new-api-key-input, #tab-content-keys button';
    document.querySelectorAll(disableSelectors).forEach(input => {
        input.disabled = (state === 'listening');
    });
}

// --- Prompt Preset & Editor ---
function populatePromptSelect(prompts, selectedPrompt, initialContent) {
    promptSelect.innerHTML = '';
    const promptList = (prompts && prompts.length > 0) ? prompts : ['default_prompt_ultra.txt', 'default_prompt.txt'];

    promptList.forEach(p => {
        const option = document.createElement('option');
        option.value = p;
        option.textContent = p.replace('.txt', '').replace(/_/g, ' ');
        if (p === selectedPrompt) {
            option.selected = true;
        }
        promptSelect.appendChild(option);
    });

    const activePrompt = selectedPrompt || promptList[0];
    newPromptNameInput.value = activePrompt;

    if (initialContent !== undefined && initialContent !== null) {
        promptContentTextarea.value = initialContent;
    } else if (window.pywebview && window.pywebview.api && window.pywebview.api.get_prompt_content) {
        window.pywebview.api.get_prompt_content(activePrompt).then(content => {
            promptContentTextarea.value = content || '';
        });
    }
}

promptSelect.addEventListener('change', () => {
    const chosen = promptSelect.value;
    console.log("JS: Selected prompt preset:", chosen);
    newPromptNameInput.value = chosen;

    if (window.pywebview && window.pywebview.api) {
        window.pywebview.api.set_selected_prompt(chosen);
        if (window.pywebview.api.get_prompt_content) {
            window.pywebview.api.get_prompt_content(chosen).then(content => {
                promptContentTextarea.value = content || '';
            });
        }
    }
});

function savePrompt() {
    let name = newPromptNameInput.value.trim() || promptSelect.value || 'custom_prompt.txt';
    if (!name.endsWith('.txt')) {
        name += '.txt';
    }
    const content = promptContentTextarea.value;

    if (window.pywebview && window.pywebview.api && window.pywebview.api.save_prompt) {
        window.pywebview.api.save_prompt(name, content).then(success => {
            if (success) {
                appendLog(`\x1b[92mINFO: \x1b[0mPrompt preset saved successfully: ${name}\n`);
                let exists = false;
                for (let i = 0; i < promptSelect.options.length; i++) {
                    if (promptSelect.options[i].value === name) {
                        exists = true;
                        promptSelect.selectedIndex = i;
                        break;
                    }
                }
                if (!exists) {
                    const opt = document.createElement('option');
                    opt.value = name;
                    opt.textContent = name.replace('.txt', '').replace(/_/g, ' ');
                    opt.selected = true;
                    promptSelect.appendChild(opt);
                }
                window.pywebview.api.set_selected_prompt(name);
            } else {
                appendLog(`\x1b[91mERROR: \x1b[0mFailed to save prompt preset: ${name}\n`);
            }
        }).catch(err => {
            appendLog(`\x1b[91mERROR: \x1b[0mError saving prompt: ${err}\n`);
        });
    }
}

function newPrompt() {
    newPromptNameInput.value = 'custom_prompt.txt';
    promptContentTextarea.value = '';
    promptContentTextarea.focus();
    appendLog("\x1b[94mINFO: \x1b[0mCreate a new preset by writing instructions above and clicking 'Save Prompt'.\n");
}

// --- Model Selection ---
function populateModelSelect(models, selectedModel) {
    modelSelect.innerHTML = '';
    const modelList = (models && models.length > 0) ? models : (selectedModel ? [selectedModel] : []);

    modelList.forEach(model => {
        const option = document.createElement('option');
        option.value = model;
        option.textContent = model.replace('models/', '');
        if (model === selectedModel) {
            option.selected = true;
        }
        modelSelect.appendChild(option);
    });
}

modelSelect.addEventListener('change', () => {
    const chosen = modelSelect.value;
    if (window.pywebview && window.pywebview.api) {
        window.pywebview.api.set_selected_model(chosen);
    }
});

// --- Capture Mode ---
function populateCaptureMode(mode) {
    if (!captureModeSelect) return;
    const targetMode = mode || 'cursor_monitor';
    captureModeSelect.value = targetMode;
    for (let i = 0; i < captureModeSelect.options.length; i++) {
        if (captureModeSelect.options[i].value === targetMode) {
            captureModeSelect.selectedIndex = i;
            break;
        }
    }
}

captureModeSelect.addEventListener('change', () => {
    const chosen = captureModeSelect.value;
    console.log("JS: Selected capture mode:", chosen);
    if (window.pywebview && window.pywebview.api && window.pywebview.api.set_capture_mode) {
        window.pywebview.api.set_capture_mode(chosen).then(() => {
            appendLog(`\x1b[92mINFO: \x1b[0mCapture mode set to: ${chosen}\n`);
        }).catch(err => {
            console.error("Error setting capture mode:", err);
            appendLog(`\x1b[91mERROR: \x1b[0mFailed to set capture mode: ${err}\n`);
        });
    }
});

// --- Interactive Hotkey Recorder ---
function populateHotkey(hotkey) {
    if (hotkeyInput) {
        hotkeyInput.value = hotkey;
    }
}

function applyHotkey(customVal) {
    const val = (customVal || hotkeyInput.value || '').trim().toLowerCase();
    if (!val) return;
    if (window.pywebview && window.pywebview.api && window.pywebview.api.set_hotkey) {
        window.pywebview.api.set_hotkey(val).then(success => {
            if (success) {
                appendLog(`\x1b[92mSUCCESS: \x1b[0mHotkey successfully registered: ${val.toUpperCase()}\n`);
                hotkeyInput.value = val;
                if (uiState === 'listening') {
                    stateStatusDiv.innerHTML = `<i class="fas fa-keyboard mr-1"></i> Listening Active (${val.toUpperCase()})`;
                }
            } else {
                appendLog(`\x1b[91mERROR: \x1b[0mFailed to set hotkey: ${val}\n`);
            }
        }).catch(err => {
            appendLog(`\x1b[91mERROR: \x1b[0mError setting hotkey: ${err}\n`);
        });
    }
}

// Start hotkey recording on focus/click
hotkeyInput.addEventListener('focus', () => {
    if (uiState === 'listening') return;
    isRecordingHotkey = true;
    hotkeyRecordingIndicator.classList.remove('hidden');
    hotkeyInput.classList.add('ring-2', 'ring-indigo-400', 'bg-indigo-50/50');
    hotkeyInput.placeholder = 'Press keys…';
});

hotkeyInput.addEventListener('blur', () => {
    isRecordingHotkey = false;
    hotkeyRecordingIndicator.classList.add('hidden');
    hotkeyInput.classList.remove('ring-2', 'ring-indigo-400', 'bg-indigo-50/50');
    hotkeyInput.placeholder = 'Click to record…';
});

// Capture keystrokes dynamically
hotkeyInput.addEventListener('keydown', (e) => {
    if (!isRecordingHotkey || uiState === 'listening') return;

    e.preventDefault();
    e.stopPropagation();

    if (e.key === 'Escape') {
        hotkeyInput.blur();
        return;
    }

    // Ignore bare modifier keys
    if (['Control', 'Shift', 'Alt', 'Meta'].includes(e.key)) {
        const modParts = [];
        if (e.ctrlKey) modParts.push('CTRL');
        if (e.altKey) modParts.push('ALT');
        if (e.shiftKey) modParts.push('SHIFT');
        if (e.metaKey) modParts.push('WIN');
        hotkeyInput.value = modParts.join('+') + ' + …';
        return;
    }

    // Combine modifiers + key
    const parts = [];
    if (e.ctrlKey) parts.push('ctrl');
    if (e.altKey) parts.push('alt');
    if (e.shiftKey) parts.push('shift');
    if (e.metaKey) parts.push('win');

    // Normalize key
    let k = e.key.toLowerCase();
    const keyMap = {
        ' ': 'space',
        'arrowup': 'up',
        'arrowdown': 'down',
        'arrowleft': 'left',
        'arrowright': 'right',
    };
    if (keyMap[k]) {
        k = keyMap[k];
    }

    parts.push(k);
    const hotkeyStr = parts.join('+');

    hotkeyInput.value = hotkeyStr;
    hotkeyInput.blur();

    // Automatically apply the new hotkey
    applyHotkey(hotkeyStr);
});

// --- PDF List Management ---
function populatePdfList() {
    pdfListUl.innerHTML = '';
    if (!currentPdfSources.length) {
        const emptyLi = document.createElement('li');
        emptyLi.className = 'text-gray-400 italic text-xs py-1';
        emptyLi.textContent = 'No PDF context sources added.';
        pdfListUl.appendChild(emptyLi);
        return;
    }

    currentPdfSources.forEach((source, index) => {
        const li = document.createElement('li');
        li.textContent = source;
        const removeBtn = document.createElement('button');
        removeBtn.innerHTML = '<i class="fas fa-times"></i>';
        removeBtn.className = 'remove-btn';
        removeBtn.title = 'Remove';
        removeBtn.onclick = () => removePdfSource(index);
        li.appendChild(removeBtn);
        pdfListUl.appendChild(li);
    });
}

function addPdfSource() {
    const source = pdfInput.value.trim();
    if (source && !currentPdfSources.includes(source)) {
        currentPdfSources.push(source);
        populatePdfList();
        pdfInput.value = '';
        if (window.pywebview && window.pywebview.api) {
            window.pywebview.api.set_pdf_sources(currentPdfSources);
        }
    }
}

function removePdfSource(index) {
    if (index >= 0 && index < currentPdfSources.length) {
        currentPdfSources.splice(index, 1);
        populatePdfList();
        if (window.pywebview && window.pywebview.api) {
            window.pywebview.api.set_pdf_sources(currentPdfSources);
        }
    }
}

pdfInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') {
        e.preventDefault();
        addPdfSource();
    }
});

function browsePdfFile() {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.browse_pdf) {
        window.pywebview.api.browse_pdf().then(filePath => {
            if (filePath) {
                pdfInput.value = filePath;
                addPdfSource();
            }
        });
    }
}

// --- Token Usage Display ---
function updateTokenDisplay(total, today) {
    if (totalTokensSpan) totalTokensSpan.textContent = Number(total || 0).toLocaleString();
    if (todayTokensSpan) todayTokensSpan.textContent = Number(today || 0).toLocaleString();
}

function getTodayDateString() {
    const today = new Date();
    const year = today.getFullYear();
    const month = (today.getMonth() + 1).toString().padStart(2, '0');
    const day = today.getDate().toString().padStart(2, '0');
    return `${year}-${month}-${day}`;
}

// --- Tabs Management ---
function switchTab(tab) {
    const logsBtn = document.getElementById('tab-btn-logs');
    const histBtn = document.getElementById('tab-btn-history');
    const keysBtn = document.getElementById('tab-btn-keys');
    const respBtn = document.getElementById('tab-btn-response-times');
    const logsContent = document.getElementById('tab-content-logs');
    const histContent = document.getElementById('tab-content-history');
    const keysContent = document.getElementById('tab-content-keys');
    const respContent = document.getElementById('tab-content-response-times');

    // Reset all tabs
    [logsBtn, histBtn, keysBtn, respBtn].forEach(btn => {
        if (btn) btn.className = 'px-4 py-2 border-b-2 border-transparent text-gray-500 hover:text-gray-700 flex items-center gap-2 transition';
    });
    if (logsContent) logsContent.classList.add('hidden');
    if (histContent) histContent.classList.add('hidden');
    if (keysContent) keysContent.classList.add('hidden');
    if (respContent) respContent.classList.add('hidden');

    if (tab === 'logs') {
        if (logsBtn) logsBtn.className = 'px-4 py-2 border-b-2 border-indigo-600 text-indigo-600 flex items-center gap-2 transition';
        if (logsContent) logsContent.classList.remove('hidden');
    } else if (tab === 'history') {
        if (histBtn) histBtn.className = 'px-4 py-2 border-b-2 border-indigo-600 text-indigo-600 flex items-center gap-2 transition';
        if (histContent) histContent.classList.remove('hidden');
    } else if (tab === 'keys') {
        if (keysBtn) keysBtn.className = 'px-4 py-2 border-b-2 border-indigo-600 text-indigo-600 flex items-center gap-2 transition';
        if (keysContent) keysContent.classList.remove('hidden');
        refreshApiKeys();
    } else if (tab === 'response-times') {
        if (respBtn) respBtn.className = 'px-4 py-2 border-b-2 border-indigo-600 text-indigo-600 flex items-center gap-2 transition';
        if (respContent) respContent.classList.remove('hidden');
        if (responseTimesDirty || !responseTimesData) {
            loadResponseTimes();
        } else {
            renderResponseTimes();
        }
    }
}

// --- Response Times & Performance Analytics (Lazy-loaded) ---
let responseTimesData = null;
let responseTimesDirty = true;
let currentResponseTimePeriod = 'today'; // 'today', 'week', 'month', 'all'
let isResponseTimesLoading = false;

function setResponseTimePeriod(period) {
    currentResponseTimePeriod = period;

    const periods = ['today', 'week', 'month', 'all'];
    periods.forEach(p => {
        const btn = document.getElementById(`resp-period-${p}`);
        if (!btn) return;
        if (p === period) {
            btn.className = 'px-3 py-1 rounded-md text-xs font-semibold bg-white text-indigo-700 shadow-sm transition';
        } else {
            btn.className = 'px-3 py-1 rounded-md text-xs font-semibold text-gray-600 hover:text-gray-900 transition';
        }
    });

    renderResponseTimes();
}

async function loadResponseTimes(force = false) {
    if (isResponseTimesLoading) return;
    isResponseTimesLoading = true;

    const refreshIcon = document.getElementById('resp-refresh-icon');
    if (refreshIcon) refreshIcon.classList.add('fa-spin');

    try {
        if (window.pywebview && window.pywebview.api && window.pywebview.api.get_response_time_stats) {
            const data = await window.pywebview.api.get_response_time_stats();
            responseTimesData = data;
            responseTimesDirty = false;
            renderResponseTimes();
        }
    } catch (err) {
        console.error('Failed to load response time stats:', err);
    } finally {
        isResponseTimesLoading = false;
        if (refreshIcon) refreshIcon.classList.remove('fa-spin');
    }
}

function renderResponseTimes() {
    if (!responseTimesData) return;

    const periodData = responseTimesData[currentResponseTimePeriod] || {
        summary: { count: 0, avg_formatted: '—', median_formatted: '—', min_formatted: '—', max_formatted: '—' },
        by_model: {}
    };

    const summary = periodData.summary || { count: 0, avg_formatted: '—', median_formatted: '—', min_formatted: '—', max_formatted: '—' };
    const byModel = periodData.by_model || {};

    // 1. Update KPI cards
    const kpiAvg = document.getElementById('resp-kpi-avg');
    const kpiMedian = document.getElementById('resp-kpi-median');
    const kpiRange = document.getElementById('resp-kpi-range');
    const kpiCount = document.getElementById('resp-kpi-count');
    const updatedAtSpan = document.getElementById('resp-updated-at');

    if (kpiAvg) kpiAvg.textContent = summary.avg_formatted || '—';
    if (kpiMedian) kpiMedian.textContent = summary.median_formatted || '—';
    if (kpiRange) {
        if (summary.count > 0) {
            kpiRange.textContent = `${summary.min_formatted} / ${summary.max_formatted}`;
        } else {
            kpiRange.textContent = '—';
        }
    }
    if (kpiCount) kpiCount.textContent = (summary.count || 0).toLocaleString();
    if (updatedAtSpan) {
        updatedAtSpan.textContent = `Generated: ${responseTimesData.generated_at || 'just now'}`;
    }

    // 2. Update Breakdown period label
    const periodNames = {
        'today': 'Today',
        'week': 'This Week (7 days)',
        'month': 'This Month (30 days)',
        'all': 'All Time'
    };
    const periodLabel = document.getElementById('resp-breakdown-period-label');
    if (periodLabel) periodLabel.textContent = periodNames[currentResponseTimePeriod] || currentResponseTimePeriod;

    // 3. Render Models Breakdown Table
    const tbody = document.getElementById('resp-models-tbody');
    const modelKeys = Object.keys(byModel);
    const countLabel = document.getElementById('resp-models-count-label');
    if (countLabel) countLabel.textContent = `${modelKeys.length} model${modelKeys.length === 1 ? '' : 's'}`;

    if (tbody) {
        tbody.innerHTML = '';
        if (modelKeys.length === 0) {
            tbody.innerHTML = `<tr><td colspan="5" class="py-6 text-center text-gray-400 text-xs">No queries recorded for this period yet.</td></tr>`;
        } else {
            modelKeys.forEach(modelName => {
                const mStats = byModel[modelName];
                const cleanModel = modelName.replace(/^models\//, '');
                const pct = summary.count > 0 ? Math.round((mStats.count / summary.count) * 100) : 0;

                const tr = document.createElement('tr');
                tr.className = 'hover:bg-slate-50/80 transition';
                tr.innerHTML = `
                    <td class="py-2.5 px-3 font-medium text-gray-900">
                        <div class="flex items-center gap-1.5">
                            <span class="w-2 h-2 rounded-full bg-indigo-500"></span>
                            <span class="font-mono text-xs font-semibold">${escapeHtml(cleanModel)}</span>
                        </div>
                    </td>
                    <td class="py-2.5 px-3 text-center">
                        <span class="inline-block px-2 py-0.5 rounded-full text-[11px] font-semibold bg-gray-100 text-gray-700">
                            ${mStats.count} <span class="text-gray-400 font-normal">(${pct}%)</span>
                        </span>
                    </td>
                    <td class="py-2.5 px-3 text-right font-mono font-bold text-indigo-600">${mStats.avg_formatted}</td>
                    <td class="py-2.5 px-3 text-right font-mono font-bold text-emerald-600">${mStats.median_formatted}</td>
                    <td class="py-2.5 px-3 text-right font-mono text-gray-500">${mStats.min_formatted} / ${mStats.max_formatted}</td>
                `;
                tbody.appendChild(tr);
            });
        }
    }

    // 4. Render All Periods Comparison Table
    const compTbody = document.getElementById('resp-comparison-tbody');
    if (compTbody) {
        compTbody.innerHTML = '';
        const order = [
            { key: 'today', label: 'Today (Napi)', icon: 'fa-calendar-day', color: 'text-indigo-600' },
            { key: 'week', label: 'This Week (Heti)', icon: 'fa-calendar-week', color: 'text-sky-600' },
            { key: 'month', label: 'This Month (Havi)', icon: 'fa-calendar', color: 'text-purple-600' },
            { key: 'all', label: 'All Time (Összesített)', icon: 'fa-infinity', color: 'text-emerald-600' },
        ];

        order.forEach(row => {
            const rowData = responseTimesData[row.key] ? responseTimesData[row.key].summary : null;
            const rCount = rowData ? rowData.count : 0;
            const rAvg = rowData && rCount > 0 ? rowData.avg_formatted : '—';
            const rMedian = rowData && rCount > 0 ? rowData.median_formatted : '—';
            const rMinMax = rowData && rCount > 0 ? `${rowData.min_formatted} / ${rowData.max_formatted}` : '—';

            const tr = document.createElement('tr');
            const isSelected = row.key === currentResponseTimePeriod;
            tr.className = isSelected ? 'bg-indigo-50/50 font-semibold' : 'hover:bg-slate-50/80 transition';
            tr.innerHTML = `
                <td class="py-2 px-3">
                    <span class="flex items-center gap-1.5 ${row.color}">
                        <i class="fas ${row.icon} text-xs"></i>
                        <span>${row.label}</span>
                        ${isSelected ? '<span class="text-[10px] text-indigo-500 font-normal ml-1">(active)</span>' : ''}
                    </span>
                </td>
                <td class="py-2 px-3 text-center">
                    <span class="inline-block px-2 py-0.5 rounded-full text-[11px] font-semibold bg-gray-100 text-gray-700">${rCount}</span>
                </td>
                <td class="py-2 px-3 text-right font-mono ${rCount > 0 ? 'text-indigo-600 font-bold' : 'text-gray-400'}">${rAvg}</td>
                <td class="py-2 px-3 text-right font-mono ${rCount > 0 ? 'text-emerald-600 font-bold' : 'text-gray-400'}">${rMedian}</td>
                <td class="py-2 px-3 text-right font-mono text-gray-500">${rMinMax}</td>
            `;
            compTbody.appendChild(tr);
        });
    }
}

// --- Lightbox Modal (Zoomable & Pannable) ---
let modalZoom = 1.0;
let modalFitZoom = 1.0;
let modalPanX = 0;
let modalPanY = 0;
let isModalDragging = false;
let modalDragStartX = 0;
let modalDragStartY = 0;
let modalDragInitialPanX = 0;
let modalDragInitialPanY = 0;
let modalIsFit = true;

function onModalImageLoaded() {
    if (!modalImg || !modalImg.naturalWidth) return;
    const vp = document.getElementById('modal-viewport');
    if (!vp) return;

    const resSpan = document.getElementById('modal-img-resolution');
    if (resSpan) {
        resSpan.textContent = `${modalImg.naturalWidth} × ${modalImg.naturalHeight} px`;
    }

    modalImg.style.width = modalImg.naturalWidth + 'px';
    modalImg.style.height = modalImg.naturalHeight + 'px';

    const availW = Math.max(100, vp.clientWidth - 20);
    const availH = Math.max(100, vp.clientHeight - 20);

    modalFitZoom = Math.min(availW / modalImg.naturalWidth, availH / modalImg.naturalHeight, 1.0);
    resetModalImageZoom();
}

function updateModalTransform(smooth = false) {
    if (!modalImg) return;
    modalImg.style.transition = smooth ? 'transform 0.12s ease-out' : 'none';
    modalImg.style.transform = `translate(${modalPanX}px, ${modalPanY}px) scale(${modalZoom})`;

    const badge = document.getElementById('modal-zoom-badge');
    if (badge) {
        if (modalIsFit) {
            badge.textContent = `Fit (${Math.round(modalZoom * 100)}%)`;
        } else {
            badge.textContent = `${Math.round(modalZoom * 100)}%`;
        }
    }
}

function resetModalImageZoom() {
    modalZoom = modalFitZoom;
    modalPanX = 0;
    modalPanY = 0;
    modalIsFit = true;
    updateModalTransform(true);
}

function setModalImageZoom(zoomLevel) {
    modalZoom = Math.max(0.15, Math.min(6.0, zoomLevel));
    modalPanX = 0;
    modalPanY = 0;
    modalIsFit = (Math.abs(modalZoom - modalFitZoom) < 0.02);
    updateModalTransform(true);
}

function zoomModalImage(factor) {
    modalZoom = Math.max(0.15, Math.min(6.0, modalZoom * factor));
    modalIsFit = false;
    updateModalTransform(true);
}

function showImageModal(thumbSrc, imagePath) {
    if (!imageModal || !modalImg) return;
    currentModalImagePath = imagePath || '';
    modalImg.onload = onModalImageLoaded;

    // Show thumbnail initially for instant feedback
    modalImg.src = thumbSrc || '';

    // If full-resolution screenshot exists on disk, load it asynchronously
    if (currentModalImagePath && window.pywebview && window.pywebview.api && window.pywebview.api.get_history_image_base64) {
        window.pywebview.api.get_history_image_base64(currentModalImagePath).then(fullB64 => {
            if (fullB64 && currentModalImagePath === imagePath && modalImg) {
                modalImg.src = fullB64;
            }
        }).catch(err => {
            console.debug("Could not fetch full-resolution image:", err);
        });
    }

    if (modalOpenExternalBtn) {
        modalOpenExternalBtn.style.display = currentModalImagePath ? 'inline-flex' : 'none';
        modalOpenExternalBtn.onclick = () => {
            if (currentModalImagePath) {
                openHistoryImage(currentModalImagePath);
            }
        };
    }
    imageModal.classList.remove('hidden');
}

function closeImageModal() {
    if (!imageModal) return;
    imageModal.classList.add('hidden');
    if (modalImg) {
        modalImg.onload = null;
        modalImg.src = '';
    }
    currentModalImagePath = '';
}

// Mouse and Wheel interaction for Viewport
function initModalViewportEvents() {
    const vp = document.getElementById('modal-viewport');
    if (!vp) return;

    vp.addEventListener('wheel', (e) => {
        e.preventDefault();
        const factor = e.deltaY < 0 ? 1.18 : 0.85;
        modalZoom = Math.max(0.15, Math.min(6.0, modalZoom * factor));
        modalIsFit = false;
        updateModalTransform(false);
    }, { passive: false });

    vp.addEventListener('mousedown', (e) => {
        if (e.button !== 0) return;
        isModalDragging = true;
        modalDragStartX = e.clientX;
        modalDragStartY = e.clientY;
        modalDragInitialPanX = modalPanX;
        modalDragInitialPanY = modalPanY;
        vp.classList.remove('cursor-grab');
        vp.classList.add('cursor-grabbing');
    });

    window.addEventListener('mousemove', (e) => {
        if (!isModalDragging) return;
        const dx = e.clientX - modalDragStartX;
        const dy = e.clientY - modalDragStartY;
        modalPanX = modalDragInitialPanX + dx;
        modalPanY = modalDragInitialPanY + dy;
        updateModalTransform(false);
    });

    window.addEventListener('mouseup', () => {
        if (isModalDragging) {
            isModalDragging = false;
            const vpCurr = document.getElementById('modal-viewport');
            if (vpCurr) {
                vpCurr.classList.remove('cursor-grabbing');
                vpCurr.classList.add('cursor-grab');
            }
        }
    });

    vp.addEventListener('dblclick', () => {
        if (modalIsFit || Math.abs(modalZoom - modalFitZoom) < 0.05) {
            setModalImageZoom(1.0); // 100% native
        } else {
            resetModalImageZoom(); // Back to fit
        }
    });
}

document.addEventListener('keydown', (e) => {
    if (!imageModal || imageModal.classList.contains('hidden')) return;

    if (e.key === 'Escape') {
        closeImageModal();
    } else if (e.key === '+' || e.key === '=') {
        zoomModalImage(1.25);
    } else if (e.key === '-' || e.key === '_') {
        zoomModalImage(0.8);
    } else if (e.key === '0' || e.key === 'r' || e.key === 'R') {
        resetModalImageZoom();
    } else if (e.key === '1') {
        setModalImageZoom(1.0);
    }
});

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initModalViewportEvents);
} else {
    initModalViewportEvents();
}

// --- History Management ---
function renderHistory() {
    historyCountSpan.textContent = queryHistory.length;

    if (!queryHistory.length) {
        historyListDiv.innerHTML = '<p class="text-xs text-gray-400 text-center py-6">No previous queries yet.</p>';
        return;
    }

    historyListDiv.innerHTML = '';
    queryHistory.forEach(item => {
        const isSuccess = item.status === 'success';
        const card = document.createElement('div');
        card.className = `p-3 rounded-lg border text-xs flex flex-col sm:flex-row items-start gap-3 transition ${
            isSuccess ? 'bg-white border-gray-200 shadow-xs hover:border-gray-300' : 'bg-red-50/50 border-red-200'
        }`;

        const shortModel = (item.model || '').replace('models/', '');
        const badgeColor = isSuccess ? 'bg-emerald-100 text-emerald-800' : 'bg-red-100 text-red-800';
        const durationText = item.duration ? `⏱️ ${item.duration}` : '';
        const captureModeText = item.capture_mode ? (
            item.capture_mode === 'active_window' ? '🪟 Active Window' :
            item.capture_mode === 'all_screens' ? '🖥️ All Screens' : '🎯 Cursor'
        ) : '';

        // Left side: Thumbnail preview if available
        let thumbContainer = null;
        if (item.thumbnail) {
            thumbContainer = document.createElement('div');
            thumbContainer.className = 'relative group cursor-pointer flex-shrink-0 w-24 h-16 bg-gray-100 rounded-lg border border-gray-200 overflow-hidden flex items-center justify-center';
            thumbContainer.title = 'Click to enlarge screenshot preview';

            const thumbImg = document.createElement('img');
            thumbImg.src = item.thumbnail;
            thumbImg.alt = 'Screenshot thumbnail';
            thumbImg.className = 'w-full h-full object-cover transition-transform duration-200 group-hover:scale-105';

            const overlay = document.createElement('div');
            overlay.className = 'absolute inset-0 bg-black/40 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center text-white text-xs gap-1 font-medium';
            overlay.innerHTML = '<i class="fas fa-search-plus"></i>';

            thumbContainer.appendChild(thumbImg);
            thumbContainer.appendChild(overlay);
            thumbContainer.addEventListener('click', () => {
                showImageModal(item.thumbnail, item.image_path);
            });
        } else if (item.image_path) {
            thumbContainer = document.createElement('div');
            thumbContainer.className = 'relative group cursor-pointer flex-shrink-0 w-24 h-16 bg-slate-100 rounded-lg border border-slate-200 overflow-hidden flex flex-col items-center justify-center text-slate-400 hover:text-slate-600 hover:bg-slate-200 transition';
            thumbContainer.title = 'Click to open screenshot in default viewer';
            thumbContainer.innerHTML = '<i class="fas fa-image text-base mb-0.5 text-sky-500"></i><span class="text-[9px] font-mono">View</span>';
            thumbContainer.addEventListener('click', () => {
                openHistoryImage(item.image_path);
            });
        }

        // Center: Details and answer
        const contentDiv = document.createElement('div');
        contentDiv.className = 'space-y-1.5 flex-1 min-w-0 w-full';

        const headerDiv = document.createElement('div');
        headerDiv.className = 'flex items-center gap-2 flex-wrap';

        headerDiv.innerHTML = `
            <span class="font-mono text-gray-400">${escapeHtml(item.time || '')}</span>
            <span class="px-2 py-0.5 rounded-full font-bold ${badgeColor}">
                ${isSuccess ? 'Success' : 'Error'}
            </span>
            ${durationText ? `<span class="px-2 py-0.5 rounded-full font-mono bg-sky-50 text-sky-700 border border-sky-200 font-semibold">${escapeHtml(durationText)}</span>` : ''}
            ${captureModeText ? `<span class="px-2 py-0.5 rounded-full font-medium bg-slate-100 text-slate-600 border border-slate-200 text-[11px]">${escapeHtml(captureModeText)}</span>` : ''}
            <span class="px-2 py-0.5 bg-gray-100 text-gray-600 rounded font-mono">${escapeHtml(shortModel)}</span>
            <span class="text-gray-400 font-mono">${item.tokens || 0} tokens</span>
        `;

        const answerDiv = document.createElement('div');
        answerDiv.className = `font-semibold text-xs ${isSuccess ? 'text-gray-800' : 'text-red-700'} mt-1 select-all break-words leading-relaxed`;
        answerDiv.textContent = item.answer || '';

        contentDiv.appendChild(headerDiv);
        contentDiv.appendChild(answerDiv);

        // Right side: Action buttons
        const actionsDiv = document.createElement('div');
        actionsDiv.className = 'flex items-center gap-1.5 flex-shrink-0 self-start sm:self-center';

        if (item.image_path) {
            const openImgBtn = document.createElement('button');
            openImgBtn.className = 'px-2 py-1 bg-sky-50 hover:bg-sky-100 text-sky-700 border border-sky-200 rounded text-xs transition flex items-center gap-1 flex-shrink-0';
            openImgBtn.title = 'Open original screenshot with default image viewer';
            openImgBtn.innerHTML = '<i class="fas fa-external-link-alt"></i> Open Image';
            openImgBtn.addEventListener('click', () => {
                openHistoryImage(item.image_path);
            });
            actionsDiv.appendChild(openImgBtn);
        }

        const copyBtn = document.createElement('button');
        copyBtn.className = 'px-2 py-1 bg-gray-100 hover:bg-gray-200 text-gray-600 rounded text-xs transition flex items-center gap-1';
        copyBtn.title = 'Copy answer';
        copyBtn.innerHTML = '<i class="fas fa-copy"></i>';
        copyBtn.addEventListener('click', () => {
            copyText(item.answer || '');
        });
        actionsDiv.appendChild(copyBtn);

        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'px-2 py-1 bg-red-50 hover:bg-red-100 text-red-600 border border-red-200 rounded text-xs transition flex items-center gap-1';
        deleteBtn.title = 'Delete this entry';
        deleteBtn.innerHTML = '<i class="fas fa-trash-alt"></i>';
        deleteBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            deleteHistoryItem(item.id !== undefined ? item.id : item.time);
        });
        actionsDiv.appendChild(deleteBtn);

        if (thumbContainer) {
            card.appendChild(thumbContainer);
        }
        card.appendChild(contentDiv);
        card.appendChild(actionsDiv);

        historyListDiv.appendChild(card);
    });
}

function deleteHistoryItem(id) {
    if (id === undefined || id === null) return;
    const targetIdx = queryHistory.findIndex(it => (it.id !== undefined && it.id == id) || it.time == id);
    if (targetIdx !== -1) {
        queryHistory.splice(targetIdx, 1);
        renderHistory();
        responseTimesDirty = true;
        const respContent = document.getElementById('tab-content-response-times');
        if (respContent && !respContent.classList.contains('hidden')) {
            loadResponseTimes();
        }
    }
    if (window.pywebview && window.pywebview.api && window.pywebview.api.delete_history_item) {
        window.pywebview.api.delete_history_item(id).then(success => {
            if (success) {
                appendLog(`\x1b[92mINFO: \x1b[0mHistory item deleted.\n`);
            }
        }).catch(err => {
            console.error("Error deleting history item:", err);
            appendLog(`\x1b[91mERROR: \x1b[0mError deleting history item: ${err}\n`);
        });
    }
}

function openHistoryImage(path) {
    if (!path) return;
    if (window.pywebview && window.pywebview.api && window.pywebview.api.open_history_image) {
        window.pywebview.api.open_history_image(path).then(opened => {
            if (!opened) {
                appendLog(`\x1b[91mWARNING: \x1b[0mCould not open screenshot image: ${path}\n`);
            }
        });
    }
}

function addHistoryItem(item) {
    queryHistory.unshift(item);
    if (queryHistory.length > 500) {
        queryHistory.pop();
    }
    renderHistory();
    responseTimesDirty = true;
    const respContent = document.getElementById('tab-content-response-times');
    if (respContent && !respContent.classList.contains('hidden')) {
        loadResponseTimes();
    }
}

function clearHistory() {
    queryHistory = [];
    renderHistory();
    responseTimesDirty = true;
    const respContent = document.getElementById('tab-content-response-times');
    if (respContent && !respContent.classList.contains('hidden')) {
        loadResponseTimes();
    }
    if (window.pywebview && window.pywebview.api && window.pywebview.api.clear_history) {
        window.pywebview.api.clear_history();
    }
}

// --- API Keys Management ---
function renderApiKeys(keys) {
    currentApiKeys = keys || [];
    keysCountSpan.textContent = currentApiKeys.length;

    if (!currentApiKeys.length) {
        apiKeysListDiv.innerHTML = '<p class="text-xs text-gray-400 text-center py-6">No API keys registered.</p>';
        return;
    }

    apiKeysListDiv.innerHTML = '';
    currentApiKeys.forEach(k => {
        const card = document.createElement('div');
        card.className = `p-3 rounded-lg border text-xs flex justify-between items-center gap-3 ${
            k.is_active ? 'bg-indigo-50/50 border-indigo-200' : 'bg-white border-gray-200'
        }`;

        const safeKeyFull = (k.key_full || '').replace(/'/g, "\\'");
        const canDelete = currentApiKeys.length > 1;

        card.innerHTML = `
            <div class="space-y-1 flex-1 min-w-0">
                <div class="flex items-center gap-2">
                    <span class="font-bold text-gray-700">Key #${k.index}</span>
                    <span class="font-mono bg-gray-100 px-2 py-0.5 rounded text-gray-800">${escapeHtml(k.key_masked)}</span>
                    ${k.is_active ? '<span class="px-2 py-0.5 rounded-full font-semibold bg-emerald-100 text-emerald-800 text-[10px]">Active</span>' : ''}
                </div>
                <div class="text-[11px] text-gray-500 flex items-center gap-3 flex-wrap">
                    <span><i class="fas fa-paper-plane mr-1 text-gray-400"></i>Requests: <strong class="text-gray-700">${k.requests || 0}</strong></span>
                    <span><i class="fas fa-coins mr-1 text-amber-500"></i>Tokens: <strong class="text-gray-700">${(k.tokens || 0).toLocaleString()}</strong></span>
                    <span><i class="far fa-clock mr-1 text-gray-400"></i>Last Used: <strong class="text-gray-700">${escapeHtml(k.last_used || 'Never')}</strong></span>
                </div>
            </div>
            ${canDelete ? `
                <button onclick="removeApiKey('${safeKeyFull}')"
                    class="px-2.5 py-1 text-red-500 hover:text-red-700 hover:bg-red-50 rounded transition text-xs flex items-center gap-1 flex-shrink-0"
                    title="Remove API key">
                    <i class="fas fa-trash-alt"></i> Remove
                </button>
            ` : '<span class="text-gray-400 text-[10px] italic">Primary key</span>'}
        `;
        apiKeysListDiv.appendChild(card);
    });
}

function refreshApiKeys() {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.get_api_keys_info) {
        window.pywebview.api.get_api_keys_info().then(keys => {
            renderApiKeys(keys);
        });
    }
}

function addApiKey() {
    const val = newApiKeyInput.value.trim();
    if (!val) return;

    if (window.pywebview && window.pywebview.api && window.pywebview.api.add_api_key) {
        window.pywebview.api.add_api_key(val).then(success => {
            if (success) {
                appendLog(`\x1b[92mSUCCESS: \x1b[0mNew API key added successfully.\n`);
                newApiKeyInput.value = '';
                refreshApiKeys();
            } else {
                appendLog(`\x1b[91mERROR: \x1b[0mFailed to add API key (invalid or already exists).\n`);
            }
        }).catch(err => {
            appendLog(`\x1b[91mERROR: \x1b[0mError adding API key: ${err}\n`);
        });
    }
}

function removeApiKey(key) {
    if (!key) return;
    if (confirm("Are you sure you want to remove this API key?")) {
        if (window.pywebview && window.pywebview.api && window.pywebview.api.remove_api_key) {
            window.pywebview.api.remove_api_key(key).then(success => {
                if (success) {
                    appendLog(`\x1b[92mINFO: \x1b[0mAPI key removed successfully.\n`);
                    refreshApiKeys();
                } else {
                    appendLog(`\x1b[91mERROR: \x1b[0mCannot remove this API key.\n`);
                }
            }).catch(err => {
                appendLog(`\x1b[91mERROR: \x1b[0mError removing API key: ${err}\n`);
            });
        }
    }
}

function copyText(text) {
    navigator.clipboard.writeText(text).then(() => {
        appendLog(`\x1b[92mINFO: \x1b[0mAnswer copied to clipboard: "${text}"\n`);
    });
}

// --- Log Level Filtering & Display ---
function populateLogLevel(level) {
    if (!level) return;
    const upper = level.toUpperCase();
    if (LOG_LEVEL_MAP[upper]) {
        currentLogLevel = upper;
        if (logLevelSelect) {
            logLevelSelect.value = upper;
        }
        renderFilteredLogs();
    }
}

function parseLogLevel(text) {
    if (!text) return { name: 'INFO', num: 20 };
    const match = text.match(/(?:^\x1b\[[0-9;]*m)*(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|SUCCESS):\s/i);
    if (match) {
        const lvl = match[1].toUpperCase();
        if (lvl === 'DEBUG') return { name: 'DEBUG', num: 10 };
        if (lvl === 'INFO' || lvl === 'SUCCESS') return { name: 'INFO', num: 20 };
        if (lvl === 'WARNING' || lvl === 'WARN') return { name: 'WARNING', num: 30 };
        if (lvl === 'ERROR') return { name: 'ERROR', num: 40 };
        if (lvl === 'CRITICAL') return { name: 'CRITICAL', num: 50 };
    }
    if (allLogs.length > 0) {
        const prev = allLogs[allLogs.length - 1];
        return { name: prev.level, num: prev.levelNum };
    }
    return { name: 'INFO', num: 20 };
}

function renderFilteredLogs() {
    if (!logOutputPre) return;
    const threshold = LOG_LEVEL_MAP[currentLogLevel] || 20;
    const visibleEntries = allLogs.filter(e => e.levelNum >= threshold);

    if (visibleEntries.length === 0) {
        if (allLogs.length > 0) {
            logOutputPre.innerHTML = `<span class="text-gray-400 italic">No logs matching level "${currentLogLevel}" or above (${allLogs.length} hidden)</span>\n`;
        } else {
            logOutputPre.innerHTML = '';
        }
    } else {
        logOutputPre.innerHTML = visibleEntries.map(e => e.html).join('');
    }

    if (logCountBadge) {
        logCountBadge.textContent = `${visibleEntries.length} lines`;
    }

    logOutputPre.scrollTop = logOutputPre.scrollHeight;
}

function appendLog(logLine) {
    if (!logLine || !logLine.trim()) return;

    const { name, num } = parseLogLevel(logLine);
    const coloredHtml = ansiToHtml(logLine);

    const entry = {
        raw: logLine,
        level: name,
        levelNum: num,
        html: coloredHtml,
    };

    allLogs.push(entry);
    if (allLogs.length > 5000) {
        allLogs.shift();
    }

    const threshold = LOG_LEVEL_MAP[currentLogLevel] || 20;
    if (num >= threshold) {
        if (logOutputPre.querySelector('.text-gray-400')) {
            logOutputPre.innerHTML = '';
        }
        logOutputPre.insertAdjacentHTML('beforeend', coloredHtml);
        logOutputPre.scrollTop = logOutputPre.scrollHeight;
    }

    if (logCountBadge) {
        const visibleCount = allLogs.filter(e => e.levelNum >= threshold).length;
        logCountBadge.textContent = `${visibleCount} lines`;
    }
}

function changeLogLevel(newLevel) {
    if (!newLevel) return;
    const upper = newLevel.toUpperCase();
    if (!LOG_LEVEL_MAP[upper]) return;

    currentLogLevel = upper;
    if (logLevelSelect && logLevelSelect.value !== upper) {
        logLevelSelect.value = upper;
    }

    renderFilteredLogs();

    if (window.pywebview && window.pywebview.api && window.pywebview.api.set_log_level) {
        window.pywebview.api.set_log_level(upper).catch(err => {
            console.error("Error setting log level:", err);
        });
    }
}

function clearLogs() {
    allLogs = [];
    if (logOutputPre) logOutputPre.innerHTML = '';
    if (logCountBadge) logCountBadge.textContent = '0 lines';
}

function copyLogs() {
    if (!logOutputPre) return;
    navigator.clipboard.writeText(logOutputPre.innerText).then(() => {
        appendLog("\x1b[92mINFO: \x1b[0mLogs copied to clipboard.\n");
    });
}

// --- Listening Controls ---
function toggleListening() {
    startStopButton.disabled = true;

    if (uiState === 'configuring') {
        window.pywebview.api.start_listening().then(() => {
            startStopButton.disabled = false;
        }).catch(err => {
            console.error("Error starting listening:", err);
            appendLog(`\x1b[91mERROR: \x1b[0mFailed to start listening: ${err}\n`);
            startStopButton.disabled = false;
        });
    } else {
        window.pywebview.api.stop_listening().then(() => {
            startStopButton.disabled = false;
        }).catch(err => {
            console.error("Error stopping listening:", err);
            appendLog(`\x1b[91mERROR: \x1b[0mFailed to stop listening: ${err}\n`);
            startStopButton.disabled = false;
        });
    }
}

function toggleVisibility() {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.toggle_ui_visibility) {
        window.pywebview.api.toggle_ui_visibility().catch(err => {
            console.error("Error toggling UI visibility:", err);
        });
    }
}

// Expose functions to global window scope
window.setInitialState = setInitialState;
window.setUIState = setUIState;
window.updateTokenDisplay = updateTokenDisplay;
window.appendLog = appendLog;
window.addHistoryItem = addHistoryItem;
window.toggleListening = toggleListening;
window.toggleVisibility = toggleVisibility;
window.addPdfSource = addPdfSource;
window.removePdfSource = removePdfSource;
window.browsePdfFile = browsePdfFile;
window.applyHotkey = applyHotkey;
window.savePrompt = savePrompt;
window.newPrompt = newPrompt;
window.switchTab = switchTab;
window.clearLogs = clearLogs;
window.copyLogs = copyLogs;
window.clearHistory = clearHistory;
window.openHistoryImage = openHistoryImage;
window.addApiKey = addApiKey;
window.removeApiKey = removeApiKey;
window.refreshApiKeys = refreshApiKeys;
window.copyText = copyText;
window.showImageModal = showImageModal;
window.closeImageModal = closeImageModal;
window.deleteHistoryItem = deleteHistoryItem;
window.changeLogLevel = changeLogLevel;
window.zoomModalImage = zoomModalImage;
window.setModalImageZoom = setModalImageZoom;
window.resetModalImageZoom = resetModalImageZoom;