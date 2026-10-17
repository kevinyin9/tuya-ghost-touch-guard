#!/usr/bin/env python3
"""
Tuya 裝置防誤觸守護程式（區域網路版）
Tuya ghost-touch guard (local network)

透過 Tuya 區域網路協定與裝置保持長連線；裝置每次狀態改變都會主動推送。
受守護的 DP（例如電源、風速）只要變成不是「目標狀態」的值，就立刻改回去。

目標狀態只能從本程式的控制頁修改。按下「暫停」後的那段時間內，任何改變
（實體按鍵、原廠 app）都會被採納為新的目標狀態，暫停結束後就守護新狀態。

用法：
  python tuya_guard.py monitor   只監聽並印出所有 DP 變化，用來找出每個按鍵對應哪個 DP
  python tuya_guard.py guard     開始守護，並在 http://<這台機器的 IP>:<http_port>/ 提供控制頁
  python tuya_guard.py demo      用模擬裝置示範守護流程，不需要真的 Tuya 裝置

選項：
  --config PATH   設定檔路徑，預設為同目錄的 config.json（也可用環境變數 TUYA_GUARD_CONFIG）
  --port N        控制頁的埠號，覆蓋設定檔的 http_port
"""

import argparse
import base64
import hmac
import html
import json
import logging
import os
import random
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import tinytuya

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("TUYA_GUARD_CONFIG", os.path.join(BASE_DIR, "config.json"))
EXAMPLE_CONFIG_PATH = os.path.join(BASE_DIR, "config.example.json")

HEARTBEAT_EVERY = 10   # 秒：多久送一次心跳維持長連線
LOOP_TIMEOUT = 1       # 秒：主迴圈最長等待時間，決定控制頁指令的反應速度
RECONNECT_DELAY = 5    # 秒：斷線後多久重連
DEMO_GHOST_EVERY = 8   # 秒：示範模式中模擬裝置多久自己亂跳一次

MISSING = object()
log = logging.getLogger("tuya_guard")


# ───────────────────────── 設定與連線 ─────────────────────────

def load_config(path=CONFIG_PATH, need_device=True):
    """need_device=False 時不檢查裝置連線資料（示範模式用）。"""
    if not os.path.exists(path):
        sys.exit(f"找不到設定檔：{path}（請複製 config.example.json 為 config.json 並填入資料）")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if need_device:
        for key in ("device_id", "ip", "local_key"):
            if not cfg.get(key):
                sys.exit(f"設定檔缺少 {key}：{path}")
    cfg.setdefault("version", 3.3)
    cfg.setdefault("http_host", "0.0.0.0")
    cfg.setdefault("http_port", 8765)
    cfg.setdefault("http_password", "")
    cfg.setdefault("language", "zh-TW")
    cfg.setdefault("min_revert_interval", 0.5)
    cfg.setdefault("max_reverts_per_minute", 30)
    cfg.setdefault("state_file", os.path.join(BASE_DIR, "guard_state.json"))
    cfg["dps"] = {str(k): v for k, v in cfg.get("dps", {}).items()}
    return cfg


def make_device(cfg):
    """ip 填 "Auto" 時會在區網廣播尋找裝置，並自動判斷協定版本。"""
    try:
        d = tinytuya.Device(cfg["device_id"], cfg["ip"], cfg["local_key"],
                            version=float(cfg["version"]))
    except RuntimeError as e:
        raise ConnectionError(f"區網內找不到裝置（{e}）") from e
    d.set_socketPersistent(True)
    return d


