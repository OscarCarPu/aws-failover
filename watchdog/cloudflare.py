import json
import os
import urllib.request

API = "https://api.cloudflare.com/client/v4"
HOSTS = ("gv-api.lab-ocp.com", "gv.lab-ocp.com")  # api first, see docs/Cloudflare.md


def call(token, method, path, body=None):
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)["result"]


def record(token, zone, host):
    found = call(token, "GET", f"/zones/{zone}/dns_records?name={host}&type=CNAME")
    if len(found) != 1:
        raise RuntimeError(f"expected 1 CNAME for {host}, found {len(found)}")
    return found[0]


def set_target(token, zone, tunnel_id):
    """Point both records at the tunnel. Idempotent, reads back each change.

    Returns True if something changed.
    """
    content = f"{tunnel_id}.cfargotunnel.com"
    changed = False
    for host in HOSTS:
        rec = record(token, zone, host)
        if rec["content"] == content:
            continue
        call(token, "PATCH", f"/zones/{zone}/dns_records/{rec['id']}", {"content": content})
        if record(token, zone, host)["content"] != content:
            raise RuntimeError(f"{host} did not read back as {content}")
        changed = True
    return changed
