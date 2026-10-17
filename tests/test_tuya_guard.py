import base64
import http.client
import json
import os
import random
from collections import deque

import pytest

import tuya_guard as tg


def make_cfg(tmp_path, **overrides):
    cfg = {
        "language": "zh-TW",
        "min_revert_interval": 0,
        "max_reverts_per_minute": 30,
        "state_file": str(tmp_path / "state.json"),
        "dps": {
            "1": {"name": "電源", "options": [True, False], "labels": ["開", "關"]},
            "3": {"name": "風速", "options": [1, 2, 3]},
        },
    }
    cfg.update(overrides)
    return cfg


@pytest.fixture
def guard(tmp_path):
    g = tg.Guard(make_cfg(tmp_path))
    g.on_dps({"1": False, "3": 2}, initial=True)
    return g


# ───────────── 守護邏輯 ─────────────

def test_first_report_becomes_target(guard):
    assert guard.desired == {"1": False, "3": 2}
    assert guard.pending_commands() == {}


def test_ghost_touch_is_reverted(guard):
    guard.on_dps({"1": True})
    assert guard.pending_commands() == {"1": False}
    assert guard.ghost_total == 1
    assert guard.events[0]["from"] == "關" and guard.events[0]["to"] == "開"


def test_dps_outside_config_are_ignored(guard):
    guard.on_dps({"99": "whatever"})
    assert guard.pending_commands() == {}
    assert guard.ghost_total == 0


def test_pause_adopts_new_target(guard):
    guard.pause(5)
    guard.on_dps({"3": 3})
    assert guard.desired["3"] == 3
    assert guard.pending_commands() == {}
    guard.resume()
    assert guard.pending_commands() == {}


def test_target_survives_restart(guard):
    guard.set_option("3", 0)
    reloaded = tg.Guard(guard.cfg)
    assert reloaded.desired == {"1": False, "3": 1}


def test_min_revert_interval(tmp_path):
    g = tg.Guard(make_cfg(tmp_path, min_revert_interval=60))
    g.on_dps({"1": False}, initial=True)
    g.on_dps({"1": True})
    assert g.pending_commands() == {"1": False}
    assert g.pending_commands() == {}   # 裝置還沒改回來，但間隔太短不重送


def test_throttle_stops_runaway_reverts(tmp_path):
    g = tg.Guard(make_cfg(tmp_path, max_reverts_per_minute=2))
    g.on_dps({"1": False}, initial=True)
    g.on_dps({"1": True})               # 裝置一直不聽話，例如設定檔的值型別寫錯
    assert g.pending_commands() == {"1": False}
    assert g.pending_commands() == {"1": False}
    assert g.pending_commands() == {}
    assert g.throttled


def test_corrupt_state_file_falls_back_to_device_state(tmp_path):
    cfg = make_cfg(tmp_path)
    (tmp_path / "state.json").write_text("not json", encoding="utf-8")
    g = tg.Guard(cfg)
    g.on_dps({"1": True}, initial=True)
    assert g.desired == {"1": True}


# ───────────── 主迴圈搭配模擬裝置 ─────────────

class ScriptedDevice(tg.FakeDevice):
    """依腳本依序產生誤觸，腳本跑完就斷線，讓 run_session 結束。"""

    def __init__(self, dp_cfg, script):
        super().__init__(dp_cfg, ghost_every=1e9)
        self.script = deque(script)

    def receive(self):
        if self.pushes:
            return {"dps": self.pushes.popleft()}
        if self.script:
            change = self.script.popleft()
            self.dps.update(change)
            return {"dps": change}
        raise ConnectionError("腳本結束")


def test_run_session_reverts_ghost_touch(tmp_path):
    cfg = make_cfg(tmp_path)
    g = tg.Guard(cfg)
    d = ScriptedDevice(cfg["dps"], [{"1": False}])
    with pytest.raises(ConnectionError):
        tg.run_session(d, g)
    assert d.dps["1"] is True          # 一開始是開，被誤觸關掉後又被改回開
    assert g.ghost_total == 1


def test_fake_device_ghost_touch_changes_a_value(tmp_path):
    cfg = make_cfg(tmp_path)
    d = tg.FakeDevice(cfg["dps"], ghost_every=0, rng=random.Random(1))
    before = dict(d.dps)
    (dp, value), = d.receive()["dps"].items()
    assert before[dp] != value


# ───────────── 設定檔 ─────────────

def test_example_config_loads():
    cfg = tg.load_config(tg.EXAMPLE_CONFIG_PATH)
    assert cfg["dps"]
    for c in cfg["dps"].values():
        assert c["options"]


def test_missing_device_fields_exit(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"dps": {}}), encoding="utf-8")
    with pytest.raises(SystemExit):
        tg.load_config(str(path))
    assert tg.load_config(str(path), need_device=False)["http_port"] == 8765


# ───────────── 控制頁 ─────────────

def start_server(guard, password=""):
    server = tg.start_http(guard, "127.0.0.1", 0, password)
    return server, server.server_address[1]


def request(port, method, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, headers=headers or {})
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    conn.close()
    return resp.status, body


@pytest.fixture
def server(guard):
    srv, port = start_server(guard)
    yield port
    srv.shutdown()
    srv.server_close()


def test_page_renders(server, guard):
    status, body = request(server, "GET", "/")
    assert status == 200
    assert "電源" in body and "風速" in body


def test_page_in_english(tmp_path):
    g = tg.Guard(make_cfg(tmp_path, language="en"))
    assert "Offline" in tg.render_page(g)


def test_api_state(server):
    status, body = request(server, "GET", "/api/state")
    assert status == 200
    assert json.loads(body)["dps"]["1"]["desired"] is False


def test_set_target(server, guard):
    status, _ = request(server, "POST", "/set?dp=3&i=2", {"Sec-Fetch-Site": "same-origin"})
    assert status == 303
    assert guard.desired["3"] == 3


def test_set_rejects_unknown_option(server, guard):
    assert request(server, "POST", "/set?dp=99&i=0")[0] == 400
    assert request(server, "POST", "/set?dp=3&i=9")[0] == 400


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "cross-site"},
    {"Origin": "http://evil.example"},
])
def test_rejects_commands_from_other_sites(server, guard, headers):
    status, _ = request(server, "POST", "/set?dp=1&i=0", headers)
    assert status == 403
    assert guard.desired["1"] is False


def test_pause_and_resume(server, guard):
    assert request(server, "POST", "/pause?min=5")[0] == 303
    assert guard.paused()
    assert request(server, "POST", "/resume")[0] == 303
    assert not guard.paused()


def test_password(guard):
    srv, port = start_server(guard, password="secret")
    try:
        def auth(pw):
            token = base64.b64encode(f"any:{pw}".encode()).decode()
            return {"Authorization": f"Basic {token}"}
        assert request(port, "GET", "/")[0] == 401
        assert request(port, "GET", "/", auth("wrong"))[0] == 401
        assert request(port, "GET", "/", auth("secret"))[0] == 200
        assert request(port, "POST", "/resume")[0] == 401
    finally:
        srv.shutdown()
        srv.server_close()


def test_state_file_written_atomically(guard):
    guard.set_option("1", 0)
    with open(guard.cfg["state_file"], encoding="utf-8") as f:
        assert json.load(f) == {"desired": {"1": True, "3": 2}}
    assert not os.path.exists(guard.cfg["state_file"] + ".tmp")