class FakeDevice:
    """模擬的 Tuya 裝置，只實作守護程式用到的 tinytuya.Device 方法。

    每隔 ghost_every 秒會自己亂改一個 DP，模擬觸控面板誤觸；
    收到指令後會像真的裝置一樣，把新狀態推送回來。
    """

    address = "demo"
    version = 3.3

    def __init__(self, dp_cfg, ghost_every=DEMO_GHOST_EVERY, rng=None):
        self.dp_cfg = dp_cfg
        self.dps = {dp: c["options"][0] for dp, c in dp_cfg.items() if c.get("options")}
        self.ghost_every = ghost_every
        self.rng = rng or random.Random()
        self.timeout = LOOP_TIMEOUT
        self.pushes = deque()
        self.next_ghost = time.monotonic() + ghost_every

    def set_socketPersistent(self, persist):
        pass

    def set_socketTimeout(self, seconds):
        self.timeout = seconds

    def close(self):
        pass

    def heartbeat(self):
        return None

    def status(self):
        return {"dps": dict(self.dps)}

    def set_value(self, dp, value, nowait=False):
        return self.set_multiple_values({dp: value}, nowait=nowait)

    def set_multiple_values(self, data, nowait=False):
        changed = {str(dp): v for dp, v in data.items()}
        self.dps.update(changed)
        self.pushes.append(changed)
        return None

    def receive(self):
        if self.pushes:
            return {"dps": self.pushes.popleft()}
        wait = min(self.timeout, self.next_ghost - time.monotonic())
        if wait > 0:
            time.sleep(wait)
        if time.monotonic() < self.next_ghost:
            return None
        self.next_ghost = time.monotonic() + self.ghost_every
        return {"dps": self.ghost_touch()}

    def ghost_touch(self):
        dp = self.rng.choice(sorted(self.dps))
        others = [o for o in self.dp_cfg[dp]["options"] if o != self.dps[dp]]
        if not others:
            return {}
        self.dps[dp] = self.rng.choice(others)
        return {dp: self.dps[dp]}


def extract_dps(data):
    """從 tinytuya 回傳的資料取出 DP 字典；沒有就回傳 None。"""
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("dps"), dict):
        return data["dps"]
    inner = data.get("data")
    if isinstance(inner, dict) and isinstance(inner.get("dps"), dict):
        return inner["dps"]
    return None


def describe_error(data):
    if isinstance(data, dict) and data.get("Err"):
        return f"錯誤碼 {data.get('Err')}：{data.get('Error')}"
    return f"沒有收到狀態資料（{data!r}）"


def check_error(data):
    if isinstance(data, dict) and data.get("Err"):
        raise ConnectionError(describe_error(data))


def ts():
    return time.strftime("%H:%M:%S")


def dp_sort_key(dp):
    return int(dp) if str(dp).isdigit() else 10**9


# ───────────────────────── 監聽模式 ─────────────────────────

def run_monitor(cfg):
    print(f"連線到 {cfg['ip']}…")
    while True:
        d = None
        try:
            d = make_device(cfg)
            print(f"[{ts()}] 裝置位址 {d.address}，協定版本 {d.version}")
            data = d.status()
            dps = extract_dps(data)
            if dps is None:
                raise ConnectionError(describe_error(data))
            print(f"[{ts()}] 初始狀態：")
            for dp in sorted(dps, key=dp_sort_key):
                print(f"    DP {dp:>4} = {dps[dp]!r}")
            print("\n現在逐一按裝置上的按鍵、或用原廠 app 調整，看哪個 DP 跟著變。Ctrl+C 結束。\n")
            d.set_socketTimeout(LOOP_TIMEOUT)
            last_hb = time.monotonic()
            while True:
                data = d.receive()
                check_error(data)
                dps = extract_dps(data)
                if dps:
                    for dp in sorted(dps, key=dp_sort_key):
                        print(f"[{ts()}] DP {dp:>4} → {dps[dp]!r}")
                if time.monotonic() - last_hb >= HEARTBEAT_EVERY:
                    check_error(d.heartbeat())
                    last_hb = time.monotonic()
        except ConnectionError as e:
            print(f"[{ts()}] 連線問題：{e}，{RECONNECT_DELAY} 秒後重連")
        finally:
            if d is not None:
                try:
                    d.close()
                except Exception:
                    pass
        time.sleep(RECONNECT_DELAY)


# ───────────────────────── 守護狀態 ─────────────────────────

