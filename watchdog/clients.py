import json
import urllib.error
import urllib.request
from dataclasses import dataclass, fields, replace
from datetime import datetime
from enum import Enum
from functools import cached_property

USER_AGENT = "Mozilla/5.0 (compatible; AWS-Lambda/1.0)"
NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError)
PREFIX = "/home-lab-failover"
# SSM command statuses that mean failback.sh is running or done, so it must not be rerun
LIVE_COMMAND = ("Pending", "InProgress", "Delayed", "Success")
# Snapshot fields stored as ISO strings
DATETIME_FIELDS = {"down_since", "up_since", "merge_since", "reminded_at"}


def http_open(url, method="GET", body=None, headers=None):
    """urlopen with the shared User-Agent and timeout. Raises on network and HTTP errors."""
    req = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
    )
    return urllib.request.urlopen(req, timeout=5)


def lazy(factory):
    """Proxy that builds the client on first use, so its secrets are only read when needed."""
    return _Lazy(factory)


class _Lazy:
    def __init__(self, factory):
        self._factory = factory
        self._obj = None

    def __getattr__(self, name):
        if self._obj is None:
            self._obj = self._factory()
        return getattr(self._obj, name)


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


class HealthCheck:
    """Plain GET against a health url. Needs no token."""

    def check(self, url, name):
        """True if url answered 200, False if it is down. Raises on a bug in the check itself."""
        try:
            with http_open(url) as r:
                healthy = r.status == 200
        except NETWORK_ERRORS:
            healthy = False
        except Exception as e:
            print(f"{name} CHECK ERROR: {e!r}")
            raise
        print(f"{name} {'HEALTHY' if healthy else 'UNHEALTHY'}")
        return healthy


