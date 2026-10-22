import dataclasses
import json
import urllib.error
from datetime import datetime, timedelta, timezone
from typing import List

import pytest

import clients

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class FakeSSM:
    class exceptions:
        class ParameterNotFound(Exception):
            pass

        class InvocationDoesNotExist(Exception):
            pass

    def __init__(self, state=None, invocation="Success"):
        self.params = {
            "/home-lab-failover/cloudflare-token": "tok",
            "/home-lab-failover/cloudflare-zone": "zone",
            "/home-lab-failover/alt-tunnel-id": "alt",
            "/home-lab-failover/home-tunnel-id": "home",
        }
        if state is not None:
            self.params[clients.StateStore.state_name] = json.dumps(state)
        self.invocation = invocation
        self.sent = 0
        self.saved = []
        self.deleted = []

    def get_parameter(self, Name, WithDecryption=False):
        if Name not in self.params:
            raise self.exceptions.ParameterNotFound()
        return {"Parameter": {"Value": self.params[Name]}}

    def get_parameters_by_path(self, Path, WithDecryption=False, NextToken=None):
        found = [
            {"Name": k, "Value": v}
            for k, v in self.params.items()
            if k.startswith(Path + "/") and "/" not in k[len(Path) + 1 :]
        ]
        return {"Parameters": found}

    def put_parameter(self, Name, Value, **kw):
        self.params[Name] = Value
        self.saved.append(json.loads(Value))

    def send_command(self, **kw):
        self.sent += 1
        return {"Command": {"CommandId": f"cmd-{self.sent}"}}

    def get_command_invocation(self, CommandId, InstanceId):
        if self.invocation is None:
            raise self.exceptions.InvocationDoesNotExist()
        return {"Status": self.invocation}

    def delete_parameters(self, Names: List[str]):
        self.deleted.append(Names)


class FakeEC2:
    def __init__(self, name="running"):
        self.name = name
        self.started = 0
        self.stopped = 0

    def describe_instances(self, InstanceIds):
        return {"Reservations": [{"Instances": [{"State": {"Name": self.name}}]}]}

    def start_instances(self, InstanceIds):
        self.started += 1

    def stop_instances(self, InstanceIds):
        self.stopped += 1


class FakeTunnels:
    flips = []

    def to_alt(self):
        self.flips.append("alt")

    def to_home(self):
        self.flips.append("home")


class FakeTelegram:
    def __init__(self, sent):
        self.send_message = sent.append

class FakeHealth:
    def __init__(self, home=True, alt=True):
        self.checks = {"HOMELAB": home, "ALT": alt}
        self.checked = []

    def check(self, url, name):
        self.checked.append(name)
        return self.checks[name]


class FakeHome:
    def __init__(self):
        self.calls = []
        self.acked = True
        self.status = None
        self.recorded = []

    def home_call(self, payload):
        self.calls.append(payload)
        return self.acked

    def record_failover(self, ec2_up_at, home_at):
        self.recorded.append((ec2_up_at, home_at))
        return self.acked

    def home_status(self):
        return self.status


@pytest.fixture
def make():
    FakeTunnels.flips = []

    def build(state=None, home=True, alt=True, ec2="running", invocation="Success"):
        ssm = FakeSSM(state, invocation)
        sent = []
        states = clients.States(
            clients.StateStore(ssm),
            clients.Instance(ssm, FakeEC2(ec2), "i-1"),
            FakeHealth(home, alt),
            FakeHome(),
            FakeTunnels(),
            FakeTelegram(sent),
            "http://home/health",
            "http://alt/health",
        )
        states.sent = sent
        return states, ssm

    return build


def iso(minutes_ago):
    return (T0 - timedelta(minutes=minutes_ago)).isoformat()


# check


def fake_urlopen(monkeypatch, status=200, exc=None):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    Resp.status = status

    def urlopen(req, timeout):
        if exc:
            raise exc
        return Resp()

    monkeypatch.setattr(clients.urllib.request, "urlopen", urlopen)


def checker():
    return clients.HealthCheck()


def test_check_ok(monkeypatch, capsys):
    fake_urlopen(monkeypatch)
    assert checker().check("http://x", "HOMELAB") is True
    assert capsys.readouterr().out.strip() == "HOMELAB HEALTHY"


