# Lambdas

## Watchdog

`watchdog/app.py`, triggered by EventBridge Scheduler every 5 minutes (`rate(5 minutes)`).

1. `GET $HOME_LAB_API_URL/health` (`https://gv-health.lab-ocp.com`, always the home tunnel), 5 s timeout
2. logs one line to CloudWatch:
   - `HOMELAB HEALTHY on <time>`: returned 200
   - `HOMELAB UNHEALTHY on <time>`: failed, timed out or not 200
   - `HOMELAB CHECK ERROR on <time>: <error>`: a bug in the check. The invocation fails; it is never read as home down

Today it only logs the result. The state machine below is the target, from `gv-api/docs/features/CLOUD.md`.

IAM already allows starting/stopping the instance, SSM Run Command and writing `/home-lab-failover/state`.

## Flow

The Lambda is the only brain: one step per run, every 5 minutes, state in SSM. EC2 is a passive worker (restores on boot, runs what Run Command sends). Each run checks home, reads the state, does at most one transition and saves the new state only after the step succeeded, so a failed run is retried by the next one.

```
NORMAL -> STARTING -> FAILED_OVER -> FAILING_BACK -> HOME_READY -> NORMAL
```

| State | What it does each run |
|---|---|
| `NORMAL` | Checks home. After 10 minutes down: starts EC2, sets `STARTING`. |
| `STARTING` | Waits until the instance is running and `gv-alt-health` answers 200 (the boot unit restored the DB and started the alt tunnel). Then flips both CNAMEs to alt, sets `FAILED_OVER`. Never checks home: once alt is ready it flips. Too long without getting ready fails the run. |
| `FAILED_OVER` | Checks home. After 10 consecutive minutes healthy: sets `FAILING_BACK` and sends the first failback command. |
| `FAILING_BACK` | `failback.sh` (started by `FAILED_OVER`) stops the apps on EC2, dumps the DB and sets `dump` (the S3 key) and `dump_ready` in the state itself. The Lambda only reruns the script if its Run Command failed. The home agent restores `dump` and sets the state parameter to `HOME_READY` itself. |
| `HOME_READY` | Checks home first; if down, waits. Then flips both CNAMEs to home, stops EC2 (cloudflared goes with it) and sets `NORMAL`. |
| `PLANNED` | Set from the gv-web maintenance button after a final backup. Home is shut down on purpose, so it is not read as an outage. |

Notes:

- The long steps (restore, dump) never block a run: each run does one step and the next run looks at the result (alt health, or a Run Command id kept in the state for failback).
- Health is checked on `gv-health` (always home) and `gv-alt-health` (always alt, `/health` only), so alt readiness is known before the flip.
- The DNS flip is idempotent ([Cloudflare](Cloudflare.md)), a half-flipped state is finished by the next run.
- While on alt, `FAILOVER_SIDE=aws` makes the Domotics routes answer `503 unavailable_on_failover`.
- Home restores the failback dump on restart; the user decides which changes made at home that are not in the dump stay.

## State and secrets

SSM parameters under `/home-lab-failover/`:

- `state`: current state, fail and ok counters, timestamp of each state entry, pending Run Command id
- `cloudflare-token`, `telegram-token`: SecureString, read only by the Lambda
- `telegram-chat-id`
- `cloudflare-zone`, `alt-tunnel-id`, `home-tunnel-id`: String, created with `make lambda-params` from `.env`

## DNS flip

`Cloudflare.flip()` in `watchdog/app.py` points `gv-api` and `gv` at a tunnel (`<tunnel id>.cfargotunnel.com`), see [Cloudflare](Cloudflare.md). `STARTING` flips to alt, `HOME_READY` flips back to home. The token, zone and tunnel ids are read from SSM only when a state flips.

## Telegram

Every message goes through the Telegram bot (`telegram-token`, `telegram-chat-id`), sent by the Lambda after the new state is saved:

- `NORMAL -> STARTING`: home is down, starting the instance
- `STARTING -> FAILED_OVER`: failed over to AWS
- `FAILED_OVER -> FAILING_BACK`: home is back, failing back
- `HOME_READY -> NORMAL`: back on home, instance stopped
- any error in a run: `Watchdog error: <error>`, then the run still fails

A failed send is only logged, it never blocks a transition. A run that dies before it can send (import error, timeout) sends nothing.
