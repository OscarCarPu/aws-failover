# Lambdas

## Watchdog

`watchdog/app.py`, triggered by EventBridge Scheduler every 5 minutes (`rate(5 minutes)`).

1. `GET $HOME_LAB_API_URL/health` (`https://gv-health.lab-ocp.com`, always the home tunnel), 5 s timeout
2. logs one line to CloudWatch:
   - `HOMELAB HEALTHY on <time>`: returned 200
   - `HOMELAB UNHEALTHY on <time>`: failed, timed out or not 200
   - `HOMELAB CHECK ERROR on <time>: <error>`: a bug in the check. The invocation fails; it is never read as home down

Only logs. No alerts or failover actions yet.

IAM already allows starting/stopping the instance and writing `/home-lab-failover/state`, for the failover to come.

## DNS flip

`watchdog/cloudflare.py` `set_target(token, zone, tunnel_id)` points `gv-api` and `gv` at a tunnel (`<tunnel id>.cfargotunnel.com`), see [Cloudflare](Cloudflare.md). Not called by the watchdog yet.
