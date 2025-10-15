// JavaScript for the UI frontend

const pdfListUl = document.getElementById('pdf-list');
const pdfInput = document.getElementById('pdf-input');
const modelSelect = document.getElementById('model-select');
const totalTokensSpan = document.getElementById('total-tokens');
const todayTokensSpan = document.getElementById('today-tokens');
const logOutputPre = document.getElementById('log-output');
const startStopButton = document.getElementById('start-stop-button');
const stateStatusDiv = document.getElementById('state-status');

let currentPdfSources = [];
let uiState = 'configuring'; // 'configuring' or 'listening'

// --- ANSI to HTML/CSS Mapping ---
// Mapping of ANSI SGR parameters to CSS classes
const ansiColorMap = {
    // Standard Colors (30-37)
    30: 'ansi-black',
    31: 'ansi-red',
    32: 'ansi-green',
    33: 'ansi-yellow',
    34: 'ansi-blue',
    35: 'ansi-magenta',
    36: 'ansi-cyan',
    37: 'ansi-white',
    // Bright Colors (90-97)
    90: 'ansi-grey', // Bright Black
    91: 'ansi-bright-red',
    92: 'ansi-bright-green',
    93: 'ansi-bright-yellow',
    94: 'ansi-bright-blue',
    95: 'ansi-bright-magenta',
    96: 'ansi-bright-cyan',
    97: 'ansi-bright-white',
    // Attributes
    1: 'ansi-bold',      // Bold
    4: 'ansi-underline', // Underline
    // Add others if needed (e.g., background colors 40-47, 100-107)
};

