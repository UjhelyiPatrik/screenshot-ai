from PIL import Image, ImageDraw, ImageFont
import pystray
import threading
from pystray import MenuItem as item

from ansi import ansi

TRAYICON_MSG = ansi.OKCYAN + "TRAYICON: " + ansi.ENDC

class TrayIcon:
    def __init__(self, quit_callback, show_gui_callback):
        self.icon = None
        self.quit_callback = quit_callback
        self.show_gui_callback = show_gui_callback
        print(TRAYICON_MSG + "Initialized.")

    def create_image(self, answer="", color="black"):
        # Create an image for the icon
        print(TRAYICON_MSG + "Creating icon image...")

        width, height = 64, 64
        image = Image.new('RGBA', (width, height), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)

        # Set background color
        draw.rectangle((0, 0, width, height), fill=color)

        # Load a custom font with a larger size
        try:
            font = ImageFont.truetype("arial.ttf", 32)  # You can change the font and size here
        except IOError:
            font = ImageFont.load_default()  # Fallback to default font if custom font isn't found

        text = answer

        # Calculate the bounding box of the text
        text_bbox = draw.textbbox((0, 0), text, font=font)
        text_width, text_height = text_bbox[2] - text_bbox[0], text_bbox[3] - text_bbox[1]

        # Position text at the center
        text_x = (width - text_width) / 2
        text_y = (height - text_height) / 2

        # Draw the text on the icon
        draw.text((text_x, text_y), text, fill="white", font=font)

        print(TRAYICON_MSG + "Icon image created.")

        return image

    def display_answer(self, answer, color="black"):
        """Displays the answer in the taskbar using a system tray icon."""

        if self.icon is not None:
            self.icon.stop()  # Stop the previous icon if it exists

        max_length = 120
        if len(answer) > max_length:
            answer = answer[:max_length] + "..."  # Truncate and add "..."

        # Create a new icon
        self.icon = pystray.Icon("Gemini Answer")
        self.icon.icon = self.create_image(answer, color=color)
        self.icon.title = answer

        # Create a right-click menu
        self.icon.menu = pystray.Menu(
            item('Toggle GUI visibility', lambda icon, item: self.show_gui_callback()),
            item('Quit', lambda icon, item: self.quit_callback()),
        )

        # Run the icon in the system tray
        threading.Thread(target=self.icon.run, daemon=True).start()

    def set_loading(self):
        """Displays a loading icon in the taskbar using a system tray icon."""

        if self.icon is not None:
            self.icon.stop()  # Stop the previous icon if it exists

        print(TRAYICON_MSG + "Displaying loading icon...")

        # Create a new icon
        self.icon = pystray.Icon("Gemini Answer")
        self.icon.icon = self.create_image("...", color="navy")
        self.icon.title = "Loading..."

        # Create a right-click menu
        self.icon.menu = pystray.Menu(
            item('Toggle GUI visibility', lambda icon, item: self.show_gui_callback()),
            item('Quit', lambda icon, item: self.quit_callback()),
        )

        # Run the icon in the system tray
        threading.Thread(target=self.icon.run, daemon=True).start()