@pytest.mark.parametrize(
    "kw",
    [
        {"exc": urllib.error.URLError("refused")},
        {"exc": TimeoutError()},
        {"status": 204},
    ],
)
def test_check_down(monkeypatch, kw):
    fake_urlopen(monkeypatch, **kw)
    assert checker().check("http://x", "HOMELAB") is False


def test_check_bug_raises(monkeypatch, capsys):
    fake_urlopen(monkeypatch, exc=ValueError("bug"))
    with pytest.raises(ValueError):
        checker().check("http://x", "HOMELAB")
    assert "HOMELAB CHECK ERROR" in capsys.readouterr().out


# run


def test_first_run_is_normal_and_saves_up_since(make):
    states, ssm = make()
    states.run(T0)
    assert ssm.saved == [{"state": "NORMAL", "up_since": T0.isoformat()}]


def test_run_does_not_save_when_nothing_changed(make):
    states, ssm = make({"state": "NORMAL", "up_since": iso(30)})
    states.run(T0)
    assert ssm.saved == []


def test_unknown_state_raises(make):
    states, _ = make({"state": "PLANNED"})
    with pytest.raises(ValueError):
        states.run(T0)


def test_handler_error_saves_nothing(make):
    states, ssm = make({"state": "NORMAL", "down_since": iso(20)}, home=False)
    states.instance.ec2.start_instances = lambda InstanceIds: 1 / 0
    with pytest.raises(ZeroDivisionError):
        states.run(T0)
    assert ssm.saved == []


# normal


def test_normal_first_failure_sets_down_since(make):
    states, ssm = make({"state": "NORMAL", "up_since": iso(30)}, home=False)
    states.run(T0)
    assert ssm.saved == [{"state": "NORMAL", "down_since": T0.isoformat()}]
    assert states.instance.ec2.started == 0


def test_normal_under_threshold_waits(make):
    states, ssm = make({"state": "NORMAL", "down_since": iso(9)}, home=False)
    states.run(T0)
    assert ssm.saved == []
    assert states.instance.ec2.started == 0


def test_normal_after_threshold_starts_ec2(make):
    states, ssm = make({"state": "NORMAL", "down_since": iso(10)}, home=False)
    states.run(T0)
    assert ssm.saved[-1]["state"] == "STARTING"
    assert states.instance.ec2.started == 1


def test_normal_recovery_resets_down_since(make):
    states, ssm = make({"state": "NORMAL", "down_since": iso(5)})
    states.run(T0)
    assert ssm.saved == [{"state": "NORMAL", "up_since": T0.isoformat()}]


def test_normal_clears_base_and_dump_before_starting_ec2(make):
    states, ssm = make({"state": "NORMAL", "down_since": iso(10)}, home=False)
    seen = []
    states.instance.ec2.start_instances = lambda InstanceIds: seen.append(list(ssm.deleted))
    states.run(T0)
    assert seen == [[[clients.StateStore.base_name, clients.StateStore.dump_name]]]


# starting


def test_starting_restarts_a_stopped_instance(make):
    states, ssm = make({"state": "STARTING", "down_since": iso(15)}, ec2="stopped")
    states.run(T0)
    assert states.instance.ec2.started == 1
    assert ssm.saved == []


@pytest.mark.parametrize("kw", [{"ec2": "pending"}, {"alt": False}])
def test_starting_waits(make, kw):
    states, ssm = make({"state": "STARTING", "down_since": iso(15)}, **kw)
    states.run(T0)
    assert ssm.saved == []
    assert FakeTunnels.flips == []


def test_starting_flips_when_alt_is_healthy_without_checking_home(make):
    states, ssm = make({"state": "STARTING", "down_since": iso(15)}, home=False)
    states.run(T0)
    assert states.health.checked == ["ALT"]
    assert FakeTunnels.flips == ["alt"]
    assert ssm.saved[-1]["state"] == "FAILED_OVER"


def test_starting_times_out(make):
    states, ssm = make({"state": "STARTING", "down_since": iso(41)}, alt=False)
    with pytest.raises(RuntimeError):
        states.run(T0)
    assert ssm.saved == []


# failed_over


def test_failed_over_unhealthy_restarts_the_window(make):
    states, ssm = make({"state": "FAILED_OVER", "up_since": iso(20)}, home=False)
    states.run(T0)
    assert ssm.saved == [{"state": "FAILED_OVER", "down_since": T0.isoformat()}]


