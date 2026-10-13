import json
import urllib.error
import urllib.request
import os
from datetime import datetime, timezone
from enum import Enum
from functools import cached_property

import boto3


class Cloudflare:
    api = "https://api.cloudflare.com/client/v4"
    hosts = (
        "gv-api.lab-ocp.com",
        "gv.lab-ocp.com",
    )  # api first, see docs/Cloudflare.md

    def __init__(self, token, zone):
        self.token = token
        self.zone = zone

    def call(self, method, path, body=None):
        req = urllib.request.Request(
            self.api + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)["result"]

    def record(self, host):
        found = self.call(
            "GET", f"/zones/{self.zone}/dns_records?name={host}&type=CNAME"
        )
        if len(found) != 1:
            raise RuntimeError(f"expected 1 CNAME for {host}, found {len(found)}")
        return found[0]

    def flip(self, tunnel_id):
        """Point both records at the tunnel. Idempotent, reads back each change.

        Returns True if something changed.
        """
        content = f"{tunnel_id}.cfargotunnel.com"
        changed = False
        for host in self.hosts:
            rec = self.record(host)
            if rec["content"] == content:
                continue
            self.call(
                "PATCH",
                f"/zones/{self.zone}/dns_records/{rec['id']}",
                {"content": content},
            )
            if self.record(host)["content"] != content:
                raise RuntimeError(f"{host} did not read back as {content}")
            changed = True
        return changed


