# Telefax 📠

A Telegram bot that receives images and prints them to a CUPS-managed printer. Think of it like faxing yourself through Telegram!

While designed to work with any CUPS-compatible printer, it was developed with the Dymo LabelWriter 4XL (4x6 inch label printer) particularly in mind. 🏷️

## ✨ Features

*   📥 Receives images sent via Telegram.
*   📐 Resizes images to fit configurable label dimensions (defaults to 4x6 inches).
*   🖨️ Prints images to a specified CUPS printer.
*   🔢 Supports printing multiple copies via image caption (e.g., "3 copies").
*   🔒 Restricts usage to allowed Telegram user IDs.
*   ⚙️ Optional command to set a maximum number of copies per print job.

## 🛠️ Setup

1.  **Clone the repository:** 📂
    ```bash
    git clone https://github.com/Johnr24/telefax
    cd telefax # Or your repository directory name
    ```
2.  **Configure Environment Variables:** 📝
    Copy the `.env.template` file to `.env` and fill in the required values:
    ```bash
    cp .env.template .env
    ```
    Edit `.env` with your details:
    *   `TELEGRAM_BOT_TOKEN`: Your Telegram Bot Token obtained from BotFather.
    *   `CUPS_PRINTER_NAME`: The name of your printer as configured in CUPS.
    *   `ALLOWED_USER_IDS`: A comma-separated list of Telegram user IDs allowed to use the bot.
    *   `CUPS_SERVER_HOST` (Optional): The hostname or IP address if your CUPS server is running on a different machine than the bot.
    *   `MAX_COPIES` (Optional): Set a default maximum number of copies allowed per print job. Defaults to 100 if not set.
    *   `LABEL_WIDTH_INCHES` (Optional): The width of the label in inches. Defaults to 4 if not set.
    *   `LABEL_HEIGHT_INCHES` (Optional): The height of the label in inches. Defaults to 6 if not set.

3.  **Build and Run with Docker Compose:** 🐳
    docker-compose up --build -d
    ```

## 🚀 Usage

1.  💬 **Start a chat** with your bot on Telegram.
2.  🖼️ **Send an image** to the bot.
3.  **(Optional)** Add a caption to the image specifying the number of copies, like `3 copies` or `copies: 5`. If no caption is provided, it defaults to 1 copy.
4.  🤖 The bot will resize the image to fit the configured label dimensions (default 4x6 inches) and send it to the configured CUPS printer.

### 🤖 Commands

*   `/start`: Displays a welcome message 👋
*   `/help`: Shows help information ℹ️
*   `/setmaxcopies <number>`: (Admin only, if configured) Sets the maximum number of copies allowed per print job 👮

## 🙌 Contributing

Contributions are welcome! Please feel free to submit a pull request or open an issue.