class HomeClient:
    """Token-bearing calls to the failover endpoint on home."""

    def __init__(self, failover_url, token):
        self.failover_url = failover_url
        self.token = token

    def home_call(self, payload):
        try:
            with http_open(
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
            with http_open(
                self.failover_url + "/status",
                headers={"X-Failover-Token": self.token},
            ) as r:
                return json.load(r)["merge"]
        except NETWORK_ERRORS:
            return None


class Params:
    """Config and secrets under the SSM prefix, read in one call on first use."""

    def __init__(self, ssm):
        self.ssm = ssm

    @cached_property
    def values(self):
        found, token = {}, {}
        while True:
            res = self.ssm.get_parameters_by_path(
                Path=PREFIX, WithDecryption=True, **token
            )
            for p in res["Parameters"]:
                found[p["Name"].removeprefix(f"{PREFIX}/")] = p["Value"]
            if "NextToken" not in res:
                return found
            token = {"NextToken": res["NextToken"]}

    def __getitem__(self, name):
        return self.values[name]


class Tunnels:
    """Where the DNS can point. The tunnel ids are read when a flip happens."""

    def __init__(self, cloudflare, params):
        self.cloudflare = cloudflare
        self.params = params

    def to_alt(self):
        return self.cloudflare.flip(self.params["alt-tunnel-id"])

    def to_home(self):
        return self.cloudflare.flip(self.params["home-tunnel-id"])


class StateStore:
    state_name = f"{PREFIX}/state"
    dump_name = f"{PREFIX}/dump"
    base_name = f"{PREFIX}/base"

    def __init__(self, ssm):
        self.ssm = ssm

    def load(self):
        """The saved state as a JSON dict, None before the first save."""
        try:
            value = self.ssm.get_parameter(Name=self.state_name)["Parameter"]["Value"]
        except self.ssm.exceptions.ParameterNotFound:
            return None
        return json.loads(value)

    def save(self, raw):
        self.ssm.put_parameter(
            Name=self.state_name,
            Value=json.dumps(raw),
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

    def run_failback(self):
        """Dumps the failover data for home. Returns the command id."""
        return self.run("bash /opt/gv/failback.sh")

    def resume_services(self):
        """Starts the stopped gv containers again."""
        return self.run("cd /opt/gv && docker compose start gv-api gv-web")

    def command_status(self, command_id):
        """SSM status of a command; an unknown invocation counts as Failed."""
        try:
            return self.ssm.get_command_invocation(
                CommandId=command_id, InstanceId=self.instance_id
            )["Status"]
        except self.ssm.exceptions.InvocationDoesNotExist:
            return "Failed"


class State(str, Enum):
    NORMAL = "NORMAL"
    STARTING = "STARTING"
    FAILED_OVER = "FAILED_OVER"
    FAILING_BACK = "FAILING_BACK"
    HOME_READY = "HOME_READY"
    PENDING_MERGE = "PENDING_MERGE"


def elapsed(since, now):
    """Seconds between a past datetime and now."""
    return (now - since).total_seconds()


@dataclass(frozen=True)
class Snapshot:
    state: State
    down_since: datetime | None = None  # NORMAL, STARTING, FAILED_OVER, PENDING_MERGE
    up_since: datetime | None = None  # NORMAL, FAILED_OVER, HOME_READY
    home_held: bool = False  # FAILED_OVER, FAILING_BACK
    command_id: str | None = None  # FAILING_BACK
    merge_since: datetime | None = None  # PENDING_MERGE
    reminded_at: datetime | None = None  # PENDING_MERGE

    def seen(self, healthy, now):
        """Records one health probe of home: starts its streak, ends the opposite one."""
        if healthy:
            return replace(self, down_since=None, up_since=self.up_since or now)
        return replace(self, up_since=None, down_since=self.down_since or now)

    def to_json(self):
        # None and False are the dataclass defaults, so they are left out of the stored
        # JSON and from_json brings them back.
        out = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if value is None or value is False:
                continue
            if isinstance(value, datetime):
                value = value.isoformat()
            elif isinstance(value, State):
                value = value.value
            out[f.name] = value
        return out

    @classmethod
    def from_json(cls, raw):
        kw = {}
        for f in fields(cls):
            if f.name not in raw:
                continue
            value = raw[f.name]
            if f.name == "state":
                value = State(value)
            elif f.name in DATETIME_FIELDS:
                value = datetime.fromisoformat(value)
            kw[f.name] = value
        return cls(**kw)


NORMAL, STARTING, FAILED_OVER, FAILING_BACK, HOME_READY, PENDING_MERGE = State


class States:
    down_after = 600  # seconds home must stay down before failing over
    up_after = 600  # seconds home must stay healthy before failing back
    starting_timeout = 2400
    remind_every = 3600  # seconds between "merge needs answers" reminders
    messages = {
        (NORMAL, STARTING): "Home is down, starting the failover instance",
        (STARTING, FAILED_OVER): "Failed over to AWS",
        (FAILED_OVER, FAILING_BACK): "Home is back, failing back",
        (FAILING_BACK, PENDING_MERGE): "Dump ready, home is merging it",
        (PENDING_MERGE, HOME_READY): "Merge done on home",
        (PENDING_MERGE, FAILED_OVER): "Home went down during the merge, back on failover",
        (HOME_READY, NORMAL): "Back on home, failover instance stopped",
    }

    def __init__(
        self,
        store,
        instance,
        health,
        home,
        tunnels,
        telegram,
        health_url,
        alt_health_url,
    ):
        self.store = store
        self.instance = instance
        self.health = health
        self.home = home
        self.tunnels = tunnels
        self.telegram = telegram
        self.health_url = health_url
        self.alt_health_url = alt_health_url
        self.handlers = {
            State.NORMAL: self.normal,
            State.STARTING: self.starting,
            State.FAILED_OVER: self.failed_over,
            State.FAILING_BACK: self.failing_back,
            State.PENDING_MERGE: self.pending_merge,
            State.HOME_READY: self.home_ready,
        }

    def run(self, now):
        raw = self.store.load()
        snap = Snapshot.from_json(raw) if raw else Snapshot(State.NORMAL)
        new = self.handlers[snap.state](snap, now)
        if new != snap:
            self.store.save(new.to_json())
        print(f"STATE {snap.state.value} -> {new.state.value}")
        text = self.messages.get((snap.state, new.state))
        if text:
            self.telegram.send_message(text)
        return new

    def probe_home(self, snap, now):
        """Checks home. Returns (healthy, snap with down_since / up_since updated)."""
        healthy = self.health.check(self.health_url, "HOMELAB")
        return healthy, snap.seen(healthy, now)

    def home_down_long_enough(self, snap, now):
        """Probes home; True once it has been down for down_after. Returns (down, snap)."""
        healthy, snap = self.probe_home(snap, now)
        return not healthy and elapsed(snap.down_since, now) >= self.down_after, snap

    def alt_healthy(self):
        return self.health.check(self.alt_health_url, "ALT")

    # STATES

    def normal(self, snap, now):
        down, snap = self.home_down_long_enough(snap, now)
        if not down:
            return snap

        self.store.clear_handoff()

        self.instance.start()
        return replace(snap, state=State.STARTING)

    def starting(self, snap, now):
        waited = elapsed(snap.down_since, now)
        if waited > self.starting_timeout:
            raise RuntimeError(f"alt not healthy after {waited:.0f}s")
        name = self.instance.power()
        if name == "stopped":
            self.instance.start()
            return snap
        if name != "running" or not self.alt_healthy():
            return snap

        self.tunnels.to_alt()
        return replace(snap, state=State.FAILED_OVER)

    def failed_over(self, snap, now):
        healthy, snap = self.probe_home(snap, now)
        if not healthy:
            return snap

        if not snap.home_held and self.home.home_call({"mode": "hold"}):
            snap = replace(snap, home_held=True)

        if elapsed(snap.up_since, now) < self.up_after:
            return snap

        self.store.clear_dump()
        command_id = self.instance.run_failback()
        return replace(snap, state=State.FAILING_BACK, command_id=command_id)

    def failing_back(self, snap, now):
        """Waits for failback.sh; only reruns it when its command failed."""
        if self.store.dump_ready():
            if not self.home.home_call({"mode": "merge"}):
                return snap
            self.tunnels.to_home()
            return replace(snap, state=State.PENDING_MERGE, merge_since=now)

        # a hand-edited state can lack the command id: just run failback again
        if snap.command_id:
            status = self.instance.command_status(snap.command_id)
            if status in LIVE_COMMAND:
                return snap

        return replace(snap, command_id=self.instance.run_failback())

    def pending_merge(self, snap, now):
        """No timeout: only home going down again is a failure."""
        status = self.home.home_status()
        if status == "done":
            return replace(snap, state=State.HOME_READY)

        if status == "needs_answers":
            if snap.reminded_at and elapsed(snap.reminded_at, now) < self.remind_every:
                return snap
            self.telegram.send_message("The merge is waiting for your answers (/merge)")
            return replace(snap, reminded_at=now)

        down, snap = self.home_down_long_enough(snap, now)
        if not down:
            return snap

        self.tunnels.to_alt()
        self.instance.resume_services()
        self.store.clear_dump()
        return Snapshot(State.FAILED_OVER, down_since=snap.down_since)

    def home_ready(self, snap, now):
        healthy, snap = self.probe_home(snap, now)
        if not healthy:
            return snap

        self.tunnels.to_home()
        self.instance.stop()
        return Snapshot(State.NORMAL)
