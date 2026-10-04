import urllib.error

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
