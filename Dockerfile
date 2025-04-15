# Use a base image with Python and common tools
FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install CUPS client and other necessary packages
# Add build-essential and libcups2-dev if pycups is needed later
RUN apt-get update && apt-get install -y --no-install-recommends \
    cups-client \
    libcups2 \
    # Add any other system dependencies needed by Pillow (e.g., for JPEG, PNG support)
    libjpeg62-turbo \
    libpng16-16 \
    libtiff6 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements file and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY bot.py .
COPY .env .

# Command to run the bot
CMD ["python", "bot.py"]
