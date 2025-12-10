import io
import os
import sys
import pathlib
import tempfile
import requests
import pyautogui

from google import genai

from ansi import ansi
from trayicon import TrayIcon

# A script abszolút elérési útja
script_dir = os.path.dirname(os.path.abspath(__file__))

# Állítsd be a munkakönyvtárat a script mappájára
os.chdir(script_dir)

client = None

API_KEY_FILE = "../apikeys.txt"
api_keys = []
last_index = -1

def _parse_last_index_line(line: str) -> int:
    """
    Parses the header line '# last_index=N' and returns N, or -1 if invalid.
    """
    line = line.strip()
    if line.startswith("#") and "last_index=" in line:
        try:
            return int(line.split("last_index=")[1].strip())
        except Exception:
            return -1
    return -1

def _read_api_keys_with_header(path: str):
    """
    Reads the header '# last_index=N' and all API keys (one per line) from the file.
    Returns (last_index, keys).
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"API key file '{path}' not found.")

    with open(path, "r", encoding="utf-8") as f:
        lines = [l.strip() for l in f.readlines() if l.strip()]

    if not lines:
        raise ValueError(f"API key file '{path}' is empty.")

    # Check first line for header
    first_line = lines[0]
    li = _parse_last_index_line(first_line)

    # Filter keys: ignore lines starting with '#' to avoid reading headers/comments as keys
    keys = [line for line in lines if not line.startswith("#")]

    if not keys:
        raise ValueError(f"API key file '{path}' contains no valid API keys.")

    # Clamp last_index to valid range
    if li < -1 or li >= len(keys):
        li = -1

    return li, keys

def _write_last_index_header(path: str, last_idx: int, keys: list[str]):
    """
    Writes back the header '# last_index=N' and the keys into the file.
    """
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# last_index={last_idx}\n")
            for k in keys:
                f.write(k + "\n")
    except Exception as e:
        print(ansi.ERROR_MSG + f"Failed to update '{path}' with last_index={last_idx}: {e}")

def _init_client_with_index(idx: int):
    """
    Initializes the Gemini client with the API key at index idx.
    """
    global client
    key = api_keys[idx]
    client = genai.Client(api_key=key)

# Load keys and initialize client
try:
    last_index, api_keys = _read_api_keys_with_header(API_KEY_FILE)
    # Use the next key after last_index for initial client setup
    initial_idx = (last_index + 1) % len(api_keys)
    _init_client_with_index(initial_idx)
    print(ansi.SUCCESS_MSG + f"Loaded {len(api_keys)} API keys. Initialized with key index: {initial_idx}.")

except FileNotFoundError as e:
    print(ansi.ERROR_MSG + str(e))
    print("Please create 'apikeys.txt' in the parent directory with one key per line.")
    sys.exit(1)
except Exception as e:
    print(ansi.ERROR_MSG + f"Error loading API keys: {e}")
    sys.exit(1)


def rotate_api_key_and_persist():
    """
    Advances to the next API key cyclically and persists the new last_index in apikeys.txt.
    Also re-initializes the client with the new key.
    """
    global last_index, api_keys
    
    if not api_keys:
        print(ansi.ERROR_MSG + "No API keys available to rotate.")
        return

    try:
        next_index = (last_index + 1) % len(api_keys)
        _init_client_with_index(next_index)
        
        # Persist the newly used index as last_index
        last_index = next_index
        _write_last_index_header(API_KEY_FILE, last_index, api_keys)
        
        print(ansi.INFO_MSG + f"Switched to API key #{next_index + 1} (Index: {next_index})")
    except Exception as e:
        print(ansi.ERROR_MSG + f"Failed to rotate and persist API key: {e}")


def take_screenshot():
    """Takes a screenshot and saves it to a temp directory."""
    temp_image_path = os.path.join(tempfile.gettempdir(), "question_screenshot.png")
    screenshot = pyautogui.screenshot()
    screenshot.save(temp_image_path)
    return temp_image_path

def load_image_part(image_path):
    """Loads an image from a file path and prepares it as a Gemini content part."""
    try:
        uploaded_file = client.files.upload(file=image_path)
        return uploaded_file
    except FileNotFoundError:
        print(ansi.ERROR_MSG + f"Image file not found at {image_path}")
        return None
    except Exception as e:
        print(ansi.ERROR_MSG + f"Error loading or processing image {image_path}: {e}")
        return None
    

def upload_pdf_part(pdf_source):
    """
    Handles uploading a PDF file (local path or URL) to Gemini and
    returns the file_data dictionary for the contents list.
    """
    is_url = pdf_source.lower().startswith('http') or pdf_source.lower().startswith('https')

    try:
        if is_url:
            print(ansi.INFO_MSG + f"Attempting to download PDF from {pdf_source}...")
            response = requests.get(pdf_source, stream=True, timeout=30)
            response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
            data = response.content
            print(ansi.SUCCESS_MSG + f"Downloaded ({len(data)} bytes).")

            # Wrap bytes in a file-like object
            file_obj = io.BytesIO(data)
            display_name = os.path.basename(pdf_source) or "downloaded.pdf"

            print(ansi.INFO_MSG + "Uploading PDF (from URL) to Gemini...")
            uploaded = client.files.upload(
                file=file_obj,
                config=dict(
                    mime_type='application/pdf',
                    display_name=display_name,
                )
            )

        else:
            local_path = pathlib.Path(pdf_source)
            print(ansi.INFO_MSG + f"Reading local PDF from {local_path!r}")
            if not local_path.exists():
                print(ansi.ERROR_MSG + f"Local file not found: {local_path!r}")
                return None

            print(ansi.INFO_MSG + f"Uploading {local_path.name!r} to Gemini...")
            uploaded = client.files.upload(
                file=local_path,
                # config is optional for local files, Gemini will infer from `.pdf`
            )

        print(ansi.SUCCESS_MSG + f"File uploaded successfully. URI: {uploaded.uri}")
        return uploaded

    except requests.exceptions.RequestException as e:
        print(ansi.ERROR_MSG + f"Error downloading PDF from {pdf_source}: {e}")
        return None
    except Exception as e:
        # Catch any other unexpected exceptions during reading/uploading
        print(ansi.ERROR_MSG + f"An unexpected error occurred during PDF upload processing for {pdf_source}: {e}")
        return None
    

def create_gemini_contents(image_path, pdf_sources, prompt_file_name):
    """
    Creates the list of content parts for the Gemini API call.
    Includes the image, uploaded PDF files, and the instruction prompt.
    """
    contents = []

    # 1. Add Image Part
    image_part = load_image_part(image_path)
    if image_part is None:
        print(ansi.ERROR_MSG + "Cannot proceed without a valid image part.")
        return None
    contents.append(image_part)
    print(ansi.SUCCESS_MSG + "Image added.")

    # 2. Add Uploaded PDF Parts (Only if upload is successful)
    uploaded_pdf_parts = []
    for source in pdf_sources:
        pdf_part = upload_pdf_part(source)
        if pdf_part:
            uploaded_pdf_parts.append(pdf_part)

    if uploaded_pdf_parts:
        contents.extend(uploaded_pdf_parts)
        print(ansi.SUCCESS_MSG + f"Successfully added {len(uploaded_pdf_parts)} uploaded PDF file parts.")
    else:
        print(ansi.WARNING_MSG + "No usable PDF files were uploaded from the provided sources.")

    # 3. Add Instruction Prompt Part (Loaded from a file)

    # set the prompt file name
    if not prompt_file_name:
        prompt_file_name = "default_prompt.txt"  # Default prompt file name if none provided
    if not prompt_file_name.endswith(".txt"):
        prompt_file_name += ".txt"

    PROMPT_FILE = "../prompt_files/" + prompt_file_name
    try:
        with open(PROMPT_FILE, "r") as file:
            instruction_prompt = file.read().strip()
        if not instruction_prompt:
            print(ansi.ERROR_MSG + f"Prompt file '{PROMPT_FILE}' is empty. Exiting.")
            exit()
        print(ansi.SUCCESS_MSG + "Prompt file loaded successfully.")
        print(ansi.SUCCESS_MSG + "API key loaded successfully.")

    except FileNotFoundError:
        print(ansi.ERROR_MSG + f"Prompt file '{PROMPT_FILE}' not found.")
        print("Please create a file named 'example.txt' in the prompt_files directory and paste your prompt inside.")
        exit()
    except Exception as e:
        print(ansi.ERROR_MSG + f"An unexpected error occurred while loading the prompt file: {e}")
        exit()


    # 3. Add the Final Instruction Text Part (Guides the model on how to respond)
    contents.append(instruction_prompt)
    print(ansi.SUCCESS_MSG + "Instruction prompt added.")

    # Basic check: Do we have at least the image and instruction prompt?
    if any(part is None for part in contents) or len(contents) < 2:
        print(ansi.ERROR_MSG + "Content creation failed. Missing image or instruction prompt.")
        return None

    return contents


def call_gemini_multimodal(contents, selected_model):
    """Calls the Gemini API with the list of multimodal content parts."""

    try:
        print(ansi.INFO_MSG + "Calling Gemini API...")
        # Use the model specified by the user
        response = client.models.generate_content(
            model=selected_model,
            contents=contents,
        )
        
        # Extract token usage
        tokens_used = 0
        if hasattr(response, 'usage_metadata') and hasattr(response.usage_metadata, 'total_token_count'):
            tokens_used = response.usage_metadata.total_token_count

        # Access the text response
        if not hasattr(response, 'text') or not response.text:
            print(ansi.WARNING_MSG + "API returned an empty response or no text content.")
            # Check for block reasons from the API
            if hasattr(response, 'prompt_feedback') and response.prompt_feedback:
                if response.prompt_feedback.block_reason:
                    print(f"API blocked response: {response.prompt_feedback.block_reason}")
                    if response.prompt_feedback.block_reason_message:
                        print(f"Block message: {response.prompt_feedback.block_reason_message}")
                else:
                    print("API returned no text, but no specific block reason provided in feedback.")

            return None, tokens_used

        answer = response.text.strip()

        # Although the prompt asks the model to keep it under 128, we add this check as a safeguard and warning.
        if len(answer) > 128:
            print(ansi.WARNING_MSG + f"API response length ({len(answer)}) exceeded the requested 128 characters.")

        return answer, tokens_used

    except Exception as e:
        if (e.__class__.__name__ == "LocalProtocolError"):
            print(ansi.FAIL + "INVALID API KEY: " + ansi.ENDC + e.__str__())

        elif ("429 RESOURCE_EXHAUSTED" in e.__str__()):
            print(ansi.ERROR_MSG + "API rate limit exceeded. Please try again later.")
            print(ansi.INFO_MSG + "If this persists, consider trying a different model or checking your API usage.")

        else:
            print(ansi.ERROR_MSG + e.__str__())

        return None
    
def process_question(trayicon: TrayIcon, pdf_sources_list, selected_model, prompt_file_name):
    # Rotate API key before processing
    rotate_api_key_and_persist()

    # Take a screenshot of the current screen
    image_path = take_screenshot()
    if not os.path.exists(image_path):
        print(ansi.ERROR_MSG + f"Image file not found at '{image_path}'.")
        return None
    
    # Prepare content and call API
    print(ansi.INFO_MSG + "Preparing content for Gemini...")

    trayicon.set_loading()
    contents = create_gemini_contents(image_path, pdf_sources_list, prompt_file_name)

    tokens_used = 0
    if contents:
        response_text, tokens_used = call_gemini_multimodal(contents, selected_model)

        print(ansi.INFO_MSG + ansi.BOLD + ansi.UNDERLINE + "Response from Gemini:" + ansi.ENDC, end=" ")
        if response_text is not None:
            print(ansi.BOLD + response_text + ansi.ENDC)
            trayicon.display_answer(response_text)
        else:
            print(ansi.ERROR_MSG + "Failed to get a valid response from the API.")
            trayicon.display_answer("ERR", color="red")
    else:
        print(ansi.ERROR_MSG + "Failed to prepare content for the API call (image or PDF upload failed).")
        trayicon.display_answer("ERR", color="red")

    # Cleanup: Remove the temporary image file
    try:
        os.remove(image_path)
        print(ansi.INFO_MSG + "Temporary image file removed.")
    except Exception as e:
        print(ansi.ERROR_MSG + f"Failed to remove temporary image file '{image_path}': {e}")
    
    return tokens_used

