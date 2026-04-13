# Use Python 3.9
FROM python:3.9-slim

# Install system dependencies (git needed for pyCraft from GitHub)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first (Docker layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project files
COPY . .

# Create necessary directories
RUN mkdir -p uploads results

# Expose Hugging Face default port
EXPOSE 7860

# Run with gunicorn — threads required for SSE streaming
CMD ["gunicorn", "--bind", "0.0.0.0:7860", "--workers", "1", "--threads", "8", "--timeout", "120", "app:app"]
