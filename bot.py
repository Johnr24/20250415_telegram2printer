# Telefax - A Telegram bot to print images via CUPS
# Copyright (C) 2025 <Your Name or Organization>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

import logging
import os
import tempfile
import subprocess
import json
from datetime import datetime, timedelta, timezone
from io import BytesIO
from dotenv import load_dotenv
from PIL import Image

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# Load environment variables from .env file
load_dotenv()

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Get configuration from environment variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CUPS_PRINTER_NAME = os.getenv("CUPS_PRINTER_NAME")
CUPS_SERVER_HOST = os.getenv("CUPS_SERVER_HOST", None) # Optional: Use if CUPS server is remote
ALLOWED_USER_IDS = os.getenv("ALLOWED_USER_IDS", "").split(',')
ALLOWED_USER_IDS = [int(user_id) for user_id in ALLOWED_USER_IDS if user_id.isdigit()] # Convert to list of integers
try:
    MAX_COPIES = int(os.getenv("MAX_COPIES", 100))
except ValueError:
    logger.warning("Invalid MAX_COPIES value in environment. Defaulting to 100.")
    MAX_COPIES = 100
if MAX_COPIES <= 0:
    logger.warning("MAX_COPIES must be positive. Defaulting to 100.")
    MAX_COPIES = 100
# Guest printing setting (defaults to True if not set or invalid)
ALLOW_GUEST_PRINTING = os.getenv("ALLOW_GUEST_PRINTING", "True").lower() in ('true', '1', 'yes')

# --- Configurable Constants ---
try:
    LABEL_WIDTH_INCHES = float(os.getenv("LABEL_WIDTH_INCHES", 4))
except ValueError:
    logger.warning("Invalid LABEL_WIDTH_INCHES value in environment. Defaulting to 4.")
    LABEL_WIDTH_INCHES = 4
if LABEL_WIDTH_INCHES <= 0:
    logger.warning("LABEL_WIDTH_INCHES must be positive. Defaulting to 4.")
    LABEL_WIDTH_INCHES = 4

try:
    LABEL_HEIGHT_INCHES = float(os.getenv("LABEL_HEIGHT_INCHES", 6))
except ValueError:
    logger.warning("Invalid LABEL_HEIGHT_INCHES value in environment. Defaulting to 6.")
    LABEL_HEIGHT_INCHES = 6
if LABEL_HEIGHT_INCHES <= 0:
    logger.warning("LABEL_HEIGHT_INCHES must be positive. Defaulting to 6.")
    LABEL_HEIGHT_INCHES = 6

IMAGE_DPI = 300 # Assume standard print resolution

# Calculate pixel dimensions
LABEL_WIDTH_PX = int(LABEL_WIDTH_INCHES * IMAGE_DPI)
LABEL_HEIGHT_PX = int(LABEL_HEIGHT_INCHES * IMAGE_DPI)

# --- Constants for Rate Limiting ---
PRINT_HISTORY_FILE = "print_history.json"
UNAUTHORIZED_USER_PRINT_INTERVAL = timedelta(days=7)

# --- Print History Management ---
print_history = {} # In-memory cache of print history

def load_print_history():
    """Loads print history from the JSON file."""
    global print_history
    try:
        if os.path.exists(PRINT_HISTORY_FILE):
            with open(PRINT_HISTORY_FILE, 'r') as f:
                history_data = json.load(f)
                loaded_history = {}
                for user_id_str, data in history_data.items():
                    try:
                        user_id = int(user_id_str)
                        if isinstance(data, dict): # New format
                            last_print = datetime.fromisoformat(data.get("last_print", ""))
                            username = data.get("username", "Unknown")
                            loaded_history[user_id] = {"last_print": last_print, "username": username}
                        elif isinstance(data, str): # Old format (just timestamp)
                            last_print = datetime.fromisoformat(data)
                            loaded_history[user_id] = {"last_print": last_print, "username": "Unknown"}
                        else:
                             logger.warning(f"Skipping invalid data type for user {user_id_str} in history file.")
                    except (ValueError, TypeError) as parse_err:
                        logger.warning(f"Skipping entry for user {user_id_str} due to parsing error: {parse_err}")

                print_history = loaded_history
                logger.info(f"Loaded print history for {len(print_history)} users from {PRINT_HISTORY_FILE}")
        else:
            logger.info(f"{PRINT_HISTORY_FILE} not found. Starting with empty history.")
            print_history = {}
    except (json.JSONDecodeError, IOError, ValueError) as e:
        logger.error(f"Error loading print history from {PRINT_HISTORY_FILE}: {e}. Starting with empty history.")
        print_history = {} # Reset history on error