def test_failed_over_first_healthy_sets_up_since(make):
    states, ssm = make({"state": "FAILED_OVER", "down_since": iso(60)})
    states.run(T0)
    assert ssm.saved == [
        {"state": "FAILED_OVER", "up_since": T0.isoformat(), "home_held": True}
    ]
    assert states.home.calls == [{"mode": "hold"}]
    assert ssm.sent == 0


def test_failed_over_after_threshold_starts_failback(make):
    states, ssm = make({"state": "FAILED_OVER", "up_since": iso(10)})
    states.run(T0)
    assert ssm.sent == 1
    assert ssm.saved[-1] == {
        "state": "FAILING_BACK",
        "up_since": iso(10),
        "home_held": True,
        "command_id": "cmd-1",
    }


def test_failed_over_clears_the_old_dump_before_failback(make):
    states, ssm = make({"state": "FAILED_OVER", "up_since": iso(10)})
    states.run(T0)
    assert ssm.deleted == [[clients.StateStore.dump_name]]
    assert ssm.sent == 1


# failing_back


def test_failing_back_with_a_dump_asks_home_to_merge_and_flips(make):
    states, ssm = make({"state": "FAILING_BACK"}, invocation=None)
    ssm.params[clients.StateStore.dump_name] = "gv-db/hourly/x"
    states.run(T0)
    assert states.home.calls == [{"mode": "merge"}]
    assert FakeTunnels.flips == ["home"]
    assert ssm.saved[-1]["state"] == "PENDING_MERGE"
    assert ssm.sent == 0


def test_failing_back_without_an_ack_does_not_flip(make):
    states, ssm = make({"state": "FAILING_BACK"}, invocation=None)
    ssm.params[clients.StateStore.dump_name] = "gv-db/hourly/x"
    states.home.acked = False
    states.run(T0)
    assert FakeTunnels.flips == []
    assert ssm.saved == []


# pending_merge


def test_pending_merge_done_goes_home_ready(make):
    states, ssm = make({"state": "PENDING_MERGE"})
    states.home.status = "done"
    states.run(T0)
    assert ssm.saved[-1]["state"] == "HOME_READY"


def test_pending_merge_waits_while_home_merges(make):
    states, ssm = make({"state": "PENDING_MERGE", "up_since": iso(5)})
    states.home.status = "running"
    states.run(T0)
    assert ssm.saved == []


def test_pending_merge_reminds_once_an_hour_when_the_restore_failed(make):
    states, ssm = make({"state": "PENDING_MERGE"})
    states.home.status = "failed"
    states.run(T0)
    states.run(T0 + timedelta(minutes=5))
    assert len(states.sent) == 1
    assert "reminded_at" in ssm.saved[-1]


def test_pending_merge_home_down_briefly_waits(make):
    states, ssm = make({"state": "PENDING_MERGE", "down_since": iso(3)}, home=False)
    states.run(T0)
    assert FakeTunnels.flips == []
    assert ssm.sent == 0


def test_pending_merge_home_down_rolls_back_to_the_instance(make):
    states, ssm = make(
        {"state": "PENDING_MERGE", "down_since": iso(11), "home_held": True}, home=False
    )
    states.run(T0)
    assert FakeTunnels.flips == ["alt"]
    assert ssm.sent == 1
    assert ssm.deleted == [[clients.StateStore.dump_name]]
    assert ssm.saved[-1]["state"] == "FAILED_OVER"
    assert "home_held" not in ssm.saved[-1]


def test_failing_back_without_a_dump_checks_the_command(make):
    states, ssm = make(
        {"state": "FAILING_BACK", "command_id": "cmd-0"}, invocation="InProgress"
    )
    states.run(T0)
    assert ssm.saved == []
    assert ssm.sent == 0


@pytest.mark.parametrize("status", ["Pending", "InProgress", "Delayed", "Success"])
def test_failing_back_leaves_a_live_command_alone(make, status):
    states, ssm = make(
        {"state": "FAILING_BACK", "command_id": "cmd-0"}, invocation=status
    )
    states.run(T0)
    assert ssm.saved == []
    assert ssm.sent == 0


@pytest.mark.parametrize("invocation", ["Failed", "TimedOut", "Cancelled", None])
def test_failing_back_reruns_a_failed_command(make, invocation):
    states, ssm = make(
        {"state": "FAILING_BACK", "command_id": "cmd-0"}, invocation=invocation
    )
    states.run(T0)
    assert ssm.saved == [{"state": "FAILING_BACK", "command_id": "cmd-1"}]


# home_ready


