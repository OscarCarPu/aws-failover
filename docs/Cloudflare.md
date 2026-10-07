# Cloudflare

Two tunnels serve `gv.lab-ocp.com` and `gv-api.lab-ocp.com`: home and alt (EC2, `gv-tunnel.service`). Their ingress is the same. Which one is live is decided by the two proxied CNAMEs, `<tunnel id>.cfargotunnel.com`.

`gv-health.lab-ocp.com` always reaches home (`/health` only). There is no alt health hostname: after a flip, check `gv-api.lab-ocp.com/health`.

## Flip

`watchdog/cloudflare.py` `set_target(token, zone, tunnel_id)`:

- flips `gv-api` first, then `gv`
- does nothing for a record already on the target
- reads each record back after changing it
- a half-flipped state is finished by running it again

From a laptop, with `.env` filled from `.env.example`:

```bash
set -a; . ./.env; set +a
python3 -I -c 'import os,sys; sys.path.insert(0,"."); from watchdog import cloudflare as c; print(c.set_target(os.environ["CF_TOKEN"], os.environ["CF_ZONE"], os.environ["ALT_TUNNEL_ID"]))'
```

Use `HOME_TUNNEL_ID` to go back. The instance must be running and restored first.

## Token

Custom API token, `Zone > DNS > Edit` on `lab-ocp.com` only. It can't read tunnels. For the Lambda it goes in SSM:

```bash
read -rs -p 'CF token: ' t; echo
aws ssm put-parameter --region eu-south-2 --type SecureString --overwrite \
  --name /home-lab-failover/cloudflare-token --value "$t"; unset t
```

The EC2 role can't read it (it only reads `/home-lab-failover/ec2/`).