def save_print_history():
    """Saves the current print history to the JSON file."""
    global print_history
    try:
        # Convert history data to JSON serializable format
        history_data_to_save = {}
        for user_id, data in print_history.items():
            history_data_to_save[str(user_id)] = {
                "last_print": data["last_print"].isoformat(),
                "username": data.get("username", "Unknown") # Ensure username exists
            }

        with open(PRINT_HISTORY_FILE, 'w') as f:
            json.dump(history_data_to_save, f, indent=4)
        # logger.debug(f"Saved print history to {PRINT_HISTORY_FILE}") # Optional: debug log
    except IOError as e:
        logger.error(f"Error saving print history to {PRINT_HISTORY_FILE}: {e}")

def can_print(user_id: int) -> tuple[bool, str | None]:
    """Checks if a user is allowed to print.
    Returns (True, None) if allowed.
    Returns (False, reason_message) if not allowed.
    """
    is_authorized = ALLOWED_USER_IDS and user_id in ALLOWED_USER_IDS

    if is_authorized:
        return True, None # Authorized users can always print

    # --- Rate Limit Check (applies to all non-authorized users) ---
    user_data = print_history.get(user_id)
    last_print_time = user_data.get("last_print") if user_data else None
    is_rate_limited = False
    rate_limit_reason = None

    if last_print_time:
        time_since_last_print = datetime.now(timezone.utc) - last_print_time
        if time_since_last_print < UNAUTHORIZED_USER_PRINT_INTERVAL:
            wait_time = UNAUTHORIZED_USER_PRINT_INTERVAL - time_since_last_print
            # Format wait time nicely (e.g., "X days, Y hours")
            days = wait_time.days
            hours, remainder = divmod(wait_time.seconds, 3600)
            minutes, _ = divmod(remainder, 60)
            wait_str = f"{days} day{'s' if days != 1 else ''}" if days > 0 else ""
            if hours > 0:
                wait_str += f"{', ' if days > 0 else ''}{hours} hour{'s' if hours != 1 else ''}"
            if days == 0 and hours == 0 and minutes > 0: # Show minutes if less than an hour
                 wait_str += f"{minutes} minute{'s' if minutes != 1 else ''}"
            if not wait_str: # Less than a minute
                wait_str = "less than a minute"

            rate_limit_reason = f"You have already printed recently. Please wait {wait_str} before printing again."
            logger.info(f"Rate limit check for user {user_id}: Still within cooldown. Time remaining: {wait_time}")
            is_rate_limited = True
        # else: User is outside the cooldown period.

    # --- Guest Printing Logic ---
    if is_rate_limited:
        # If rate limited, always return the rate limit reason, regardless of guest setting
        return False, rate_limit_reason
    else:
        # If not rate limited, check if guest printing is allowed
        if ALLOW_GUEST_PRINTING:
            # Guest printing enabled and user is not rate limited -> Allow print
            return True, None
        else:
            # Guest printing disabled and user is not rate limited -> Deny print
            logger.warning(f"Guest printing disabled. Rejecting print for non-authorized user {user_id} (passed rate limit check).")
            return False, "Printing is restricted to authorized users only."


def record_print(user_id: int, username: str | None):
    """Records a print action for the user (including username) and saves history."""
    global print_history
    now = datetime.now(timezone.utc)
    user_display_name = username or "Unknown" # Use "Unknown" if username is None
    print_history[user_id] = {"last_print": now, "username": user_display_name}
    logger.info(f"Recorded print for user {user_id} ({user_display_name}) at {now}")
    save_print_history()


# --- Helper Functions ---

