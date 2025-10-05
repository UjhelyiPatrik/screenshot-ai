import ctypes
from datetime import date
import os
import sys
import time
import keyboard
import win32con
import win32gui

from trayicon import TrayIcon
from ansi import ansi
import gemini
import token_db
from ui import UI, LogRedirector

# A script abszolút elérési útja
script_dir = os.path.dirname(os.path.abspath(__file__))

# Állítsd be a munkakönyvtárat a script mappájára
os.chdir(script_dir)

# --- Global State ---
quitting = False
is_listening = False # Track if hotkeys are active
is_hidden = False # Track if UI is hidden
pdf_sources_list = [] # List of PDF paths/URLs
selected_model = "models/gemini-2.5-flash-preview-04-17-thinking" # Default model
token_data = {} # Dictionary to store token usage loaded from token_db
hwnd = None # Handle for the console window

# --- Objects ---
trayicon = None
ui_app = None

def set_quitting_flag():
    """Sets the global flag and signals UI to close."""
    global quitting
    if not quitting: # Only signal once
        print(ansi.INFO_MSG + "Shutdown requested.")
        quitting = True

        # Set the tray icon to a loading state
        if trayicon:
            trayicon.set_loading()

        # Signal the UI window to close if it exists
        if ui_app and ui_app.window:
            try:
                print(ansi.INFO_MSG + "Signaling UI window to destroy...")
                # Calling destroy() on the window should cause webview.start() to return
                ui_app.destroy()
                print(ansi.INFO_MSG + "UI window destroy signaled.")
            except Exception as e:
                print(ansi.ERROR_MSG + f"Error signaling UI window destroy: {e}")
        else:
            # If no UI, we still need to unhook keyboard
            try:
                keyboard.unhook_all_hotkeys()
                print(ansi.INFO_MSG + "Hotkeys unhooked (UI not active).")
            except Exception as e:
                print(ansi.ERROR_MSG + f"Error unhooking hotkeys: {e}")

def toggle_ui_visibility():
    """Shows the UI window if hidden, hides if shown."""
    global is_hidden
    if ui_app and ui_app.window:
        if ui_app.window.get_current_url():
            if is_hidden:
                ui_app.show()
            else:
                ui_app.hide()         
            is_hidden = not is_hidden
        else:
            print(ansi.WARNING_MSG + "UI window not yet loaded.")


# Callback for the Start/Stop button in the UI
def toggle_listening_state():
    """Toggles between configuring and listening states."""
    global is_listening
    if is_listening:
        stop_listening()
    else:
        start_listening()
    # The start/stop functions will call ui_app.update_ui_state


def start_listening():
    """Sets state to listening and registers hotkeys."""
    global is_listening
    if is_listening:
        print(ansi.INFO_MSG + "Already in listening state.")
        return

    print("\n" + "-" * 50)
    print(ansi.INFO_MSG + "Entering listening state...")

    if not pdf_sources_list:
        print(ansi.WARNING_MSG + "No PDF sources added. Will only use image for context.")
    if not selected_model:
        print(ansi.ERROR_MSG + "No AI model selected. Cannot start listening.")
        ui_app.update_ui_state('configuring') # Stay in configuring state
        return False # Indicate failure to start listening

    # Disable config elements in UI
    if ui_app:
        ui_app.update_ui_state('listening') # Call JS function to update UI look

    # Add hotkeys
    try:
        # Use a lambda to pass current state variables to the handler
        # The hotkey handler will be called in a separate thread!
        keyboard.add_hotkey("ctrl+shift+q", lambda: process_question_handler())
        keyboard.add_hotkey("ctrl+alt+shift+c", lambda: set_quitting_flag())
        print(ansi.SUCCESS_MSG + "Keyboard hotkeys registered.")
        is_listening = True
        print(ansi.INFO_MSG + "Listening state active. Hotkeys are enabled.")
        print("-" * 50 + "\n")
        return True # Indicate success

    except Exception as e:
        print(ansi.ERROR_MSG + f"Failed to register hotkeys: {e}")
        # Revert state if hotkey registration fails
        if ui_app:
            ui_app.update_ui_state('configuring')
        print(ansi.INFO_MSG + "Returning to configuring state.")
        print("-" * 50 + "\n")
        return False # Indicate failure


