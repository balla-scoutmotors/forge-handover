from dotenv import load_dotenv
load_dotenv()

import boto3
import time
import requests
import os
import json

# relevant uri/api calls

QUEUE_URL = "https://sqs.us-east-2.amazonaws.com/749972935500/forge-detection-events-queue"

OLLAMA_URL = "http://ollama:11434/api/embeddings"
QDRANT_URL = "http://qdrant:6333/collections/detection_events/points"

S3_BUCKET = os.getenv("S3_BUCKET")

sqs = boto3.client('sqs', region_name='us-east-2')
s3 = boto3.client('s3', region_name='us-east-2')


while True:
    response = sqs.receive_message(
        QueueUrl=QUEUE_URL,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=20  # long polling (fast + no waste)
    )

    # store received sqs messages in array messages
    messages = response.get('Messages', [])

    if not messages:
        continue

    for message in messages:
        receipt_handle = message['ReceiptHandle']
        raw_body = message.get('Body', '')
        
        # Lambda parsing system works now by  receiving separate json and md messages
        # each has a correlation id to bind md -> json, and vice versa.
        # they pair as from s3, the corresponding json and md share filename suffixes

        # incoming message contains MD to be embedded, JSON to populate metadata fields, 
        # and a correlation_ID that was used intially to match the original md and json
        # together but is now also used to assign to defect_id
        if not raw_body or not raw_body.strip():
            print("Skipping empty SQS message body")
            sqs.delete_message(
                QueueUrl=QUEUE_URL,
                ReceiptHandle=receipt_handle
            )
            continue

        metadata = {}
        correlation_id = None
        
        
        try:
            body = json.loads(raw_body)
            markdown_text = body.get('markdown', '')
            metadata = body.get('metadata', {}) or {}
            correlation_id = body.get('correlation_id')
        except json.JSONDecodeError:
            markdown_text = raw_body

        print("\n--- New Detection Message ---")
        print(f"Raw body type: {type(raw_body)}")
        print(f"Raw body (first 200 chars): {raw_body[:200]}")
        print(f"Correlation ID: {correlation_id}")
        print(f"Metadata keys: {list(metadata.keys())}")
        print(f"Metadata: {metadata}")
        print(f"Defect: {metadata.get('defect_name')}")

        try:
            # storing unconverted vector here to also insert as plaintext/md into qdrant point
            embed_text = markdown_text
            if not embed_text or not embed_text.strip():
                print(f"Skipping point — empty markdown for correlation_id: {correlation_id}")
                sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=receipt_handle)
                continue
            
            nomic_api_payload = {
                "model": "nomic-embed-text",
                "prompt": embed_text
            }

            response = requests.post(OLLAMA_URL, json=nomic_api_payload, timeout=120)
            response.raise_for_status()

            data = response.json()
            # embedded data point:
            embedding = data["embedding"]
            
            # payload stores, structured metadata plus defect_ID, and md contents under "content"
            # Flatten nested objects (like 'causes') to avoid Qdrant schema issues 
            flat_payload = {}

            for key, val in metadata.items():
                if isinstance(val, dict):
                    if key == "causes":
                        # Store cause parameters directly without prefix
                        for subkey, subval in val.items():
                            flat_payload[subkey] = subval
                    else:
                        # Flatten other nested dicts with key prefix (e.g. bounding_box_x)
                        for subkey, subval in val.items():
                            flat_payload[f"{key}_{subkey}"] = subval
                else:
                    flat_payload[key] = val

            # Always use correlation_id (file UUID from lambda) as defect_ID for parity, creates
            # defect_ID if nonexistant 
            flat_payload["defect_ID"] = correlation_id

            # Store the source markdown so the LLM gets original md text as context instead of just raw JSON.
            if embed_text:
                flat_payload["content"] = embed_text

            # Creating new field in qdrant for purposes of purge system
            flat_payload["ingested_at"] = int(time.time())

            point = {
                "points": [
                    {
                        "id": int(time.time() * 1000000),  # unique ID
                        "vector": embedding,
                        "payload": flat_payload
                    }
                ]
            }

            headers = {
                "Content-Type": "application/json",
                "api-key": os.getenv("QDRANT_API_KEY")
            }

            qdrant_response = requests.put(
                QDRANT_URL,
                headers=headers,
                json=point,
                timeout=30
            )

            qdrant_response.raise_for_status()

            print("Stored in Qdrant")
            print("Vector length:", len(embedding))
            print("First 4 values:", embedding[:4])
            print(f"Payload keys: {list(flat_payload.keys())}")


            # ---------------Embedded cold storage------------
            # Write cold-storage point to S3: payload + vector so historical queries
            # can retrieve and (optionally) do in-memory cosine similarity without re-embedding.
            if S3_BUCKET and correlation_id:
                try:
                    # Include date prefix so cold-path queries can list by date directly
                    # without needing to cross-reference defect-json/ filenames.
                    date_prefix = time.strftime("%Y%m%d")
                    s3_key = f"detections/defect-points/{date_prefix}_{correlation_id}.json"
                    s3.put_object(
                        Bucket=S3_BUCKET,
                        Key=s3_key,
                        Body=json.dumps({"payload": flat_payload, "vector": embedding}),
                        ContentType="application/json",
                    )
                    print(f"Cold point written to S3: {s3_key}")
                except Exception as s3_err:
                    # Non-fatal: Qdrant write already succeeded, don't block the pipeline
                    print(f"Warning — S3 cold write failed: {s3_err}")

            sqs.delete_message(
                QueueUrl=QUEUE_URL,
                ReceiptHandle=receipt_handle
            )

            print("Message removed from queue")

        except Exception as e:
            print(f"Error processing message: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"Response status: {e.response.status_code}")
                try:
                    print(f"Response body: {e.response.text}")
                except:
                    pass