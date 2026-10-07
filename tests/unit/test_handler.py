import urllib.error

import pytest

from watchdog import app


class FakeResponse:
    status = 200


def test_health_ok(monkeypatch, capsys):
    monkeypatch.setattr(app.urllib.request, "urlopen", lambda req, timeout: FakeResponse())

    app.lambda_handler({}, None)

    assert capsys.readouterr().out.startswith("HOMELAB HEALTHY on ")


def test_health_down(monkeypatch, capsys):
    def fail(req, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(app.urllib.request, "urlopen", fail)

    app.lambda_handler({}, None)

    assert capsys.readouterr().out.startswith("HOMELAB UNHEALTHY on ")


def test_health_non_200(monkeypatch, capsys):
    class NoContent:
        status = 204

    monkeypatch.setattr(app.urllib.request, "urlopen", lambda req, timeout: NoContent())

    app.lambda_handler({}, None)

    assert capsys.readouterr().out.startswith("HOMELAB UNHEALTHY on ")


def test_check_bug_is_not_unhealthy(monkeypatch, capsys):
    def bug(req, timeout):
        raise KeyError("oops")

    monkeypatch.setattr(app.urllib.request, "urlopen", bug)

    with pytest.raises(KeyError):
        app.lambda_handler({}, None)

    out = capsys.readouterr().out
    assert "CHECK ERROR" in out and "UNHEALTHY" not in out
