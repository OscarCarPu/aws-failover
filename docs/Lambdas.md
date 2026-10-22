# Lambdas

## Watchdog

`watchdog/app.py`, triggered by EventBridge Scheduler every 5 minutes (`rate(5 minutes)`).

1. `GET $HOME_LAB_API_URL/health` (`https://gv-health.lab-ocp.com`, always the home tunnel), 5 s timeout
2. logs one line to CloudWatch:
   - `HOMELAB HEALTHY on <time>`: returned 200
   - `HOMELAB UNHEALTHY on <time>`: failed, timed out or not 200
   - `HOMELAB CHECK ERROR on <time>: <error>`: a bug in the check. The invocation fails; it is never read as home down

Today it only logs the result. The state machine below is the target, from `gv-api/docs/features/CLOUD.md`.

IAM already allows starting/stopping the instance, SSM Run Command, writing `/home-lab-failover/state` and deleting `/home-lab-failover/base` and `dump`. The Lambda has no S3 access: it never reads the dumps, home does.

## Flow

The Lambda is the only brain: one step per run, every 5 minutes, state in SSM. EC2 is a passive worker (restores on boot, runs what Run Command sends). Each run checks home, reads the state, does at most one transition and saves the new state only after the step succeeded, so a failed run is retried by the next one.

```
NORMAL -> STARTING -> FAILED_OVER -> FAILING_BACK -> PENDING_MERGE -> HOME_READY -> NORMAL
```

| State | What it does each run |
|---|---|
| `NORMAL` | Checks home. After 10 minutes down: starts EC2, sets `STARTING`. |
| `STARTING` | Waits until the instance is running and `gv-alt-health` answers 200 (the boot unit restored the DB and started the alt tunnel). Then flips both CNAMEs to alt, sets `FAILED_OVER`. Never checks home: once alt is ready it flips. Too long without getting ready fails the run. |
| `FAILED_OVER` | Checks home. As soon as it answers, asks it once to hold (`POST /failover {"mode":"hold"}`, remembered as `home_held`): home stops serving data and running its workers. After 10 consecutive minutes healthy: deletes `dump`, sets `FAILING_BACK` and sends the first failback command. |
| `FAILING_BACK` | `failback.sh` (started by `FAILED_OVER`) stops the apps on EC2, dumps the DB and puts the S3 key in the `dump` parameter. Without `dump` the Lambda reruns the script only if its Run Command failed. With `dump` present it asks home to merge (`{"mode":"merge"}`). **No acknowledgement, no flip**: it tries again next run. With it, it flips both CNAMEs to home and sets `PENDING_MERGE`. |
| `PENDING_MERGE` | Home merges `dump` into its database, using `base` as the common starting point; a conflict is answered by the user in gv-web. The Lambda reads `GET /failover/status`: `done` sets `HOME_READY`; `needs_answers` sends a Telegram reminder at most once an hour; no timeout. If home stays down for 10 minutes it flips back to alt, starts the apps on EC2 and returns to `FAILED_OVER`. |
| `HOME_READY` | Checks home first; if down, waits. Then flips both CNAMEs to home, stops EC2 (cloudflared goes with it) and sets `NORMAL`. |
| `PLANNED` | Set from the gv-web maintenance button after a final backup. Home is shut down on purpose, so it is not read as an outage. |

Notes:

- The long steps (restore, dump) never block a run: each run does one step and the next run looks at the result (alt health, or a Run Command id kept in the state for failback).
- Health is checked on `gv-health` (always home) and `gv-alt-health` (always alt, `/health` only), so alt readiness is known before the flip.
- The DNS flip is idempotent ([Cloudflare](Cloudflare.md)), a half-flipped state is finished by the next run.
- While on alt, `FAILOVER_SIDE=aws` makes the Domotics routes answer `503 unavailable_on_failover`.
- `/failover` is only routed on `gv-health.lab-ocp.com` and needs the `failover-token` in the `X-Failover-Token` header. A 401 or 403 is a wrong token, not an outage: the run fails and Telegram reports it. Any other failure to reach home just counts as "no acknowledgement".
- gv-api checks the SSM state before acting, so the token alone cannot start a merge or choose a dump.
- Merge rules and gv-api side: `Restore.md` in the `aws-failover-plan` repo.

## State and secrets

SSM parameters under `/home-lab-failover/`:

- `state`: current state, `down_since` / `up_since`, pending Run Command id, `home_held`, `merge_since`, `reminded_at`
- `base`: S3 key of the dump EC2 restored when the failover started. Written once by `boot.sh`, deleted by the Lambda before it starts the instance
- `dump`: S3 key of the failback dump. Written by `failback.sh`, deleted by the Lambda before it starts the failback and when a merge is rolled back
- `cloudflare-token`, `telegram-token`, `failover-token`: SecureString, read only by the Lambda (home has its own copy of `failover-token` in gv-api's `.env`)
- `telegram-chat-id`
- `cloudflare-zone`, `alt-tunnel-id`, `home-tunnel-id`: String
- All of the above except `base` and `dump` are created with `make lambda-params` from `.env`. `failover-token` comes from `FAILOVER_TOKEN`: generate it once with `openssl rand -hex 32`

## DNS flip

`Cloudflare.flip()` in `watchdog/clients.py` points `gv-api` and `gv` at a tunnel (`<tunnel id>.cfargotunnel.com`), see [Cloudflare](Cloudflare.md). `STARTING` flips to alt, `HOME_READY` flips back to home. The token, zone and tunnel ids are read from SSM only when a state flips.

## Telegram

Every message goes through the Telegram bot (`telegram-token`, `telegram-chat-id`), sent by the Lambda after the new state is saved:

- `NORMAL -> STARTING`: home is down, starting the instance
- `STARTING -> FAILED_OVER`: failed over to AWS
- `FAILED_OVER -> FAILING_BACK`: home is back, failing back
- `FAILING_BACK -> PENDING_MERGE`: dump ready, home is merging it
- `PENDING_MERGE -> HOME_READY`: merge done on home
- `PENDING_MERGE -> FAILED_OVER`: home went down during the merge, back on failover
- `PENDING_MERGE`, conflicts waiting: reminder, at most once an hour
- `HOME_READY -> NORMAL`: back on home, instance stopped
- any error in a run: `Watchdog error: <error>`, then the run still fails

A failed send is only logged, it never blocks a transition. A run that dies before it can send (import error, timeout) sends nothing.