def resize_image(image_bytes):
    """Resizes an image to fit within the label dimensions while maintaining aspect ratio."""
    try:
        img = Image.open(BytesIO(image_bytes))
        img.thumbnail((LABEL_WIDTH_PX, LABEL_HEIGHT_PX), Image.Resampling.LANCZOS)

        # Optional: Create a white background and paste the resized image onto it
        # This ensures the output is always 4x6, even if the aspect ratio doesn't match perfectly.
        # background = Image.new('RGB', (LABEL_WIDTH_PX, LABEL_HEIGHT_PX), (255, 255, 255))
        # paste_x = (LABEL_WIDTH_PX - img.width) // 2
        # paste_y = (LABEL_HEIGHT_PX - img.height) // 2
        # background.paste(img, (paste_x, paste_y))
        # img = background # Use the background image now

        output_buffer = BytesIO()
        # Save as PNG or JPEG, depending on what CUPS handles better (PNG often preferred for graphics)
        img_format = 'PNG' if img.mode == 'RGBA' or 'P' in img.mode else 'JPEG'
        img.save(output_buffer, format=img_format)
        output_buffer.seek(0)
        return output_buffer, img_format.lower()
    except Exception as e:
        logger.error(f"Error resizing image: {e}")
        return None, None

def print_image_cups(image_buffer, printer_name, copies=1, image_format='png'):
    """Sends the image data to the specified CUPS printer."""
    lp_command = ["lp"]

    if CUPS_SERVER_HOST:
        lp_command.extend(["-h", CUPS_SERVER_HOST])

    lp_command.extend(["-d", printer_name])
    lp_command.extend(["-n", str(copies)])
    # Add options for 4x6 media size and scaling. Adjust these based on your printer driver!
    # Common options: 'media=w101h152mm' or 'media=Custom.4x6in'
    # Scaling: 'fit-to-page' or 'scaling=100'
    # You might need to experiment with `lpoptions -p <printer_name> -l` on the CUPS server
    # to find the exact options your printer supports.
    # Format dimensions for CUPS - handle potential floats by converting to int/string as needed
    # CUPS might prefer 'media=Custom.4x6in' or 'media=w101h152mm' depending on driver
    # Using the Custom.WxHin format seems common.
    media_option = f"media=Custom.{LABEL_WIDTH_INCHES:.2f}x{LABEL_HEIGHT_INCHES:.2f}in".replace('.00', '') # Format nicely
    lp_command.extend(["-o", media_option])
    lp_command.extend(["-o", "fit-to-page"]) # Try to scale the image to fit the media
    # lp_command.extend(["-o", "scaling=100"]) # Alternative: print at 100%

    # Use a temporary file to pass data to lp
    try:
        with tempfile.NamedTemporaryFile(suffix=f'.{image_format}', delete=True) as temp_file:
            temp_file.write(image_buffer.getvalue())
            temp_file.flush() # Ensure data is written to disk

            lp_command.append(temp_file.name) # Add filename to command

            logger.info(f"Executing CUPS command: {' '.join(lp_command)}")
            result = subprocess.run(lp_command, capture_output=True, text=True, check=True)
            logger.info(f"CUPS Output: {result.stdout}")
            logger.info(f"CUPS Error Output: {result.stderr}") # Log stderr as well
            return True, result.stdout
    except subprocess.CalledProcessError as e:
        logger.error(f"CUPS printing failed. Command: '{' '.join(e.cmd)}'")
        logger.error(f"Return code: {e.returncode}")
        logger.error(f"Output: {e.output}")
        logger.error(f"Stderr: {e.stderr}")
        return False, e.stderr
    except Exception as e:
        logger.error(f"An unexpected error occurred during printing: {e}")
        return False, str(e)

