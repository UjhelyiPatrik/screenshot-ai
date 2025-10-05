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

API_KEY_FILE = "../apikey.txt"
try:
    with open(API_KEY_FILE, "r") as file:
        key = file.read().strip()
    if not key:
        print(ansi.ERROR_MSG + f"API key file '{API_KEY_FILE}' is empty. Exiting.")
        exit()
    
    client = genai.Client(api_key=key)
    print(ansi.SUCCESS_MSG + "API key loaded successfully.")

except FileNotFoundError:
    print(ansi.ERROR_MSG + f"API key file '{API_KEY_FILE}' not found.")
    print("Please create a file named 'apikey.txt' in the same directory and paste your API key inside.")
    exit()
except Exception as e:
    print(ansi.ERROR_MSG + f"An unexpected error occurred while loading the API key: {e}")
    exit()


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

