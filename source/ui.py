import os
import webview
import threading
import queue
import sys
import time
import json

from ansi import ansi

UI_MSG = ansi.OKCYAN + "UI: " + ansi.ENDC

class LogRedirector(object):
    """Redirects stdout to a queue for the UI."""
    def __init__(self, queue):
        self.queue = queue
        self._stdout = sys.stdout # Keep original stdout
        self._buffer = "" # Buffer for incomplete lines

    def write(self, text):
        self._stdout.write(text) # Also write to original stdout (console)
        self._buffer += text
        while '\n' in self._buffer:
            line, self._buffer = self._buffer.split('\n', 1)
            self.queue.put(line + '\n') # Add newline back for consistency

    def flush(self):
        self._stdout.flush() # Flush original stdout
        # If there's anything left in the buffer without a newline, put it in the queue
        if self._buffer:
             self.queue.put(self._buffer)
             self._buffer = ""

    # Required for Python 3
    def isatty(self):
        return False

    # Required for Python 3
    def fileno(self):
        return self._stdout.fileno()
    
    # Required for Python 3's print() function to work correctly with redirected stdout
    @property
    def encoding(self):
        try:
            return self._original_stream.encoding
        except Exception:
            return 'utf-8' # Default encoding


class UI:
    def __init__(self, main_app_callbacks, hidden=False):
        """
        Initializes the UI window and communication bridge.
        Args:
            main_app_callbacks: A dictionary of functions from main.py to be called by the UI/JS.
        """
        self.window = None
        self.log_queue = queue.Queue()
        self.log_redirector = LogRedirector(self.log_queue)
        self._log_thread = None
        self._log_thread_running = False

        # Store callbacks from the main application
        self.main_app_callbacks = main_app_callbacks

        # We serve the HTML file using Flask
        current_dir = os.path.dirname(os.path.abspath(__file__))
        self.app = Flask(__name__, 
                         static_url_path='', # Serve static files from the root URL path
                         static_folder=current_dir, # Serve static files from the current directory
                         template_folder=current_dir) # Serve templates from the current directory

        @self.app.route('/')
        def index():
            # Render ui.html from the same directory
            return render_template('ui.html')

        # Create the webview window, serving the Flask app
        # headless=True might be useful if you want to start without the GUI visible
        self.window = webview.create_window(
            'Gemini QA Config',
            url='http://127.0.0.1:5000/',
            width=800,
            height=600,
            resizable=True,
            hidden=hidden,
        )

        self.window.expose(self.browse_pdf)
        for callback in self.main_app_callbacks:
            self.window.expose(self.main_app_callbacks[callback]) # Expose each callback to the JS context

    def browse_pdf(self):
        """Opens a file dialog to select a PDF file."""
        # This method is called from the JS side
        # Use the webview API to open a file dialog and return the selected file path
        try:
            pdf_path = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=('PDF Files (*.pdf)',),
                allow_multiple=False)
            if pdf_path:
                pdf_path = pdf_path[0]
                return pdf_path
            else:
                print(UI_MSG + "No PDF file selected.")
                return None
        except Exception as e:
            print(ansi.ERROR_MSG + f"Error opening file dialog: {e}")
            return None

    def start_ui(self):
        """Starts the pywebview GUI and the log monitoring thread."""
        print(UI_MSG + "Starting UI window...")
        # Redirect stdout AFTER Flask app is configured but BEFORE webview.start()
        # so logs from Flask/webview setup also go to console.
        sys.stdout = self.log_redirector

        # Start the thread that monitors the log queue and sends updates to the UI
        self._log_thread_running = True
        self._log_thread = threading.Thread(target=self._send_logs_to_ui, daemon=True)
        self._log_thread.start()
        print(UI_MSG + "Log monitoring thread started.")

        # We need to start the Flask server in a separate thread because webview.start() blocks the main thread.
        def run_flask():
            # Use a simple development server
            self.app.run(use_reloader=False) # Important: use_reloader=False with threading

        # Start Flask server in a thread
        self._flask_thread = threading.Thread(target=run_flask, daemon=True)
        self._flask_thread.start()
        print(UI_MSG + "Flask development server started.")

        # Start the webview GUI - this call blocks the main thread until the window is closed
        # The 'on_shown' callback is fired when the window is first displayed
        # The 'on_closed' callback is fired when the window is closed by the user
        self.window.events.closed += self._on_window_closed # Hook closed event
        self.window.events.shown += self._on_window_shown # Hook shown event (for initial state sync)

        print(UI_MSG + "Entering webview.start() blocking call...")
        webview.start() # This call blocks the main thread

        # This code is reached AFTER webview.start() returns (i.e. window is closed)
        print(UI_MSG + "webview.start() returned. Shutting down UI.")
        self._log_thread_running = False # Signal log thread to stop
        if self._log_thread and self._log_thread.is_alive():
            # Put a dummy item in queue to unblock the thread if it's waiting
            self.log_queue.put(None)
            self._log_thread.join(timeout=2) # Wait for log thread to finish

        # Restore original stdout
        sys.stdout = self.log_redirector._stdout
        print(UI_MSG + "Restored original stdout.")


    def _on_window_shown(self):
        """Callback executed when the webview window is first shown."""
        print(UI_MSG + "Window shown. Syncing initial state.")
        # Send initial data to the UI after it's ready
        # Call JavaScript function to populate settings
        # Example: window.pywebview.api.load_settings() on the JS side calls Python's load_settings
        # Here we'll call JS functions from Python to update the UI state.
        try:
            # Ask main.py for initial state and send to JS
            initial_state = {
                'pdfSources': self.main_app_callbacks['get_pdf_sources'](),
                'availableModels': self.main_app_callbacks['get_available_models'](),
                'selectedModel': self.main_app_callbacks['get_selected_model'](),
                'tokenUsage': self.main_app_callbacks['get_token_usage'](),
                # UI state (listening/configuring) should be handled by main.py and sent via update_ui_state
            }
            # Call a JS function to apply this initial state
            self.window.evaluate_js(f'setInitialState({json.dumps(initial_state)})')
            print(UI_MSG + "Sent initial state to JS.")
        except Exception as e:
            print(ansi.ERROR_MSG + f"Error sending initial state to JS: {e}")


    def _on_window_closed(self):
        """Callback executed when the webview window is closed by the user."""
        print(UI_MSG + "Window closed by user.")
        # Signal the main application to quit gracefully
        if self.main_app_callbacks['quit_app']:
            print(UI_MSG + "Calling quit_app callback.")
            self.main_app_callbacks['quit_app']() # Call the callback provided by main.py


    def _send_logs_to_ui(self):
        """Reads logs from the queue and sends them to the UI."""
        print(UI_MSG + "Log sending thread started.")
        while self._log_thread_running:
            try:
                # Get log line from the queue (blocks until item is available or timeout)
                log_line = self.log_queue.get(timeout=0.5) # Use timeout to check running flag

                if log_line is None: # Special signal to stop
                    break

                # Send the log line to the UI using evaluate_js
                # Use json.dumps to properly escape the string for JavaScript
                if self.window: # Ensure window object still exists
                    try:
                        self.window.evaluate_js(f'appendLog({json.dumps(log_line)})')
                    except Exception as e:
                         # This might happen if the window is closing while thread is running
                         # print(f"UI: Error sending log to JS: {e}") # Avoid excessive error logs during shutdown
                         pass

            except queue.Empty:
                # No logs in the queue, continue looping and check _log_thread_running
                pass
            except Exception as e:
                print(ansi.ERROR_MSG + f"Error in log sending thread: {e}")
                # Continue trying unless flag is set or queue gets None

        print(UI_MSG + "Log sending thread finished.")


    # Methods called by the main application to update the UI from Python
    def update_logs(self, log_text):
        """Explicitly add a log entry to the UI (less common if redirector is used)."""
        # This method is less needed if the LogRedirector works well.
        # Could be used for special messages not sent to stdout.
        if self.window:
            try:
                self.window.evaluate_js(f'appendLog({json.dumps(log_text)})')
            except Exception as e:
                print(ansi.ERROR_MSG + f"Error calling JS appendLog: {e}")

    def update_token_usage(self, total, today):
        """Updates the token usage display in the UI."""
        if self.window:
            try:
                self.window.evaluate_js(f'updateTokenDisplay({total}, {today})')
            except Exception as e:
                print(ansi.ERROR_MSG + f"Error calling JS updateTokenDisplay: {e}")


    def update_ui_state(self, state):
        """Updates the UI elements based on the application state (e.g., 'configuring', 'listening')."""
        # This method is called by main.py when the state changes
        if self.window:
            try:
                self.window.evaluate_js(f'setUIState({json.dumps(state)})')
            except Exception as e:
                print(ansi.ERROR_MSG + f"Error calling JS setUIState: {e}")


    def show(self):
        """Shows the UI window if it's hidden."""
        if self.window:
            try:
                self.window.show()
                print(UI_MSG + "Window shown.")
            except Exception as e:
                print(ansi.ERROR_MSG + f"Error showing window: {e}")


    def hide(self):
        """Hides the UI window."""
        if self.window:
            try:
                self.window.hide()
                print(UI_MSG + "Window hidden.")
            except Exception as e:
                print(ansi.ERROR_MSG + f"Error hiding window: {e}")

    def destroy(self):
        """Destroys the UI window."""
        if self.window:
            try:
                self.window.destroy()
                print(UI_MSG + "Window destroyed.")
            except Exception as e:
                print(ansi.ERROR_MSG + f"Error destroying window: {e}")
            self.window = None # Clear reference


# Need Flask setup here because it's used by UI class
from flask import Flask, render_template
# Ensure Flask only runs its dev server when imported and used by UI.
# Flask dev server setup happens inside the UI class __init__ and start_ui methods.