class Guard:
    def __init__(self, cfg):
        self.cfg = cfg
        self.dp_cfg = cfg["dps"]
        self.lock = threading.Lock()
        self.desired = {}           # dp -> 目標值
        self.actual = {}            # dp -> 裝置回報的目前值
        self.connected = False
        self.paused_until = 0.0     # time.time()
        self.events = deque(maxlen=30)
        self.ghost_total = 0
        self.last_revert = {}       # dp -> time.monotonic()
        self.revert_times = deque() # 最近一分鐘送出改回指令的時間
        self.throttled = False
        self._load()

    # 狀態檔只存目標狀態，重啟後沿用
    def _load(self):
        try:
            with open(self.cfg["state_file"], encoding="utf-8") as f:
                saved = json.load(f).get("desired", {})
            self.desired = {k: v for k, v in saved.items() if k in self.dp_cfg}
            if self.desired:
                log.info("讀到上次的目標狀態：%s", self.fmt_dps(self.desired))
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("狀態檔讀取失敗（%s），改以裝置目前狀態為目標", e)

    def _save(self):
        tmp = self.cfg["state_file"] + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"desired": self.desired}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.cfg["state_file"])

    def name(self, dp):
        return self.dp_cfg.get(dp, {}).get("name", f"DP {dp}")

    def label(self, dp, value):
        c = self.dp_cfg.get(dp, {})
        opts, labels = c.get("options", []), c.get("labels", [])
        for i, o in enumerate(opts):
            if o == value and i < len(labels):
                return str(labels[i])
        return "—" if value is MISSING else str(value)

    def fmt_dps(self, dps):
        return "、".join(f"{self.name(dp)}={self.label(dp, v)}"
                        for dp, v in sorted(dps.items(), key=lambda kv: dp_sort_key(kv[0])))

    def paused(self):
        return time.time() < self.paused_until

    def set_connected(self, ok):
        with self.lock:
            self.connected = ok

    def on_dps(self, dps, initial=False):
        """處理裝置回報的狀態。"""
        with self.lock:
            dirty = False
            for dp, value in dps.items():
                old = self.actual.get(dp, MISSING)
                self.actual[dp] = value
                if dp not in self.dp_cfg:
                    continue
                if dp not in self.desired:
                    self.desired[dp] = value
                    dirty = True
                    log.info("以目前狀態作為目標：%s=%s", self.name(dp), self.label(dp, value))
                    continue
                want = self.desired[dp]
                if value == want:
                    continue
                if self.paused():
                    self.desired[dp] = value
                    dirty = True
                    log.info("暫停中，採納新目標：%s=%s", self.name(dp), self.label(dp, value))
                elif initial:
                    log.warning("連線時%s是 %s，目標是 %s，將改回",
                                self.name(dp), self.label(dp, value), self.label(dp, want))
                else:
                    self.ghost_total += 1
                    self.events.appendleft({
                        "time": time.strftime("%m/%d %H:%M:%S"),
                        "dp": dp, "name": self.name(dp),
                        "from": self.label(dp, old), "to": self.label(dp, value),
                    })
                    log.warning("偵測到誤觸：%s %s → %s，改回 %s",
                                self.name(dp), self.label(dp, old),
                                self.label(dp, value), self.label(dp, want))
            if dirty:
                self._save()

    def pending_commands(self):
        """回傳需要送給裝置的指令 {dp: value}，含節流保護。"""
        with self.lock:
            if self.paused():
                return {}
            now = time.monotonic()
            while self.revert_times and now - self.revert_times[0] > 60:
                self.revert_times.popleft()
            cmds = {}
            for dp, want in self.desired.items():
                have = self.actual.get(dp, MISSING)
                if have is MISSING or have == want:
                    continue
                if now - self.last_revert.get(dp, -1e9) < self.cfg["min_revert_interval"]:
                    continue
                cmds[dp] = want
            if not cmds:
                self.throttled = False
                return {}
            if len(self.revert_times) >= self.cfg["max_reverts_per_minute"]:
                if not self.throttled:
                    log.error("一分鐘內已送出 %d 次改回指令，暫停送出以免狂刷；"
                              "若持續發生，請確認設定檔的選項值型別與裝置回報的一致",
                              len(self.revert_times))
                    self.throttled = True
                return {}
            for dp in cmds:
                self.last_revert[dp] = now
            self.revert_times.append(now)
            return cmds

    def set_option(self, dp, index):
        opts = self.dp_cfg[dp]["options"]
        with self.lock:
            self.desired[dp] = opts[index]
            self.last_revert.pop(dp, None)
            self._save()
        log.info("控制頁設定目標：%s=%s", self.name(dp), self.label(dp, opts[index]))

    def pause(self, minutes):
        with self.lock:
            self.paused_until = time.time() + minutes * 60
        log.info("暫停守護 %g 分鐘，期間的改變都會被採納", minutes)

    def resume(self):
        with self.lock:
            self.paused_until = 0.0
        log.info("恢復守護")

    def snapshot(self):
        with self.lock:
            return {
                "connected": self.connected,
                "paused_seconds_left": max(0, int(self.paused_until - time.time())),
                "ghost_total": self.ghost_total,
                "dps": {
                    dp: {
                        "name": self.name(dp),
                        "actual": self.actual.get(dp),
                        "desired": self.desired.get(dp),
                        "actual_label": self.label(dp, self.actual.get(dp, MISSING)),
                        "desired_label": self.label(dp, self.desired.get(dp, MISSING)),
                        "options": [
                            {"index": i, "value": o, "label": self.label(dp, o)}
                            for i, o in enumerate(self.dp_cfg[dp].get("options", []))
                        ],
                    }
                    for dp in sorted(self.dp_cfg, key=dp_sort_key)
                },
                "events": list(self.events),
            }


