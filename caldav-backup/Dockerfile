FROM python:3.11-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir caldav

# Copy backup script
COPY backup.py .
COPY entrypoint.sh .

# Create output directory
RUN mkdir -p /backup /logs

# Make script executable
RUN chmod +x backup.py entrypoint.sh

# Entry point
ENTRYPOINT ["/app/entrypoint.sh"]