def parse_copies(caption):
    """Parses the number of copies from the caption.
    Requires the caption to be exactly 'x<number>' or 'copies=<number>' (case-insensitive, ignoring surrounding whitespace).
    Defaults to 1 otherwise.
    """
    if not caption:
        return 1

    caption = caption.strip().lower() # Remove whitespace and convert to lower case

    import re

    # Check for exact match 'x<number>'
    match_x = re.fullmatch(r'x(\d+)', caption)
    if match_x:
        try:
            copies = int(match_x.group(1))
            # Add a sanity check for unreasonably large numbers
            if 1 <= copies <= MAX_COPIES: # Limit copies based on env var
                return copies
            else:
                logger.warning(f"User requested {copies} copies, which is outside the allowed range (1-{MAX_COPIES}). Defaulting to 1.")
                return 1
        except ValueError:
            # This case should ideally not be reached due to \d+
            logger.error(f"Could not parse number in caption '{caption}' despite regex match.")
            return 1

    # Check for exact match 'copies=<number>'
    match_copies = re.fullmatch(r'copies\s*=\s*(\d+)', caption)
    if match_copies:
        try:
            copies = int(match_copies.group(1))
            # Add a sanity check for unreasonably large numbers
            if 1 <= copies <= MAX_COPIES: # Limit copies based on env var
                return copies
            else:
                logger.warning(f"User requested {copies} copies, which is outside the allowed range (1-{MAX_COPIES}). Defaulting to 1.")
                return 1
        except ValueError:
            # This case should ideally not be reached due to \d+
            logger.error(f"Could not parse number in caption '{caption}' despite regex match.")
            return 1

    # If caption is not empty but didn't match the exact formats, default to 1
    logger.info(f"Caption '{caption}' did not match copy format. Defaulting to 1 copy.")
    return 1

# --- Telegram Bot Handlers ---