def test_home_ready_waits_while_home_is_down(make):
    states, ssm = make({"state": "HOME_READY"}, home=False)
    states.run(T0)
    assert FakeTunnels.flips == []
    assert states.instance.ec2.stopped == 0


def test_home_ready_flips_stops_and_goes_normal(make):
    states, ssm = make({"state": "HOME_READY", "dump": "gv-db/hourly/x"})
    states.run(T0)
    assert FakeTunnels.flips == ["home"]
    assert states.instance.ec2.stopped == 1
    assert ssm.saved == [{"state": "NORMAL"}]
    assert states.home.recorded == []


def test_home_ready_records_the_failover_then_goes_normal(make):
    states, ssm = make(
        {"state": "HOME_READY", "ec2_up_at": iso(40), "merge_since": iso(5)}
    )
    states.run(T0)
    assert states.home.recorded == [
        (T0 - timedelta(minutes=40), T0 - timedelta(minutes=5))
    ]
    assert states.instance.ec2.stopped == 1
    assert ssm.saved == [{"state": "NORMAL"}]


def test_home_ready_without_an_ack_waits_and_stops_nothing(make):
    states, ssm = make(
        {"state": "HOME_READY", "ec2_up_at": iso(40), "merge_since": iso(5)}
    )
    states.home.acked = False
    states.run(T0)
    assert FakeTunnels.flips == []
    assert states.instance.ec2.stopped == 0
    assert all(s["state"] == "HOME_READY" for s in ssm.saved)


def test_starting_flip_records_when_ec2_became_available(make):
    states, ssm = make({"state": "STARTING", "down_since": iso(15)})
    states.run(T0)
    assert ssm.saved[-1]["state"] == "FAILED_OVER"
    assert ssm.saved[-1]["ec2_up_at"] == T0.isoformat()


def test_pending_merge_done_keeps_both_instants(make):
    states, ssm = make(
        {"state": "PENDING_MERGE", "ec2_up_at": iso(30), "merge_since": iso(5)}
    )
    states.home.status = "done"
    states.run(T0)
    assert ssm.saved[-1]["state"] == "HOME_READY"
    assert ssm.saved[-1]["ec2_up_at"] == iso(30)
    assert ssm.saved[-1]["merge_since"] == iso(5)


def test_pending_merge_rollback_keeps_ec2_up_at(make):
    states, ssm = make(
        {
            "state": "PENDING_MERGE",
            "down_since": iso(11),
            "ec2_up_at": iso(60),
            "merge_since": iso(20),
        },
        home=False,
    )
    states.run(T0)
    assert ssm.saved[-1]["state"] == "FAILED_OVER"
    assert ssm.saved[-1]["ec2_up_at"] == iso(60)
    assert "merge_since" not in ssm.saved[-1]


# home client


class Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_record_failover_posts_both_instants(monkeypatch):
    seen = {}

    def urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data)
        seen["token"] = req.get_header("X-failover-token")
        return Resp()

    monkeypatch.setattr(clients.urllib.request, "urlopen", urlopen)
    home = clients.HomeClient("https://home/failover", "tok")

    assert home.record_failover(T0 - timedelta(minutes=40), T0) is True
    assert seen == {
        "url": "https://home/failover/outage",
        "body": {
            "ec2_up_at": (T0 - timedelta(minutes=40)).isoformat(),
            "home_at": T0.isoformat(),
        },
        "token": "tok",
    }


def test_record_failover_with_a_wrong_token_raises(monkeypatch):
    def urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)

    monkeypatch.setattr(clients.urllib.request, "urlopen", urlopen)
    home = clients.HomeClient("https://home/failover", "tok")

    with pytest.raises(RuntimeError):
        home.record_failover(T0, T0)


def test_record_failover_without_a_network_is_no_ack(monkeypatch):
    def urlopen(req, timeout):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(clients.urllib.request, "urlopen", urlopen)
    home = clients.HomeClient("https://home/failover", "tok")

    assert home.record_failover(T0, T0) is False


# telegram


def test_transitions_notify(make):
    states, _ = make({"state": "NORMAL", "down_since": iso(10)}, home=False)
    states.run(T0)
    assert states.sent == ["Home is down, starting the failover instance"]


def test_waiting_does_not_notify(make):
    states, _ = make({"state": "NORMAL", "down_since": iso(5)}, home=False)
    states.run(T0)
    assert states.sent == []