// --- Function to parse ANSI and convert to HTML ---
function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function ansiToHtml(text) {
  // Matches \x1b[ ... m
  const ansiRegex = /\x1b\[((?:\d|;)*?)m/g;
  let html = '';
  let lastIndex = 0;

  // Stack of open color‐span class names
  const openSpans = [];
  // Whether we currently have a <strong> open
  let isBold = false;

  let match;
  while ((match = ansiRegex.exec(text)) !== null) {
    // 1) Append everything before this code (escaped)
    html += escapeHtml(text.slice(lastIndex, match.index));
    lastIndex = ansiRegex.lastIndex;

    // 2) Parse the SGR parameters
    const params = match[1].split(';').map(Number);
    for (const p of params) {
      if (p === 0) {
        // Reset everything
        if (isBold) {
          html += '</strong>';
          isBold = false;
        }
        while (openSpans.length) {
          html += '</span>';
          openSpans.pop();
        }

      } else if (p === 1) {
        // Bold on
        if (!isBold) {
          html += '<strong>';
          isBold = true;
        }

      } else if (ansiColorMap[p] && ansiColorMap[p] !== 'bold') {
        // Color
        const cls = ansiColorMap[p];
        html += `<span class="${cls}">`;
        openSpans.push(cls);
      }
      // (You can add underline, background, etc., here.)
    }
  }

  // 3) Append remaining text after the last ANSI code
  html += escapeHtml(text.slice(lastIndex));

  // 4) Close any still‐open tags
  if (isBold) html += '</strong>';
  while (openSpans.length) {
    html += '</span>';
    openSpans.pop();
  }

  return html;
}

// Function called by Python to set the initial state
function setInitialState(state) {
    console.log("JS: Received initial state:", state);

    currentPdfSources = state.pdfSources || [];
    populatePdfList();

    const availableModels = state.availableModels || [];
    populateModelSelect(availableModels, state.selectedModel);

    updateTokenDisplay(state.tokenUsage.total, state.tokenUsage.daily[getTodayDateString()] || 0);

    // Set the initial listening state
    if (state.isListening) {
        setUIState('listening');
    } else {
        setUIState('configuring');
    }

    // Set initial UI state based on how the app starts (likely configuring initially)
    // setUIState('configuring'); // Assuming it starts in configuring mode
    // If main.py starts in listening mode, it should call setUIState after webview.start()
}

// Function called by Python to update the UI state
function setUIState(state) {
    console.log("JS: Setting UI state to:", state);
    uiState = state;
    document.body.classList.remove('configuring-state', 'listening-state');
    document.body.classList.add(state + '-state');

    stateStatusDiv.innerHTML = state === 'configuring'
    ? '<i class="fas fa-gear mr-1"></i> Configuring'
    : '<i class="fas fa-keyboard mr-1"></i> Listening (Hotkeys Active)';
    stateStatusDiv.className = 'status-badge ' + state; // Update classes for styling

    startStopButton.innerHTML = state === 'configuring'
        ? '<i class="fas fa-play mr-1"></i> Start Listening'
        : '<i class="fas fa-stop mr-1"></i> Stop Listening';

    startStopButton.classList.remove('configuring', 'listening');
    startStopButton.classList.add(state);
    startStopButton.classList.remove('bg-red-500', 'bg-green-600', 'hover:bg-red-600', 'hover:bg-green-700');
    startStopButton.classList.add(state === 'listening' ? 'bg-red-500' : 'bg-green-600');
    startStopButton.classList.add(state === 'listening' ? 'hover:bg-red-600' : 'hover:bg-green-700');

    // Disable/enable input fields and buttons based on state
    const inputs = document.querySelectorAll('#pdf-section input, #pdf-section button, #model-section select');
    inputs.forEach(input => {
        input.disabled = state === 'listening';
    });
}


// --- PDF List Management ---
function populatePdfList() {
    pdfListUl.innerHTML = ''; // Clear current list
    currentPdfSources.forEach((source, index) => {
        const li = document.createElement('li');
        li.textContent = source;
        const removeBtn = document.createElement('button');
        removeBtn.textContent = 'Remove';
        removeBtn.className = 'remove-btn';
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
        // Inform Python about the updated list
        window.pywebview.api.set_pdf_sources(currentPdfSources);
    } else if (source) {
        console.log("JS: PDF source already in the list.");
    }
}

function removePdfSource(index) {
    if (index >= 0 && index < currentPdfSources.length) {
        currentPdfSources.splice(index, 1);
        populatePdfList();
        // Inform Python about the updated list
        window.pywebview.api.set_pdf_sources(currentPdfSources);
    }
}

// Allow adding PDF source by pressing Enter in the input field
pdfInput.addEventListener('keypress', function(event) {
    if (event.key === 'Enter') {
        event.preventDefault(); // Prevent default form submission if applicable
        addPdfSource();
    }
});


// --- Model Selection ---
function populateModelSelect(models, selectedModel) {
    modelSelect.innerHTML = ''; // Clear current options
    models.forEach(model => {
        const option = document.createElement('option');
        option.value = model;
        option.textContent = model;
        if (model === selectedModel) {
            option.selected = true;
        }
        modelSelect.appendChild(option);
    });
}

modelSelect.addEventListener('change', function() {
    const selectedModel = modelSelect.value;
    console.log("JS: Selected model:", selectedModel);
    // Inform Python about the selected model
    window.pywebview.api.set_selected_model(selectedModel);
});

function browsePdfFile() {
    // pywebview provides access to file paths via API
    if (window.pywebview) {
        window.pywebview.api.browse_pdf().then(function (filePath) {
            if (filePath) {
                pdfInput.value = filePath;
                addPdfSource();
            }
        });
    } else {
        alert("pywebview not available.");
    }
}


function copyLogs() {
    const logText = logOutputPre.innerText;
    navigator.clipboard.writeText(logText);
}


// --- Token Usage Display ---
function updateTokenDisplay(total, today) {
    totalTokensSpan.textContent = total;
    todayTokensSpan.textContent = today;
}

function getTodayDateString() {
    const today = new Date();
    const year = today.getFullYear();
    const month = (today.getMonth() + 1).toString().padStart(2, '0');
    const day = today.getDate().toString().padStart(2, '0');
    return `${year}-${month}-${day}`;
}


// --- Log Display ---
// Function called by Python to append a log line
function appendLog(logLine) {
    if (logLine.includes("sending thread") && logLine.includes("monitoring thread")) {
        logLine.split('.').forEach(line => {
            if (line.trim()) {
                appendLog(line.trim() + '.\n');
            }
        });
        return;
    }
    // Skip empty lines
    if (!logLine.trim()) return;

    // Convert ANSI to HTML
    const coloredHtml = ansiToHtml(logLine);

    // Append the HTML to the PRE element's innerHTML
    // Using insertAdjacentHTML is generally more performant and safer than += innerHTML
    logOutputPre.insertAdjacentHTML('beforeend', coloredHtml);

    // Auto-scroll to the bottom - works for PRE element too
    logOutputPre.scrollTop = logOutputPre.scrollHeight;
}


// --- Start/Stop Listening Control ---
function toggleListening() {
    startStopButton.disabled = true; // Prevent double clicks

    if (uiState === 'configuring') {
        console.log("JS: Calling Python start_listening()");
        window.pywebview.api.start_listening().then(() => {
            // Python's startListening will call setUIState on success
            startStopButton.disabled = false;
        }).catch(error => {
            console.error("JS: Error calling start_listening:", error);
            appendLog(`Error starting listening: ${error}\n`);
            startStopButton.disabled = false; // Re-enable button on error
        });
    } else if (uiState === 'listening') {
        console.log("JS: Calling Python stop_listening()");
        window.pywebview.api.stop_listening().then(() => {
            // Python's stopListening will call setUIState on success
            startStopButton.disabled = false;
        }).catch(error => {
            console.error("JS: Error calling stop_listening:", error);
            appendLog(`Error stopping listening: ${error}\n`);
            startStopButton.disabled = false; // Re-enable button on error
        });
    }
}

function clearLogs() {
    logOutputPre.innerHTML = ''; // Clear the log display
}

function toggleVisibility() {
    window.pywebview.api.toggle_ui_visibility().catch(error => {
        console.error("JS: Error calling hide_ui:", error);
        appendLog(`Error hiding UI: ${error}\n`);
    });
}


// --- Initial State Sync (Called by Python after window is shown) ---
// The function `setInitialState` is called from Python's `_on_window_shown` callback.
// This ensures the DOM is ready before we try to populate elements.

// --- Handle window close ---
// webview.start() handles the GUI event loop. When the user closes the window,
// webview fires a 'closed' event. In ui.py, we've hooked this event to call
// the main script's quitApp callback (set_quitting_flag).
// The TrayIcon's quit menu also calls set_quitting_flag.

// --- Initial UI Setup ---
// The initial UI state is 'configuring' by default, but main.py will call setInitialState
// and potentially setUIState to reflect the actual application state.
setUIState('configuring'); // Set initial visual state until Python overrides