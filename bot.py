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


# --- Constants ---
LABEL_WIDTH_INCHES = 4
LABEL_HEIGHT_INCHES = 6
IMAGE_DPI = 300 # Assume standard print resolution

# Calculate pixel dimensions
LABEL_WIDTH_PX = LABEL_WIDTH_INCHES * IMAGE_DPI
LABEL_HEIGHT_PX = LABEL_HEIGHT_INCHES * IMAGE_DPI

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
                # Convert string timestamps back to datetime objects
                print_history = {
                    int(user_id): datetime.fromisoformat(ts)
                    for user_id, ts in history_data.items()
                }
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
        # Convert datetime objects to ISO format strings for JSON compatibility
        history_data = {
            str(user_id): ts.isoformat()
            for user_id, ts in print_history.items()
        }
        with open(PRINT_HISTORY_FILE, 'w') as f:
            json.dump(history_data, f, indent=4)
        # logger.debug(f"Saved print history to {PRINT_HISTORY_FILE}") # Optional: debug log
    except IOError as e:
        logger.error(f"Error saving print history to {PRINT_HISTORY_FILE}: {e}")

def can_print(user_id: int) -> tuple[bool, str | None]:
    """Checks if a user is allowed to print.
    Returns (True, None) if allowed.
    Returns (False, reason_message) if not allowed.
    """
    if ALLOWED_USER_IDS and user_id in ALLOWED_USER_IDS:
        return True, None # Authorized users can always print

    # Check rate limit for unauthorized users
    last_print_time = print_history.get(user_id)
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

            reason = f"You have already printed recently. Please wait {wait_str} before printing again."
            logger.info(f"Rate limit hit for user {user_id}. Time remaining: {wait_time}")
            return False, reason
        else:
            # It's been long enough, they can print again
            return True, None
    else:
        # User not in history, they can print for the first time (or first time since history reset)
        return True, None

def record_print(user_id: int):
    """Records a print action for the user and saves history."""
    global print_history
    # Use timezone-aware datetime
    print_history[user_id] = datetime.now(timezone.utc)
    logger.info(f"Recorded print for user {user_id} at {print_history[user_id]}")
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
    lp_command.extend(["-o", f"media=Custom.{LABEL_WIDTH_INCHES}x{LABEL_HEIGHT_INCHES}in"])
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
    is_authorized = ALLOWED_USER_IDS and user.id in ALLOWED_USER_IDS

    # Define base help text with placeholders
    base_help_text = (
        "<b>🤖 Bot Commands & Usage:</b>\n\n"
        "👋 /start - Display the welcome message.\n"
        "❓ /help - Show this help message.\n"
        "⚙️ /setmaxcopies &lt;number&gt; - Set the max copies allowed per print (e.g., <code>/setmaxcopies 50</code>). (Authorized users only)\n\n"
        "<b>🖨️ Printing:</b>\n"
        "Simply send an image 🖼️ to the chat. The bot will automatically resize it and print it on a 4x6 label.\n\n"
        "<b>#️⃣ Multiple Copies:</b>\n"
        "To print multiple copies, the image caption must contain <b>only</b> the copy specifier (case-insensitive, ignoring surrounding whitespace):\n"
        "• <code>x3</code> (prints 3 copies)\n"
        "• <code>copies=5</code> (prints 5 copies)\n"
        "Any other text in the caption, or no caption, will result in 1 copy being printed.\n\n"
        "<b>⚠️ Max Copies Limit:</b>\nThe maximum number of copies per request is currently <b>{}</b>."
    )
    # Add rate limiting info only if guest printing is enabled and user is not authorized
    rate_limit_info = ""
    if ALLOW_GUEST_PRINTING and not is_authorized:
        rate_limit_info = (
            "\n\n"
            "<b>⏳ Rate Limit:</b>\n"
            "As a non-authorized user, you can print one image every 7 days."
        )

    # Format the string with the current MAX_COPIES value and add rate limit info
    formatted_help_text = base_help_text.format(MAX_COPIES) + rate_limit_info
    await update.message.reply_html(formatted_help_text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message when the /start command is issued."""
    user = update.effective_user
    # No authorization check here, anyone can start

    welcome_message = rf"Hi {user.mention_html()}! Send me an image to print on the label printer."

    # Add printer configuration warning if needed
    if not CUPS_PRINTER_NAME:
        warning_message = "\n\n<b>⚠️ Warning:</b> The printer is not configured. Printing is currently disabled. Please contact the administrator."
        welcome_message += warning_message
        logger.warning(f"Informing user {user.id} via /start that printer is not configured.")

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

    # Parse copies from caption
    caption = update.message.caption
    copies = parse_copies(caption)

    await update.message.reply_text(f"Received image. Resizing and preparing to print {copies} cop{'y' if copies == 1 else 'ies'}...")

    # Resize the image
    resized_image_buffer, image_format = resize_image(file_bytes)

    if not resized_image_buffer:
        await update.message.reply_text("Failed to process the image.")
        return

    # Print the image
    success, message = print_image_cups(resized_image_buffer, CUPS_PRINTER_NAME, copies, image_format)

    if success:
        logger.info(f"Successfully sent image to printer {CUPS_PRINTER_NAME} for user {user.id} ({user.username})")
        await update.message.reply_text(f"Sent {copies} cop{'y' if copies == 1 else 'ies'} to printer! CUPS message: {message}")
        # Record the print time only if the user is NOT in the permanently allowed list
        if not (ALLOWED_USER_IDS and user.id in ALLOWED_USER_IDS):
            record_print(user.id)
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
