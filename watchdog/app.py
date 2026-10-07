import urllib.error
import urllib.request
import os
from datetime import datetime


def check_home(url):
    """True if home answered 200, False if it is down. Raises on a bug in the check itself."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; AWS-Lambda/1.0)"},
        )
        return urllib.request.urlopen(req, timeout=5).status == 200
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        # HTTPError is a URLError; urlopen raises it for 4xx/5xx
        return False


def lambda_handler(event, context):
    health_url = os.getenv("HOME_LAB_API_URL", "https://gv-api.lab-ocp.com") + "/health"
    now = datetime.now()

    try:
        healthy = check_home(health_url)
    except Exception as e:
        # not "home down": never let this start a failover
        print(f"HOMELAB CHECK ERROR on {now}: {e!r}")
        raise

    print(f"HOMELAB {'HEALTHY' if healthy else 'UNHEALTHY'} on {now}")
