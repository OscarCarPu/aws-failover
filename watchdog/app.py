import json
import urllib.error
import urllib.request
import os
from datetime import datetime, timezone
from enum import Enum
from functools import cached_property

import boto3

# Constants

USER_AGENT = "Mozilla/5.0 (compatible; AWS-Lambda/1.0)"
NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError)


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


class HomeClient:
    def __init__(self, failover_url, token):
        self.failover_url = failover_url
        self.token = token

    def open(self, url, method="GET", body=None, headers=None):
        req = urllib.request.Request(
            url,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"User-Agent": USER_AGENT, **(headers or {})},
        )
        return urllib.request.urlopen(req, timeout=5)

    def check(self, url, name):
        try:
            with self.open(url) as r:
                healthy = r.status == 200
        except NETWORK_ERRORS:
            healthy = False
        except Exception as e:
            print(f"{name} CHECK ERROR: {e!r}")
            raise
        print(f"{name} {'HEALTHY' if healthy else 'UNHEALTHY'}")
        return healthy

    def home_call(self, payload):
        try:
            with self.open(
                self.failover_url,
                method="POST",
                body=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Failover-Token": self.token,
                },
            ) as r:
                acked = r.status == 200
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise RuntimeError(f"home refused the failover token({e.code})") from e
            acked = False
        except NETWORK_ERRORS:
            acked = False
        print(f"HOME CALL {payload['mode']} {'ACK' if acked else 'NO ACK'}")
        return acked

    def home_status(self):
        try:
            with self.open(
                self.failover_url + "/status",
                headers={"X-Failover-Token": self.token},
            ) as r:
                return json.load(r)["merge"]
        except NETWORK_ERRORS:
            return None


class State(str, Enum):
    NORMAL = "NORMAL"
    STARTING = "STARTING"
    FAILED_OVER = "FAILED_OVER"
    FAILING_BACK = "FAILING_BACK"
    HOME_READY = "HOME_READY"
    PENDING_MERGE = "PENDING_MERGE"


class StateStore:
    prefix = "/home-lab-failover"
    state_name = f"{prefix}/state"
    dump_name = f"{prefix}/dump"
    base_name = f"{prefix}/base"

    def __init__(self, ssm):
        self.ssm = ssm

    def param(self, name):
        return self.ssm.get_parameter(
            Name=f"{self.prefix}/{name}", WithDecryption=True
        )["Parameter"]["Value"]

    def load(self):
        try:
            raw = json.loads(
                self.ssm.get_parameter(Name=self.state_name)["Parameter"]["Value"]
            )
        except self.ssm.exceptions.ParameterNotFound:
            return {"state": State.NORMAL}
        return {**raw, "state": State(raw["state"])}

    def save(self, state):
        self.ssm.put_parameter(
            Name=self.state_name,
            Value=json.dumps(state),
            Type="String",
            Overwrite=True,
        )

    def dump_ready(self):
        try:
            self.ssm.get_parameter(Name=self.dump_name)
        except self.ssm.exceptions.ParameterNotFound:
            return False
        return True

    def clear_dump(self):
        self.ssm.delete_parameters(Names=[self.dump_name])

    def clear_handoff(self):
        """Drops base and dump so a new failover starts from nothing."""
        self.ssm.delete_parameters(Names=[self.base_name, self.dump_name])


class Instance:
    """The failover EC2 instance: power and the shell commands run on it through SSM."""

    def __init__(self, ssm, ec2, instance_id):
        self.ssm = ssm
        self.ec2 = ec2
        self.instance_id = instance_id

    def start(self):
        self.ec2.start_instances(InstanceIds=[self.instance_id])

    def stop(self):
        self.ec2.stop_instances(InstanceIds=[self.instance_id])

    def power(self):
        """EC2 state name: pending, running, stopping, stopped..."""
        res = self.ec2.describe_instances(InstanceIds=[self.instance_id])
        return res["Reservations"][0]["Instances"][0]["State"]["Name"]

    def run(self, command):
        """Starts a shell command on the instance. Returns the command id."""
        sent = self.ssm.send_command(
            InstanceIds=[self.instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [command]},
        )
        return sent["Command"]["CommandId"]

    def command_status(self, command_id):
        """SSM status of a command; an unknown invocation counts as Failed."""
        try:
            return self.ssm.get_command_invocation(
                CommandId=command_id, InstanceId=self.instance_id
            )["Status"]
        except self.ssm.exceptions.InvocationDoesNotExist:
            return "Failed"