# ───────────────────────── 守護主迴圈 ─────────────────────────

def run_session(d, g):
    data = d.status()
    dps = extract_dps(data)
    if dps is None:
        raise ConnectionError(describe_error(data))
    g.set_connected(True)
    log.info("已連線，目前狀態：%s", g.fmt_dps({k: v for k, v in dps.items() if k in g.dp_cfg}))
    g.on_dps(dps, initial=True)

    d.set_socketTimeout(LOOP_TIMEOUT)
    last_hb = time.monotonic()
    while True:
        cmds = g.pending_commands()
        if cmds:
            if len(cmds) == 1:
                (dp, value), = cmds.items()
                check_error(d.set_value(dp, value, nowait=True))
            else:
                check_error(d.set_multiple_values(cmds, nowait=True))

        data = d.receive()              # 最多等 LOOP_TIMEOUT 秒
        check_error(data)
        dps = extract_dps(data)
        if dps:
            g.on_dps(dps)

        if time.monotonic() - last_hb >= HEARTBEAT_EVERY:
            check_error(d.heartbeat())
            last_hb = time.monotonic()


def run_guard(cfg, device_factory=make_device):
    if not cfg["dps"]:
        sys.exit("設定檔的 dps 是空的：請先用 monitor 模式找出要守護的 DP")
    g = Guard(cfg)
    start_http(g, cfg["http_host"], cfg["http_port"], cfg["http_password"])
    log.info("控制頁：http://<這台機器的 IP>:%d/", cfg["http_port"])
    if not cfg["http_password"]:
        log.warning("控制頁沒有設定密碼（http_password），同一個網路裡的人都能操作")
    while True:
        d = None
        try:
            d = device_factory(cfg)
            log.info("裝置位址 %s，協定版本 %s", d.address, d.version)
            run_session(d, g)
        except ConnectionError as e:
            log.warning("連線問題：%s，%d 秒後重連", e, RECONNECT_DELAY)
        except Exception:
            log.exception("未預期的錯誤，%d 秒後重連", RECONNECT_DELAY)
        finally:
            g.set_connected(False)
            if d is not None:
                try:
                    d.close()
                except Exception:
                    pass
        time.sleep(RECONNECT_DELAY)


# ───────────────────────── 控制頁 ─────────────────────────

TEXT = {
    "zh-TW": {
        "html_lang": "zh-Hant",
        "title": "裝置守護",
        "offline": "未連線，重試中",
        "paused": "暫停中，剩約 {m} 分鐘",
        "guarding": "守護中",
        "now": "目前",
        "target": "目標",
        "pause": "暫停",
        "pause_for": "暫停 {m} 分鐘",
        "resume": "恢復守護",
        "pause_hint": "暫停期間，實體按鍵與原廠 app 的調整都會被採納為新目標。",
        "events": "誤觸紀錄（本次啟動共 {n} 次）",
        "no_events": "還沒有紀錄",
        "demo": "示範模式：這是模擬裝置，每隔幾秒會自己亂跳一次，看守護程式把它改回來。",
    },
    "en": {
        "html_lang": "en",
        "title": "Device Guard",
        "offline": "Offline, retrying",
        "paused": "Paused, about {m} min left",
        "guarding": "Guarding",
        "now": "Now",
        "target": "Target",
        "pause": "Pause",
        "pause_for": "Pause {m} min",
        "resume": "Resume",
        "pause_hint": "While paused, changes from the physical panel or the vendor app "
                      "become the new target.",
        "events": "Ghost touches ({n} since start)",
        "no_events": "None yet",
        "demo": "Demo mode: this device is simulated and flips a setting every few seconds "
                "so you can watch the guard revert it.",
    },
}

