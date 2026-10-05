# Lambdas

## Watchdog

`watchdog/app.py`, triggered by EventBridge Scheduler every 5 minutes (`rate(5 minutes)`).

1. `GET $HOME_LAB_API_URL/health` (default `https://gv-api.lab-ocp.com`), 5 s timeout
2. logs one line to CloudWatch:
   - `HOMELAB HEALTHY on <time>`: returned 200
   - `HOMELAB UNHEALTHY on <time>`: failed or timed out

Only logs. No alerts or failover actions yet.