class States:
    down_after = 600  # seconds home must stay down before failing over
    up_after = 600  # seconds home must stay healthy before failing back
    starting_timeout = 2400
    failback_command = "bash /opt/gv/failback.sh"
    messages = {
        (State.NORMAL, State.STARTING): "Home is down, starting the failover instance",
        (State.STARTING, State.FAILED_OVER): "Failed over to AWS",
        (State.FAILED_OVER, State.FAILING_BACK): "Home is back, failing back",
        (State.FAILING_BACK, State.PENDING_MERGE): "Dump ready, home is merging it",
        (State.PENDING_MERGE, State.HOME_READY): "Merge done on home",
        (
            State.PENDING_MERGE,
            State.FAILED_OVER,
        ): "Home went down during the merge, back on failover",
        (State.HOME_READY, State.NORMAL): "Back on home, failover instance stopped",
    }

    def __init__(
        self,
        store,
        instance,
        health_url,
        alt_health_url,
        failover_url="https://gv-health.lab-ocp.com/failover",
    ):
        self.store = store
        self.instance = instance
        self.health_url = health_url
        self.alt_health_url = alt_health_url
        self.failover_url = failover_url
        self.handlers = {
            State.NORMAL: self.normal,
            State.STARTING: self.starting,
            State.FAILED_OVER: self.failed_over,
            State.FAILING_BACK: self.failing_back,
            State.PENDING_MERGE: self.pending_merge,
            State.HOME_READY: self.home_ready,
        }

    # HELPERS
    @cached_property
    def cloudflare(self):
        return Cloudflare(
            self.store.param("cloudflare-token"), self.store.param("cloudflare-zone")
        )

    @cached_property
    def telegram(self):
        return Telegram(
            self.store.param("telegram-token"), self.store.param("telegram-chat-id")
        )

    @cached_property
    def home(self):
        return HomeClient(self.failover_url, self.store.param("failover-token"))

    def run(self, now):
        state = self.store.load()
        new = self.handlers[state["state"]](dict(state), now)
        if new != state:
            self.store.save(new)
        print(f"STATE {state['state'].value} -> {new['state'].value}")
        text = self.messages.get((state["state"], new["state"]))
        if text:
            self.telegram.send_message(text)
        return new

    def home_healthy(self, state, now):
        """Also tracks down_since / up_since in `state`."""
        healthy = self.home.check(self.health_url, "HOMELAB")
        gone, here = (
            ("down_since", "up_since") if healthy else ("up_since", "down_since")
        )
        state.pop(gone, None)
        state.setdefault(here, now.isoformat())
        return healthy

    def alt_healthy(self):
        return self.home.check(self.alt_health_url, "ALT")

    # STATES

    def normal(self, state, now):
        if self.home_healthy(state, now):
            return state

        down_for = (now - datetime.fromisoformat(state["down_since"])).total_seconds()
        if down_for < self.down_after:
            return state

        self.store.clear_handoff()

        self.instance.start()
        return {**state, "state": State.STARTING}

    def starting(self, state, now):
        waited = (now - datetime.fromisoformat(state["down_since"])).total_seconds()
        if waited > self.starting_timeout:
            raise RuntimeError(f"alt not healthy after {waited:.0f}s")
        name = self.instance.power()
        if name == "stopped":
            self.instance.start()
            return state
        if name != "running" or not self.alt_healthy():
            return state

        self.cloudflare.flip(self.store.param("alt-tunnel-id"))
        return {**state, "state": State.FAILED_OVER}

    def failed_over(self, state, now):
        if not self.home_healthy(state, now):
            return state

        if not state.get("home_held") and self.home.home_call({"mode": "hold"}):
            state = {**state, "home_held": True}

        up_for = (now - datetime.fromisoformat(state["up_since"])).total_seconds()
        if up_for < self.up_after:
            return state

        self.store.clear_dump()
        command_id = self.instance.run(self.failback_command)
        return {**state, "state": State.FAILING_BACK, "command_id": command_id}

    def failing_back(self, state, now):
        """Waits for failback.sh; only reruns it when its command failed."""
        if self.store.dump_ready():
            if not self.home.home_call({"mode": "merge"}):
                return state
            self.cloudflare.flip(self.store.param("home-tunnel-id"))
            return {
                **state,
                "state": State.PENDING_MERGE,
                "merge_since": now.isoformat(),
            }

        status = self.instance.command_status(state["command_id"])
        if status in ("Pending", "InProgress", "Delayed", "Success"):
            return state

        return {**state, "command_id": self.instance.run(self.failback_command)}

    def pending_merge(self, state, now):
        """No timeout: only home going down again is a failure."""
        status = self.home.home_status()
        if status == "done":
            return {**state, "state": State.HOME_READY}

        if status == "needs_answers":
            last = state.get("reminded_at")
            if last and (now - datetime.fromisoformat(last)).total_seconds() < 3600:
                return state
            self.telegram.send_message("The merge is waiting for your answers (/merge)")
            return {**state, "reminded_at": now.isoformat()}

        if self.home_healthy(state, now):
            return state
        down_for = (now - datetime.fromisoformat(state["down_since"])).total_seconds()
        if down_for < self.down_after:
            return state

        self.cloudflare.flip(self.store.param("alt-tunnel-id"))
        self.instance.run("cd /opt/gv && docker compose start gv-api gv-web")
        self.store.clear_dump()
        for key in ("home_held", "merge_since", "reminded_at", "command_id"):
            state.pop(key, None)
        return {**state, "state": State.FAILED_OVER}

    def home_ready(self, state, now):
        if not self.home_healthy(state, now):
            return state

        self.cloudflare.flip(self.store.param("home-tunnel-id"))
        self.instance.stop()
        return {"state": State.NORMAL}


def lambda_handler(event, context):
    ssm = boto3.client("ssm")
    states = States(
        StateStore(ssm),
        Instance(ssm, boto3.client("ec2"), os.environ["FAILOVER_INSTANCE_ID"]),
        os.getenv("HOME_LAB_API_URL", "https://gv-api.lab-ocp.com") + "/health",
        os.getenv("ALT_HEALTH_URL", "https://gv-alt-health.lab-ocp.com") + "/health",
        os.getenv("HOME_FAILOVER_URL", "https://gv-health.lab-ocp.com/failover"),
    )
    try:
        states.run(datetime.now(timezone.utc))
    except Exception as e:
        states.telegram.send_message(f"Watchdog error: {e!r}")
        raise