class Telegram:
    api = "https://api.telegram.org"

    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id

    def send_message(self, text):
        req = urllib.request.Request(
            f"{self.api}/bot{self.token}/sendMessage",
            data=json.dumps({"chat_id": self.chat_id, "text": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            print(f"TELEGRAM ERROR: {e!r}")


class State(str, Enum):
    NORMAL = "NORMAL"
    STARTING = "STARTING"
    FAILED_OVER = "FAILED_OVER"
    FAILING_BACK = "FAILING_BACK"
    HOME_READY = "HOME_READY"


class States:
    state_ssm = "/home-lab-failover/state"
    down_after = 600  # seconds home must stay down before failing over
    up_after = 600  # seconds home must stay healthy before failing back
    starting_timeout = 2400
    messages = {
        (State.NORMAL, State.STARTING): "Home is down, starting the failover instance",
        (State.STARTING, State.FAILED_OVER): "Failed over to AWS",
        (State.FAILED_OVER, State.FAILING_BACK): "Home is back, failing back",
        (State.HOME_READY, State.NORMAL): "Back on home, failover instance stopped",
    }

    def __init__(
        self,
        ssm,
        ec2,
        instance_id,
        health_url,
        alt_health_url,
    ):
        self.ssm = ssm
        self.ec2 = ec2
        self.instance_id = instance_id
        self.health_url = health_url
        self.alt_health_url = alt_health_url
        self.handlers = {
            State.NORMAL: self.normal,
            State.STARTING: self.starting,
            State.FAILED_OVER: self.failed_over,
            State.FAILING_BACK: self.failing_back,
            State.HOME_READY: self.home_ready,
        }

    def param(self, name):
        return self.ssm.get_parameter(
            Name=f"/home-lab-failover/{name}", WithDecryption=True
        )["Parameter"]["Value"]

    @cached_property
    def cloudflare(self):
        return Cloudflare(self.param("cloudflare-token"), self.param("cloudflare-zone"))

    @cached_property
    def telegram(self):
        return Telegram(self.param("telegram-token"), self.param("telegram-chat-id"))

    def load(self):
        try:
            raw = json.loads(
                self.ssm.get_parameter(Name=self.state_ssm)["Parameter"]["Value"]
            )
        except self.ssm.exceptions.ParameterNotFound:
            return {"state": State.NORMAL}
        return {**raw, "state": State(raw["state"])}

    def save(self, state):
        self.ssm.put_parameter(
            Name=self.state_ssm,
            Value=json.dumps(state),
            Type="String",
            Overwrite=True,
        )

    def run(self, now):
        state = self.load()
        new = self.handlers[state["state"]](dict(state), now)
        if new != state:
            self.save(new)
        print(f"STATE {state['state'].value} -> {new['state'].value}")
        text = self.messages.get((state["state"], new["state"]))
        if text:
            self.telegram.send_message(text)
        return new

    def check(self, url, name):
        """True if url answered 200, False if it is down. Raises on a bug in the check itself."""
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; AWS-Lambda/1.0)"},
        )
        try:
            healthy = urllib.request.urlopen(req, timeout=5).status == 200
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            healthy = False
        except Exception as e:
            print(f"{name} CHECK ERROR: {e!r}")
            raise
        print(f"{name} {'HEALTHY' if healthy else 'UNHEALTHY'}")
        return healthy

    def home_healthy(self, state, now):
        """Also tracks down_since / up_since in `state`."""
        healthy = self.check(self.health_url, "HOMELAB")
        gone, here = (
            ("down_since", "up_since") if healthy else ("up_since", "down_since")
        )
        state.pop(gone, None)
        state.setdefault(here, now.isoformat())
        return healthy

    def alt_healthy(self):
        return self.check(self.alt_health_url, "ALT")

    def normal(self, state, now):
        if self.home_healthy(state, now):
            return state

        down_for = (now - datetime.fromisoformat(state["down_since"])).total_seconds()
        if down_for < self.down_after:
            return state

        self.ec2.start_instances(InstanceIds=[self.instance_id])
        return {**state, "state": State.STARTING}

    def starting(self, state, now):
        waited = (now - datetime.fromisoformat(state["down_since"])).total_seconds()
        if waited > self.starting_timeout:
            raise RuntimeError(f"alt not healthy after {waited:.0f}s")
        res = self.ec2.describe_instances(InstanceIds=[self.instance_id])
        name = res["Reservations"][0]["Instances"][0]["State"]["Name"]
        if name == "stopped":
            self.ec2.start_instances(InstanceIds=[self.instance_id])
            return state
        if name != "running" or not self.alt_healthy():
            return state

        self.cloudflare.flip(self.param("alt-tunnel-id"))
        return {**state, "state": State.FAILED_OVER}

    def failed_over(self, state, now):
        if not self.home_healthy(state, now):
            return state

        up_for = (now - datetime.fromisoformat(state["up_since"])).total_seconds()
        if up_for < self.up_after:
            return state

        sent = self.ssm.send_command(
            InstanceIds=[self.instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": ["bash /opt/gv/failback.sh"]},
        )
        return {
            **state,
            "state": State.FAILING_BACK,
            "command_id": sent["Command"]["CommandId"],
        }

    def failing_back(self, state, now):
        """Only reruns a failed failback.sh. Anything else returns the state untouched,
        so it never saves over the script's or the home agent's writes."""
        if state.get("dump_ready"):
            return state

        try:
            status = self.ssm.get_command_invocation(
                CommandId=state["command_id"], InstanceId=self.instance_id
            )["Status"]
        except self.ssm.exceptions.InvocationDoesNotExist:
            status = "Failed"
        if status in ("Pending", "InProgress", "Delayed", "Success"):
            return state

        sent = self.ssm.send_command(
            InstanceIds=[self.instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": ["bash /opt/gv/failback.sh"]},
        )
        return {**state, "command_id": sent["Command"]["CommandId"]}

    def home_ready(self, state, now):
        if not self.home_healthy(state, now):
            return state

        self.cloudflare.flip(self.param("home-tunnel-id"))
        self.ec2.stop_instances(InstanceIds=[self.instance_id])
        return {"state": State.NORMAL}


def lambda_handler(event, context):
    states = States(
        boto3.client("ssm"),
        boto3.client("ec2"),
        os.environ["FAILOVER_INSTANCE_ID"],
        os.getenv("HOME_LAB_API_URL", "https://gv-api.lab-ocp.com") + "/health",
        os.getenv("ALT_HEALTH_URL", "https://gv-alt-health.lab-ocp.com") + "/health",
    )
    try:
        states.run(datetime.now(timezone.utc))
    except Exception as e:
        states.telegram.send_message(f"Watchdog error: {e!r}")
        raise
