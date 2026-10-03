"""STOP and HOLD LIVE refuse cross-site requests but still work from the real page (#50)."""

import json
import re

import pytest
from fastapi.testclient import TestClient

from trader import config, dashboard, golive, runner

USER = "me@example.com"
SITE = "https://box.tailnet.ts.net:8444"
CONTROLS = [("/api/stop", "STOP"), ("/api/hold-live", "HOLD")]

# What `tailscale serve --https=8444 unix:<sock>` hands the app: Host is the socket's
# `localhost`, and the browser's host arrives only in X-Forwarded-Host.
TAILSCALE_SERVE = {
    "Tailscale-User-Login": USER,
    "Tailscale-User-Name": "Me",
    "X-Forwarded-Host": "box.tailnet.ts.net:8444",
    "X-Forwarded-Proto": "https",
    "X-Forwarded-For": "100.64.0.2",
}


@pytest.fixture
def acted(monkeypatch):
    """Record which control actually ran instead of flattening or vetoing anything."""
    calls = []
    monkeypatch.setattr(dashboard, "USERS", {USER})
    monkeypatch.setattr(runner, "request_stop", lambda: calls.append("STOP") or "stop requested")
    monkeypatch.setattr(golive, "hold", lambda notify, by: calls.append("HOLD") or "held")
    return calls


@pytest.fixture
def client():
    return TestClient(dashboard.app, base_url="http://localhost", headers=TAILSCALE_SERVE)


def _page_fetch(path: str) -> dict:
    """The fetch options index.html's own STOP/HOLD button sends to `path`. Deliberately pinned
    to the page's literal call: reformatting it fails this loudly, so update the regex with it."""
    page = (config.CODE_ROOT / "dashboard" / "static" / "index.html").read_text()
    m = re.search(r'api\("' + re.escape(path) + r'", \{ method: "(\w+)", headers: (\{[^}]*\}), body: JSON\.stringify\((\{[^}]*\})\)', page)
    assert m, f"no fetch of {path} found in index.html"
    to_json = lambda js: json.loads(re.sub(r'(\w[\w-]*|"[^"]*"): ', lambda k: json.dumps(k[1].strip('"')) + ": ", js))
    return {"method": m[1], "headers": to_json(m[2]), "body": to_json(m[3])}


@pytest.mark.parametrize("path,word", CONTROLS)
def test_real_page_through_tailscale_serve_succeeds(acted, client, path, word):
    sent = _page_fetch(path)
    assert sent["body"] == {"confirm": word}
    browser = {"Origin": SITE, "Referer": SITE + "/", "Sec-Fetch-Site": "same-origin",
               "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
    r = client.request(sent["method"], path, content=json.dumps(sent["body"]), headers={**browser, **sent["headers"]})
    assert r.status_code == 200, r.text
    assert acted == [word]


@pytest.mark.parametrize("path,word", CONTROLS)
@pytest.mark.parametrize("fetch_site", ["cross-site", None])  # None: a browser without Fetch Metadata
@pytest.mark.parametrize("enctype", ["text/plain", "application/x-www-form-urlencoded"])
def test_cross_site_form_post_refused(acted, client, path, word, fetch_site, enctype):
    # <form method=post enctype=text/plain> with a field named '{"confirm":"WORD","x":"'
    # and value '"}' posts a body that parses as JSON.
    headers = {"Content-Type": enctype, "Origin": "https://evil.example"}
    if fetch_site:
        headers["Sec-Fetch-Site"] = fetch_site
    r = client.post(path, content='{"confirm":"%s","x":"="}' % word, headers=headers)
    assert r.status_code in (403, 415)
    assert acted == []


@pytest.mark.parametrize("path,word", CONTROLS)
@pytest.mark.parametrize("fetch_site", ["cross-site", "same-site"])
def test_json_from_another_site_refused(acted, client, path, word, fetch_site):
    r = client.post(path, json={"confirm": word}, headers={"Origin": "https://other.tailnet.ts.net", "Sec-Fetch-Site": fetch_site})
    assert r.status_code == 403
    assert acted == []


@pytest.mark.parametrize("path,word", CONTROLS)
def test_non_object_body_is_a_bad_request(acted, client, path, word):
    assert client.post(path, json=[word]).status_code == 400
    assert acted == []