def test_send_message_posts_to_the_bot(monkeypatch):
    seen = {}

    def urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data)

    monkeypatch.setattr(clients.urllib.request, "urlopen", urlopen)
    clients.Telegram("tok", "42").send_message("hi")
    assert seen == {
        "url": "https://api.telegram.org/bottok/sendMessage",
        "body": {"chat_id": "42", "text": "hi"},
    }


def test_send_message_failure_is_only_logged(monkeypatch, capsys):
    def boom(req, timeout):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(clients.urllib.request, "urlopen", boom)
    clients.Telegram("tok", "42").send_message("hi")
    assert "TELEGRAM ERROR" in capsys.readouterr().out


# full cycle


def test_full_cycle(make):
    states, ssm = make(alt=False)
    checks = states.health.checks
    clock = {"minutes": 0}

    def tick():
        states.run(T0 + timedelta(minutes=clock["minutes"]))
        clock["minutes"] += 5
        return json.loads(ssm.params[clients.StateStore.state_name])["state"]

    def write(state):
        ssm.params[clients.StateStore.state_name] = json.dumps(state)

    assert tick() == "NORMAL"

    checks["HOMELAB"] = False
    assert [tick(), tick(), tick()] == ["NORMAL", "NORMAL", "STARTING"]
    assert states.instance.ec2.started == 1

    states.instance.ec2.name = "pending"
    assert tick() == "STARTING"
    states.instance.ec2.name = "running"
    assert tick() == "STARTING"
    checks["ALT"] = True
    assert tick() == "FAILED_OVER"
    assert FakeTunnels.flips == ["alt"]

    assert tick() == "FAILED_OVER"
    checks["HOMELAB"] = True
    assert [tick(), tick(), tick()] == ["FAILED_OVER", "FAILED_OVER", "FAILING_BACK"]
    assert ssm.sent == 1
    assert json.loads(ssm.params[clients.StateStore.state_name])["command_id"] == "cmd-1"

    ssm.invocation = "InProgress"
    assert tick() == "FAILING_BACK"
    assert ssm.sent == 1

    ssm.invocation = "Success"
    failing = json.loads(ssm.params[clients.StateStore.state_name])
    failing.pop("command_id")
    ssm.params[clients.StateStore.dump_name] = "gv-db/hourly/x"
    assert tick() == "PENDING_MERGE"
    assert states.home.calls[-1] == {"mode": "merge"}
    assert ssm.sent == 1
    assert ssm.deleted == [[clients.StateStore.base_name, clients.StateStore.dump_name], [clients.StateStore.dump_name]]
    assert FakeTunnels.flips == ["alt", "home"]

    states.home.status = "running"
    assert tick() == "PENDING_MERGE"
    states.home.status = "done"
    assert tick() == "HOME_READY"

    assert tick() == "NORMAL"
    assert FakeTunnels.flips == ["alt", "home", "home"]
    assert states.instance.ec2.stopped == 1

    assert states.sent == [
        "Home is down, starting the failover instance",
        "Failed over to AWS",
        "Home is back, failing back",
        "Dump ready, home is merging it",
        "Merge done on home",
        "Back on home, failover instance stopped",
    ]


# cloudflare


class FakeCall:
    def __init__(self, contents):
        self.recs = {h: {"id": h, "content": c} for h, c in contents.items()}
        self.patches = []

    def __call__(self, method, path, body=None):
        if method == "GET":
            host = path.split("name=")[1].split("&")[0]
            return [self.recs[host]] if host in self.recs else []
        host = path.rsplit("/", 1)[1]
        self.recs[host]["content"] = body["content"]
        self.patches.append(host)


def cloudflare(monkeypatch, contents):
    cf = clients.Cloudflare("t", "z")
    fake = FakeCall(contents)
    monkeypatch.setattr(cf, "call", fake)
    return cf, fake


def test_flip_api_first_then_web(monkeypatch):
    cf, fake = cloudflare(
        monkeypatch,
        {
            "gv-api.lab-ocp.com": "h.cfargotunnel.com",
            "gv.lab-ocp.com": "h.cfargotunnel.com",
        },
    )
    assert cf.flip("alt") is True
    assert fake.patches == ["gv-api.lab-ocp.com", "gv.lab-ocp.com"]


def test_flip_is_idempotent_and_finishes_a_half_flip(monkeypatch):
    cf, fake = cloudflare(
        monkeypatch,
        {
            "gv-api.lab-ocp.com": "alt.cfargotunnel.com",
            "gv.lab-ocp.com": "h.cfargotunnel.com",
        },
    )
    assert cf.flip("alt") is True
    assert fake.patches == ["gv.lab-ocp.com"]
    assert cf.flip("alt") is False


