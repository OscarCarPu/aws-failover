import json
import urllib.error
from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from watchdog import app

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
            self.params[app.StateStore.state_name] = json.dumps(state)
        self.invocation = invocation
        self.sent = 0
        self.saved = []
        self.deleted = []

    def get_parameter(self, Name, WithDecryption=False):
        if Name not in self.params:
            raise self.exceptions.ParameterNotFound()
        return {"Parameter": {"Value": self.params[Name]}}

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


class FakeCloudflare:
    flips = []

    def __init__(self, token, zone):
        pass

    def flip(self, tunnel_id):
        self.flips.append(tunnel_id)


class FakeTelegram:
    def __init__(self, sent):
        self.send_message = sent.append

class FakeHome:
    def __init__(self, home=True, alt=True):
        self.checks = {"HOMELAB": home, "ALT": alt}
        self.checked = []
        self.calls = []
        self.acked = True
        self.status = None

    def check(self, url, name):
        self.checked.append(name)
        return self.checks[name]

    def home_call(self, payload):
        self.calls.append(payload)
        return self.acked

    def home_status(self):
        return self.status


@pytest.fixture
def make(monkeypatch):
    FakeCloudflare.flips = []
    monkeypatch.setattr(app, "Cloudflare", FakeCloudflare)

    def build(state=None, home=True, alt=True, ec2="running", invocation="Success"):
        ssm = FakeSSM(state, invocation)
        states = app.States(
            app.StateStore(ssm),
            app.Instance(ssm, FakeEC2(ec2), "i-1"),
            "http://home/health",
            "http://alt/health",
        )
        states.__dict__["home"] = FakeHome(home, alt)
        states.sent = []
        states.__dict__["telegram"] = FakeTelegram(states.sent)
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

    monkeypatch.setattr(app.urllib.request, "urlopen", urlopen)


def checker():
    return app.HomeClient("http://home/failover", "tok")


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
    assert seen == [[[app.StateStore.base_name, app.StateStore.dump_name]]]


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
    assert FakeCloudflare.flips == []


def test_starting_flips_when_alt_is_healthy_without_checking_home(make):
    states, ssm = make({"state": "STARTING", "down_since": iso(15)}, home=False)
    states.run(T0)
    assert states.home.checked == ["ALT"]
    assert FakeCloudflare.flips == ["alt"]
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
    assert ssm.deleted == [[app.StateStore.dump_name]]
    assert ssm.sent == 1


# failing_back


def test_failing_back_with_a_dump_asks_home_to_merge_and_flips(make):
    states, ssm = make({"state": "FAILING_BACK"}, invocation=None)
    ssm.params[app.StateStore.dump_name] = "gv-db/hourly/x"
    states.run(T0)
    assert states.home.calls == [{"mode": "merge"}]
    assert FakeCloudflare.flips == ["home"]
    assert ssm.saved[-1]["state"] == "PENDING_MERGE"
    assert ssm.sent == 0


def test_failing_back_without_an_ack_does_not_flip(make):
    states, ssm = make({"state": "FAILING_BACK"}, invocation=None)
    ssm.params[app.StateStore.dump_name] = "gv-db/hourly/x"
    states.home.acked = False
    states.run(T0)
    assert FakeCloudflare.flips == []
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


def test_pending_merge_reminds_once_an_hour(make):
    states, ssm = make({"state": "PENDING_MERGE"})
    states.home.status = "needs_answers"
    states.run(T0)
    states.run(T0 + timedelta(minutes=5))
    assert len(states.sent) == 1
    assert "reminded_at" in ssm.saved[-1]


def test_pending_merge_home_down_briefly_waits(make):
    states, ssm = make({"state": "PENDING_MERGE", "down_since": iso(3)}, home=False)
    states.run(T0)
    assert FakeCloudflare.flips == []
    assert ssm.sent == 0


def test_pending_merge_home_down_rolls_back_to_the_instance(make):
    states, ssm = make(
        {"state": "PENDING_MERGE", "down_since": iso(11), "home_held": True}, home=False
    )
    states.run(T0)
    assert FakeCloudflare.flips == ["alt"]
    assert ssm.sent == 1
    assert ssm.deleted == [[app.StateStore.dump_name]]
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
    assert FakeCloudflare.flips == []
    assert states.instance.ec2.stopped == 0


def test_home_ready_flips_stops_and_goes_normal(make):
    states, ssm = make({"state": "HOME_READY", "dump": "gv-db/hourly/x"})
    states.run(T0)
    assert FakeCloudflare.flips == ["home"]
    assert states.instance.ec2.stopped == 1
    assert ssm.saved == [{"state": "NORMAL"}]


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

    monkeypatch.setattr(app.urllib.request, "urlopen", urlopen)
    app.Telegram("tok", "42").send_message("hi")
    assert seen == {
        "url": "https://api.telegram.org/bottok/sendMessage",
        "body": {"chat_id": "42", "text": "hi"},
    }


def test_send_message_failure_is_only_logged(monkeypatch, capsys):
    def boom(req, timeout):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(app.urllib.request, "urlopen", boom)
    app.Telegram("tok", "42").send_message("hi")
    assert "TELEGRAM ERROR" in capsys.readouterr().out


# full cycle


def test_full_cycle(make):
    states, ssm = make(alt=False)
    checks = states.home.checks
    clock = {"minutes": 0}

    def tick():
        states.run(T0 + timedelta(minutes=clock["minutes"]))
        clock["minutes"] += 5
        return json.loads(ssm.params[app.StateStore.state_name])["state"]

    def write(state):
        ssm.params[app.StateStore.state_name] = json.dumps(state)

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
    assert FakeCloudflare.flips == ["alt"]

    assert tick() == "FAILED_OVER"
    checks["HOMELAB"] = True
    assert [tick(), tick(), tick()] == ["FAILED_OVER", "FAILED_OVER", "FAILING_BACK"]
    assert ssm.sent == 1
    assert json.loads(ssm.params[app.StateStore.state_name])["command_id"] == "cmd-1"

    ssm.invocation = "InProgress"
    assert tick() == "FAILING_BACK"
    assert ssm.sent == 1

    ssm.invocation = "Success"
    failing = json.loads(ssm.params[app.StateStore.state_name])
    failing.pop("command_id")
    ssm.params[app.StateStore.dump_name] = "gv-db/hourly/x"
    assert tick() == "PENDING_MERGE"
    assert states.home.calls[-1] == {"mode": "merge"}
    assert ssm.sent == 1
    assert ssm.deleted == [[app.StateStore.base_name, app.StateStore.dump_name], [app.StateStore.dump_name]]
    assert FakeCloudflare.flips == ["alt", "home"]

    states.home.status = "running"
    assert tick() == "PENDING_MERGE"
    states.home.status = "done"
    assert tick() == "HOME_READY"

    assert tick() == "NORMAL"
    assert FakeCloudflare.flips == ["alt", "home", "home"]
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
    cf = app.Cloudflare("t", "z")
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