def stop_listening():
    """Sets state to configuring and unhooks hotkeys."""
    global is_listening
    if not is_listening:
        print(ansi.INFO_MSG + "Already in configuring state.")
        return

    print("\n" + "-" * 50)
    print(ansi.INFO_MSG + "Entering configuring state...")

    # Unhook hotkeys
    try:
        keyboard.unhook_all_hotkeys()
        print(ansi.SUCCESS_MSG + "Keyboard hotkeys unhooked.")
    except Exception as e:
        print(ansi.WARNING_MSG + f"Error unhooking hotkeys: {e}")

    is_listening = False
    print(ansi.INFO_MSG + "Configuring state active. Hotkeys are disabled.")

    # Enable config elements in UI
    if ui_app:
        ui_app.update_ui_state('configuring') # Call JS function to update UI look/feel

    print("-" * 50 + "\n")


def process_question_handler():
    """Handles the process question hotkey trigger."""
    global is_listening, token_data, selected_model, pdf_sources_list

    # Check state before processing
    if not is_listening or quitting:
        return # Do nothing if not listening or shutting down

    print(ansi.INFO_MSG + "Question hotkey detected.")

    # Call the actual processing logic in gemini.py
    # This might take time, but since it's called from a keyboard thread, it shouldn't block the UI/main loop.
    # However, if gemini.process_question has blocking network calls, it *will* block this hotkey thread.
    try:
        # Pass current config from main.py state
        tokens_used = gemini.process_question(
            trayicon,
            pdf_sources_list,
            selected_model,
            prompt_file_name
        )

        if tokens_used > 0:
            print(ansi.INFO_MSG + f"Used {tokens_used} tokens for this query.")
            token_data = token_db.update_token_data(token_data, tokens_used)
            token_db.save_token_data(token_data)
            # Update UI with new token counts
            if ui_app:
                today_str = str(date.today()) # Need date from datetime
                ui_app.update_token_usage(token_data["total"], token_data["daily"].get(today_str, 0))

    except Exception as e:
        print(ansi.ERROR_MSG + f"An error occurred in the hotkey handler: {e}")
        # Update tray icon state to error if possible
        if trayicon:
            trayicon.display_answer("ERR", color="red")



# --- Functions exposed to UI/JS ---

def get_current_config():
    """Returns current configuration for the UI to display."""
    global pdf_sources_list, selected_model
    return {
        'pdfSources': pdf_sources_list,
        'selectedModel': selected_model,
        'availableModels': get_available_models(), # Get from API or a list
        'tokenUsage': get_token_usage(), # Get current usage
    }

def set_pdf_sources(sources):
    """Updates the list of PDF sources from the UI."""
    global pdf_sources_list
    print(ansi.INFO_MSG + f"UI updated PDF sources: {sources}")

    pdf_sources_list = sources

    if is_listening:
        print(ansi.WARNING_MSG + "PDF sources changed while listening. Automatically stopping listening.")
        stop_listening()


def get_available_models():
    """Fetches available models from the Gemini API."""
    if gemini.client is None:
        print(ansi.ERROR_MSG + "Gemini client not initialized. Cannot get models.")
        return [selected_model] # Return only default if client failed

    try:
        print(ansi.INFO_MSG + "Fetching available models...")
        # List models that support generateContent and have multimodal capability
        models = [m.name for m in gemini.client.models.list()
                  if 'generateContent' in m.supported_actions]
        print(ansi.SUCCESS_MSG + f"Fetched {len(models)} available models.")

        return models

    except Exception as e:
        print(ansi.ERROR_MSG + f"Failed to fetch models: {e}")
        return [selected_model] # Return only default on error

def set_selected_model(model):
    """Sets the selected AI model from the UI."""
    global selected_model
    print(ansi.INFO_MSG + f"UI selected model: {model}")

    selected_model = model

    # Configuration changed, ensure not in listening state
    if is_listening:
        print(ansi.WARNING_MSG + "Model changed while listening. Automatically stopping listening.")
        stop_listening()


def get_token_usage():
    """Returns current token usage data."""
    global token_data
    # token_data is updated by the process_question_handler and loaded on startup
    if not token_data:
        token_data = token_db.load_token_data()
    return token_data