def test_flip_missing_record_raises(monkeypatch):
    cf, _ = cloudflare(monkeypatch, {"gv-api.lab-ocp.com": "h.cfargotunnel.com"})
    with pytest.raises(RuntimeError):
        cf.flip("alt")


# Snapshot


def test_snapshot_json_roundtrip_and_omits_defaults():
    snap = clients.Snapshot(
        clients.State.FAILING_BACK, up_since=T0, home_held=True, command_id="cmd-1"
    )
    raw = snap.to_json()
    assert raw == {
        "state": "FAILING_BACK",
        "up_since": T0.isoformat(),
        "home_held": True,
        "command_id": "cmd-1",
    }
    assert clients.Snapshot.from_json(raw) == snap


def test_snapshot_seen_starts_one_streak_and_ends_the_other():
    snap = clients.Snapshot(clients.State.NORMAL, up_since=T0)
    down = snap.seen(False, T0 + timedelta(minutes=5))
    assert down.up_since is None and down.down_since == T0 + timedelta(minutes=5)
    assert down.seen(False, T0 + timedelta(minutes=9)).down_since == down.down_since


# build_states


def test_build_states_wires_the_clients_from_env():
    import app

    ssm = FakeSSM()
    states = app.build_states(
        {"FAILOVER_INSTANCE_ID": "i-9", "HOME_LAB_API_URL": "http://h"}, ssm, FakeEC2()
    )
    assert states.store.ssm is ssm
    assert states.instance.instance_id == "i-9"
    assert states.health_url == "http://h/health"
    assert states.alt_health_url == "https://gv-alt-health.lab-ocp.com/health"


# Snapshot / lazy / params / handler


def test_snapshot_roundtrips_every_field():
    values = {}
    for f in dataclasses.fields(clients.Snapshot):
        if f.name == "state":
            values[f.name] = clients.State.PENDING_MERGE
        elif f.name in clients.DATETIME_FIELDS:
            values[f.name] = T0
        elif f.type is bool:
            values[f.name] = True
        else:
            values[f.name] = "x"
    snap = clients.Snapshot(**values)
    assert clients.Snapshot.from_json(snap.to_json()) == snap


def test_datetime_fields_are_exactly_the_datetime_annotations():
    declared = {
        f.name
        for f in dataclasses.fields(clients.Snapshot)
        if "datetime" in str(f.type)
    }
    assert declared == clients.DATETIME_FIELDS


def test_lazy_builds_once_and_only_on_first_use():
    built = []
    proxy = clients.lazy(lambda: built.append(1) or FakeHome())
    assert built == []
    proxy.home_status()
    proxy.home_status()
    assert built == [1]


def test_params_reads_everything_in_one_call_and_misses_raise_on_use():
    ssm = FakeSSM()
    ssm.params["/home-lab-failover/failover-token"] = "secret"
    params = clients.Params(ssm)
    assert params["failover-token"] == "secret"
    with pytest.raises(KeyError):
        params["nope"]


def test_healthy_run_never_reads_a_secret(monkeypatch):
    import app

    ssm = FakeSSM()
    ssm.get_parameters_by_path = lambda **kw: 1 / 0
    monkeypatch.setattr(
        clients,
        "http_open",
        lambda url, **kw: type(
            "R", (), {"status": 200, "__enter__": lambda s: s, "__exit__": lambda s, *a: False}
        )(),
    )
    states = app.build_states({"FAILOVER_INSTANCE_ID": "i-1"}, ssm, FakeEC2())
    assert states.run(T0).state == clients.State.NORMAL


def test_failing_back_without_command_id_reruns_failback(make):
    states, ssm = make({"state": "FAILING_BACK"}, invocation=None)
    new = states.run(T0)
    assert ssm.sent == 1
    assert new.command_id == "cmd-1"


def test_handler_error_still_raises_the_original_when_notifying_fails(monkeypatch):
    import app

    class Boom:
        def run(self, now):
            raise ValueError("original")

        @property
        def telegram(self):
            raise RuntimeError("ssm down")

    monkeypatch.setattr(app, "build_states", lambda *a: Boom())
    monkeypatch.setattr(app.boto3, "client", lambda name: None)
    with pytest.raises(ValueError, match="original"):
        app.lambda_handler({}, None)
