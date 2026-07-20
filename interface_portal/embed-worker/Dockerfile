FROM python:3.10-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir \
    boto3 \
    requests \
    python-dotenv

# Copy the embedding script
COPY embed-json.py .

# Run the embedding worker
CMD ["python", "embed-json.py"]