PAGE_CSS = """
:root{--bg:#f6f5f2;--card:#fff;--fg:#1d1d1b;--mute:#6b6a66;--line:#e2e0da;
--accent:#1f6f5c;--accent-fg:#fff;--warn:#b5532a;--ok:#2f8a4e}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#211f1d;--fg:#ecebe7;
--mute:#9b9993;--line:#34322f;--accent:#4fb393;--accent-fg:#0d1512;--warn:#e08a5f;--ok:#5cc07c}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.5 -apple-system,"PingFang TC","Noto Sans TC",sans-serif}
main{max-width:520px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:15px;margin:0 0 8px;color:var(--mute);font-weight:600}
.status{margin:0 0 16px;font-weight:600}
.status i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;
vertical-align:1px;background:var(--ok)}
.status.paused i{background:var(--warn)} .status.off i{background:var(--mute)}
.demo{font-size:14px;color:var(--warn);margin:0 0 12px}
section{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:14px;margin-bottom:12px}
.now{margin:0 0 10px;color:var(--mute);font-size:14px}
.now b{color:var(--fg)}
.row{display:flex;flex-wrap:wrap;gap:8px}
button{font:inherit;font-size:15px;min-width:56px;padding:9px 14px;border-radius:10px;
border:1px solid var(--line);background:transparent;color:var(--fg);cursor:pointer}
button.on{background:var(--accent);border-color:var(--accent);color:var(--accent-fg);font-weight:600}
.hint{font-size:13px;color:var(--mute);margin:10px 0 0}
ul{list-style:none;margin:0;padding:0;font-size:14px}
li{padding:6px 0;border-top:1px solid var(--line);display:flex;gap:10px}
li:first-child{border-top:0} li span{color:var(--mute);font-variant-numeric:tabular-nums}
"""


def render_page(g):
    s = g.snapshot()
    t = TEXT.get(g.cfg.get("language"), TEXT["zh-TW"])
    title = g.cfg.get("title") or t["title"]
    e = html.escape
    if not s["connected"]:
        status = f'<p class="status off"><i></i>{t["offline"]}</p>'
    elif s["paused_seconds_left"]:
        m = (s["paused_seconds_left"] + 59) // 60
        status = f'<p class="status paused"><i></i>{t["paused"].format(m=m)}</p>'
    else:
        status = f'<p class="status"><i></i>{t["guarding"]}</p>'
    demo = f'<p class="demo">{t["demo"]}</p>' if g.cfg.get("demo") else ""

    parts = []
    for dp, info in s["dps"].items():
        buttons = "".join(
            f'<button data-post="/set?dp={e(dp)}&amp;i={o["index"]}" '
            f'class="{"on" if o["value"] == info["desired"] else ""}">{e(o["label"])}</button>'
            for o in info["options"]
        )
        parts.append(
            f'<section><h2>{e(info["name"])}</h2>'
            f'<p class="now">{t["now"]} <b>{e(info["actual_label"])}</b>　'
            f'{t["target"]} <b>{e(info["desired_label"])}</b></p>'
            f'<div class="row">{buttons}</div></section>'
        )

    if s["paused_seconds_left"]:
        pause_ctl = f'<button data-post="/resume" class="on">{t["resume"]}</button>'
    else:
        pause_ctl = "".join(
            f'<button data-post="/pause?min={m}">{t["pause_for"].format(m=m)}</button>'
            for m in (5, 30)
        )

    events = "".join(
        f'<li><span>{e(ev["time"])}</span>{e(ev["name"])}：{e(ev["from"])} → {e(ev["to"])}</li>'
        for ev in s["events"]
    ) or f'<li>{t["no_events"]}</li>'

    return f"""<!doctype html><html lang="{t["html_lang"]}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title>
<meta name="apple-mobile-web-app-capable" content="yes">
<style>{PAGE_CSS}</style></head><body><main>
<h1>{e(title)}</h1>{status}{demo}
{''.join(parts)}
<section><h2>{t["pause"]}</h2><div class="row">{pause_ctl}</div>
<p class="hint">{t["pause_hint"]}</p></section>
<section><h2>{t["events"].format(n=s["ghost_total"])}</h2><ul>{events}</ul></section>
</main>
<script>
// 用 fetch 在背景送出指令：Safari 只會對一般表單送出跳「資訊未受到保護」的警告
let busy = false;
document.addEventListener("click", async (ev) => {{
  const b = ev.target.closest("button[data-post]");
  if (!b || busy) return;
  busy = true;
  b.disabled = true;
  try {{ await fetch(b.dataset.post, {{ method: "POST" }}); }} catch (_) {{}}
  setTimeout(() => location.reload(), 600);  // 等裝置回報新狀態再重新整理
}});
setInterval(() => {{ if (!busy) location.reload(); }}, 5000);
</script>
</body></html>"""


