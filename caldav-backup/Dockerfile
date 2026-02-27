FROM python:3.11-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir caldav

# Copy backup script
COPY backup.py .

# Create output directory
RUN mkdir -p /backup

# Make script executable
RUN chmod +x backup.py

# Entry point
ENTRYPOINT ["python", "backup.py"]
