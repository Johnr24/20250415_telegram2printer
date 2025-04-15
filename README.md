# Telefax

A Telegram bot that receives images and prints them to a CUPS-managed printer. Think of it like faxing yourself through Telegram!

While designed to work with any CUPS-compatible printer, it was developed with the Dymo LabelWriter 4XL (4x6 inch label printer) particularly in mind.

## Features

*   Receives images sent via Telegram.
*   Resizes images to fit standard 4x6 inch labels (or configurable dimensions).
*   Prints images to a specified CUPS printer.
*   Supports printing multiple copies via image caption (e.g., "3 copies").
*   Restricts usage to allowed Telegram user IDs.
*   Optional command to set a maximum number of copies per print job.

## Setup

1.  **Clone the repository:**
    ```bash
    git clone <your-repo-url>
    cd telefax # Or your repository directory name
    ```
2.  **Configure Environment Variables:**
    Copy the `.env.template` file to `.env` and fill in the required values:
    ```bash
    cp .env.template .env
    ```
    Edit `.env` with your details:
    *   `TELEGRAM_BOT_TOKEN`: Your Telegram Bot Token obtained from BotFather.
    *   `CUPS_PRINTER_NAME`: The name of your printer as configured in CUPS.
    *   `ALLOWED_USER_IDS`: A comma-separated list of Telegram user IDs allowed to use the bot.
    *   `CUPS_SERVER_HOST` (Optional): The hostname or IP address if your CUPS server is running on a different machine than the bot.
    *   `MAX_COPIES` (Optional): Set a default maximum number of copies allowed per print job. Defaults internally if not set.

3.  **Build and Run with Docker Compose:**
    This is the recommended way to run the bot.
    ```bash
    docker-compose up --build -d
    ```

## Usage

1.  **Start a chat** with your bot on Telegram.
2.  **Send an image** to the bot.
3.  **(Optional)** Add a caption to the image specifying the number of copies, like `3 copies` or `copies: 5`. If no caption is provided, it defaults to 1 copy.
4.  The bot will resize the image and send it to the configured CUPS printer.

### Commands

*   `/start`: Displays a welcome message.
*   `/help`: Shows help information.
*   `/setmaxcopies <number>`: (Admin only, if configured) Sets the maximum number of copies allowed per print job.

## Contributing

Contributions are welcome! Please feel free to submit a pull request or open an issue.