def start_http(g, host, port, password=""):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype, headers=()):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            """有設 http_password 時，用瀏覽器內建的帳密視窗（HTTP Basic）驗證；帳號隨意。"""
            if not password:
                return True
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return False
            try:
                given = base64.b64decode(header[6:]).decode("utf-8").partition(":")[2]
            except ValueError:
                return False
            return hmac.compare_digest(given.encode("utf-8"), password.encode("utf-8"))

        def _same_origin(self):
            """擋掉其他網站透過訪客的瀏覽器偷送的指令（跨站請求偽造）。"""
            site = self.headers.get("Sec-Fetch-Site")
            if site is not None:
                return site in ("same-origin", "none")
            origin = self.headers.get("Origin")
            if origin is None:      # curl 等非瀏覽器工具不會帶這個標頭
                return True
            return urlparse(origin).netloc == self.headers.get("Host", "")

        def _check_auth(self):
            if self._authorized():
                return True
            self._send(401, "需要密碼", "text/plain; charset=utf-8",
                       [("WWW-Authenticate", 'Basic realm="tuya-guard", charset="UTF-8"')])
            return False

        def do_GET(self):
            if not self._check_auth():
                return
            path = urlparse(self.path).path
            if path == "/":
                self._send(200, render_page(g), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._send(200, json.dumps(g.snapshot(), ensure_ascii=False),
                           "application/json; charset=utf-8")
            else:
                self._send(404, "找不到頁面", "text/plain; charset=utf-8")

        def do_POST(self):
            if not self._check_auth():
                return
            if not self._same_origin():
                self._send(403, "拒絕來自其他網站的指令", "text/plain; charset=utf-8")
                return
            u = urlparse(self.path)
            q = parse_qs(u.query)
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                q.update(parse_qs(self.rfile.read(length).decode("utf-8", "replace")))
            try:
                if u.path == "/set":
                    dp, i = q["dp"][0], int(q["i"][0])
                    if dp not in g.dp_cfg or not 0 <= i < len(g.dp_cfg[dp].get("options", [])):
                        raise ValueError("dp 或選項編號不存在")
                    g.set_option(dp, i)
                elif u.path == "/pause":
                    g.pause(float(q.get("min", ["10"])[0]))
                elif u.path == "/resume":
                    g.resume()
                else:
                    self._send(404, "找不到頁面", "text/plain; charset=utf-8")
                    return
            except (KeyError, ValueError) as e:
                self._send(400, f"參數錯誤：{e}", "text/plain; charset=utf-8")
                return
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ───────────────────────── 進入點 ─────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("monitor", "guard", "demo"))
    parser.add_argument("--config")
    parser.add_argument("--port", type=int)
    args = parser.parse_args(argv)

    # 輸出被導向檔案時，避免非中文語系的 Windows 因編碼不符而當掉
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and not stream.isatty() and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%m-%d %H:%M:%S")

    if args.mode == "demo":
        # 示範模式不連真的裝置；沒有自己的設定檔時直接用範例設定
        path = args.config or (CONFIG_PATH if os.path.exists(CONFIG_PATH) else EXAMPLE_CONFIG_PATH)
        cfg = load_config(path, need_device=False)
        cfg["demo"] = True
        cfg["state_file"] = os.path.join(BASE_DIR, "guard_state.demo.json")
    else:
        cfg = load_config(args.config or CONFIG_PATH)
    if args.port:
        cfg["http_port"] = args.port

    try:
        if args.mode == "monitor":
            run_monitor(cfg)
        elif args.mode == "guard":
            run_guard(cfg)
        else:
            log.info("示範模式：開啟 http://localhost:%d/ 觀看", cfg["http_port"])
            run_guard(cfg, device_factory=lambda c: FakeDevice(c["dps"]))
    except KeyboardInterrupt:
        print("\n結束")


if __name__ == "__main__":
    main()
