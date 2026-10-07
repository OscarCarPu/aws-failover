import pytest

from watchdog import cloudflare


class Fake:
    def __init__(self, contents):
        self.recs = {h: {"id": h, "content": c} for h, c in contents.items()}
        self.patches = []

    def __call__(self, token, method, path, body=None):
        if method == "GET":
            host = path.split("name=")[1].split("&")[0]
            return [self.recs[host]] if host in self.recs else []
        self.recs[path.rsplit("/", 1)[1]]["content"] = body["content"]
        self.patches.append(path)


def fake(monkeypatch, contents):
    f = Fake(contents)
    monkeypatch.setattr(cloudflare, "call", f)
    return f


def test_flips_both_api_first(monkeypatch):
    f = fake(monkeypatch, {"gv-api.lab-ocp.com": "h.cfargotunnel.com", "gv.lab-ocp.com": "h.cfargotunnel.com"})
    assert cloudflare.set_target("t", "z", "alt") is True
    assert [p.rsplit("/", 1)[1] for p in f.patches] == ["gv-api.lab-ocp.com", "gv.lab-ocp.com"]
    assert all(r["content"] == "alt.cfargotunnel.com" for r in f.recs.values())


def test_idempotent(monkeypatch):
    f = fake(monkeypatch, {h: "alt.cfargotunnel.com" for h in cloudflare.HOSTS})
    assert cloudflare.set_target("t", "z", "alt") is False
    assert f.patches == []


def test_half_flipped_finishes(monkeypatch):
    f = fake(monkeypatch, {"gv-api.lab-ocp.com": "alt.cfargotunnel.com", "gv.lab-ocp.com": "h.cfargotunnel.com"})
    assert cloudflare.set_target("t", "z", "alt") is True
    assert len(f.patches) == 1


def test_missing_record(monkeypatch):
    fake(monkeypatch, {"gv-api.lab-ocp.com": "h.cfargotunnel.com"})
    with pytest.raises(RuntimeError):
        cloudflare.set_target("t", "z", "alt")
