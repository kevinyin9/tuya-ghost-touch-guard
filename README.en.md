# tuya-ghost-touch-guard

[繁體中文](README.md) | **English**

[![CI](https://github.com/kevinyin9/tuya-ghost-touch-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/kevinyin9/tuya-ghost-touch-guard/actions/workflows/ci.yml)

Does your smart fan turn itself on in the middle of the night? Does your heater change its temperature, or your lamp light up by itself? If the device runs on a Tuya Wi-Fi module, this program watches it over your local network, catches these phantom touch-panel presses ("ghost touches"), and puts the device back to the state you want within a second.

You don't need to open or modify the device. All you need is a computer or a Raspberry Pi that stays on and is connected to the same Wi-Fi.

<img src="docs/screenshot.png" alt="Phone control page" width="360">

## Why

Many budget smart appliances use capacitive touch panels. Humidity, dust or electrical noise can make them press themselves: a fan that was off starts spinning, the speed jumps around, a light switches on. Replacing the panel is a hassle, and vendor apps rarely offer a "lock this state" option.

This program takes a different approach. If you can't stop the ghost touches, undo each one the moment it happens.

## How it works

On the local network, Tuya devices report every state change as it happens. The program keeps a persistent connection to the device. Each time a report arrives, such as "power is now on", the program compares it with your target state and immediately sends a command to restore anything that differs.

The target state can only be changed from the program's phone-friendly control page. If you want to adjust the device with the vendor app or the physical buttons, press **Pause** on the control page first. Any change made while the guard is paused becomes the new target.

A bad config could make the program fight the device forever, so there are two safeguards. Reverts of the same setting are spaced at least 0.5 s apart. If more than 30 reverts are sent within a minute, the program stops sending them and logs the most likely cause.

## Try it without a device

You need Python 3.10 or newer.

```bash
pip install -r requirements.txt
python tuya_guard.py demo
```

Open <http://localhost:8765/>. Demo mode simulates a fan that flips one of its settings every 8 seconds. You can watch the guard revert each change and list it under the ghost-touch log.

The demo uses the example config, so the page is in Chinese. To see it in English, copy `config.example.json` to `config.json`, set `"language": "en"` and translate the `name` and `labels` fields.

## Use it with a real device

**1. Get the device id and local key**

The local key is the password the program uses to control the device over your local network. Get it with the [tinytuya setup wizard](https://github.com/jasonacox/tinytuya#setup-wizard---getting-local-keys): create a cloud project on the Tuya developer platform, link your app account to it, then run `python -m tinytuya wizard`.

**Re-pairing the device changes its local key**, so you will need to run the wizard again after re-pairing.

**2. Create your config**

```bash
cp config.example.json config.json
```

Fill in `device_id` and `local_key`. Leave `ip` set to `"Auto"`, and the program will find the device on your network for you. `config.json` is listed in `.gitignore`, so it never gets committed.

**3. Find out which data point (DP) each button controls**

```bash
python tuya_guard.py monitor
```

Press the buttons on the device one at a time. The program prints which DP changed and what its new value is. Add the DPs you want to guard to `dps` in the config. **Use the same value types that the monitor prints.** For example, if the fan speed shows up as `3`, write the integer `3`, not the string `"3"`.

**4. Start guarding**

```bash
python tuya_guard.py guard
```

Open `http://<this computer's IP>:8765/` on your phone to reach the control page.

## Config reference

| Key | Meaning |
|---|---|
| `device_id`, `local_key` | Device id and local key (see above) |
| `ip` | Device IP; `"Auto"` finds it via broadcast |
| `version` | Tuya protocol version; detected automatically when `ip` is `"Auto"` |
| `title` | Control page title |
| `language` | Control page language: `zh-TW` or `en` |
| `http_host`, `http_port` | Address and port the control page listens on |
| `http_password` | Control page password; leave empty to disable it |
| `min_revert_interval` | Minimum seconds between two reverts of the same DP |
| `max_reverts_per_minute` | Maximum revert commands per minute |
| `dps` | DPs to guard: `name` is the display name, `options` lists the allowed values, `labels` gives display text for each value (optional) |

Log messages are currently in Traditional Chinese.

## Control page security

Setting `http_password` is recommended. Your browser will then show its built-in login prompt. Any username works, and the password is the value you set.

The control page also rejects commands sent from other websites. Without this, a malicious page you happen to visit could quietly make your browser operate the devices in your home.

**Do not expose the control page's port to the internet.** To use the page while you are away, connect to your home network through a VPN such as Tailscale or WireGuard.

## Keep it running

The guard only works while it is running. If the computer sleeps or shuts down, the device is unprotected.

- **Raspberry Pi or Linux**: start the guard at boot with systemd.

  ```ini
  # /etc/systemd/system/tuya-guard.service
  [Unit]
  Description=Tuya ghost-touch guard
  After=network-online.target
  Wants=network-online.target

  [Service]
  WorkingDirectory=/home/pi/tuya-ghost-touch-guard
  ExecStart=/usr/bin/python3 tuya_guard.py guard
  Restart=always

  [Install]
  WantedBy=multi-user.target
  ```

  Then run `sudo systemctl enable --now tuya-guard`.

- **Windows**: create a Task Scheduler task. Set the trigger to "At log on", the program to `python`, the arguments to `tuya_guard.py guard`, and "Start in" to the project folder.
- **macOS**: run `caffeinate -i python3 tuya_guard.py guard` in a terminal so that the Mac doesn't sleep while idle.

## Architecture

Everything lives in `tuya_guard.py`, and the only dependency is [tinytuya](https://github.com/jasonacox/tinytuya). The file has three parts:

1. **Connection loop** (`run_guard`, `run_session`): connects to the device, receives state reports, sends revert commands and heartbeats, and reconnects every 5 s after a disconnect.
2. **Guard state** (`Guard`): stores the target state, decides which reports are ghost touches and which commands to send, and handles throttling and pausing. The target state is saved to `guard_state.json` and survives restarts.
3. **Control page** (`start_http`, `render_page`): a phone page and a `/api/state` JSON endpoint, served by Python's built-in HTTP server.

The connection loop talks to the device through only a handful of methods. This makes it easy to substitute `FakeDevice` for a real device, which is how both the demo mode and the tests work.

## FAQ

**`monitor` fails with `WinError 10048` or "address already in use".**

The guard is already running. Both modes bind UDP ports 6666 and 6667 to find the device, so only one of them can run on a computer at a time.

**The guard keeps sending reverts and logs "30 reverts within a minute".**

The value types under `options` in your config probably don't match what the device reports. For example, the config says `"3"` (a string), but the device reports `3` (an integer). Check the values with `monitor` and fix the config.

**Can I still use the vendor app?**

Yes. However, unless the guard is paused, any change you make in the app is treated as a ghost touch and reverted. To change settings, use the control page or press Pause first.

## Tested devices

- 威技 WPF02 series DC fan (Tuya Wi-Fi module, vendor app: H smart-HW)

Any Tuya device that tinytuya can control should work in principle. Please open an issue to share your device and its DP mapping.

## Development

```bash
pip install -r requirements-dev.txt
python -m ruff check .
python -m pytest
```

Every push runs the lint check and the tests on GitHub Actions across Windows, macOS and Linux, with Python 3.10 and 3.13.

## Author

Kevin Yin. I take on custom work in Python automation, smart-home integration and data processing. If you have a similar problem with devices that won't behave or workflows you would like to automate, feel free to get in touch: <kevinyin999@gmail.com>

## License

[MIT](LICENSE)