async def set_max_copies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows authorized users to set the maximum number of copies."""
    user = update.effective_user
    # --- Authorization Check ---
    # Only authorized users can change settings
    if ALLOWED_USER_IDS and user.id not in ALLOWED_USER_IDS:
        logger.warning(f"Unauthorized /setmaxcopies attempt by user {user.id} ({user.username})")
        await update.message.reply_text("Sorry, you are not authorized to use this command.")
        return
    # --- End Authorization Check ---

    global MAX_COPIES # Declare intention to modify the global variable

    args = context.args
    if not args or len(args) != 1:
        await update.message.reply_text("Usage: /setmaxcopies <number>\nExample: /setmaxcopies 50")
        return

    try:
        new_max = int(args[0])
        if new_max <= 0:
            await update.message.reply_text("Maximum copies must be a positive number.")
            return
        # Optional: Add an upper sanity limit if desired, e.g., 1000
        # if new_max > 1000:
        #     await update.message.reply_text("Setting maximum copies above 1000 is not allowed.")
        #     return

        MAX_COPIES = new_max
        logger.info(f"User {user.id} set MAX_COPIES to {MAX_COPIES}")
        await update.message.reply_text(f"Maximum copies per request set to <b>{MAX_COPIES}</b> for this session.", parse_mode='HTML')

    except ValueError:
        await update.message.reply_text("Invalid number provided. Please enter a whole number.")
    except Exception as e:
        logger.error(f"Error setting max copies: {e}")
        await update.message.reply_text("An error occurred while setting the maximum copies.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a help message when the /help command is issued."""
    user = update.effective_user
    logger.info(f"User {user.id} ({user.username}) entered /help command.") # DIAGNOSTIC LOG
    is_authorized = ALLOWED_USER_IDS and user.id in ALLOWED_USER_IDS

    # Define base help text using an f-string and escape HTML special chars
    # Use .replace('.0', '') for cleaner display if width/height are whole numbers
    label_width_str = str(LABEL_WIDTH_INCHES).replace('.0', '')
    label_height_str = str(LABEL_HEIGHT_INCHES).replace('.0', '')

    # --- Construct Help Text Based on Authorization ---
    if is_authorized:
        # Full help text for authorized users
        base_help_text = (
            f"<b>🤖 Bot Commands & Usage:</b>\n\n"
            f"👋 /start - Display the welcome message.\n"
            f"❓ /help - Show this help message.\n"
            f"⚙️ /setmaxcopies &lt;number&gt; - Set the max copies allowed per print (e.g., <code>/setmaxcopies 50</code>). (Authorized users only)\n\n"
            f"<b>🖨️ Printing:</b>\n"
            f"Simply send an image 🖼️ to the chat. The bot will automatically resize it and print it on a {label_width_str}x{label_height_str} inch label.\n\n"
            f"<b>#️⃣ Multiple Copies:</b>\n"
            f"To print multiple copies, the image caption must contain <b>only</b> the copy specifier (case-insensitive, ignoring surrounding whitespace):\n"
            f"• <code>x3</code> (prints 3 copies)\n"
            f"• <code>copies=5</code> (prints 5 copies)\n"
            f"Any other text in the caption, or no caption, will result in 1 copy being printed.\n\n"
            f"<b>⚠️ Max Copies Limit:</b>\nThe maximum number of copies per request is currently <b>{MAX_COPIES}</b>."
        )
        # Guest status is less relevant for authorized users, but we can keep it for consistency or remove if desired.
        # Let's keep it for now.
    else:
        # Simplified help text for non-authorized users
         base_help_text = (
            f"<b>🤖 Bot Commands & Usage:</b>\n\n"
            f"👋 /start - Display the welcome message.\n"
            f"❓ /help - Show this help message.\n\n"
            f"<b>🖨️ Printing:</b>\n"
            f"Simply send an image 🖼️ to the chat. The bot will automatically resize it and print <b>one copy</b> on a {label_width_str}x{label_height_str} inch label."
            # No mention of /setmaxcopies, multiple copies, or max limit.
        )

    # --- Add Guest/Rate Limit Info (Applies mostly to non-authorized, but shown to all for now) ---
    guest_status_info = ""
    if ALLOW_GUEST_PRINTING:
        guest_status_info = (
            "\n\n"
            "<b>👤 Guest Printing:</b>\n"
            "Guest printing is currently <b>enabled</b>. Users not on the authorized list can print one image every 7 days."
        )
    else:
        guest_status_info = (
            "\n\n"
            "<b>👤 Guest Printing:</b>\n"
            "Guest printing is currently <b>disabled</b>. Only authorized users can print."
        )

    # Combine base text and guest status info
    help_text_to_send = base_help_text + guest_status_info

    # Add rate limit status for non-authorized users
    if not is_authorized:
        can_print_now, reason = can_print(user.id)
        if not can_print_now and reason and "Please wait" in reason: # Check if rate limited
             # Append the specific rate limit reason
             rate_limit_warning = f"\n\n<b>⏳ Status:</b> {reason}"
             help_text_to_send += rate_limit_warning

    await update.message.reply_html(help_text_to_send)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message when the /start command is issued."""
    user = update.effective_user
    logger.info(f"User {user.id} ({user.username}) entered /start command.") # DIAGNOSTIC LOG
    # No authorization check here, anyone can start

    welcome_message = rf"Hi {user.mention_html()}! Send me an image to print on the label printer."

    # Add printer configuration warning if needed
    if not CUPS_PRINTER_NAME:
        warning_message = "\n\n<b>⚠️ Warning:</b> The printer is not configured. Printing is currently disabled. Please contact the administrator."
        welcome_message += warning_message
        logger.warning(f"Informing user {user.id} via /start that printer is not configured.")

    # Add rate limit status for non-authorized users
    is_authorized = ALLOWED_USER_IDS and user.id in ALLOWED_USER_IDS
    if not is_authorized:
        can_print_now, reason = can_print(user.id)
        if not can_print_now and reason and "Please wait" in reason: # Check if rate limited
             # Append the specific rate limit reason
             rate_limit_warning = f"\n\n<b>⏳ Status:</b> {reason}"
             welcome_message += rate_limit_warning

    await update.message.reply_html(welcome_message)

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles incoming photos, checks authorization/rate limits, resizes, and prints."""
    user = update.effective_user

    # --- Authorization & Rate Limit Check ---
    is_allowed_to_print, reason = can_print(user.id)
    if not is_allowed_to_print:
        logger.warning(f"Print rejected for user {user.id} ({user.username}). Reason: {reason}")
        await update.message.reply_text(f"Sorry, you cannot print right now. {reason}")
        return
    # --- End Check ---

    if not update.message.photo:
        # This check might be redundant if the handler only triggers on photos, but good practice.
        await update.message.reply_text("Please send an image file.")
        return

    if not CUPS_PRINTER_NAME:
        logger.error("CUPS_PRINTER_NAME environment variable is not set.")
        await update.message.reply_text("Printer is not configured. Please contact the administrator.")
        return

    # Get the highest resolution photo
    photo_file = await update.message.photo[-1].get_file()
    file_bytes = await photo_file.download_as_bytearray()

    # Determine copies based on authorization
    is_authorized = ALLOWED_USER_IDS and user.id in ALLOWED_USER_IDS
    caption = update.message.caption
    requested_copies = 1 # Default
    if is_authorized:
        requested_copies = parse_copies(caption) # Authorized users can request multiple copies
        copies_to_print = requested_copies
        copies_message = f"{copies_to_print} cop{'y' if copies_to_print == 1 else 'ies'}"
    else:
        # Unauthorized users always print 1 copy
        copies_to_print = 1
        requested_copies_parsed = parse_copies(caption) # Check if they tried to request more
        if requested_copies_parsed > 1:
            copies_message = f"1 copy (multiple copies ignored for guest users)"
            logger.info(f"Unauthorized user {user.id} requested {requested_copies_parsed} copies, printing 1.")
        else:
            copies_message = "1 copy"


    await update.message.reply_text(f"Received image. Resizing for {LABEL_WIDTH_INCHES}x{LABEL_HEIGHT_INCHES}in label and preparing to print {copies_message}...")

    # Resize the image
    resized_image_buffer, image_format = resize_image(file_bytes)

    if not resized_image_buffer:
        await update.message.reply_text("Failed to process the image.")
        return

    # Print the image using copies_to_print
    success, message = print_image_cups(resized_image_buffer, CUPS_PRINTER_NAME, copies_to_print, image_format)

    if success:
        logger.info(f"Successfully sent image to printer {CUPS_PRINTER_NAME} for user {user.id} ({user.username}), copies: {copies_to_print}")
        await update.message.reply_text(f"Sent {copies_to_print} cop{'y' if copies_to_print == 1 else 'ies'} to printer! CUPS message: {message}")
        # Record the print time only if the user is NOT in the permanently allowed list
        # and guest printing is enabled (implicitly checked by can_print)
        # is_authorized = ALLOWED_USER_IDS and user.id in ALLOWED_USER_IDS # Already determined above
        if ALLOW_GUEST_PRINTING and not is_authorized:
            # Pass user ID and username to record_print
            record_print(user.id, user.username)
    else:
        logger.error(f"Failed to print image for user {user.id} ({user.username}). Error: {message}")
        await update.message.reply_text(f"Failed to send to printer. Error: {message}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the developer."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    # Optionally, send a message to a specific chat ID (e.g., admin) about the error
    # traceback_str = ''.join(traceback.format_exception(None, context.error, context.error.__traceback__))
    # await context.bot.send_message(chat_id=DEVELOPER_CHAT_ID, text=f"An error occurred: {context.error}\n{traceback_str[:4000]}")


def main() -> None:
    """Start the bot."""
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN environment variable is not set. Exiting.")
        return
    if not CUPS_PRINTER_NAME:
        logger.warning("CUPS_PRINTER_NAME environment variable is not set. Printing will fail.")
        # Allow starting, but printing won't work until configured.

    if ALLOWED_USER_IDS:
        logger.info(f"Bot access restricted to user IDs: {ALLOWED_USER_IDS}")
        if ALLOW_GUEST_PRINTING:
            logger.info("Guest printing ENABLED (1 print per week limit applies to non-authorized users).")
        else:
            logger.info("Guest printing DISABLED. Only authorized users can print.")
    else:
        if ALLOW_GUEST_PRINTING:
            logger.warning("ALLOWED_USER_IDS is not set. Bot is open to everyone (1 print per week limit applies).")
        else:
            # This state is a bit contradictory - no allowed users, but guest printing off? Log a warning.
            logger.warning("ALLOWED_USER_IDS is not set AND Guest printing is DISABLED. No one can print!")

    # Load print history from file (only relevant if guest printing is enabled)
    if ALLOW_GUEST_PRINTING:
        load_print_history()

    # Create the Application and pass it your bot's token.
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # on different commands - answer in Telegram
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("setmaxcopies", set_max_copies_command))

    # on non command i.e message - handle the image message
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_image))

    # Add error handler
    application.add_error_handler(error_handler)

    # Run the bot until the user presses Ctrl-C
    logger.info("Starting bot polling...")
    application.run_polling()

if __name__ == "__main__":
    main()
