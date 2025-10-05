# Screenshot AI

Screenshot AI takes a screenshot of your screen when a keybind is pressed, prompts an LLM to answer the question visible in the screenshot, then shows the answer in a tray icon. All this without anyone realizing you are using AI to answer the quesion.

## Credits
The project was originally developed by Dávid Strumpf. I’ve only added a new feature.

## Setup
* You have to have Python installed on your system. The program was tested under Python 3.12.6
* Run this command from the cloned directory to install dependencies:
  ```console
  pip install -r requirements.txt
  ```
* After that, you can start the app via starting scrai.bat or double clicking on source/main.py

## Setup advanced
If you add the project root directory to the PATH environment variable, then you will be able to open the app from any terminal window using the command
```console
scrai
```

## Own prompts 
If you want to write an own prompt for the app, you can do that by creating a `.txt` file in the `./prompt_files` directory. To set that file as the file to use for the program you should provide the filename after the scrai command as the first argument.
### Example
If you make a prompt file in the `./prompt_files` directory called example.txt you can write
```console
C:\>scrai example.txt
```
or without the file extension
```console
C:\>scrai example
```

## Features & Usage
* You can extend the AI-s knowledge by uploading files
    - paste a link to a pdf and click "Add pdf"
    - click "Browse file" to upload from your device
* Choose the AI model to process the question
* Check out your daily and all-time token usage statistics
* See logs to know exactly what happens in the background
* Click start listening to listen for keybinds
* Optionally, you can hide the config window
* Finally, navigate to your question, and press `Ctrl+Shift+Q`

> [!NOTE]
> Some models are only available in the paid tier thus returning an error when called using free-tier API token

> [!IMPORTANT]
> When listening is in progress, settings cannot be modified

## Tray icon
When opening the program, a tray icon appears in the bottom right corner in your taskbar.

### Status indication
* Green background, displaying "RDY" means the program is running. It might not listen to keybinds though.
* Yellow background indicates loading.
* Red background, showing "ERR" means an error has occured. Check logs.
* Black/Gray background with some content, show the answer to the previous prompt.

> [!TIP]
> If you do not see the tray icon, click on the small upwards-pointing arrow, it might be there. In Windows Settings, you can pin the icon so it never gets hidden under the popup menu.

### Tray icon actions
By right clicking on the tray icon, you have two options:
* Show/Hide the config window
* Close the program

## Config window
![image](https://github.com/user-attachments/assets/30c8f79d-4d64-43e3-a29e-b8af016210fa)
