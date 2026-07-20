import json
import boto3
import urllib.parse
import re

s3 = boto3.client('s3')
sqs = boto3.client('sqs')

QUEUE_URL = "https://sqs.us-east-2.amazonaws.com/749972935500/forge-detection-events-queue"

def lambda_handler(event, context):
    print("Event received:", json.dumps(event))

    try:
        #Get S3 info from event
        record = event['Records'][0]
        bucket = record['s3']['bucket']['name']
        key = urllib.parse.unquote_plus(record['s3']['object']['key'])

        print("Bucket:", bucket)
        print("Key:", key)

        # Extract UUID and file type
        match = re.match(r'(.+?)_([a-f0-9]+)\.(md|json)$', key)
        if not match:
            raise ValueError(f"Invalid filename format: {key}")
        
        uuid = match.group(2)
        file_type = match.group(3)

        # Swap folders based on incoming file type
        # This is method by which we match the 2 
        if file_type == 'md':
            md_key = key
            json_key = key.replace('/defect-md/', '/defect-json/').replace('.md', '.json')
        else:  # json
            json_key = key
            md_key = key.replace('/defect-json/', '/defect-md/').replace('.json', '.md')

        # Read both files from S3
        try:
            md_response = s3.get_object(Bucket=bucket, Key=md_key)
        except s3.exceptions.NoSuchKey:
            raise FileNotFoundError(f"MD file not found: {md_key}. Will retry when available.")
        
        try:
            json_response = s3.get_object(Bucket=bucket, Key=json_key)
        except s3.exceptions.NoSuchKey:
            raise FileNotFoundError(f"JSON file not found: {json_key}. Will retry when available.")
        
        markdown_content = md_response['Body'].read().decode('utf-8')
        metadata = json.loads(json_response['Body'].read().decode('utf-8'))

        print(f"Files retrieved successfully for UUID: {uuid}")

        # Build combined message
        content = {
            "correlation_id": uuid,
            "markdown": markdown_content,
            "metadata": metadata
        }

        #Send to SQS
        sqs.send_message(
            QueueUrl=QUEUE_URL,
            MessageBody=json.dumps(content)
        )

        print(f"Message sent to SQS successfully for {uuid}")

        return {
            'statusCode': 200,
            'body': f'Successfully queued {uuid}'
        }

    except Exception as e:
        print(f"Error: {str(e)}")
        raise e