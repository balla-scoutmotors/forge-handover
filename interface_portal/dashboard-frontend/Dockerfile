FROM python:3.10-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir \
    streamlit \
    requests \
    python-dotenv \
    boto3 \
    spacy \
    dateparser \
    plotly \
    pandas && \
    python -m spacy download en_core_web_sm

# Copy the dashboard script
COPY app.py .
COPY intent_parser.py .
COPY styles.css .
COPY Truth.json .

# Expose Streamlit port
EXPOSE 8501

# Disable Python output buffering so print() calls appear in docker logs immediately
ENV PYTHONUNBUFFERED=1

CMD ["streamlit", "run", "app.py", "--server.port", "8501", "--server.address", "0.0.0.0"]
