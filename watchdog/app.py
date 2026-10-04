import urllib.request
import os
from datetime import datetime


def lambda_handler(event, context):
    health_url = os.getenv("HOME_LAB_API_URL", "https://gv-api.lab-ocp.com") + "/health"
    now = datetime.now()

    try:
        req = urllib.request.Request(
            health_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; AWS-Lambda/1.0)"},
        )
        health_response = urllib.request.urlopen(req, timeout=5)
        if health_response.status == 200:
            print(f"HOMELAB HEALTHY on {now}")
    except Exception as e:
        print(f"HOMELAB UNHEALTHY on {now}")
