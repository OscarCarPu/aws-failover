import os
from datetime import datetime, timezone

import boto3

from clients import Instance, StateStore, States


def build_states(env, ssm, ec2):
    return States(
        StateStore(ssm),
        Instance(ssm, ec2, env["FAILOVER_INSTANCE_ID"]),
        env.get("HOME_LAB_API_URL", "https://gv-api.lab-ocp.com") + "/health",
        env.get("ALT_HEALTH_URL", "https://gv-alt-health.lab-ocp.com") + "/health",
        env.get("HOME_FAILOVER_URL", "https://gv-health.lab-ocp.com/failover"),
    )


def lambda_handler(event, context):
    states = build_states(os.environ, boto3.client("ssm"), boto3.client("ec2"))
    try:
        states.run(datetime.now(timezone.utc))
    except Exception as e:
        states.telegram.send_message(f"Watchdog error: {e!r}")
        raise
