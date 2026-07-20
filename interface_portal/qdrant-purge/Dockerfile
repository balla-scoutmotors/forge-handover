FROM python:3.10-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir \
    requests \
    python-dotenv

# Copy purge + one-time backfill scripts
COPY purge.py .
COPY test_purge.py .

# Run the daily purge loop by default.
# -u: unbuffered stdout so every print() appears immediately in docker logs.
CMD ["python", "-u", "purge.py"]
