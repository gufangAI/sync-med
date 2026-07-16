import os, boto3
from botocore.config import Config

EP = os.environ["S_EP"]; AK = os.environ["S_AK"]; SK = os.environ["S_SK"]; BUCKET = os.environ["S_BUCKET"]
s3 = boto3.client("s3", endpoint_url=EP, aws_access_key_id=AK, aws_secret_access_key=SK,
                  region_name="auto", config=Config(connect_timeout=15, read_timeout=30))

keys = ["_cc/med_pages.json", "_cc/pan_done.json", "_cc/med_names.json", "_cc/med_allow.txt", "_cc/guji_pages.json"]
for k in keys:
    try:
        h = s3.head_object(Bucket=BUCKET, Key=k)
        print("OK  %s  size=%s  modified=%s" % (k, h["ContentLength"], h["LastModified"]), flush=True)
    except Exception as e:
        print("MISSING  %s  (%s)" % (k, str(e)[:150]), flush=True)