if __name__ == "__main__":

    # --- Prompt fájl beolvasása parancssorból ---
    if len(sys.argv) > 1:
        prompt_file_name = sys.argv[1]
        print(ansi.INFO_MSG + f"Prompt file: {prompt_file_name}")
    else:
        prompt_file_name = "default_prompt.txt"
        print(ansi.WARNING_MSG + f"Nem adtál meg prompt fájlt, alapértelmezett: {prompt_file_name}")

    # Ensure Gemini client is initialized by importing gemini module
    if gemini.client is None:
        # This condition should only be true if gemini.py failed to init and exited
        print(ansi.ERROR_MSG + "Gemini client initialization failed. Exiting...")
        sys.exit(1)

    # Load existing token usage data
    token_data = token_db.load_token_data()
    print(ansi.INFO_MSG + f"Loaded initial token data: Total={token_data['total']}, Daily for Today={token_data['daily'].get(str(date.today()), 0)}")

    # Init the tray icon with this script's instance
    trayicon = TrayIcon(quit_callback=set_quitting_flag, show_gui_callback=toggle_ui_visibility)
    trayicon.display_answer("RDY", color="green")

    print(ansi.INFO_MSG + "Initializing UI...")

    # Pass necessary callbacks to the UI instance
    ui_callbacks = {
        'start_listening': start_listening, # UI -> Main
        'stop_listening': stop_listening,   # UI -> Main
        'get_pdf_sources': lambda: pdf_sources_list, # UI -> Main (gets current list)
        'set_pdf_sources': set_pdf_sources, # UI -> Main (sets list)
        'get_available_models': get_available_models, # UI -> Main (fetches models)
        'get_selected_model': lambda: selected_model, # UI -> Main (gets current model)
        'set_selected_model': set_selected_model, # UI -> Main (sets model)
        'get_token_usage': get_token_usage, # UI -> Main (gets token data)
        'quit_app': set_quitting_flag,       # UI -> Main (signals quit)
        'toggle_ui_visibility': toggle_ui_visibility, # UI -> Main (toggles visibility)
        # UI can also call update methods on ui_app directly from main.py
    }

    try:
        ui_app = UI(main_app_callbacks=ui_callbacks)
        print(ansi.SUCCESS_MSG + "UI initialized.")

        # Note: ui_app.start_ui() includes the webview.start() blocking call
        # and runs the Flask server and log thread.

    except Exception as e:
        print(ansi.ERROR_MSG + f"Failed to initialize UI: {e}")
        print(ansi.WARNING_MSG + "Continuing without GUI. Console will be the only interface.")
        ui_app = None # Ensure ui_app is None if initialization failed

    print("-" * 50)
    print(ansi.INFO_MSG + "Setup complete.")

    # Hide the console window after a small delay
    print(ansi.INFO_MSG + "Hiding console window...")
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    win32gui.SetForegroundWindow(hwnd)
    hwnd = win32gui.GetForegroundWindow() 
    if hwnd:
        win32gui.ShowWindow(hwnd, win32con.SW_HIDE)

    if ui_app:
        # Start the UI - this call blocks the main thread until the window is closed
        print(ansi.INFO_MSG + "Starting UI application loop...")
        ui_app.start_ui() # This blocks until window is closed

    else:
        # If UI failed to initialize, fall back to console interface
        print(ansi.WARNING_MSG + "UI not available. Using console interface.")
        print(ansi.WARNING + "RESTRICTED: " + ansi.ENDC + "Hotkeys are NOT active without UI state management.")
        print(ansi.INFO_MSG + "Press Ctrl+C to quit.")

        # Keep the script running (fallback mode)
        try:
            while not quitting: # Use the quitting flag even in fallback
                time.sleep(0.1) # Polling loop fallback

        except KeyboardInterrupt:
            print(ansi.INFO_MSG + "Keyboard interrupt (Ctrl+C) detected in fallback loop.")
            set_quitting_flag() # Signal quitting

        except Exception as e:
            print(ansi.ERROR_MSG + f"An unexpected error occurred in fallback loop: {e}")
            set_quitting_flag() # Signal quitting

    # Cleanup
    print(ansi.INFO_MSG + "Application loop finished. Starting cleanup...")

    # Keyboard hook cleanup
    try:
        keyboard.unhook_all_hotkeys()
        print(ansi.INFO_MSG + "Final keyboard hotkey unhook attempt complete.")
    except Exception as e:
        # This might happen if no hotkeys were ever hooked (e.g. UI failed and never started listening)
        print(ansi.WARNING_MSG + f"Error during final hotkey unhook attempt: {e}")

    # Final message
    print(ansi.INFO_MSG + "Program finished.")
    sys.exit(0)

