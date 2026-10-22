import os
from datetime import datetime, timezone

import boto3

from clients import (
    Cloudflare,
    HealthCheck,
    HomeClient,
    Instance,
    Params,
    StateStore,
    States,
    Telegram,
    Tunnels,
    lazy,
)


def build_states(env, ssm, ec2):
    """The one place that knows the SSM parameter names. Clients read their secrets lazily."""
    params = Params(ssm)
    failover_url = env.get("HOME_FAILOVER_URL", "https://gv-health.lab-ocp.com/failover")
    cloudflare = lazy(lambda: Cloudflare(params["cloudflare-token"], params["cloudflare-zone"]))
    return States(
        StateStore(ssm),
        Instance(ssm, ec2, env["FAILOVER_INSTANCE_ID"]),
        HealthCheck(),
        lazy(lambda: HomeClient(failover_url, params["failover-token"])),
        Tunnels(cloudflare, params),
        lazy(lambda: Telegram(params["telegram-token"], params["telegram-chat-id"])),
        env.get("HOME_LAB_API_URL", "https://gv-api.lab-ocp.com") + "/health",
        env.get("ALT_HEALTH_URL", "https://gv-alt-health.lab-ocp.com") + "/health",
    )


def lambda_handler(event, context):
    states = build_states(os.environ, boto3.client("ssm"), boto3.client("ec2"))
    try:
        states.run(datetime.now(timezone.utc))
    except Exception as e:
        try:
            states.telegram.send_message(f"Watchdog error: {e!r}")
        except Exception as notify_error:
            print(f"WATCHDOG ERROR NOTIFICATION FAILED: {notify_error!r}")
        raise
